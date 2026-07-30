from flask.views import MethodView
from flask import request
import json, time

from ....vemu_config.config import PROJ_CONFIG
import os
import docker
import threading
from concurrent.futures import ThreadPoolExecutor
from ....Implement_layer.LinkManager import shell_execute
from ....Service_layer.redisAPI import UserMapRedis
from ....tools.log_tools import UserLogger, UserLogLevel

def worker_config_host(user, topo, monitor_interfaces, bridge_name, intra_collector_ip):
    docker_cli = docker.from_env()
    user_db_map = UserMapRedis()
    user_db_cli = user_db_map.get_user_db(user)


    def _configure_container(container_id, nic_name, bridge_name, intra_collector_ip):
        '''
        用于多线程执行在容器中配置的操作
        '''
        shell_execute(f"docker network connect {bridge_name} {container_id}")
        # 使用shell命令将文件拷贝到容器中
        dir_path = os.path.dirname(__file__) + "/hsflowd-ubuntu18_2.0.25-3_amd64.deb"
        shell_execute(f'docker cp {dir_path} {container_id}:/root/')
        dir_path = os.path.dirname(__file__) + "/hsflowd.conf"
        shell_execute(f'docker cp {dir_path} {container_id}:/etc/')
        # 修改文件并启动hsflowd
        container = docker_cli.containers.get(container_id)
        container.exec_run('dpkg -i /root/hsflowd-ubuntu18_2.0.25-3_amd64.deb')
        container.exec_run('update-rc.d hsflowd defaults')
        cmd1 = f"sed -i 's/172.17.0.127/{intra_collector_ip}/g' /etc/hsflowd.conf"
        container.exec_run(cmd1)
        # 在容器中修改hsflowd.conf文件中的网卡名字
        for nic in nic_name:
            cmd2 = f"sed -i '2i  pcap {{ dev = {nic} }}' /etc/hsflowd.conf"
            container.exec_run(cmd2)
        container.exec_run('service hsflowd start')
        # TODO: 这里需要检查hsflowd是否启动成功

    def config_host():
        threads = []
        for container_id, nic_name in monitor_interfaces.items():
            t = threading.Thread(target=_configure_container, args=(container_id, nic_name, bridge_name, intra_collector_ip))
            threads.append(t)
            t.start()
        
        # 等待所有线程完成
        for t in threads:
            t.join()
        
        user_db_cli.set_value(f"{topo}_sflow", "log", "sflow监控服务已启动")
        return "sflow主机配置完毕，启动服务"
    
    config_host()

def worker_sflow_delete(monitor_interfaces,bridge_name):
    docker_cli = docker.from_env()
    for container_id, nic_name in monitor_interfaces.items():
        #防止容器已经被删除
        try:
            shell_execute(f"docker network disconnect {bridge_name} {container_id}")
            container = docker_cli.containers.get(container_id)
            #停止host容器中的hsflowd
            container.exec_run('service hsflowd stop')
        except Exception as e:
            print(e)
            print(f'The container {container_id} may have been removed.')
    msg = "主机中sflow相关组件已经被清除"
    return msg

class SflowAPI(MethodView):
    def post(self):
        try:
            data = json.loads(request.get_data(as_text=True))
            # user, topo, monitor_interfaces, bridge_name, collector_ip = \
            #     data["user"], data["topo"], data["monitor_interfaces"], data["bridge_name"], data["collector_ip"]
            msg = worker_config_host(**data)
            return {'code': 1, 'msg': msg}
        except Exception as e:
            return {"code": 0, "msg": str(e)}

    def delete(self):
        try:
            data = json.loads(request.get_data(as_text=True))
            user, topo = data["user"], data["topo"]
            monitor_interfaces = data["monitor_interfaces"]
            bridge_name = data["bridge_name"]
            mag = worker_sflow_delete(monitor_interfaces,bridge_name)
            return {'code': 1, 'msg': msg}
        except Exception as e:
            return {'code': 0, 'msg': str(e)}