# 虚仿使用的SDN代码

from flask import Flask, request
from vemu_uestc.Service_layer.redisAPI import UserMapRedis
from vemu_uestc.tools.context import redis_context
from pprint import pp, pprint
import json


app = Flask(__name__)
MASTER_PORT = "10014" # 当前master的port



def get_ne_list(user, topo):
    with redis_context(user) as user_db_cli:
        plane_topo = user_db_cli.get_value('plane_topo_list', topo)
    return plane_topo["NEs"]


@app.route('/switch_dpid/', methods=["GET", "POST"])
def get_switch_dpid():
    data = json.loads(request.get_data(as_text=True))
    user, topo = data["user"], data["topo"]
    ne_list = get_ne_list(user, topo)
    sw2dpid = {}
    print(ne_list)
    user_db_map = UserMapRedis()
    user_db_cli = user_db_map.get_user_db(user)
    
    for container_name in ne_list:
        if container_name.startswith('s'):
            print(container_name)
            config = user_db_cli.get_value(
                f"{topo}_{container_name}", "NEconfig")
            sw2dpid[container_name] = config['config']['dpid']

    user_db_cli.close()
    pprint(sw2dpid)
    return sw2dpid


@app.route('/host_mac/', methods=["GET", "POST"])
def get_host_mac():
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
    pprint(host2mac)
    return host2mac


@app.route('/link_port/', methods=["GET", "POST"])
def get_switch_port():
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
    pprint(sw2port)
    return sw2port


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=MASTER_PORT)





# python3 ...