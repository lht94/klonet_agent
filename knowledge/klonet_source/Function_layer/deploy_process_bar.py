from psutil import process_iter
from ..tools import get_host_ip
from ..vemu_config.config import PROJ_CONFIG


class ProcessBar:
    '''
    进度条类，用于更新数据库内 PROCESS_BAR_TABLE_NAME 表的内容
    '''
    def __init__(self, step: float, user_db_cli, topo):
        """
        对于某步骤更新进度条的进度值
        Args:
            step (float): 已完成的步数（若非整数，意味着某步完成了一部分）
            user_db_cli : 用户数据库管理对象
            topo (str)  : 拓扑名，用于构造进度条表名
        Returns:
            None
        """
        self.step = step
        self.user_db_cli = user_db_cli
        self.topo = topo
        self.process_bar_db_update()

    def process_bar_db_update(self):
        
        # 进度条表名
        pb_table_name = PROJ_CONFIG.pb_table_name_prefix + '_' + self.topo + '_' + self.usage
        # worker 总数量，用于分配百分比
        worker_count = len(self.user_db_cli.get_all_values(pb_table_name).values()) - 1
        # 该 worker 的 ip，用于进度表项查找
        worker_ip = get_host_ip()

        # 新值更新
        previous_prop = sum(self.process_prop[:int(self.step)])                    # 已经完成的步骤占比
        running_prop = 0 if self.step == int(self.step) \
            else (self.step - int(self.step)) * self.process_prop[int(self.step)]  # 正在运行的步骤占比 
        update_val = 100 * (previous_prop + running_prop) / sum(self.process_prop) / worker_count

        # 表项不存在，或者新值大于数据库中的原始值，进行更新
        if self.user_db_cli.check_exist(pb_table_name, worker_ip) == False or \
            self.user_db_cli.get_value(pb_table_name, worker_ip) < update_val:
            self.user_db_cli.set_value(pb_table_name, worker_ip, update_val)


class ProcessBarDeploy(ProcessBar):    
    def __init__(self, step: float, user_db_cli, topo):
        # 拓扑创建七步骤各所占比重，前三为节点创建，后四为服务创建。该值保存于 config.py
        self.process_prop = list(PROJ_CONFIG.deploy_process_bar_proportion.values())
        self.usage = 'deploy'
        super().__init__(step, user_db_cli, topo)


class ProcessBarDelete(ProcessBar):
    def __init__(self, step: float, user_db_cli, topo):
        # 拓扑创建四步骤各所占比重，前三为节点删除，后一为数据库信息删除。该值保存于 config.py
        self.process_prop = list(PROJ_CONFIG.delete_process_bar_proportion.values())
        self.usage = 'delete'
        super().__init__(step, user_db_cli, topo)
    