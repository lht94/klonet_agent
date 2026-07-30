import json
from flask_login import login_required
from  flask import request
from flask.views import MethodView
from pymysql import NULL
import requests

from ....Service_layer.deploy_error import ParaError


from ....vemu_config.config import PROJ_CONFIG

from ....Service_layer.redisAPI import UserMapRedis
from ....Service_layer.redis_error import (DbCreateFailedError, DbAlreadyExistError,
                                        NoFreeDbForUserError)

from ....tools.log_tools import *

class NodesUrpfConfigAPI(MethodView):
    '''启停节点URPF服务
    
    该API为特定实验开发，目前只针对于系统所使用的ubuntu镜像，对于其它镜像使用本功能时可
    能会出现预期之外的错误。
    请求数据示例如下：
        {
            "user": "wtx",  
            "topo": "123",  
            "ne_choice":"ALL/PART"
            "ne": ['h1','h2']
        }
    ALL代表拓扑所有节点，PART代表部分节点，选择part选项时，ne列表有效，且需要用户添加需
    要启动或者停止uRPF的节点
    '''
    # 目前只实现了一个节点的全部接口，没有对接口做区分
    # 暂时没有宽松模式，只有严格uRPF与无uRPF
    def post(self):
        """启动节点URPF服务
        
        POST /master/node/urpf/
        
        Returns:
            dict: 执行结果字典
        """
        return self._handle_req_info(request, 'post')
    def delete(self):
        """停止节点URPF服务 
        
        DELETE /master/node/urpf/
        
        Returns:
            dict: 执行结果字典
        """
        return self._handle_req_info(request, 'delete')
    def _handle_req_info(self, request, method: str):
        """处理并转发请求

        Args:
            request : 请求
            method : Restful接口

        Returns:
            dict: 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo = data['user'], data['topo']

        # 确定节点所在worker的ip
        try:
            worker_node_urpf_dict = self._category_ne_worker(user, topo, data)
        except ValueError as e:
            return {'code': 0, 'msg':e.args[0]}

        # master转发请求至对应的worker
        resp_result = []
        for worker_ip, worker_urpf_nodes in worker_node_urpf_dict.items():
            info_dict = {'user': user, 'topo': topo, 'ne': worker_urpf_nodes}
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/node/urpf/'
            req_method = getattr(requests, method)
            resp_result.append(req_method(req_url, json=info_dict))
        error_msgs_dict = {}
        error_msgs_dict['worker_url'] = []
        error_msgs_dict['worker_msg'] = []
        resp_result_list = [] 
        for i,resp in enumerate(resp_result, 1):
            resp_result_list.append(resp.json()["code"])
            if resp.json()["code"] != 1:
                return_msg = resp.json()["msg"]
                error_msgs_dict['worker_url'].append(f"请求url：{req_url}") 
                error_msgs_dict['worker_msg'].append(f"{return_msg}")
                
        # print(resp_result_list)
        if not all(resp_result_list):
            FLASK_LOGGER.error(error_msgs_dict)
            if method == 'delete':
                return {'code': 0, 'msg':  'uRPF服务停止失败'}
            else:
                return {'code': 0, 'msg':  'uRPF服务启动失败'}
        if method == 'delete':
            return {'code': 1, 'msg': 'uRPF服务停止成功'}
        else:
            return {'code': 1, 'msg': 'uRPF服务启动成功'}
        
    def _category_ne_worker(self, user, topo, data:dict):
        """确定节点所在worker
        
        查询数据库，获取每个节点所在worker，并将其按worker分类返回，便于后续转发

        Args:
            user: 用户名
            topo: 项目名
            data: 请求数据

        Raises:
            ValueError: 错误数据，项目名或节点名出错

        Returns:
            dict: 根据IP分类的字典
        """
        # 查询数据库
        user_map_redis = UserMapRedis()
        user_db_cli = user_map_redis.get_user_db(user)
        info_dict = {}
        choice = data['choice']
        if choice == 'PART':
            nes = data['ne']
        else:
            nes = user_db_cli.get_value('plane_topo_list', f'{topo}')['NEs']
        # 构造并分类返回信息，以字典形式返回
        for ne in nes:
            try:
                worker_ip = user_db_cli.get_worker_ip_by_ne_name(topo, ne)
                temp_list = info_dict.setdefault(worker_ip, [])
                temp_list.append(ne)
            except:
                # 找不到对应节点信息，说明项目名或者节点名出错
                raise ValueError(f"拓扑{topo}或者节点{ne}信息出错")
        user_map_redis.close()
        user_db_cli.close()
        return info_dict
    
class NodesNetworkConfigAPI(MethodView):
    '''启停节点网络服务

    该API旨在方便联通容器与宿主机网络，目前仅支持平台所使用的ubuntu镜像，特别注意，
    网络服务将使用docker0网络（一般为172.17.0.0/16网段）以及eth0网卡，注意避免混用。
    其原理是将容器以bridge模式启动，进行apt换源，最后down掉对应的网卡，需要网络服务时再开启。
    因此，网络服务仅在容器以bridge网络模式启动时有效。
    请求数据例如：
        {
            "user": "wtx",  
            "topo": "123",  
            "ne": "h1"
        }
    '''
    def get(self):
        """获取节点网络状态"""
        data = request.args
        user, topo, ne = data['user'], data['topo'], data['ne']
        try:
            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(user)
            net_status = user_db_cli.get_value(f'{topo}_{ne}', 'NEnet')
        except: 
            FLASK_LOGGER.error("Redis database connection failed")
            # traceback.print_exc(e)
            return {'code':0, 'msg': '网络状态查询失败'}
        finally:
            user_map_redis.close()
            user_db_cli.close()
        return {'code': 1, 
                'status': net_status,
                'msg': f'节点网络服务{"已" if net_status else "未"}启动'}

    def post(self):
        """启动节点网络服务"""
        data = json.loads(request.get_data(as_text=True))
        user, topo, ne = data['user'], data['topo'], data['ne']
        try:
            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(user)
            forward_dict = self._handle_req_info(user_db_cli, topo, ne)
        except:
            FLASK_LOGGER.error("Redis database connection failed")
            # traceback.print_exc(e)
            return {'code': 0, 'msg': '失败'}
        finally:
            user_map_redis.close()
            user_db_cli.close()
        resp_list = self._forward_req(forward_dict, 'post')
        if not all(resp_list):
            return {'code': 0, 'msg':  '节点网络服务启动失败'}
        else:
            user_db_cli.set_value(f'{topo}_{ne}', 'NEnet', 1)
            return {'code': 1, 'msg': '节点网络服务启动成功'}

 
    def delete(self):
        """停止节点网络服务"""
        data = json.loads(request.get_data(as_text=True))
        user, topo, ne = data['user'], data['topo'], data['ne']
        try:
            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(user)
            forward_dict = self._handle_req_info(user_db_cli, topo, ne)
        except:
            FLASK_LOGGER.error("Redis database connection failed")
            # traceback.print_exc(e)
            return {'code': 0, 'msg': '失败'}
        finally:
            user_map_redis.close()
            user_db_cli.close()
        resp_list = self._forward_req(forward_dict, 'delete')
        if not all(resp_list):
            return {'code': 0, 'msg':  '节点网络服务停止失败'}
        else:
            user_db_cli.set_value(f'{topo}_{ne}', 'NEnet', 0)
            return {'code': 1, 'msg': '节点网络服务停止成功'}
    
    def _handle_req_info(self, user_db_cli, topo, ne):
        """处理请求信息

        Args:
            user_db_cli: 数据库连接实例
            topo: 项目名
            ne: 节点名

        Returns:
            dict: 分类字典
        """
        # 确定节点所在worker的ip
        try:
            req_dict = {}
            worker_ip = user_db_cli.get_worker_ip_by_ne_name(topo, ne)
            ctn_id = user_db_cli.get_value(f'{topo}_{ne}', 'NEid')
            req_dict['ne_id'] = ctn_id
        except:
            raise RuntimeError
        return {worker_ip:req_dict}

    def _forward_req(self, forward_dict:dict, method:str):
        """ 
        转发请求

        Args: 
            forward_dict: 转发字典
            method: Restful接口
        
        Returns:
            resp_result_list，返回码列表
            error_msgs_list，错误信息列表
        """
        # forward
        resp_result = []
        for worker_ip, req_data in forward_dict.items():
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/node/network/'
            req_method = getattr(requests, method)
            resp_result.append(req_method(req_url, json=req_data))
        # response
        resp_result_list = [] 
        for i, resp in enumerate(resp_result, 1):
            resp_result_list.append(resp.json()["code"])
            if resp.json()["code"] != 1:
                FLASK_LOGGER.error(resp.json()["msg"])
        return resp_result_list