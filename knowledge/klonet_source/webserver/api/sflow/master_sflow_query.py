import json
from flask.views import MethodView
from flask import request
import requests
import os
import docker

from ....Implement_layer.LinkManager import shell_execute
from ....vemu_config.config import PROJ_CONFIG
from ....Service_layer.redisAPI import UserMapRedis

from ....tools.log_tools import UserLogger, UserLogLevel, FLASK_LOGGER
from ....Function_layer.resource_manager import ResourceNotEnoughError

docker_cli = docker.from_env()
user_db_map = UserMapRedis()

class SflowQueryAPI(MethodView):
    """
    /sflow/
    """
    def get_flow_info(self, inter_collector_ip, collector_port, keys, value, _filter, after):
        """
        从sflow监控器获取流量信息
        """
        url = f"http://{inter_collector_ip}:{collector_port}/app/browse-flows/scripts/top.js/flows/json?keys={keys}&value={value}&filter={_filter}&after={after}"
        #sflow_info = shell_execute(f"curl {url}")
        response = requests.get(url)
        print(response.text)
        return response.json()

    def get(self):
        data = request.args
        user, topo = data["user"], data["topo"]
        user_db_cli = user_db_map.get_user_db(user)
        inter_collector_ip = user_db_cli.get_value(f"{topo}_sflow", "inter_collector_ip")
        #由于宿主机直接通信，所以端口不需要映射
        collector_port = '8008'
        type,keys,value,_filter,after  = data['type'], data['keys'], data['value'], data['filter'], data['after']
        if type == 'flow':
            sflow_info = self.get_flow_info(inter_collector_ip, collector_port, keys, value, _filter, after)
        if type == 'delay':
            pass
        return sflow_info
