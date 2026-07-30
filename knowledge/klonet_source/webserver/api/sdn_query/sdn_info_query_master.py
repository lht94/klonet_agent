#######################################
## 用于虚仿实验四：ryu控制器的数据获取 ##
#######################################

from flask import request
from flask.views import MethodView
from ....Service_layer.redisAPI import UserMapRedis
from ....tools.context import redis_context
from ....tools.log_tools import FLASK_LOGGER
import json
from flask_login import login_required

def get_ne_list(user, topo):
    with redis_context(user) as user_db_cli:
        plane_topo = user_db_cli.get_value('plane_topo_list', topo)
    return plane_topo["NEs"]


class SwitchDpidAPI(MethodView):
    """
    /switch_dpid/
    """

    def get(self):
        data = json.loads(request.get_data(as_text=True))
        user, topo = data["user"], data["topo"]
        ne_list = get_ne_list(user, topo)
        sw2dpid = {}
        FLASK_LOGGER.debug(ne_list)
        user_db_map = UserMapRedis()
        user_db_cli = user_db_map.get_user_db(user)
        
        for container_name in ne_list:
            if container_name.startswith('s'):
                FLASK_LOGGER.debug(container_name)
                config = user_db_cli.get_value(
                    f"{topo}_{container_name}", "NEconfig")
                sw2dpid[container_name] = config['config']['dpid']

        user_db_cli.close()
        return sw2dpid


class HostMacAPI(MethodView):
    """
    /host_mac/
    """

    def get(self):
        data = json.loads(request.get_data(as_text=True))
        user, topo = data["user"], data["topo"]
        ne_list = get_ne_list(user, topo)
        host2mac = {}
        user_db_map = UserMapRedis()
        user_db_cli = user_db_map.get_user_db(user)

        for container_name in ne_list:
            if container_name.startswith('h'):
                container_info = user_db_cli.get_all_values(
                    f"{topo}_{container_name}")
                for k in container_info:
                    if k.startswith("link"):
                        # 因为只有一张卡
                        mac = container_info[k]['mac']
                host2mac[container_name] = mac
        
        user_db_cli.close()
        return host2mac


class LinkPortAPI(MethodView):
    """
    /link_port/
    """

    def get(self):
        data = json.loads(request.get_data(as_text=True))
        user, topo = data["user"], data["topo"]
        ne_list = get_ne_list(user, topo)
        sw2port = {}
        user_db_map = UserMapRedis()
        user_db_cli = user_db_map.get_user_db(user)

        for container_name in ne_list:
            if container_name.startswith('s'):
                container_info = user_db_cli.get_all_values(
                    f"{topo}_{container_name}")
                for k in container_info:
                    if k.startswith("link"):
                        sw_port_dict = sw2port.setdefault(container_name, {})
                        link_info = container_info[k]
                        sw_port_dict[link_info['name']] = link_info['port']
        
        user_db_cli.close()
        return sw2port
