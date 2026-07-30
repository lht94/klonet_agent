import traceback
from flask import request
from flask.views import MethodView
from ....Function_layer.deployed_proj_manager import retrieve_topo
from ....Function_layer.deployed_proj_manager import retrieve_topo_list,retrieve_topo_list_and_topo_info
from ....Function_layer.deployed_proj_manager import retrieve_nes2interfaces_info
from ....Function_layer.deployed_proj_manager import retrieve_link_info,retrieve_node_info,retrieve_worker_ip


class redis_topo_info(MethodView):
    """
       从Redis中聚合拓扑信息
    """
    def get(self, project_name=None):
        """
        project_name (str): 项目名/拓扑名
        """
        data = request.args
        user = data['user']
        if project_name:
            return retrieve_topo(user, project_name)
        else:
            return retrieve_topo_list(user)

class redis_topo_list_and_info(MethodView):
    """
       从Redis中获取某用户的topo列表以及该topo对应的节点、链路信息和创建时间
    """
    def get(self):
        data = request.args
        user = data['user']
        return retrieve_topo_list_and_topo_info(user)

class redis_topo_nes2interfaces(MethodView):
    """
        从Redis中得到所有的不同类型的节点
    """
    def get(self, project_name=None):
        data = request.args
        user = data['user']
        ne_types = data['ne_types'].split(',')
        try:
            if project_name:
                return retrieve_nes2interfaces_info(user, project_name, ne_types)
            else:
                return {'code': 0, 'msg': 'url中缺少项目名参数'}
        except Exception as e:
            traceback.print_exc()
            return {'code': 0, 'msg': str(e)}

class redis_topo_link(MethodView):
    """
     从redis中获取某topo中所有的链路信息：名称、源目的节点、端口信息、tc规则
    """
    def get(self, project_name=None):  
        data = request.args
        user = data['user']

        try:
            if project_name:
                return retrieve_link_info(user,project_name)
            else:
                return {'code': 0, 'msg': 'url中缺少项目名参数'}
        except Exception as e:
            traceback.print_exc()
            return {'code': 0, 'msg': str(e)}



class redis_topo_node(MethodView):
    """
     从redis中获取某topo中所有的节点信息：名称、镜像、宿主机
    
    """
    def get(self, project_name=None):
        print(project_name)
        data = request.args
        user = data['user']
        try:
            if project_name:
                return retrieve_node_info(user,project_name)
            else:
                return {'code': 0, 'msg': 'url中缺少项目名参数'}
        except Exception as e:
            traceback.print_exc()
            return {'code': 0, 'msg': str(e)}

class redis_topo_worker_ip(MethodView):
    """
     从redis中获取某topo中所有节点的所在的worker_ip地址
    
    """
    def get(self, project_name=None):
        print(project_name)
        data = request.args
        user = data['user']
        try:
            if project_name:
                return retrieve_worker_ip(user,project_name)
            else:
                return {'code': 0, 'msg': 'url中缺少项目名参数'}
        except Exception as e:
            traceback.print_exc()
            return {'code': 0, 'msg': str(e)}