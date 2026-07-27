from ..LinkManager.link_operate import shell_execute
import docker
import os
class MasterSwarmManager(object):

    @staticmethod
    def master_init():
        print('I层 docker swarm init')
        folder_path = f'{os.getcwd()}/vemu_uestc/static_resources/'
        try:
            # shell_execute('docker swarm init')
            # 成功了需要写入数据库？
            docker_client = docker.from_env()
            init_ret = docker_client.swarm.init()  # init_ret是master节点的(str)ID
        except:
            pass
        try:
            shell_execute(f'sudo mkdir -p {folder_path}')
            shell_execute(f'sudo chmod 777 -R {folder_path}')
        except:
            pass
        try:
            worker_token = shell_execute('docker swarm join-token -q manager')
            file_path = f'{os.getcwd()}/vemu_uestc/static_resources/docker_swarm_token'
            with open(file_path, 'w') as f:
                f.write(worker_token)
        except:
            return {"code": 0}
        return {"code": 1}

    @staticmethod
    def master_leave():
        print('I层 docker swarm leave')
        try:
            shell_execute('docker swarm leave --force')

            return {'code': 1}
        except:

            return {'code': 0}