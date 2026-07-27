import json
import traceback

from  flask import request
from flask.views import MethodView
from pymysql import NULL
import requests


from ....vemu_config.config import PROJ_CONFIG

from ....Service_layer.redisAPI import UserMapRedis
from ....Service_layer.redis_error import (DbCreateFailedError, DbAlreadyExistError,
                                        NoFreeDbForUserError)
from ....Service_layer import worker_node_urpf
from ....Service_layer import worker_node_network
from ....tools.log_tools import *

class NodesUrpfConfigAPI(MethodView):
    '''启停URPF服务

    详细见master下同名API
    '''
    def post(self):
        """启动节点URPF服务

        POST /worker/node/urpf/
        
        Returns:
            dict : 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, ne = data['user'], data['topo'], data['ne']
        try:
            worker_node_urpf.urpf_config(user, topo, ne, 1)
        except :
            traceback.print_exc()
            return {'code': 0, 'msg': 'uRPF服务启动失败'}
        return {'code':1, 'msg':'uRPF服务启动成功'}
        
    def delete(self):
        """停止节点URPF服务

        DELETE /worker/node/urpf/
        
        Returns:
            dict : 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, ne = data['user'], data['topo'], data['ne']
        try:
            worker_node_urpf.urpf_config(user, topo, ne, 0)
        except :
            traceback.print_exc()
            return {'code': 0, 'msg': 'uRPF服务停止失败'}
        return {'code':1, 'msg':'uRPF服务停止成功'}

class NodesNetworkConfigAPI(MethodView):
    '''启停节点网络服务

    详细见master下同名API
    '''
    def post(self):
        """启动节点网络服务

        POST   /master/node/network/
            {
                "ne_id": 'ctn_id'
            }
            
        Returns:
            dict : 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        ctn_id = data['ne_id']
        if not worker_node_network.network_enable(ctn_id):
            return {'code':0, 'msg':'网络服务启动失败'}
        return {'code':1, 'msg':'网络服务启动成功'}
   
    def delete(self):
        """停止节点网络服务

        DELETE /master/node/network/
        
        Returns:
            dict : 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        ctn_id = data['ne_id']
        if not worker_node_network.network_disable(ctn_id):
            return {'code':0, 'msg':'网络服务停止失败'}
        return {'code':1, 'msg':'网络服务停止成功'}
    

