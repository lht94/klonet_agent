from flask.views import MethodView
import grequests
from ....vemu_config.config import PROJ_CONFIG
from ....tools.log_tools import *
from ....Service_layer.DockerSwarm import SwarmMaster
from ....Service_layer.redisAPI import WorkerRedis
import os
from ....vemu_config.config import PROJ_CONFIG
from ....tools.tools import get_host_ip
from flask_login import login_required
from ....tools.log_tools import FLASK_LOGGER

class DockerSwarmMaster(MethodView):
    """
    master上的docker swarm初始
    """

    def __init__(self):
        self.worker_redis = WorkerRedis()
        self.worker_list = self.worker_redis.get_all_workers()
        self.master_local_IP = get_host_ip()
        self.swarm_master = SwarmMaster()


    def post(self):
        """
        Post /master/swarm/

        Return:
            dict: {
                'code': 0失败，1成功,
                'msg': 提示信息,
                'url': 完整的url
            }
        """

        # docker swarm init, 成功说明初始化成功；失败说明已经初始化，无需处理，程序不会中断
        self.swarm_master.docker_swarm_init()

        # 不管初始化是否正确，都需要向每个worker发送请求
        file_path = f'{os.getcwd()}/vemu_uestc/static_resources/docker_swarm_token'
        try:
            with open(file_path, 'r') as f:
               worker_token = f.read()
        except:
            FLASK_LOGGER.debug(f'"{file_path}" not exist')
            return {'code': 0, 'msg': f'"{file_path}" not exist'}
        data2worker = {'worker_token': worker_token, 'master_local_ip': self.master_local_IP}

        for worker_ip in self.worker_list:
            if worker_ip == self.master_local_IP:
                continue
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/swarm/'
            rs = (grequests.post(req_url, json=data2worker),)
            resp_result = grequests.map(rs)
            resp = [resp.json() for resp in resp_result]

        return {"code":1}


    def delete(self):
        """
        Delete /master/swarm/

        Return:
            dict: {
                'code': 0失败，1成功,
                'msg': 提示信息,
                'url': 完整的url
            }
        """
        FLASK_LOGGER.debug(self.worker_list)
        FLASK_LOGGER.debug(self.master_local_IP)
        for worker_ip in self.worker_list:
            if worker_ip == self.master_local_IP:
                continue
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/swarm/'
            rs = (grequests.delete(req_url),)
            resp_result = grequests.map(rs)
            resp = [resp.json() for resp in resp_result]
        self.swarm_master.docker_swarm_master_leave()
        return {"code":1}
