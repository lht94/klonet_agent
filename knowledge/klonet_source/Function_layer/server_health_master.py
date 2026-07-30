import time
import ast
import multiprocessing
from ..tools.context import Db0Context, redis_context
from ..tools.upper_level_redis_API import get_projects_on_worker, get_user2db
from ..vemu_config.config import PROJ_CONFIG
from ..Service_layer.redis_error import TableNotExistError, KeyNotExistError
from ..Service_layer.redisAPI import DB0
from ..tools.log_tools import FLASK_LOGGER

'''
worker重启与失效方案概述
    - master定期检查worker状态（心跳包）。若判定worker宕机，则将worker从可创建拓扑的
      worker列表（worker_list）中移除；同时，查询该worker上创建的子拓扑，进而查询到拓
      扑，将该拓扑的"broken"状态置为True。
    - 前端定时请求，查询拓扑是否"broken"，若broken，告知用户损坏的节点列表，提示用户及
      时从未损坏的节点中下载实验数据、在下载完毕后删除本拓扑并重新创建拓扑。
    - （重启特有）另外，worker宿主机重启后，worker进程会自动重启，并向master注册（将
      该worker添加进worker_list中）
    - worker进程启动时会向master自动注册

方案假设：
    - 常态化运行，master/worker进程一旦启动后，就不再关闭；而非像平时开发一样频繁启停
        master/worker
    - 目前对各种情况的考虑不是很完善
'''

HEARTBEAT_TABLE_NAME = PROJ_CONFIG.heartbeat_table_name
BROKEN_PROJECTS_TABLE_NAME = PROJ_CONFIG.broken_projects_table_name

class WorkerHealthGuardian():
    """
    worker健康检查
    """
    def __init__(self, timeout_s=60, poll_interval_s=20):
        """
        初始化
            - timeout_s: 超时时间（单位：秒）
            - poll_interval_s: 轮询间隔（单位：秒）
        """
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self.init_last_heartbeat_timestamps = 0
        self.db0_cli = DB0()

    def __del__(self):
        """
        关闭数据库连接
        """
        self.db0_cli.close()

    def on_recv_heartbeat(self, worker_ip):
        '''
        接收到心跳包（HTTP请求）后的逻辑：更新heartbeat表中worker最近一次心跳的时间戳
        调用于 POST /master/heartbeat/
        '''
        # 日志记录
        FLASK_LOGGER.info(f"{worker_ip} is alive")
        
        # 获得所有可用worker的ip
        worker_list = self._get_worker_list_table()
        # 查询worker_heatbeat表是否存在。若不存在，则初始化
        self._try_to_init_heartbeat_table(worker_list)

        # 更新worker的时间戳为当前时间
        self._update_worker_timestamp(worker_ip)

    def start_poll_worker_timestamps(self):
        '''
        - 当心跳检查功能启用，在master新启动时，将启动多进程，对各worker时间戳进行轮询
          （常驻进程）。
        - 若当前时间减去时间戳大于超时时间，则判定该worker失效。
        - 若worker失效，则将其从worker_list表和heartbeat表中移除。
        '''
        try:
            # 正常运行的worker列表
            worker_list = self._get_worker_list_table()
            # 初始化heartbeat表，否则worker会注定超时
            self._init_heartbeat_table(worker_list)
            # 持久运行常驻进程
            while True:
                # 获取heartbeat表的所有内容
                heartbeat_table = self._get_heartbeat_table()
                # 当前时间
                cur_time_s = time.time()
                
                # 遍历检查每个worker时戳是否超时
                for worker_ip, last_heatbeat_timestamp_s in heartbeat_table.items():
                    # 时戳为0，可能是worker忘记启动
                    if last_heatbeat_timestamp_s == 0:
                        FLASK_LOGGER.warning(f"{worker_ip} still has no heartbeat,"
                                             " do you forget to start it?")
                        continue
                    # 时戳不为0，worker已启动
                    else:
                        # 计算距上次心跳的时间差
                        delta_time_s = cur_time_s - last_heatbeat_timestamp_s
                        # 若时间差大于超时时间，进行日志记录，并处理worker失效操作
                        if delta_time_s > self.timeout_s:
                            FLASK_LOGGER.error(f"{worker_ip} is "
                                f"crashed! ({worker_ip}'s last heatbeat time "
                                f"is {last_heatbeat_timestamp_s}, current time"
                                f"is {cur_time_s}, delta time is {delta_time_s}"
                                f")")
                            self._deal_with_crashed(worker_ip)
                
                # 每隔一个轮询间隔，master进行心跳检测
                time.sleep(self.poll_interval_s)
        
        # 若手动关闭心跳检查功能进程
        except KeyboardInterrupt:
            FLASK_LOGGER.info("Exit worker health check process.")

    def get_project_broken_status(self, user, project_name):
        '''
        获取某用户的某项目的损坏状态

        Args:
            user: 用户名
            project_name: 项目名

        Returns:
            {
                "is_broken": True/False,
                "broken_nes": [] # 未损坏则为空
            }
        '''
        # 默认项目未损坏，已损坏的网元为空列表
        is_broken = False
        broken_nes = []

        # 读取数据库
        with redis_context(user) as user_db_cli:
            try:
                # 尝试读取broken_projects表中，项目里已损坏的网元列表
                #     - 若broken_projects表本身不存在，触发 TableNotExistError
                #     - 若broken_projects表无项目的键，触发 KeyNotExistError
                broken_nes =  user_db_cli.get_value(BROKEN_PROJECTS_TABLE_NAME,
                                                    project_name)
                # 若上条代码执行不报错，说明项目已损坏
                is_broken = True
            # 忽略 “特意为之” 的报错
            except KeyNotExistError:
                pass
            except TableNotExistError:
                pass

        # 返回损坏状态信息
        return {"is_broken": is_broken, "broken_nes": broken_nes}
    
    def get_user_all_broken_projects(self, user):
        '''
        获取用户的所有已损坏项目（拓扑）

        Args:
            user: 用户名

        Returns:
            {
                # 损坏项目名及损坏的节点列表
                "broken_project_name": ["h1", "h2", "h3"],
                ...
            }
        '''
        # 读取broken_projects表
        # 通过hgetall读取不存在的表时，会输出空字典
        broken_projects = {}
        with redis_context(user) as user_db_cli:
            broken_projects = user_db_cli.get_hash_table(
                BROKEN_PROJECTS_TABLE_NAME)
        # 使用ast.literal_eval函数，将从redis中读取到的str转为list
        for broken_project_name, broken_nes in broken_projects.items():
            # 例如："[\"a\", \"b\", \"c\"]" -> ["a", "b", "c"]
            broken_projects[broken_project_name] = ast.literal_eval(broken_nes)
        return broken_projects

    def _update_worker_timestamp(self, worker_ip):
        '''
        更新worker的时间戳为当前时间
        '''
        # 当前时间，单位秒
        cur_time_s = time.time()
        # 将当前时间写入heatbeat表
        self.db0_cli.set_value(HEARTBEAT_TABLE_NAME, worker_ip, cur_time_s)

    def _init_heartbeat_table(self, worker_list):
        '''
        初始化heatbeat表，将每个worker的初始时间戳设置为0
        '''
        # 日志记录
        FLASK_LOGGER.info("Initialize heartbeat table")
        # 对worker_list中的每个worker的ip
        for worker in worker_list:
            # 设置表项为0
            self.db0_cli.set_value(HEARTBEAT_TABLE_NAME, worker,
                self.init_last_heartbeat_timestamps)

    def _get_heartbeat_table(self):
        '''
        获取heartbeat表的所有内容

        Returns:
            {
                <worker_ip_1>: <last_heatbeat_timestamp_1> (类型为float),
                <worker_ip_2>: <last_heatbeat_timestamp_2> (类型为float),
                ...
            }
        '''
        return self.db0_cli.get_all_values(HEARTBEAT_TABLE_NAME)

    def _get_worker_list_table(self):
        '''
        获取worker_list表的内容

        Returns:
            所有启用的worker的ip组成的列表，例如[<worker_ip_1>, <worker_ip_2>, ...]
        '''      
        return self.db0_cli.get_elements_in_set(PROJ_CONFIG.worker_list)

    def _try_to_init_heartbeat_table(self, worker_list):
        '''
        查询worker_heatbeat表是否存在。若不存在，则初始化

        Args:
            worker_list = [<worker_ip_1>, <worker_ip_2>, ...]
        '''
        try:
            # 检查heatbeat表是否存在
            # 若不存在，则进行 “raise TableNotExistError” 操作，转到 “except”
            self.db0_cli.check_table_exist(HEARTBEAT_TABLE_NAME)
        except TableNotExistError:
            # 初始化heatbeat表
            self._init_heartbeat_table(worker_list)

    def stop_check():
        '''
        停止对worker时间戳的轮询（无用）
        '''
        pass

    def _deal_with_crashed(self, worker_ip):
        '''
        master确定worker失效后的处理函数
        
        主要进行数据库的信息记录
        - 将失效worker记录为不可用的，避免在上面继续部署拓扑
        - 将失效worker上部署的拓扑标定出来
        '''
        # 将worker_ip从redis相关表中移除
        self._remove_from_tables(worker_ip)
        # 获取失效worker上的项目
        projects_on_worker = get_projects_on_worker(worker_ip)
        # 在每个用户的DB的broken_projects表中记录已损坏的项目
        self._record_broken_projects(projects_on_worker)

    def _remove_from_tables(self, worker_ip):
        '''
        将worker_ip从worker_list表和heartbeat中移除
        '''
        # 日志记录
        FLASK_LOGGER.info(f"remove {worker_ip} from {PROJ_CONFIG.worker_list}"
                          f" and {HEARTBEAT_TABLE_NAME}")
        # 移除表项
        self.db0_cli._db_conn.srem(PROJ_CONFIG.worker_list, worker_ip)
        self.db0_cli.del_value(HEARTBEAT_TABLE_NAME, worker_ip)

    def _record_broken_projects(self, projects_on_worker):
        '''
        在每个用户的DB的broken_projects表中记录损坏的项目

        broken_projects表设计：
            key: 已损坏的项目名称
            value: 项目中损坏节点列表，例如：["h1", "h2", "h3"]

        Args:
            projects_on_worker = {
                <user_name>: {
                    "project1": ["h1", "h2", ...],
                    "project2": ["h1", "h2", ...], ...
                }, ...
            }
        '''
        for user, project in projects_on_worker.items():
            # 获取用户已有的broken_projects表内容
            user_broken_projects = self._get_user_broken_projects(user)
            
            # 由于又有新worker损坏，需扩充broken_projects表内容
            #   - 情况1：在broken_projects表中已有该项目，需扩展其损坏节点列表
            #   - 情况2：在broken_projects表中没有该项目，则直接赋值
            for project_name, broken_nes in project.items():
                # 情况2
                if project_name in user_broken_projects.keys():
                    # 在项目已损坏节点列表中加入新损坏的节点
                    user_broken_projects[project_name].extend(broken_nes)
                    # 去掉重复节点（通常不会出现此情况）
                    user_broken_projects[project_name] = \
                        list(set((user_broken_projects[project_name])))
                # 情况1
                else:
                    # 项目已损坏节点列表仅包括新损坏的节点
                    user_broken_projects[project_name] = broken_nes
            
            # 写入数据库，更新broken_projects表
            with redis_context(user) as user_db_cli:
                user_db_cli.set_all_values(BROKEN_PROJECTS_TABLE_NAME, 
                    user_broken_projects)

    def _get_user_broken_projects(self, user):
        '''
        获取某用户的broken_projects表。若无此表，则返回空字典

        broken_projects表设计：
            key: 已损坏的项目名称
            value: 项目中损坏节点列表，例如：["h1", "h2", "h3"]

        Returns:
            {
                # 损坏项目名及损坏的节点列表
                <broken_project_name>: ["h1", "h2", "h3"],
                ...
            }
        '''
        # 初始时，broken_projects表为空字典
        broken_projects = {}
        # 读取数据库
        with redis_context(user) as user_db_cli:
            # 若broken_projects表存在，则读取表信息
            if user_db_cli._db_conn.exists(BROKEN_PROJECTS_TABLE_NAME):
                broken_projects = user_db_cli.get_all_values(
                    BROKEN_PROJECTS_TABLE_NAME)
        # 返回broken_projects表
        return broken_projects


def start_worker_health_check_process():
    """
    当心跳检查功能启用，在master运行时，启动多进程，对各worker时间戳进行轮询
    """
    guardian = WorkerHealthGuardian(timeout_s=60, poll_interval_s=20)
    p = multiprocessing.Process(target=guardian.start_poll_worker_timestamps)
    p.start()
    FLASK_LOGGER.info(f"Start worker health check process! Pid={p.pid}")


def remove_all_broken_projects_table():
    """
    当心跳检查功能停用，在master运行时，删除和此功能相关的redis表项
    """
    user2db = get_user2db()
    for user in user2db.keys():
        with redis_context(user) as user_db_cli:
            user_db_cli.del_table(BROKEN_PROJECTS_TABLE_NAME)