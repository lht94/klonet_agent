import grequests
from ..tools.context import redis_context
from ..vemu_config.config import PROJ_CONFIG
from ..Service_layer.redis_error import TableNotExistError, KeyNotExistError

class NeCheckMaster:
    '''
    拓扑的节点健康检查

    Attributes:
        user: 用户名
        topo: 拓扑名
    '''
    
    def __init__(self, user, topo):
        self.user = user
        self.topo = topo
    
    def _get_subtopos_and_ips(self):
        '''
        获取指定拓扑的子拓扑名及其对应的worker_ip

        Returns:
            subtopos: 拓扑的子拓扑列表
            worker_ips: worker_ip列表
        '''
        with redis_context(self.user) as user_db_cli:
            # 创建一个redis管道
            pipe = user_db_cli._db_conn.pipeline()
            # 获取拓扑的若干子拓扑
            subtopos = user_db_cli.get_value("topo2subtopo", self.topo)
            # 将多个redis操作添加到管道中
            for subtopo in subtopos:
                pipe.hget("subtopo2worker", subtopo)
            # 执行管道中所有命令，返回一个包含所有命令结果的列表
            # 该列表为部署该拓扑的所有worker的ip
            # 获取带引号的若干worker_ip，如 '"172.31.0.4"'
            worker_ips_temp = pipe.execute()

            # 去掉引号，如 '"172.31.0.4"' -> '172.31.0.4'
            worker_ips = []
            for worker_ip in worker_ips_temp:
                worker_ip = worker_ip.strip("\"")
                worker_ips.append(worker_ip)

            # 返回
            return subtopos, worker_ips
    
    def send_request(self):
        """
        向包含用户拓扑的worker发送检查信号，以检查节点的健康状况

        Returns:
            - 成功获取
            {
                "code": 1  # 获取节点健康状态成功
                "error_nes": [h1、h2...],
                "msg": "获取节点健康状态成功",
                "error_msgs": []
            }
            - 获取失败
            {
                "code": 0  # 获取节点健康状态部分失败
                "error_nes": [h1、h2...],
                "msg": "获取节点健康状态部分失败, 部分worker获取报错",
                "error_msgs": ["发往worker的第{i}个请求失败....", "..."]
            }
        """
        # 获取subtopo名及其对应worker_ip
        subtopos, worker_ips = self._get_subtopos_and_ips()

        # 异步请求列表
        reqs = []
        # 对每个要发送请求的worker_ip
        for i, worker_ip in enumerate(worker_ips):
            # 生成url和信息字段
            worker_url = (f"http://{worker_ip}:{PROJ_CONFIG.worker_port}"
                          f"/worker/ne_health/")
            info = {
                "user": self.user,
                "topo": self.topo,
                "subtopo": subtopos[i]
            }
            # 加入异步请求列表
            req = grequests.post(worker_url, json=info)
            reqs.append(req)

        # 进行异步请求
        resp_result = grequests.map(reqs)

        # 默认认为每个请求都得到正确的响应
        resp_status = True
        # 发生错误的节点列表统计
        error_nes = []
        # 发生错误的worker请求统计
        error_msgs = []

        # 对每个worker响应
        for i, resp in enumerate(resp_result, 1):
            # 将错误节点加入列表
            error_nes.extend(resp.json()['error_nes'])
            # 若worker响应发生错误，code为0时
            if resp.json()["code"] != 1:
                # 记录错误信息
                return_msg = resp.json()["msg"]
                error_msgs.append(f"发往worker的第{i}个请求失败！"
                                  f"请求url：{worker_url}，"
                                  f"worker返回的msg为：{return_msg}")
                # 标记健康状态检查部分失败，部分worker获取报错
                resp_status = False
        
        # 返回
        if not resp_status:
            return {'code': 0, 
                    'error_nes': error_nes, 
                    'msg': '获取节点健康状态部分失败, 部分worker获取报错',
                    'error_msgs': error_msgs}
        else:
            return {'code': 1,
                    'error_nes': error_nes,
                    'msg': '获取节点健康状态成功',
                    'error_msgs': error_msgs}
      