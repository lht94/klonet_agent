from ..LinkManager.link_operate import shell_execute
from ...vemu_config.config import PROJ_CONFIG

class WorkerSwarmManager(object):    

    @staticmethod
    def worker_join(token):
        print('I层 docker swarm join')
        try:
            # shell_execute('docker swarm init')
            # 成功了需要写入数据库？
            worker_token = shell_execute(f'docker swarm join --token {token} {PROJ_CONFIG.master_ip}:2377')
            worker_id = shell_execute(f'docker info | grep NodeID')
            print(worker_id)

            return {'code': 1, 'worker_token': worker_token}
        except:

            return {'code': 1}

    @staticmethod
    def worker_leave():
        print('I层 docker swarm join')
        try:
            shell_execute('docker swarm leave --force')

            return {'code': 1}
        except:
            
            return {'code': 0}