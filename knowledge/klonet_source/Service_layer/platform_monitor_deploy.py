import requests
import json
import os
from ..Implement_layer import ContainerManager as container_manager
from ..tools import get_host_ip
from ..vemu_config.config import PROJ_CONFIG
from .deploy_error import PlatformDeployError
from ..Function_layer.config_prometheus import INIT_FILE
from traceback import print_exc

# import docker

# docker_cli = docker.from_env()

MONITOR_DICT = {
    "prometheus": {
        "name": "prometheus",
        "image": "prom/prometheus",
        "tty": "False",
        "stdin": "False",
        "net": "host",
        "volume": {
            PROJ_CONFIG.prometheus_file_path + PROJ_CONFIG.prometheus_file_name: "/etc/prometheus/prometheus.yml"
        },
        "env": "",
        "port": {
            PROJ_CONFIG.prometheus_port: "9090"
        },
        "memory": "",
        "cpuset_cpus": "", 
        "cpu_shares": "",
        "cpu_period": "", 
        "cpu_quota": "",
        "workdir": "",
        "extend": ""
    },
    "node-exporter": {
        "name": "node-exporter",
        "image": "prom/node-exporter",
        "tty": "False",
        "stdin": "False",
        "net": "bridge",
        "volume": {
            "/proc": "/host/proc",
            "/sys": "/host/sys",
            "/": "/rootfs"
        },
        "env": "",
        "port": {
            str(PROJ_CONFIG.node_exporter): "9100"
        },
        "memory": "",
        "cpuset_cpus": "", 
        "cpu_shares": "",
        "cpu_period": "", 
        "cpu_quota": "",
        "workdir": "",
        "extend": {
            "--path.procfs": "/host/proc",
            "--path.sysfs": "/host/sys",
            "--collector.filesystem.ignored-mount-points": "\"^/(sys|proc|dev|host|etc)($|/)\""
        }
    },
    "cadvisor": {
        "name": "cadvisor",
        "image": "google/cadvisor",
        "tty": "False",
        "stdin": "False",
        "net": "bridge",
        "volume": {
            "/var/run": "/var/run:rw",
            "/sys": "/sys:ro",
            "/": "/rootfs:ro",
            "/var/lib/docker/": "/var/lib/docker:ro"
        },
        "env": "",
        "port": {
            str(PROJ_CONFIG.cadvisor): "8080"
        },
        "memory": "",
        "cpuset_cpus": "", 
        "cpu_shares": "",
        "cpu_period": "", 
        "cpu_quota": "",
        "workdir": "",
        "extend": ""
    },
    # "process-exporter": {
    #     "name": "process-exporter",
    #     "image": "ncabatoff/process-exporter",
    #     "tty": "True",
    #     "stdin": "True",
    #     "net": "bridge",
    #     "volume": {
    #         "/proc": "/host/proc",
    #         "/home/monitor/prometheus/process_exporter/config/": "/config"
    #     },
    #     "env": "",
    #     "port": {
    #         "9256": "9256"
    #     },
    #     "memory": "",
    #     "cpuset_cpus": "", 
    #     "cpu_shares": "",
    #     "cpu_period": "", 
    #     "cpu_quota": "",
    #     "workdir": "",
    #     "extend": {
    #         "--procfs": "/host/proc",
    #         "-config.path": "config/process-exporter.yml"
    #     }
    # },
    "grafana": {
        "name": "grafana",
        "image": "grafana/grafana",
        "tty": "False",
        "stdin": "True",
        "net": "host",
        "volume": "",
        "env": {
            "GF_SERVER_ROOT_URL": "http://grafana.server.name",
            "GF_AUTH_ANONYMOUS_ENABLED": "true",
            "GF_SECURITY_ADMIN_PASSWORD": "[REDACTED]",
            "GF_SERVER_HTTP_PORT": PROJ_CONFIG.grafana_port
        },
        "port": "",
        "memory": "",
        "cpuset_cpus": "", 
        "cpu_shares": "",
        "cpu_period": "", 
        "cpu_quota": "",
        "workdir": "",
        "extend": {
        }
    }
}

PORT_MAP = {
    "node-exporter": {
        str(PROJ_CONFIG.node_exporter): "9100"
    },
    "cadvisor": {
        str(PROJ_CONFIG.cadvisor): "8080"
    },
    # "process-exporter": {
    #     "6003": "9256"
    # }
}


class PlatMonitorDeployManager:
    """
    平台监控创建

    Attributes:
        master_ip: 平台的master IP
        master_port: 平台的master port
        monitor_config: 监控配置信息，包括各监控组件容器的启动配置
        port_map: 监控组件的端口映射信息，可以在settings中修改
    """
    def __init__(self, master_ip, master_port):
        self.ip = get_host_ip()
        self.master_ip = master_ip
        self.master_port = master_port
        self.monitor_config = MONITOR_DICT
        self.port_map = PORT_MAP
    
    # 在Worker上创建监控容器并返回Prometheus需修改的port_list
    def run_monitor(self):
        """
        运行监控组件,并发送所创建监控组件的端口给master宿主机,以更新监控配置信息
        Returns:
            发送端口成功与否的字典
        """
        try:
            create_result = self._create_monitor_container()
            print("create_result；", create_result)
            send_port_info = self._send_monitor_port(create_result, "post")
        except Exception as e:
            print_exc()
            return {'code': 0, 'msg': f'平台监控运行失败, {e.args[0]}'}
        print(send_port_info)
        if send_port_info['code']:
            return {'code': 1, 'msg': '平台监控运行成功'}
        else:
            return {'code': 0, 'msg': '平台监控运行失败'}
        # self.monitor.start_monitor_container()
    
    def del_monitor(self):
        """
        删除监控组件,并发送所删除监控组件的端口给master宿主机,以更新监控配置信息
        Returns:
            发送端口成功与否的字典
        """
        try:
            del_result = self._delete_monitor_container()
            send_port_info = self._send_monitor_port(del_result, "delete")
        except Exception as e:
            return {'code': 0, 'msg': f'平台监控删除失败, {e.args[0]}'}
        # 打印反馈信息？
        print(send_port_info)
        if send_port_info['code']:
            return {'code': 1, 'msg': '平台监控删除成功'}
        else:
            return {'code': 0, 'msg': '平台监控删除失败'}

    def _send_monitor_port(self, port_list, method):
        """
        与Master交互, Master宿主机接收端口信息,并修改Prometheus的配置
        Returns:

        """
        url = 'http://%s:%s%s' % (self.master_ip, self.master_port, '/master/platmonitor_file/')
        print("Trying to send this worker's monitor port: ")
        print("url is ", url)
        try:
            if method == "post":
                response = requests.post(url=url, data={"port_list": port_list})
            elif method == "delete":
                response = requests.delete(url=url, data={"port_list": port_list})
            data = response.text
            send_result = json.loads(data)
            return send_result
        except Exception as e:
            return e

    def _create_monitor_container(self) -> list:
        '''
        创建监控容器并返回Prometheus需修改的配置(IP:PORT的list)
        Returns:
            port_list: 每个容器暴露的端口列表，以方便Prometheus修改配置
        '''
        run_container_list = []
        if self.ip == self.master_ip:
            # 因为promethues初次启动需要配置文件，因此需要判断
            file_path = PROJ_CONFIG.prometheus_file_path
            file_name = PROJ_CONFIG.prometheus_file_name
            if not os.path.exists(file_path):
                os.makedirs(file_path)
                with open(file_path + file_name, "w") as f:
                    f.write(INIT_FILE)
                    print("write prometheus file!!", file_path + file_name)
            for container in self.monitor_config:
                if not self._check_exist_container(container):
                    container_manager.create_container(**self.monitor_config[container])
                else:
                    print(f"{container} exist!!")
                run_container_list.append(container)
            for container in run_container_list:
                result = self._check_exist_container(container)
                if not result:
                    print(f"{container} don't exist")
                    raise PlatformDeployError(f"{container}启动失败")                   
        else:
            for container in self.monitor_config:
                if container != "prometheus" and container != "grafana":
                    if not self._check_exist_container(container):
                        container_manager.create_container(**self.monitor_config[container])
                    else:
                        print(f"{container} exist!!")
                    run_container_list.append(container)
            for container in run_container_list:
                result = self._check_exist_container(container)
                if not result:
                    print(f"{container} don't exist")
                    raise PlatformDeployError(f"{container}启动失败")
        port_list = []
        for exporter_name in self.port_map:
            for port in self.port_map[exporter_name].keys():
                port_list.append(self.ip + ":" + port)
        return port_list
    
    def _check_exist_container(self, container):
        """
        检查容器是否存在

        Args:
            container: 容器名
        Returns:
            True or False: 容器是否已经在宿主机中
        """
        result = container_manager.run_shell(f"docker ps -a").decode()
        if container in result:
            return True
        else:
            return False

    def _delete_monitor_container(self):
        """
        删除监控组件
        """
        del_container_list = []
        if self.ip == self.master_ip:
            for container in self.monitor_config:
                # 不重复删除容器
                if self._check_exist_container(container):
                    container_manager.delete_container(container)
                del_container_list.append(container)
        else:
            for container in self.monitor_config:
                if container != "prometheus" and container != "grafana":
                    if self._check_exist_container(container):
                        container_manager.delete_container(container)
                    del_container_list.append(container)
        alive_docker = container_manager.run_shell(f"docker ps -a").decode()
        for container in del_container_list:
            if container in alive_docker:
                raise PlatformDeployError(f"{container}删除失败")
        port_list = []
        for exporter_name in self.port_map:
            for port in self.port_map[exporter_name].keys():
                port_list.append(self.ip + ":" + port)
        return port_list
