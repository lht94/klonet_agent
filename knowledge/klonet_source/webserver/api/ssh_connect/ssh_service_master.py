import re
import json
import traceback
import docker
import requests
from flask.views import MethodView
from flask import request
from flask_login import login_required
from ....Implement_layer.LinkManager.link_operate import shell_execute
from ....tools.upper_level_redis_API import get_workers_to_nes
from ....tools.context import redis_context
from ....Service_layer.redis_error import TableNotExistError
from ....Service_layer.redisAPI import HostPortsAvailableRedis
from ....tools.schema.schema import parameter_check
from ....tools.schema.ssh_service_schema import *
from ....vemu_config.config import PROJ_CONFIG


docker_cli = docker.from_env()


def get_worker_ip(user, topo, ne):
    """
    节点所在的worker的ip
    """
    worker_ne_dict = get_workers_to_nes(user, topo)
    for worker, nes in worker_ne_dict.items():
        for ne_list in nes.values():
            if ne in ne_list:
                return worker


def is_netstat_free(port):
    """
    检查端口是否被占用
    """
    ports_in_use = [int(port) for port in re.findall(r'(?:\d{5})', shell_execute('netstat -nlt'))]
    return port not in ports_in_use


class SSHServiceAPI(MethodView):
    """
    有关ssh服务的api
    """
    def post(self):
        """
        开启ssh服务
        """
        try:
            # json数据解析
            data = json.loads(request.get_data(as_text=True))
            # 检查参数
            result = parameter_check(data, schema_ssh_post)
            if result['code'] == 0:
                return {'code': 0, 'msg': result['msg']}
            # 信息提取
            user, topo, ne = data['user'], data['topo'], data['ne']

            # 获得网元容器所在worker ip，并向其发送请求
            with redis_context(user) as user_db_cli:
                table = f'{topo}_{ne}'
                user_db_cli.check_table_exist(table)
                subtopo_of_ne = user_db_cli.get_value(table, 'NEloc')
                worker_ip = user_db_cli.get_value("subtopo2worker", 
                    subtopo_of_ne)

                resp = requests.post(f"http://{worker_ip}:"
                    f"{PROJ_CONFIG.worker_port}/worker/ssh_service/",
                    json=data)
                return resp.json() # 直接返回worker的返回值

        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": f"ssh开关请求发生错误：{str(e)}"}


    def get(self):
        """
        ssh服务连接信息的获取，
        连接信息包括worker的ip和节点的所有端口映射
        """
        try:
            # json数据解析
            data = json.loads(request.get_data(as_text=True))
            # 检查参数
            result = parameter_check(data, schema_ssh_get)
            if result['code'] == 0:
                return {'code': 0, 'msg': result['msg']}
            # 信息提取
            user, topo, ne = data['user'], data['topo'], data['ne']
            
            # 获得节点所在的worker的ip，用于ssh连接
            worker_ip = get_worker_ip(user, topo, ne)
            
            # 读数据库中节点已有的端口映射
            with redis_context(user) as user_db_cli:
                try:
                    table = f'{topo}_port_mapping'
                    user_db_cli.check_table_exist(table)
                    ne_port = user_db_cli.get_value(table, ne) if user_db_cli.check_exist(table, ne) else {}
                except:
                    ne_port = {}

            return {"code": 1, "worker_ip": worker_ip, "ne_port": ne_port, "msg": "节点数据获取请求成功结束！"}

        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": f"节点数据获取请求发生错误：{str(e)}"}


    def delete(self):
        return {'code': 0, 'msg': 'method not allowed', 'status': 405}


class ModifyNePortMapping(MethodView):

    def put(self):
        """
        编辑里修改端口映射，就到这里
        """
        try:
            # json数据解析
            data = json.loads(request.get_data(as_text=True))
            # 检查参数
            result = parameter_check(data, schema_port_modify)
            if result['code'] == 0:
                return {'code': 0, 'msg': result['msg']}
            # 信息提取
            user, topo, ne, port_mapping = data['user'], data['topo'], data['ne'], data['port_mapping']

            # 获得网元容器所在worker ip，并向其发送请求
            with redis_context(user) as user_db_cli:
                table = f'{topo}_{ne}'
                user_db_cli.check_table_exist(table)
                subtopo_of_ne = user_db_cli.get_value(table, 'NEloc')
                worker_ip = user_db_cli.get_value("subtopo2worker", 
                    subtopo_of_ne)

                resp = requests.put(f"http://{worker_ip}:"
                    f"{PROJ_CONFIG.worker_port}/worker/modify_port_mapping/",
                    json=data)
                return resp.json() # 直接返回worker的返回值
        
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": f"修改节点端口映射请求发生错误：{str(e)}"}

    def get(self):
        return {'code': 0, 'msg': 'method not allowed', 'status': 405}


    def delete(self):
        # 宿主机可用端口初始化
        HostPortsAvailableRedis().set_port_default()
        return {'code': 1, 'msg': '宿主机可用端口初始化成功！'}
        