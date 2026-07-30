import json
from flask.views import MethodView
from flask import request
import os
import docker
import threading
from concurrent.futures import ThreadPoolExecutor
from ....Implement_layer.LinkManager import shell_execute
from ....Service_layer.redisAPI import UserMapRedis
from ....tools.log_tools import UserLogger, UserLogLevel
import grequests,requests

from ....vemu_config.config import PROJ_CONFIG
docker_cli = docker.from_env()
user_db_map = UserMapRedis()

def master_deploy_sflow(user, topo):
    docker_cli = docker.from_env()
    user_db_map = UserMapRedis()
    user_db_cli = user_db_map.get_user_db(user)

    def _get_nic_name(container_id):
        """
        获得主机上所有桥接网卡的名字
        """
        cmd = f"docker exec {container_id} ifconfig | awk '/^[a-zA-Z]/ {{print $1}}' | sed 's/://g'"
        nic_name = shell_execute(cmd)
        nic_name = nic_name.split('\n')
        return nic_name

    def _get_nic_ip(container_id, nic_name):
        """
        获得主机上某网卡的ip
        """
        #例如：docker exec kingcide_kctest_sflow ifconfig eth0 | grep 'inet ' | awk '{ print $2 }' | cut -d':' -f2
        cmd = f"docker exec {container_id} ifconfig {nic_name} | grep 'inet ' | awk '{{print $2}}' | cut -d':' -f2"
        ip = shell_execute(cmd)
        return ip

    def create_overlay_net(net_name):
        """
            通过network的名字得到docker.Network对象

            Args:
                net_name (str): The ID of the network.

            Returns:
                (:py:class:`Network`) The network.

            Raises:
                :py:class:`docker.errors.APIError`
                    If the docker server returns an error.

        """
        try:
            overlay = docker_cli.networks.get(net_name)
        except docker.errors.NotFound:
            net_para = {'name': net_name, 'driver': 'overlay', 'attachable': True}
            overlay = docker_cli.networks.create(**net_para)
        return overlay

    def create_sflow(user, topo):

        user_logger = UserLogger(user, UserLogLevel.Second, topo)
        user_logger.log_to_redis("Starting to deploy sflow service...")

        table_name = f"{topo}_sflow"
        bridge_name = f"{user}_{topo}_sflow"
        collector_name = f"{user}_{topo}_sflow"
        #TODO：这是sflow的8008 web端口，映射到的宿主机端口，每个拓扑需要不一样
        try:
            max_port = user_db_cli.get_value("sflow_max_port", "port")
        except:
            max_port = 11007
        collector_port = int(max_port)+1
        user_db_cli.set_value(table_name, "collector_port", collector_port)
        user_logger.log_to_redis("Creating a Private Bridge")
        msg=''
        try:
            #msg=shell_execute(f"docker network create --driver bridge {bridge_name}")

            overlay = create_overlay_net(bridge_name)

            user_db_cli.set_value(table_name, "bridge_name", bridge_name)
        
            user_logger.log_to_redis("Creating a sflow container")
            #TODO：以后可以考虑使用平台的接口创建容器
            #shell_execute(f'docker run -d -p 10008:8008 -p 6343:6343/udp --network {bridge_name} --name {collector_name} sflow/prometheus')
            msg=shell_execute(f'docker run -d -p {collector_port}:8008 --network {bridge_name} --name {collector_name} sflow/prometheus')

            # sflow_ctr = docker_cli.containers.run(
            #                 "sflow/prometheus",
            #                 detach=True,
            #                 ports={f"{collector_port}/tcp": 8008},  # 映射容器的端口到主机的端口
            #                 network=bridge_name,  # 使用指定的overlay网络
            #                 name=collector_name  # 指定容器的名称
            #             )
            ctr_net_json = shell_execute(
                "sudo docker inspect -f '{{json .NetworkSettings.Networks}}' "
                + collector_name
            )
            ctr_net_dict = json.loads(ctr_net_json)
            intra_collector_ip = ctr_net_dict[bridge_name]["IPAddress"]
        except Exception as e:
            print(e)
            print(msg)

        intra_collector_ip = _get_nic_ip(collector_name, "eth0")
        inter_collector_ip = _get_nic_ip(collector_name, "eth1")
        user_db_cli.set_value(table_name, "collector_name", collector_name)
        user_db_cli.set_value(table_name, "intra_collector_ip", intra_collector_ip)
        user_db_cli.set_value(table_name, "inter_collector_ip", inter_collector_ip)
        user_db_cli.set_value(table_name, "log", "sflow监控组件已部署")
        user_db_cli.set_value("sflow_max_port", "port", collector_port)
        return collector_port

    def worker_config_host(user,topo):
        '''
        all_monitor_interfaces = {
            "192.168.1.124" : {
                "lqp9qrlejtq": ["tos1","toh2"],
                "" : {}
            },
            "192.168.1.125": {
            
            }

        }
        '''
        intra_collector_ip = user_db_cli.get_value(f"{topo}_sflow", "intra_collector_ip")
        bridge_name = user_db_cli.get_value(f"{topo}_sflow", "bridge_name")
        topo_split = user_db_cli.get_value("topo_split_scheme", f'{topo}')
        worker_list = topo_split['worker_list']
        all_monitor_interfaces = {}
        # 请求列表
        reqs = []
        
        for worker_ip in worker_list:
            ne_list = topo_split[worker_ip]["ne_list"]
            all_monitor_interfaces[worker_ip] = {}
            host_list = [ne for ne in ne_list if ne.startswith('h')]
            for host in host_list:
                container_id = user_db_cli.get_value(f"{topo}_{host}", "NEid")
                all_monitor_interfaces[worker_ip][container_id]=[]

                keys = user_db_cli.get_all_keys(f"{topo}_{host}")
                link_keys = [key for key in keys if key.startswith('link')]
                for link_key in link_keys:
                    link_info = user_db_cli.get_value(f"{topo}_{host}", link_key)
                    nic_name = link_info["nic"]
                    if nic_name :
                        all_monitor_interfaces[worker_ip][container_id].append(nic_name)
            
            reqs.append(grequests.post(
                            f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/sflow/',
                            json = {
                                'user': user,
                                'topo': topo,
                                'monitor_interfaces': all_monitor_interfaces[worker_ip],
                                'bridge_name': bridge_name,
                                'intra_collector_ip': intra_collector_ip
                            }
                        ))
        user_db_cli.set_value(f"{topo}_sflow", "all_monitor_interfaces", all_monitor_interfaces)
        # 并发发送请求
        resp_result = grequests.map(reqs)
        # 检测请求结果
        resp_status = [resp.json()['code'] for resp in resp_result]
        if not all(resp_status):
            return 'sflow主机配置失败'
        else:
            return 'sflow主机配置成功'


    # 在master所在宿主机上创建sflow监控容器
    collector_port = create_sflow(user, topo)
    # 在所有的host节点上汇总待监控的网卡，然后部署sflow服务
    msg = worker_config_host(user, topo)
    msg = msg + f' 映射到宿主机的端口为{collector_port}' 
    user_db_cli.close()
    return msg
    

def sflow_delete(user, topo):
    """
    删除sflow容器和网桥
    删除redis中相关表项
    """
    user_db_cli = user_db_map.get_user_db(user)
    bridge_name = f"{user}_{topo}_sflow"
    #删除sflow容器
    collector_name = user_db_cli.get_value(f"{topo}_sflow", "collector_name")
    try:
        shell_execute(f"docker stop {collector_name}")
        shell_execute(f"docker rm -f {collector_name}")
    except Exception as e:
        pass
    #将每个与网桥相连的接口断开
    all_monitor_interfaces = user_db_cli.get_value(f"{topo}_sflow", "all_monitor_interfaces")
    reqs = []
    for worker_ip, monitor_interfaces in all_monitor_interfaces.items():
        reqs.append(grequests.delete(
                f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/sflow/',
                json = {
                    'user': user,
                    'topo': topo,
                    'monitor_interfaces': monitor_interfaces,
                    'bridge_name': bridge_name
                }
            ))
    # 并发发送请求
    resp_result = grequests.map(reqs)
    #删除网桥docker network rm sflow_kct
    try:
        code = shell_execute(f"docker network rm {bridge_name}")
        if code :
            print(code)
    except Exception as e :
        print(e)
    #删除redis中相关表项
    user_db_cli.del_table(f"{topo}_sflow")
    user_db_cli.close()
    return True

    
class SflowAPI(MethodView):
    """
    /master/sflow/
    """
    def get(self):
        data = json.loads(request.get_data(as_text=True))
        user, topo = data["user"], data["topo"]
        user_db_cli = user_db_map.get_user_db(user)
        log = "sflow监控服务未部署"
        intra_collector_ip = ""
        collector_port = ""
        monitor_interfaces = {}
        try:
            log=user_db_cli.get_value(f"{topo}_sflow", "log")
            intra_collector_ip = user_db_cli.get_value(f"{topo}_sflow", "intra_collector_ip")
            inter_collector_ip = user_db_cli.get_value(f"{topo}_sflow", "inter_collector_ip")
            collector_port = user_db_cli.get_value(f"{topo}_sflow", "collector_port")
            monitor_interfaces = user_db_cli.get_value(f"{topo}_sflow", "monitor_interfaces")
        except Exception as e:
            pass
        sflow_info = {
            "log": log,
            "intra_collector_ip": intra_collector_ip,
            "inter_collector_ip": inter_collector_ip,
            "collector_port": collector_port,
            "monitor_interfaces": monitor_interfaces
        }
        return sflow_info

    def post(self):
        try:
            data = json.loads(request.get_data(as_text=True))
            user, topo = data["user"], data["topo"]
            #para = data["para"]
            msg = master_deploy_sflow(user,topo)
            return {'code': 1, 'msg': msg}
        except Exception as e:
            return {"code": 0, "msg": str(e)}

    def delete(self):
        try:
            data = json.loads(request.get_data(as_text=True))
            user, topo = data["user"], data["topo"]
            if sflow_delete(user, topo):
                return {'code': 1, 'msg': 'sflow删除成功！'}
        except Exception as e:
            return {'code': 0, 'msg': str(e)}