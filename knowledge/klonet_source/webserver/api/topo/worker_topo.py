from flask import Blueprint, request, redirect, url_for
from flask.views import MethodView
import json

from ....Service_layer import (TopoDeleteManager, ServiceManager, TopoDeployManager)
from ....tools.context import redis_context
from ....tools.log_tools import FLASK_LOGGER
from ....tools.file_tool import clear_empty_directory, check_directory_exits
from ....vemu_config.config import PROJ_CONFIG

class TopoDeployAPI(MethodView):
    """
    处理拓扑的实际创建请求
    """
    def post(self):
        """
        创建该worker上的子拓扑
        """
        data = json.loads(request.get_data(as_text=True))
        FLASK_LOGGER.debug(data)
        deploy_manager = TopoDeployManager(**data)
        result = deploy_manager.deploy_topo()
        return result

    def delete(self):
        """
        删除该worker上的子拓扑
        """
        data = json.loads(request.get_data(as_text=True))
        FLASK_LOGGER.debug("worker_topo.py")
        delete_manager = TopoDeleteManager(**data)
        result = delete_manager.destroy_topo()
        # (Wudx)删除空的镜像文件夹
        if check_directory_exits(f"{PROJ_CONFIG.kvm_image_registry_dir}/{data['user']}/kvm_image/{data['topo']}/"):
            clear_empty_directory(f"{PROJ_CONFIG.kvm_image_registry_dir}/{data['user']}/kvm_image/{data['topo']}/")
        return result
    
    def get(self):
        """
        不支持GET请求
        """
        return {'msg': 'this url can be routed', 'code': 1}


class TopoServiceAPI(MethodView):
    """
    处理worker实际的起服务HTTP请求
    """
    def post(self):
        """
        启动worker中子拓扑的所有服务
        """
        data = json.loads(request.get_data(as_text=True))
        service_manager = ServiceManager(**data)
        return service_manager.service_deploy()

    def delete(self):
        """
        服务无法删除
        """
        return {'code': 0, 'msg': 'method not allowed', 'status': 405}

    def get(self):
        """
        不支持 get请求
        """
        return {'msg': 'this url can be routed', 'code': 1}


class ServiceDeployApi(MethodView):
    """
    处理worker实际的起服务HTTP请求
    
    ### 该类应该没有实际用到？
    """

    def post(self):
        """
        启动worker中子拓扑的所有服务
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, subtopo = data['user'], data['topo'], data['subtopo']
        with redis_context(user) as user_db_cli:
            nes = user_db_cli.get_value('subtopo_service', subtopo)
        service_deploy_manager = ServiceDeployManager(user, topo, nes)
        return service_deploy_manager.service_deploy()

    def delete(self):
        """
        服务无法删除
        """
        return {'code': 0, 'msg': 'method not allowed', 'status': 405}

    def get(self):
        """
        不支持 get请求
        """
        return {'msg': 'this url can be routed', 'code': 1}