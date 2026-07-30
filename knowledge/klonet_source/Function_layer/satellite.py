"""
卫星相关工具
"""

from math import atan2, atan, acos, asin, sin, cos, pi, pow, sqrt, erfc
import numpy as np
from time import strftime, localtime, time, sleep
from sympy import symbols, solve
import requests, multiprocessing
from skyfield.api import EarthSatellite, load
from skyfield.toposlib import wgs84

from ..vemu_config.config import PROJ_CONFIG
from ..Implement_layer.LinkManager import shell_execute
from ..Service_layer.NEManager import QuaggaEditor
from ..Service_layer.redis_error import (KeyNotExistError, TableNotExistError)
from ..tools.context import check_table_existence


# 默认使用的路由协议
ospf = True
bgp = False
rip = False

# 卫星跳出刷新循环时发生的Error
sat_update_error = (TableNotExistError, KeyNotExistError)

################ 和veth-pair相关 ################

def veth_move(user, topo, user_db_cli, l_name,
              dev_stable, from_dev, to_dev, mode,
              ip="", mask=""):
    """
    veth换绑，目前不支持vxlan（星地链路、星座链路）
    原来的veth-pair: dev_stable <---> from_dev
    后来的veth-pair: dev_stable <---> to_dev

    Args:
        user: 用户名
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        l_name: 链路名称
        dev_stable: veth-pair原来的一端，在换绑过程中不变
        from_dev: veth-pair原来的一端，在换绑过程改变，veth从这里迁出
        to_dev: veth-pair后来的一端，veth从这里迁入
        mode: 星间转发模式
        ip: 星地链路 - 卫星对地提供的网关
            星座链路 - 无需指定
            若为"no modify" - 不进行IP配置
        mask: 星地链路 - 卫星对地提供的子网的掩码
              星座链路 - 无需指定
    
    Returns:
        dict，其中code字段说明是否成功
    """
    try:
        # 1、准备工作
        # 1）读取数据库里的容器id
        from_dev_id = user_db_cli.get_value(f"{topo}_{from_dev}", "NEid")
        to_dev_id = user_db_cli.get_value(f"{topo}_{to_dev}", "NEid")
        # 2）链路全称
        link_name = "link_" + l_name
        # 3）读取数据库里的链路字段
        link_data = user_db_cli.get_value(f"{topo}_{from_dev}", link_name)
        # 4）迁出设备类型
        if from_dev[0] == "h":
            from_dev_type = "host"
        elif from_dev[0] == "r":
            from_dev_type = "router"
        else:
            from_dev_type = "switch"
        # 5）迁入设备类型
        if to_dev[0] == "h":
            to_dev_type = "host"
        elif to_dev[0] == "r":
            to_dev_type = "router"
        else:
            to_dev_type = "switch"
        # 6）迁移网卡的名字
        ne = f"to{dev_stable}"

        # 2、veth迁移（核心）
        # 获得容器pid和网卡名
        from_pid = shell_execute(f"docker inspect {from_dev_id} "
                                 f"| grep 'Pid\"' | sed 's/[^0-9]//g'")
        to_pid = shell_execute(f"docker inspect {to_dev_id} "
                               f"| grep 'Pid\"' | sed 's/[^0-9]//g'")
        # 迁移命令
        shell_execute(f"sudo nsenter -t {from_pid} --net "
                      f"ip link set {ne} netns {to_pid}")
        shell_execute(f"sudo nsenter -t {to_pid} --net "
                      f"ip link set {ne} up")

        # 3、将网卡加入ovs网桥中，且端口序号自增
        if from_dev_type == "switch":
            shell_execute(f"sudo docker exec {from_dev_id} "
                          f"ovs-vsctl del-port init-br0 {ne}")
        if to_dev_type == "switch":
            shell_execute(f"sudo docker exec {to_dev_id} "
                          f"ovs-vsctl add-port init-br0 {ne}")
            link_data['port'] = shell_execute(f"sudo docker exec {to_dev_id} "
                                              f"ovs-ofctl show init-br0 | grep {ne}"
                                              f" | sed 's/(.*//'")

        # 若需要进行IP配置
        if ip != "no modify":
            # 4、配置ip
            # 1）对星座链路，指定换绑侧ip
            if ip == "" and (from_dev_type == "router" or to_dev_type == "router"):
                # 不动侧节点的容器id
                dev_stable_id = user_db_cli.get_value(f"{topo}_{dev_stable}", "NEid")
                # 不动侧节点的所有网卡ip
                stable_ips = shell_execute(f"sudo docker exec {dev_stable_id} "
                                           f"ifconfig | grep -oE 'inet ([0-9]{{1,3}}\.){{3}}[0-9]{{1,3}}'"
                                           f" | awk '{{print $2}}'").split()
                # 不动侧节点的星座链路对应最大ip
                # 换绑侧ip比不动侧ip大“1”
                ip = int2ip(1 + max([ip2int(stable_ip) for stable_ip in stable_ips
                                    if is_subnet_of(PROJ_CONFIG.sat_link_ip, stable_ip)]))
                mask = PROJ_CONFIG.link_subnet_mask
            # 2）在迁入网元配置ip
            if to_dev_type == "router":
                shell_execute(f"sudo docker exec {to_dev_id} "
                            f"ifconfig {ne} {ip} netmask {mask}")
            
            # 5、配置路由
            if from_dev_type == "router" or to_dev_type == "router":
                # 1）统计路由变化(changed)、提取路由信息(info)
                # from_dev路由器信息
                if from_dev_type == "router":
                    from_dev_info = user_db_cli.get_all_values(f'{topo}_{from_dev}')
                    from_dev_changed = {}
                # to_dev路由器信息
                if to_dev_type == "router":
                    to_dev_info = user_db_cli.get_all_values(f'{topo}_{to_dev}')
                    to_dev_changed = {}
                # 2）对不同路由协议，统计路由变化
                if ospf:
                    if mode == 'IP-MODIFY' or mode == 'IP-NO-MODIFY':
                        # 待增减的子网和area号
                        net_and_area = [
                            f"{int2ip(ip2int(ip) & ip2int(mask))}/{netmask2cidr(mask)}",
                            '0.0.0.0']
                        # from_dev，修改路由信息并加入变化统计
                        if from_dev_type == "router":
                            # 尝试移除旧neighbor
                            try:
                                from_dev_info['NEconfig']['config']['ospf']['networks'].remove(net_and_area)
                            except:
                                pass
                            from_dev_changed['ospf'] = from_dev_info['NEconfig']['config']['ospf']
                        # to_dev，修改路由信息并加入变化统计
                        if to_dev_type == "router":
                            to_dev_info['NEconfig']['config']['ospf']['networks'].append(net_and_area)
                            to_dev_changed['ospf'] = to_dev_info['NEconfig']['config']['ospf']
                if rip:
                    if mode == 'IP-MODIFY' or mode == 'IP-NO-MODIFY':
                        # 待增减的子网
                        net = f"{int2ip(ip2int(ip) & ip2int(mask))}/{netmask2cidr(mask)}"
                        # from_dev，修改路由信息并加入变化统计
                        if from_dev_type == "router":
                            # 尝试移除旧neighbor
                            try:
                                from_dev_info['NEconfig']['config']['rip']['networks'].remove(net)
                            except:
                                pass
                            from_dev_changed['rip'] = from_dev_info['NEconfig']['config']['rip']
                        # to_dev，修改路由信息并加入变化统计
                        if to_dev_type == "router":
                            to_dev_info['NEconfig']['config']['rip']['networks'].append(net)
                            to_dev_changed['rip'] = to_dev_info['NEconfig']['config']['rip']
                if bgp:
                    pass
                # 3）修改quagga路由，并写入数据库
                if from_dev_type == "router":
                    QuaggaEditor(topo, from_dev, from_dev_changed, from_dev_info, user_db_cli).modify()
                    user_db_cli.set_value(f"{topo}_{from_dev}", 'NEconfig', from_dev_info['NEconfig'])
                if to_dev_type == "router":
                    QuaggaEditor(topo, to_dev, to_dev_changed, to_dev_info, user_db_cli).modify()
                    user_db_cli.set_value(f"{topo}_{to_dev}", 'NEconfig', to_dev_info['NEconfig'])

        # 6、更新数据库
        # 1）to_dev的数据库新增字段，等于from_dev数据库删的字段
        user_db_cli.set_value(f"{topo}_{to_dev}", link_name, link_data)
        user_db_cli.del_value(f"{topo}_{from_dev}", link_name)
        # 2）topo_lx中，sourceNE和targetNE一端变化，sourceType和targetType、sourceID和targetID据此变化
        to_change = "target" if dev_stable == user_db_cli.get_value(
            f"{topo}_{l_name}", "sourceNE") else "source"
        user_db_cli.set_value(f"{topo}_{l_name}", f"{to_change}NE", to_dev)
        user_db_cli.set_value(f"{topo}_{l_name}", f"{to_change}Type", to_dev_type)
        user_db_cli.set_value(f"{topo}_{l_name}", f"{to_change}ID", to_dev_id)

        # 7、返回
        return {'code': 1, 'msg': 'veth换绑成功'}
    
    except sat_update_error:
        raise TableNotExistError
    except Exception as e:
        if not check_table_existence(user,
                                     f"{topo}{PROJ_CONFIG.sat_table_name}"):
            raise TableNotExistError
        return {'code': 0, 'msg': f'veth换绑失败，{e}'}


def veth_delete(user, topo, user_db_cli, l_name,
                dev1, dev2, ne_up_down=False):
    """
    跳过请求响应删除veth-pair（星间链路）

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        l_name: 链路名称
        dev1、dev2: veth-pair两端设备
        ne_up_down: 布尔值，若为True，则使用网卡up/down模拟增删链路
    
    Returns:
        dict，其中code字段说明是否成功
    """
    try:
        # 1、读取数据库
        # 1.1 容器id
        dev1_id = user_db_cli.get_value(f"{topo}_{dev1}", "NEid")
        dev2_id = user_db_cli.get_value(f"{topo}_{dev2}", "NEid")
        # 1.2 链路全称和设备类型全称
        link_name = f"link_{l_name}"
        link_table = f"{topo}_{l_name}"

        # 2、获得容器pid和网卡名
        pid1 = shell_execute(f"docker inspect {dev1_id} | grep 'Pid\"' | sed 's/[^0-9]//g'")
        pid2 = shell_execute(f"docker inspect {dev2_id} | grep 'Pid\"' | sed 's/[^0-9]//g'")
        ne1 = f"to{dev2}"
        ne2 = f"to{dev1}"

        # 3、删除veth
        if ne_up_down:  # 对两端网卡进行up/down
            shell_execute(f"sudo nsenter -t {pid1} --net ifconfig {ne1} down")
            shell_execute(f"sudo nsenter -t {pid2} --net ifconfig {ne2} down")
        else:           # 删除veth-pair
            shell_execute(f"sudo nsenter -t {pid1} --net ip link delete {ne1}")
        
        # 4、删除ovs网桥的端口
        if dev1[0] == "s":
            shell_execute(f'sudo docker exec {dev1_id} ovs-vsctl --if-exists del-port init-br0 {ne1}')
        if dev2[0] == "s":
            shell_execute(f'sudo docker exec {dev2_id} ovs-vsctl --if-exists del-port init-br0 {ne2}')
        
        # 5、删除数据库表项
        # 5.1 删除两端设备表项“topo_xx”中，“link_lxx”字段
        user_db_cli.del_value(f"{topo}_{dev1}", link_name)
        user_db_cli.del_value(f"{topo}_{dev2}", link_name)
        # 5.2 删除“topo_lxx”表项
        user_db_cli.del_table(link_table)
        # 5.3 删除拓扑链路集中的链路信息
        topo_data = user_db_cli.get_value("plane_topo_list", topo)
        topo_data["links"].remove(l_name)
        user_db_cli.set_value("plane_topo_list", topo, topo_data)
        
        # 6、返回
        return {'code': 1, 'msg': 'veth删除成功'}
    
    except sat_update_error:
        raise TableNotExistError
    except Exception as e:
        if not check_table_existence(user,
                                     f"{topo}{PROJ_CONFIG.sat_table_name}"):
            raise TableNotExistError
        return {'code': 0, 'msg': f'veth删除失败，{e}'}


def veth_create(user, topo, user_db_cli, l_name,
                dev1, dev2,
                subnet_ip="", ne_up_down=False):
    """
    跳过请求响应新增veth-pair（星间链路）
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        l_name: 链路名称
        dev1、dev2: veth-pair两端设备
        subnet_ip: 卫星作为路由器时，设置链路子网ip
        ne_up_down: 布尔值，若为True，则使用网卡up/down模拟增删链路
    
    Returns:
        dict，其中code字段说明是否成功
    """
    try:
        # 0、变量定义
        mask = ip1 = ip2 = ""

        # 1、读取数据库
        # 1.1 容器id
        dev1_id = user_db_cli.get_value(f"{topo}_{dev1}", "NEid")
        dev2_id = user_db_cli.get_value(f"{topo}_{dev2}", "NEid")
        # 1.2 链路全称和设备类型全称
        link_name = "link_" + l_name
        link_table = f"{topo}_{l_name}"
        # 1.3 卫星节点身份
        sat_identity = "switch" if dev1[0] == "s" else "router"

        # 2、获得容器pid和网卡名
        pid1 = shell_execute(f"docker inspect {dev1_id} | grep 'Pid\"' | sed 's/[^0-9]//g'")
        pid2 = shell_execute(f"docker inspect {dev2_id} | grep 'Pid\"' | sed 's/[^0-9]//g'")
        ne1 = f"to{dev2}"
        ne2 = f"to{dev1}"

        # 3、创建veth
        #  | ne_up_down | ne exist    | output (up_down) |
        #  | √ 已使能   | √ 网卡已存在 | √ 进行up/down    |
        #  | √ 已使能   | x 网卡不存在 | x 不进行up/down  |
        #  | x 未使能   | 无论网卡状态 | x 不进行up/down  |
        up_down = ne_up_down and ne1 in shell_execute(f"sudo nsenter -t {pid1} --net ip link show")
        # 进行up/down，对两端网卡
        if up_down:
            shell_execute(f"sudo nsenter -t {pid1} --net ifconfig {ne1} up")
            shell_execute(f"sudo nsenter -t {pid2} --net ifconfig {ne2} up")
        # 不进行up/down，创建veth-pair
        else:
            shell_execute(f"sudo nsenter -t {pid1} --net ip link add {ne1} type veth peer name {ne2} netns {pid2}")
            shell_execute(f"sudo nsenter -t {pid1} --net ip link set {ne1} up")
            shell_execute(f"sudo nsenter -t {pid2} --net ip link set {ne2} up")
            # 配置ip
            if sat_identity == "router":
                mask = PROJ_CONFIG.link_subnet_mask
                if int(dev1[1:]) > int(dev2[1:]):
                    ip1 = int2ip(subnet_ip + 2)
                    ip2 = int2ip(subnet_ip + 1)
                else:
                    ip1 = int2ip(subnet_ip + 1)
                    ip2 = int2ip(subnet_ip + 2)
                shell_execute(f'sudo docker exec {dev1_id} ifconfig {ne1} {ip1} netmask {mask}')
                shell_execute(f'sudo docker exec {dev2_id} ifconfig {ne2} {ip2} netmask {mask}')
        # 获得相应网卡的mac地址
        mac1 = shell_execute(f"sudo nsenter -t {pid1} --net ifconfig {ne1} | grep ether | awk '{{print $2}}'")
        mac2 = shell_execute(f"sudo nsenter -t {pid2} --net ifconfig {ne2} | grep ether | awk '{{print $2}}'")

        # 4、新增ovs的端口
        if sat_identity == "switch":
            shell_execute(f'sudo docker exec {dev1_id} ovs-vsctl add-port init-br0 {ne1}')
            shell_execute(f'sudo docker exec {dev2_id} ovs-vsctl add-port init-br0 {ne2}')
            
        # 5、新增数据库表项
        # 5.1 新增两端设备表项“topo_xx”中，“link_lxx”字段
        user_db_cli.set_value(f"{topo}_{dev1}", link_name, {
            "ip": ip1,
            "mask": mask,
            "nic": ne1,
            "name": f"{dev1}{dev2}",
            "mac": mac1
        })
        user_db_cli.set_value(f"{topo}_{dev2}", link_name, {
            "ip": ip2,
            "mask": mask,
            "nic": ne2,
            "name": f"{dev2}{dev1}",
            "mac": mac2
        })
        # 5.2 新增“topo_lxx”表项
        user_db_cli.set_value(link_table, 'targetType', sat_identity)
        user_db_cli.set_value(link_table, 'targetPort', ne1)
        user_db_cli.set_value(link_table, 'targetNE', dev1)
        user_db_cli.set_value(link_table, 'targetIP', ip1)
        user_db_cli.set_value(link_table, 'targetID', dev1_id)
        user_db_cli.set_value(link_table, 'sourceType', sat_identity)
        user_db_cli.set_value(link_table, 'sourcePort', ne2)
        user_db_cli.set_value(link_table, 'sourceNE', dev2)
        user_db_cli.set_value(link_table, 'sourceIP', ip2)
        user_db_cli.set_value(link_table, 'sourceID', dev2_id)
        # 5.3 新增拓扑链路集中的链路信息
        topo_data = user_db_cli.get_value("plane_topo_list", topo)
        topo_data["links"].append(l_name)
        user_db_cli.set_value("plane_topo_list", topo, topo_data)

        # 6、返回
        return {'code': 1, 'msg': 'veth创建成功'}

    except sat_update_error:
        raise TableNotExistError
    except Exception as e:
        if not check_table_existence(user,
                                     f"{topo}{PROJ_CONFIG.sat_table_name}"):
            raise TableNotExistError
        return {'code': 0, 'msg': f'veth创建失败，{e}'}


def __get_modify_link_json(user, topo, link_name, source, target,
                           sourceIP="", targetIP="", method="create"):
    """
    获取增删链路的json（废弃）
    
    Args:
        user: 用户名
        topo: 拓扑名
        link_name: 链路名称
        source: 源设备
        target: 目的设备
        sourceIP/targetIP: 链路两端的IP，经研究，增删链路无需配置这个
        method: "create"（默认）或"delete"，分别对应增删链路

    Return:
        删除或创建链路的json
    """
    # 获取源、目的设备的类型
    if source[0]=='r':
        source_type = 'router'
    elif source[0]=='s':
        source_type = 'switch'
    else:
        source_type = 'host'
    if target[0]=='r':
        target_type = 'router'
    elif target[0]=='s':
        target_type = 'switch'
    else:
        target_type = 'host'
    # 返回删除或创建链路的json
    if method == 'delete':
        return {
            "user": user,
            "topo": topo,
            "info": {
                "name": link_name,
                "source": source,
                "sourceIP": sourceIP,
                "sourceType": source_type,
                "target": target,
                "targetIP": targetIP,
                "targetType": target_type
            }
        }
    elif method == 'create':
        return {
            "user": user,
            "topo": topo,
            "info": {
                "name": link_name,
                "source": source,
                "sourceIP": sourceIP,
                "sourceType": source_type,
                "target": target,
                "targetIP": targetIP,
                "targetType": target_type,
                "config": {
                    "source": {
                        "bw_kbit": "",
                        "queue_size_byte": "",
                        "delay_us": "",
                        "loss_rate": "",
                        "jitter_us": "",
                        "correlation": "",
                        "delay_distribution": "normal"
                    },
                    "target": {
                        "bw_kbit": "",
                        "queue_size_byte": "",
                        "delay_us": "",
                        "loss_rate": "",
                        "jitter_us": "",
                        "correlation": "",
                        "delay_distribution": "normal"
                    }
                }
            }
        }


def __modify_gnd_node_nic(user, topo, node_name, to_node,
                          gw, mask, ip, user_db_cli):
    """
    对地面节点新增网卡，并配置IP（废弃）

    Args:
        user: 用户名
        topo: 拓扑名
        node_name: 需要新增网卡的节点名
        to_node: 所连接另一端的节点名
        gw: 网关
        mask: 掩码
        ip: 网卡IP地址

    Return:
        若成功返回True，否则返回False
    """
    # 想新创建的网卡
    nics = [{
        "ip": ip,
        "name": f"{node_name}{to_node}",
        "netmask": mask
    }]
    # 从数据库得到所有已有网卡
    node_table = f"{topo}_{node_name}"
    for link in user_db_cli.get_value('plane_topo_list', topo)['links']:
        link_name = 'link_' + link
        if user_db_cli.check_exist(node_table, link_name):       # 链路和节点相关
            data = user_db_cli.get_value(node_table, link_name)  # 则获取端口信息
            if f"{node_name}{to_node}" != data["name"]:
                nics.append({
                    "ip": data["ip"],
                    "name": data["name"],
                    "netmask": data["mask"]
                })
    # 从数据库得到节点位置
    NEx = user_db_cli.get_value(node_table, 'NEx')
    NEy = user_db_cli.get_value(node_table, 'NEy')
    # 请求网卡修改，即在原基础上新增了一个网卡
    url = f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/modification/container/"
    try:
        # 若地面站的类型为host
        if node_name[0] == 'h':
            rsp = requests.put(url, json={
                "user": user,
                "topo": topo,
                "info":{
                    "gateway": gw,
                    "image_name": "host/ubuntu",
                    "interfaces": nics,
                    "linestyle": "solid",
                    "name": node_name,
                    "resource_limit": {},
                    "subtype": "ubuntu",
                    "type": "host",
                    "x": NEx,
                    "y": NEy
                }
            }).json()
            if rsp['code'] == 0:
                return False
            return True
        
        # 地面站类型为router还没写（实验1不涉及）
        else:
            # image_name = "router/quagga"
            # subtype = "quagga"
            # node_type = "router"
            return False
    except:
        return False

############## 和部署拓扑的json相关 ##############

def get_node_json(node_name, 
                  sdn=False, stp=True,
                  position=[0,0]):
    """
    对每个交换机、路由器、主机、控制器，返回拓扑json中的字典信息

    Args:
        node_name: 网元名称
        sdn: bool，开启SDN标志，对交换机有效
        stp: bool，开启STP标志，对交换机有效
        position: 卫星前端坐标显示

    Returns:
        拓扑json中的设备对应字典
    """
    if node_name[0] == 'r':
        ret = {
            "name": node_name,
            "config":{
                "bgp":{
                    "asn": "",
                    "enable": 0,
                    "neighbors": [],
                    "networks":[],
                    "router_id":""
                },
                "ospf":{
                    "areas":{},
                    "enable": 0,
                    "networks":[],
                    "router_id":""
                },
                "rip":{
                    "enable":0,
                    "neighbors":[],
                    "networks":[],
                    "version":2
                }
            },
            "gateway":"", 
            "image_name":"router/quagga",
            "interfaces":[],
            "linestyle":"solid",
            "resource_limit":{
                "cpu":"20",
                "mem":"200"
            },
            "subtype":"quagga",
            "type":"router",
            "x":position[0],
            "y":position[1]
        }
    elif node_name[0] == 's':
        if sdn:
            ret = {
                "name": node_name,
                "config":{
                    "controllers":[PROJ_CONFIG.default_ryu_name],
                    "stp": False
                },
                "image_name":"switch/ovs",
                "linestyle":"solid",
                "resource_limit":{
                    "cpu":"20",
                    "mem":"200"
                },
                "subtype":"ovs",
                "type":"switch",
                "x":position[0],
                "y":position[1]
            }
        else:
            ret = {
                "name": node_name,
                "config":{
                    "controllers":[],
                    "stp": stp
                },
                "image_name":"switch/ovs",
                "linestyle":"solid",
                "resource_limit":{
                    "cpu":"20",
                    "mem":"200"
                },
                "subtype":"ovs",
                "type":"switch",
                "x":position[0],
                "y":position[1]
            }
    elif node_name[0] == 'c':
        ret = {
            "name":node_name,
            "config":{
                "port": 6633
            },
            "image_name":"controller/ryu",
            "linestyle":"solid",
            "resource_limit":{
                "cpu":"20",
                "mem":"200"
            },
            "subtype":"ryu",
            "type":"controller",
            "x":position[0],
            "y":position[1]
        }
    else:
        ret = {
            "name":node_name,
            "config":{},
            "gateway":"",
            "image_name":"host/ubuntu",
            "interfaces":[],
            "linestyle":"solid",
            "resource_limit":{
                "cpu":"20",
                "mem":"200"
            },
            "subtype":"ubuntu",
            "type":"host",
            "x":position[0],
            "y":position[1]
        }
    return ret


def get_link_json(link_name, source, target,
                  rs, bw,
                  sourceIP="", targetIP="",
                  source_para=[PROJ_CONFIG.sat_EIRP, PROJ_CONFIG.sat_GT],
                  target_para=[PROJ_CONFIG.sat_EIRP, PROJ_CONFIG.sat_GT],
                  dist=0, place='sat-sat'):
    """
    对每条链路，返回拓扑json中的字典信息
    
    Args:
        link_name: 链路名称
        source: 源设备
        target: 目的设备
        rs: 星间存储转发延迟，单位是毫秒
        bw: 上行、下行、星间的带宽
        sourceIP / targetIP: 链路两端的IP，经研究，链路无需配置这个
        sourceIP: 源设备网卡IP
        targetIP: 目的设备网卡IP
        source_para: 源设备的参数，需为长度为2的list
                     [天线发射功率（单位瓦）, 天线等效面积（单位平方米）]
        target_para: 目的设备的参数，需为长度为2的list
        dist: （可选）两设备之间的距离（单位千米）
        place: 'sat-sat'（默认）或'sat-gnd'，标定链路是星地还是星间的

    Return: 返回链路json信息
    """

    # 获取源、目的设备的类型
    if source[0]=='r':
        source_type = 'router'
    elif source[0]=='s':
        source_type = 'switch'
    else:
        source_type = 'host'
    if target[0]=='r':
        target_type = 'router'
    elif target[0]=='s':
        target_type = 'switch'
    else:
        target_type = 'host'
    
    # 若定义距离，则计算链路属性
    if dist:
        # 开启链路配置
        flag = True

        # 延迟：根据光速计算
        # delay_us = str(int(dist * 1e9 / PROJ_CONFIG.light_speed))
        delay_us1 = delay_us2 = int(dist * 1e9 / PROJ_CONFIG.light_speed) + rs*1000
        # 仅有星地链路的地面站处无Rs
        if place != 'sat-sat':
            delay_us2 -= rs*1000
        #print(f'delay_us2: {delay_us2}')

        # 带宽：赋予设备频率和比特率
        if place == 'sat-sat':  # 星间链路
            freq1 = freq2 = PROJ_CONFIG.freq["sat-sat"]
            bw1 = bw2 = bw["sat-sat"]
        else:                   # 星地链路，默认第一个设备是卫星节点，第二个是地面节点
            freq1 = PROJ_CONFIG.freq["sat-gnd down"]
            freq2 = PROJ_CONFIG.freq["sat-gnd up"]
            bw1 = bw["sat-gnd down"]
            bw2 = bw["sat-gnd up"]

        # 丢包：保留五位小数
        ber0 = erfc(PROJ_CONFIG.light_speed / 4 / pi / dist / freq1 *sqrt(
                    dBW2W(source_para[0] + target_para[1] - PROJ_CONFIG.L_a) \
                    / PROJ_CONFIG.boltzmann_k / bw1 / 1e3 )) / 2
        pkt_loss0 = '{:.5f}'.format((1 - pow(1 - ber0, PROJ_CONFIG.pkt_avg_len))*100)
        ber1 = erfc(PROJ_CONFIG.light_speed / 4 / pi / dist / freq2 *sqrt(
                    dBW2W(target_para[0] + source_para[1] - PROJ_CONFIG.L_a) \
                    / PROJ_CONFIG.boltzmann_k / bw2 / 1e3)) / 2
        pkt_loss1 = '{:.5f}'.format((1 - pow(1 - ber1, PROJ_CONFIG.pkt_avg_len))*100)
        pkt_loss0 = pkt_loss1 = "0.00000"
        
        # 返回结果
        return {
            "name":link_name,
            "source":source,
            "sourceIP":sourceIP,
            "sourceType":source_type,
            "target":target,
            "targetIP":targetIP,
            "targetType":target_type,
            "config": {
                "flag": True,
                "source": {
                    "bw_kbps": str(bw1),
                    "correlation":"0",
                    "delay_distribution":"uniform",
                    "delay_us": str(delay_us1),
                    "jitter_us": "0",
                    "loss": pkt_loss0,
                    "queue_size_bytes":"100000",
                    "linkchoice": "static",
                    "link": f"link_{link_name}",
                    "ne": source
                },
                "target": {
                    "bw_kbps": str(bw2),
                    "correlation":"0",
                    "delay_distribution":"uniform",
                    "delay_us": str(delay_us2),
                    "jitter_us": "0",
                    "loss": pkt_loss1,
                    "queue_size_bytes":"100000",
                    "linkchoice": "static",
                    "link": f"link_{link_name}",
                    "ne": target
                }
            }
        }
    
    # 否则忽略链路属性
    else:
        return {
            "name":link_name,
            "source":source,
            "sourceIP":sourceIP,
            "sourceType":source_type,
            "target":target,
            "targetIP":targetIP,
            "targetType":target_type,
            "config": {
                "flag": False,
                "source": {
                    "linkchoice": "static",
                    "link": f"link_{link_name}",
                    "ne": source,
                    "bw_kbps": "20000000",
                    "correlation": "",
                    "delay_distribution": "normal",
                    "delay_us": "",
                    "jitter_us": "",
                    "loss": "",
                    "queue_size_bytes": ""
                },
                "target": {
                    "linkchoice": "static",
                    "link": f"link_{link_name}",
                    "ne": target,
                    "bw_kbps": "20000000",
                    "correlation": "",
                    "delay_distribution": "normal",
                    "delay_us": "",
                    "jitter_us": "",
                    "loss": "",
                    "queue_size_bytes": ""
                }
            }
        }


def modify_2d_front_node_y(json_dic, add_y):
    """
    将拓扑json中所有节点向下平移add_y个单位
    """
    for k, v in json_dic.items():
        if k == 'y':
            json_dic[k] += add_y
        if isinstance(v, dict):
            modify_2d_front_node_y(v, add_y)

################## 和tc配置相关 ##################

def dBW2W(db):
    """
    单位转换，将dBW转换为W
    """
    return 10 ** (db/10)


def tc_create(user, topo, user_db_cli,
              l_name, dev1, dev2,
              rs, bw,
              source_para=[PROJ_CONFIG.sat_EIRP, PROJ_CONFIG.sat_GT],
              target_para=[PROJ_CONFIG.sat_EIRP, PROJ_CONFIG.sat_GT],
              dist=0, place='sat-sat'):
    """
    链路使用的新增tc配置
    为了跳过请求相应而开发

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        l_name: 链路名称
        dev1、dev2: veth-pair两端设备
        rs: 星间存储转发延迟，单位是毫秒
        bw: 上行、下行、星间的带宽
        source_para: 源设备的参数，需为长度为2的list
                    [天线发射功率（单位瓦）, 天线等效面积（单位平方米）]
        target_para: 目的设备的参数，需为长度为2的list
        dist: （可选）两设备之间的距离（单位千米）
        place: 'sat-sat'（默认）或'sat-gnd'，标定链路是星地还是星间的
    
    Returns:
        dict，其中code字段说明是否成功
    """

    try:
        # 1、指标计算
        # 1.1、延迟：光速+距离
        if dist:  # 若定义了距离，则计算延迟
            delay_us1 = delay_us2 = int(dist * 1e9 / PROJ_CONFIG.light_speed) + rs*1000
            # 仅有星地链路的地面站处无Rs
            if place != 'sat-sat':
                delay_us2 -= rs*1000
        else:     # 否则忽略链路延迟
            delay_us1 = delay_us2 = ""
        # 1.2、带宽：赋予设备频率和比特率
        if place == 'sat-sat':  # 星间链路
            freq1 = freq2 = PROJ_CONFIG.freq["sat-sat"]
            bw1 = bw2 = bw["sat-sat"]
        else:                   # 星地链路，默认第一个设备是卫星节点，第二个是地面节点
            freq1 = PROJ_CONFIG.freq["sat-gnd down"]
            freq2 = PROJ_CONFIG.freq["sat-gnd up"]
            bw1 = bw["sat-gnd down"]
            bw2 = bw["sat-gnd up"]
        # 1.3、丢包：若定义了设备间距离，则计算丢包率，保留五位小数
        loss_module_1 = loss_module_2 = ""
        if dist:
            ber1 = erfc(PROJ_CONFIG.light_speed / 4 / pi / dist / freq1 *sqrt(
                        dBW2W(source_para[0] + target_para[1] - PROJ_CONFIG.L_a) \
                        / PROJ_CONFIG.boltzmann_k / bw1 / 1e3)) / 2
            loss1 = (1 - pow(1 - ber1, PROJ_CONFIG.pkt_avg_len))*100
            ber2 = erfc(PROJ_CONFIG.light_speed / 4 / pi / dist / freq2 *sqrt(
                        dBW2W(target_para[0] + source_para[1] - PROJ_CONFIG.L_a) \
                        / PROJ_CONFIG.boltzmann_k / bw2 / 1e3)) / 2
            loss2 = (1 - pow(1 - ber2, PROJ_CONFIG.pkt_avg_len))*100
            loss1 = loss2 = 0
            if loss1 != 0:
                loss_module_1 = f"loss {loss1}"
            if loss2 != 0:
                loss_module_2 = f"loss {loss2}"
        # 1.4、队列大小，单位bit
        queue_size_byte = 100000

        # 2、读取数据库
        # 2.1 容器id
        dev1_id = user_db_cli.get_value(f"{topo}_{dev1}", "NEid")
        dev2_id = user_db_cli.get_value(f"{topo}_{dev2}", "NEid")
        # 2.2 tc数据库表、表中链路名称、链路全称
        table_name = f"{topo}_links_config"
        link_cfg = f"link_{l_name}_config"
        link_name = f"link_{l_name}"
        # 2.3 网卡名查询
        ne1 = user_db_cli.get_value(f"{topo}_{dev1}", link_name)["nic"]
        ne2 = user_db_cli.get_value(f"{topo}_{dev2}", link_name)["nic"]

        # 3、tc配置
        # 获得容器pid
        pid1, pid2 = [shell_execute(f"docker inspect {dev_id}"
                                    f" | grep 'Pid\"' | sed 's/[^0-9]//g'")
                    for dev_id in (dev1_id, dev2_id)]
        

        # tc配置命令
        shell_cmd_prefix = f"sudo nsenter -t {pid1} --net "
        shell_execute(shell_cmd_prefix + \
                    f"tc qdisc replace dev {ne1} root handle 5:0 tbf rate {bw1}kbit "
                    f"burst {bw1/1000}kb limit {queue_size_byte}b")
        shell_execute(shell_cmd_prefix + \
                    f"tc qdisc replace dev {ne1} parent 5:0 handle 10:0 "
                    f"netem limit 100 delay {delay_us1}us {loss_module_1}")
        shell_cmd_prefix = f"sudo nsenter -t {pid2} --net "
        shell_execute(shell_cmd_prefix + \
                    f"tc qdisc replace dev {ne2} root handle 5:0 tbf rate {bw2}kbit "
                    f"burst {bw2/1000}kb limit {queue_size_byte}b")
        shell_execute(shell_cmd_prefix + \
                    f"tc qdisc replace dev {ne2} parent 5:0 handle 10:0 "
                    f"netem limit 100 delay {delay_us2}us {loss_module_2}")

        # 4、更新数据库
        user_db_cli.set_value(table_name, link_cfg, [
            {
                "bw_kbps": str(bw1),
                "correlation": "0%",
                "delay_distribution": "uniform",
                "delay_us": str(delay_us1),
                "jitter_us": "0",
                "loss": str(loss1),
                "queue_size_bytes": "",
                "linkchoice": "static",
                "link": link_name,
                "ne": dev1
            },
            {
                "bw_kbps": str(bw2),
                "correlation": "0%",
                "delay_distribution": "uniform",
                "delay_us": str(delay_us2),
                "jitter_us": "0",
                "loss": str(loss2),
                "queue_size_bytes": "",
                "linkchoice": "static",
                "link": link_name,
                "ne": dev2
            }
        ])

        # 5、返回
        return {'code': 1, 'msg': 'tc配置成功'}
    
    except sat_update_error:
        raise TableNotExistError
    except Exception as e:
        if not check_table_existence(user,
                                     f"{topo}{PROJ_CONFIG.sat_table_name}"):
            raise TableNotExistError
        return {'code': 0, 'msg': f'tc配置失败，{e}'}


def __get_delete_link_tc_json(user, topo, link_name, source, target):
    """
    获取删除链路参数配置的json（废弃）
    
    Args: user: 用户名
          topo: 拓扑名
          link_name: 链路名称
          source: 源设备
          target: 目的设备

    Return: 返回删除配置的json信息
    """
    return {
        "user": user,
        "topo": topo,
        "links":[
            {
                "link": f"link_{link_name}",
                "ne": source,
                "linkchoice":"static"
            },
            {
                "link": f"link_{link_name}",
                "ne": target,
                "linkchoice":"static"
            }
        ]
    }


def __get_create_link_tc_json(user, topo, link_name, source, target, 
                              source_para=[PROJ_CONFIG.sat_EIRP, PROJ_CONFIG.sat_GT],
                              target_para=[PROJ_CONFIG.sat_EIRP, PROJ_CONFIG.sat_GT],
                              dist=0, place='sat-sat'):
    """
    获取创建链路参数配置的json（废弃）
    
    Args:
        user: 用户名
        topo: 拓扑名
        link_name: 链路名称
        source: 源设备
        target: 目的设备
        source_para: 源设备的参数，需为长度为2的list
                    [天线发射功率（单位瓦）, 天线等效面积（单位平方米）]
        target_para: 目的设备的参数，需为长度为2的list
        dist: （可选）两设备之间的距离（单位千米）
        place: 'sat-sat'（默认）或'sat-gnd'，标定链路是星地还是星间的

    Return:
        返回创建配置的json信息
    """
    # 根据光速计算延迟
    if dist:                      # 若定义了距离，则计算延迟
        delay_us = str(int(dist * 1e9 / PROJ_CONFIG.light_speed))
    else:                         # 否则忽略链路延迟
        delay_us = ""
    
    # 赋予设备频率和比特率
    if place == 'sat-sat':  # 星间链路
        freq1 = freq2 = PROJ_CONFIG.freq["sat-sat"]
        bw1 = bw2 = PROJ_CONFIG.bw["sat-sat"]
    else:                   # 星地链路，默认第一个设备是卫星节点，第二个是地面节点
        freq1 = PROJ_CONFIG.freq["sat-gnd down"]
        freq2 = PROJ_CONFIG.freq["sat-gnd up"]
        bw1 = PROJ_CONFIG.bw["sat-gnd down"]
        bw2 = PROJ_CONFIG.bw["sat-gnd up"]

    # 若定义了设备间距离，则计算丢包率，保留五位小数
    if dist:
        ber0 = erfc(PROJ_CONFIG.light_speed / 4 / pi / dist / freq1 *sqrt(
                    dBW2W(source_para[0] + target_para[1] - PROJ_CONFIG.L_a) \
                    / PROJ_CONFIG.boltzmann_k / bw1 / 1e3)) / 2
        pkt_loss0 = '{:.5f}'.format((1 - pow(1 - ber0, PROJ_CONFIG.pkt_avg_len))*100)
        ber1 = erfc(PROJ_CONFIG.light_speed / 4 / pi / dist / freq2 *sqrt(
                    dBW2W(target_para[0] + source_para[1] - PROJ_CONFIG.L_a) \
                    / PROJ_CONFIG.boltzmann_k / bw2 / 1e3)) / 2
        pkt_loss1 = '{:.5f}'.format((1 - pow(1 - ber1, PROJ_CONFIG.pkt_avg_len))*100)
        pkt_loss0 = pkt_loss1 = "0.00000"
        # print(f'bit loss: {ber0}, {ber1}')
        # print(f'loss: {pkt_loss0}, {pkt_loss1}')
    else:
        pkt_loss0 = pkt_loss1 = ""
    
    # 返回请求修改链路属性的json
    return {
        "user": user,
        "topo": topo,
        "links":[
            {
                "bw_kbps": str(bw1),
                "correlation": "0%",
                "delay_distribution": "uniform",
                "delay_us": delay_us,
                "jitter_us": "0",
                "loss": pkt_loss0,
                "queue_size_bytes": "",
                "linkchoice": "static",
                "link": f"link_{link_name}",
                "ne": source
            },
            {
                "bw_kbps": str(bw2),
                "correlation": "0%",
                "delay_distribution": "uniform",
                "delay_us": delay_us,
                "jitter_us": "0",
                "loss": pkt_loss1,
                "queue_size_bytes": "",
                "linkchoice": "static",
                "link": f"link_{link_name}",
                "ne": target
            }
        ]
    }

################### 和组网相关 ###################

def int2ip(number: int):
    """
    将一个int数转化为ipv4
    """
    ret = ''
    for i in range(4):
        ret += str(int(number / 256 ** (3-i) % 256)) + '.'
    return ret[:-1]


def ip2int(ip: str):
    """
    将一个ipv4字符串转化为int数
    """
    ret = 0
    for i, num in enumerate(ip.split('.')):
        ret += int(num) * 256 ** (3-i)
    return ret


def netmask2cidr(netmask: str):
    """
    将掩码转换为CIDR中“/”后面的数字
    """
    return ''.join(format(int(part), '08b')
                   for part in netmask.split('.')).count('1')


def cidr2netmask(cidr_num: int):
    """
    将CIDR中“/”后面的数字转换为掩码
    """
    parts = [0, 0, 0, 0]
    for i in range(cidr_num):
        parts[i // 8] |= (1 << (7 - i % 8))
    return '.'.join(map(str, parts))


def _subnet_count(ip_mask_list: list):
    """
    子网数统计

    Args:
        ip_mask_list: 列表，其中每个元素是一个长度为2的列表，第0个元素是ip，第1个元素是掩码

    Return:
        返回列表中各个ip所属于的子网数
    """
    subnets = set()
    for ip_mask in ip_mask_list:
        subnets.add(ip2int(ip_mask[0]) & ip2int(ip_mask[1]))
    return len(subnets)


def is_subnet_of(cidr_net: str, ip: str):
    """
    判断ip是否在cidr_net网段中
    """
    net_ip, cidr_num = cidr_net.split('/')
    return int2ip(ip2int(ip) & ip2int(cidr2netmask(int(cidr_num)))) == net_ip

################### 和DHCP相关 ###################

def start_dhcp_server(topo, user_db_cli, dev, ip, ne):
    """
    在卫星上开启dhcp服务，预先已运行以下命令
        - chmod 777 /tmp
        - apt update
        - apt install isc-dhcp-server
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
        ip: 设备网卡ip
        ne: 网卡名
    
    Returns:
        dict，其中code字段说明是否成功
    """
    # 小子网掩码
    mask = ip2int(PROJ_CONFIG.sat_gnd_subnet_mask)
    # 容器id
    dev_id = user_db_cli.get_value(f"{topo}_{dev}", "NEid")
    # docker执行命令前缀
    shell_cmd_prefix = f'sudo docker exec {dev_id} '
    # 整数形式的网络号
    subnet_int = ip & mask
    # 网段内的ip数 - 1
    ip_cnt = ip2int('255.255.255.255') - mask

    # 步骤1：修改 /etc/dhcp/dhcpd.conf
    content = \
        'option domain-name \\' + '"example.org\\' + '";default-lease-time 600;' + \
        'max-lease-time 7200;ddns-update-style none;' + \
        'subnet ' + int2ip(subnet_int) + ' netmask ' + PROJ_CONFIG.sat_gnd_subnet_mask + \
        '{\n    range ' + int2ip(subnet_int + 2) + ' ' + int2ip(subnet_int + ip_cnt - 1) + \
        ';\n    option routers ' + int2ip(subnet_int + 1) + \
        ';\n    option subnet-mask ' + PROJ_CONFIG.sat_gnd_subnet_mask + \
        ';\n    option broadcast-address ' + int2ip(subnet_int + ip_cnt) + \
        ';\n    option domain-name-servers ' + int2ip(subnet_int + 1) + \
        ';\n}'
    shell_execute(shell_cmd_prefix + \
                  f"sh -c \"echo '{content}' > /etc/dhcp/dhcpd.conf\"")
    # 步骤2：修改 /etc/default/isc-dhcp-server
    content = \
        'INTERFACESv4=\\' + f'"to{ne}\\' + '"\n' + 'INTERFACESv6=\\"\\"'
    shell_execute(shell_cmd_prefix + \
                  f"sh -c \"echo '{content}' > /etc/default/isc-dhcp-server\"")
    # 步骤3：启动服务
    shell_execute(shell_cmd_prefix + \
                  "service isc-dhcp-server restart")


def start_dhcp_client(topo, user_db_cli, dev, ip):
    """
    在地面站主机上开启dhcp连接

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
        ip: 小子网中的一个ip，用来求出卫星上dhcp服务器的ip
    
    Returns:
        dict，其中code字段说明是否成功
    """
    # 容器id
    dev_id = user_db_cli.get_value(f"{topo}_{dev}", "NEid")
    # docker执行命令前缀
    shell_cmd_prefix = f'sudo docker exec {dev_id} '
    # 撤销DHCP参数，释放DHCP租约
    shell_execute(shell_cmd_prefix + "dhclient -r")
    # 若有新卫星连接，则更新DHCP参数
    if ip:
        # 小子网掩码
        mask = ip2int(PROJ_CONFIG.sat_gnd_subnet_mask)
        # 卫星上dhcp服务器的ip
        server_ip = int2ip(ip & mask + 1)
        # 步骤1：修改 /etc/resolv.conf
        content = f"nameserver {server_ip}"
        shell_execute(shell_cmd_prefix + \
                    f"echo '{content}' > /etc/resolv.conf")
        # 步骤2：启动服务
        shell_execute(shell_cmd_prefix + "dhclient")


def stop_dhcp_server(topo, user_db_cli, dev):
    """
    在卫星上关闭dhcp服务
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
    
    Returns:
        dict，其中code字段说明是否成功
    """
    # 容器id
    dev_id = user_db_cli.get_value(f"{topo}_{dev}", "NEid")
    # 关闭服务
    shell_execute(f'sudo docker exec {dev_id} service isc-dhcp-server stop')


def stop_dhcp_client(topo, user_db_cli, dev):
    """
    在地面站主机上关闭dhcp连接

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
    
    Returns:
        dict，其中code字段说明是否成功
    """
    # 容器id
    dev_id = user_db_cli.get_value(f"{topo}_{dev}", "NEid")
    # 撤销DHCP参数，释放DHCP租约
    shell_execute(f'sudo docker exec {dev_id} dhclient -r')

################### 和隧道相关 ###################

def create_tunnel_between(topo, user_db_cli,
                          dev1, ip_in1, dev2, ip_in2,
                          tunnel_name='tun1', tunnel_mode='gre'):
    """
    在主机设备间创建隧道
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev1, dev2: 设备名称
        ip_in1, ip_in2: 设备内网IP，带cidr的斜杠格式
        tunnel_name: 隧道名称
        tunnel_mode: 隧道类型
    """
    try:
        # 1、准备工作
        # 数据库读取容器id
        dev1_id, dev2_id = (user_db_cli.get_value(f"{topo}_{dev}", "NEid")
                            for dev in [dev1, dev2])
        # 命令docker exec的运行前缀
        shell_cmd_prefix_1 = f'sudo docker exec {dev1_id} '
        shell_cmd_prefix_2 = f'sudo docker exec {dev2_id} '
        
        # 2、获取外网IP
        ip_out1 = shell_execute(shell_cmd_prefix_1 + \
                                f"ifconfig | grep 'inet 10' | awk '{{print $2}}'")
        ip_out2 = shell_execute(shell_cmd_prefix_2 + \
                                f"ifconfig | grep 'inet 10' | awk '{{print $2}}'")
        
        # 3、主机1内运行
        # 创建隧道（外网）
        shell_execute(shell_cmd_prefix_1 + \
                        f"ip tunnel add {tunnel_name} mode {tunnel_mode} remote "
                        f"{ip_out2} local {ip_out1} ttl 255")
        # 添加隧道的接口地址（内网）
        shell_execute(shell_cmd_prefix_1 + \
                    f"ip addr add dev {tunnel_name} {ip_in1} peer {ip_in2}")
        # 开启隧道虚拟网卡
        shell_execute(shell_cmd_prefix_1 + \
                    f"ip link set {tunnel_name} up")
        
        # 3、主机2内运行
        # 创建隧道（外网） 
        shell_execute(shell_cmd_prefix_2 + \
                    f"ip tunnel add {tunnel_name} mode {tunnel_mode} remote "
                    f"{ip_out1} local {ip_out2} ttl 255")
        # 添加隧道的接口地址（内网）
        shell_execute(shell_cmd_prefix_2 + \
                    f"ip addr add dev {tunnel_name} {ip_in2} peer {ip_in1}")
        # 开启隧道虚拟网卡
        shell_execute(shell_cmd_prefix_2 + \
                    f"ip link set {tunnel_name} up")
    
    except sat_update_error:
        raise TableNotExistError
    

def change_tunnel_between(topo, user_db_cli,
                          dev, peer_dev, ip_out, 
                          link_name,
                          tunnel_name='tun1',
                          tunnel_mode='gre',
                          exec_already=False):
    """
    在单主机设备上修改隧道，主要修改的是外网连接的IP
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
        peer_dev: 隧道对端设备
        ip_out: 设备外网IP，不带cidr的斜杠格式
        link_name: 链路名称
        tunnel_name: 隧道名称
        tunnel_mode: 隧道类型
        exec_already: 主机内执行部分代码仅执行一次
    """
    try:
        # 1、准备工作
        # 数据库读取容器id
        dev_id = user_db_cli.get_value(f"{topo}_{dev}", "NEid")
        peer_dev_id = user_db_cli.get_value(f"{topo}_{peer_dev}", "NEid")
        # 小子网掩码
        mask = ip2int(PROJ_CONFIG.sat_gnd_subnet_mask)
        # 卫星IP，作为地面站网关
        gateway = int2ip((ip2int(ip_out) & mask) + 1)
        
        # 2、隧道对端设备运行
        shell_cmd_prefix = f"sudo docker exec {peer_dev_id} "
        # 获得IP
        peer_ip_out = shell_execute(shell_cmd_prefix + \
                                    f"ifconfig | grep 'broadcast 10' | "
                                    f"awk '{{print $2}}'")
        # 修改隧道IP
        shell_execute(shell_cmd_prefix + \
                    f"ip tunnel change {tunnel_name} mode {tunnel_mode} remote "
                    f"{ip_out} local {peer_ip_out} ttl 255")
        
        # 以下部分仅执行一次即可
        if exec_already == False:
            # 3、主机内运行
            shell_cmd_prefix = f"sudo docker exec {dev_id} "
            # 修改隧道IP
            shell_execute(shell_cmd_prefix + \
                        f"ip tunnel change {tunnel_name} mode {tunnel_mode} remote "
                        f"{peer_ip_out} local {ip_out} ttl 255")
            # 地面站连接至卫星的网卡名
            ne = shell_execute(shell_cmd_prefix + \
                            f"ifconfig | grep to | awk '{{print $1}}'")[:-1]
            # 修改地面站网卡IP
            shell_execute(shell_cmd_prefix + \
                        f'ifconfig {ne} {ip_out} netmask {mask}')
            # 修改地面站网关
            shell_execute(shell_cmd_prefix + \
                        f'route add default gw {gateway}')

            # 4、数据库修改
            tb_name = f"{topo}_{dev}"
            link_conf = user_db_cli.get_value(tb_name, link_name)
            link_conf["ip"] = ip_out
            user_db_cli.set_value(tb_name, link_name, link_conf)
            link_conf = user_db_cli.set_value(tb_name, "NEgateway", gateway)
    
    except sat_update_error:
        raise TableNotExistError


def delete_tunnel_between(topo, user_db_cli,
                          dev1, dev2, tunnel_name='tun1'):
    """
    在主机设备间删除隧道
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev1, dev2: 设备名称
        tunnel_name: 隧道名称
    """
    try:
        # 1、准备工作
        # 数据库读取容器id
        dev1_id, dev2_id = (user_db_cli.get_value(f"{topo}_{dev}", "NEid")
                            for dev in [dev1, dev2])
      
        # 2、主机1内运行删除隧道
        shell_execute(f"sudo docker exec {dev1_id} "
                      f"ip tunnel del {tunnel_name}")
        
        # 3、主机2内运行删除隧道
        shell_execute(f"sudo docker exec {dev2_id} "
                      f"ip tunnel del {tunnel_name}")
    
    except sat_update_error:
        raise TableNotExistError

################### 和模式相关 ###################

def sat_start_mode(topo, user_db_cli, old_mode, new_mode,
                   walkers, sat_gnd_links, sat_id1,
                   sat_gnd_nets, sat_ovs_gnd,
                   dev_list, gnd_devices):
    """
    某模式启动时，进行初始化配置

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        old_mode: 旧模式
        new_mode: 新模式，需要启用
        walkers: 星座参数
        sat_gnd_links: 星地链路
        sat_id1: 第一个卫星编号
        sat_gnd_nets: 小子网创建映射
        sat_ovs_gnd: 地面站星下ovs
        dev_list: 地面站列表
        gnd_devices: 地面站参数字典
    """
    # 1、模式功能配置
    if new_mode == 'STP':
        # 配置所有交换机的RSTP
        existed_sat_id = 0
        for walker in walkers:
            N = walker['N']
            # 对每个卫星，开启rstp
            for j in range(N):
                # 卫星id
                sat_id = existed_sat_id + j
                # 卫星设备名
                sat_dev = f"s{sat_id1+sat_id}"
                # 设备容器id
                cnt_id = user_db_cli.get_value(f"{topo}_{sat_dev}", "NEid")
                # 运行指令
                shell_execute(f"sudo docker exec {cnt_id} "
                              f'ovs-vsctl set bridge init-br0 rstp_enable=true')
            existed_sat_id += N
  
    elif new_mode == 'NO-STP':
        # 关闭所有交换机的RSTP
        existed_sat_id = 0
        for walker in walkers:
            N = walker['N']
            # 对每个卫星，开启rstp
            for j in range(N):
                # 卫星id
                sat_id = existed_sat_id + j
                # 卫星设备名
                sat_dev = f"s{sat_id1+sat_id}"
                # 设备容器id
                cnt_id = user_db_cli.get_value(f"{topo}_{sat_dev}", "NEid")
                # 运行指令
                shell_execute(f"sudo docker exec {cnt_id} "
                              f'ovs-vsctl set bridge init-br0 rstp_enable=false')
            existed_sat_id += N
    
    elif new_mode == 'DHCP':
        # 卫星启动 DHCP server
        existed_sat_id = 0
        for walker in walkers:
            N = walker['N']
            # 对每个卫星，查看上层卫星连接
            for j in range(N):
                # 卫星id
                sat_id = existed_sat_id + j
                # 卫星设备名
                sat_dev = f"r{sat_id1+sat_id}"
                start_dhcp_server(topo, user_db_cli, sat_dev,
                                  sat_gnd_nets[str(sat_id)],
                                  sat_ovs_gnd[str(sat_id)])
            existed_sat_id += N
        # 地面站主机启动 DHCP client
        for dev, val in sat_gnd_links.items():
            start_dhcp_client(topo, user_db_cli, dev,
                              sat_gnd_nets[str(val[0])])
    
    elif new_mode == 'IP-TUNNEL':
        if old_mode != '':
            # 小子网掩码
            mask = ip2int(PROJ_CONFIG.sat_gnd_subnet_mask)
            # 为每个有卫星连接的地面站配置IP
            for dev in dev_list:
                connect_to_sat = str(sat_gnd_links[dev][0])
                if connect_to_sat != None:
                    # 容器id
                    dev_id = user_db_cli.get_value(f"{topo}_{dev}", "NEid")
                    # 执行命令前缀
                    shell_cmd_prefix = f"sudo docker exec {dev_id} "
                    # 网卡名
                    ne = shell_execute(shell_cmd_prefix + \
                                       f"ifconfig | grep tos | awk '{{print $1}}'")[:-1]
                    # 修改ip
                    shell_execute(shell_cmd_prefix + \
                                  f"ifconfig {ne} {int2ip(sat_gnd_nets[connect_to_sat])} "
                                  f"netmask {int2ip(mask)}")
                    # 子网内下一可用ip自增
                    sat_gnd_nets[connect_to_sat] += 1
                    # 卫星IP
                    sat_ip = int2ip((sat_gnd_nets[connect_to_sat] & ip2int(PROJ_CONFIG.sat_gnd_subnet_mask)) + 1)
                    # 计算下一可用ip是否溢出
                    if sat_gnd_nets[connect_to_sat] >= ip2int(sat_ip) + \
                        2*(2**(32-netmask2cidr(PROJ_CONFIG.sat_gnd_subnet_mask)) - 1):
                        sat_gnd_nets[connect_to_sat] = int2ip(ip2int(sat_ip) + 1)
        
        # 建立主机两两之间的隧道
        # tunnel名字：按从前往后顺序的设备名相加，如h1h2
        for i in range(len(dev_list)):
            for j in range(i + 1, len(dev_list)):
                dev1, dev2 = dev_list[i], dev_list[j]
                create_tunnel_between(topo, user_db_cli, 
                                      dev1, gnd_devices[dev1]['ip'], 
                                      dev2, gnd_devices[dev2]['ip'], 
                                      tunnel_name=dev1+dev2)


def sat_stop_mode(topo, user_db_cli, mode,
                  walkers, sat_gnd_links, sat_id1,
                  sat_gnd_nets, sat_ovs_gnd,
                  dev_list, gnd_devices):
    """
    某模式退出时，删除配置

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        mode: 旧模式，需要暂停
        walkers: 星座参数
        sat_gnd_links: 星地链路
        sat_id1: 第一个卫星编号
        sat_gnd_nets: 小子网创建映射
        sat_ovs_gnd: 地面站星下ovs
        dev_list: 地面站列表
        gnd_devices: 地面站参数字典
    """
    # 1、模式功能配置
    if mode == 'DHCP':
        # 卫星停止 DHCP server
        existed_sat_id = 0
        for walker in walkers:
            N = walker['N']
            # 对每个卫星，查看上层卫星连接
            for j in range(N):
                # 卫星id
                sat_id = existed_sat_id + j
                # 卫星设备名
                sat_dev = f"r{sat_id1+sat_id}"
                stop_dhcp_server(topo, user_db_cli, sat_dev)
            existed_sat_id += N
        # 地面站主机停止 DHCP client
        for dev in dev_list:
            stop_dhcp_client(topo, user_db_cli, dev)
    
    elif mode == 'IP-TUNNEL':
        # 删除主机两两之间的隧道
        # tunnel名字：按从前往后顺序的设备名相加，如h1h2
        for i in range(len(dev_list)):
            for j in range(i + 1, len(dev_list)):
                dev1, dev2 = dev_list[i], dev_list[j]
                delete_tunnel_between(topo, user_db_cli, 
                                      dev1, dev2, tunnel_name=dev1+dev2)


def sat_change_mode(topo, user_db_cli, mode_from, mode_to, *args):
    """
    模式变更时，删除并配置模式

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        mode_from, mode_to: 模式变更
        arg: 其他在进行配置时使用的参数

    Returns:
        新模式
    """
    if mode_from != mode_to:
        sat_stop_mode(topo, user_db_cli, mode_from, *args)
        sat_start_mode(topo, user_db_cli, mode_from, mode_to, *args)
    return mode_to


def ctn_satlog(topo, user_db_cli, dev, string):
    """
    在容器中写入卫星相关日志信息
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
        string: 写入字符串
    """
    try:
        # 1、准备工作
        # 数据库读取容器id
        dev_id = user_db_cli.get_value(f"{topo}_{dev}", "NEid")
      
        # 2、主机内运行
        shell_execute(f"sudo docker exec {dev_id} "
                      f'sh -c \"echo \'{string}\' >> {PROJ_CONFIG.container_log_file}\"')
        
    except sat_update_error:
       raise TableNotExistError  

################### 和星座相关 ###################

def get_walker_para(walker_dict):
    """
    从星座字典中获得N、P、i、F、h、sensor_angle等参数
    以创建walker对象
    """
    return [walker_dict[key] for key in ["N", "P", "i", "F", "h", "sensor_angle"]]


def timestamp2date(timestamp):
    """
    将时间戳转化为日期的格式

    Args:
        timestamp: 时间戳，单位为秒

    Returns:
        list，包含年、月、日、时、分、秒六个元素
    """
    timeArray = localtime(timestamp)
    time_list = strftime("%Y-%m-%d-%H-%M-%S", timeArray).split('-')
    return [int(val) for val in time_list]


def wgs84_to_spotdown(wgs84_position):
    """
    将wgs84坐标转化为星下点和海拔高度
    """
    # 经度：东经正数，西经负数
    lon = atan2(wgs84_position[1], wgs84_position[0]) * 180 / pi
    # 海拔：距地心高度
    alt = np.linalg.norm(wgs84_position)
    # 纬度：北纬正数，南纬负数
    try:
        lat = asin(wgs84_position[2] / alt) * 180 / pi
    except ValueError:
        lat = 90 if wgs84_position[2] / alt > 1 else -90
    # 返回
    return lon, lat, alt 


def spotdown_to_wgs84(lon, lat, alt):
    """
    将星下点和海拔高度转化为wgs84直角坐标
    """
    x = alt * cos(lat/180*pi) * cos(lon/180*pi)
    y = alt * cos(lat/180*pi) * sin(lon/180*pi)
    z = alt * sin(lat/180*pi)
    return x, y, z


def get_limit_elevation_ang_or_dist(h1, h2, up_ang, down_ang,
                                    output="elevation_ang"):
    """
    获取两设备间的极限值，包括最小仰角或最大距离
    计算星地链路和不同高度轨道的星座链路使用

    Args:
        h1, h2: 两设备高度
        up_ang: 位于低处的设备向上看的最大张角
        down_ang: 位于高处的设备向下看的最大张角
        output: 输出内容，"elevation_ang"指输出最小仰角，"dist"指输出最大距离
        
    Returns:
        单位为度的最小仰角
    """
    # 模型准备
    h = max(h1, h2)
    R = min(h1, h2)
    K = h * h - R * R
    cos_2 = cos(down_ang/360*pi)
    cos_1 = cos(up_ang/360*pi)
    # 模型求解
    l = symbols('l', real=True)
    f1 = l * l - 2 * l * h * cos_2 + K
    f2 = l * l + 2 * l * R * cos_1 - K
    ans1 = solve([f1])  # 第一个方程的解集，可能0~2个解
    ans2 = solve([f2])  # 第二个方程的解集，有2个解
    if len(ans1) != 2:
        l = ans2[1][l]  # 设备距离最大值
    else:
        if ans1[1][l] <= ans2[1][l]:
            l = ans2[1][l]
        else:
            l = min(ans1[0][l], ans2[1][l])
    if output == "dist":
        return l
    # 满足约束的最优值
    M = acos((h*h + R*R - l*l) /2 /R /h)  # ∠3的最大值
    if M >= (up_ang + down_ang) / 2:
        return 90 - up_ang / 2
    else:
        return 90 - down_ang / 2 - M


def get_visible_sats(time, sat_id, low_walker,
                     high_walker, max_dist, existed_sat_id):
    """
    （星座链路）walker星座不同高度轨道间，低轨卫星可见的所有高轨卫星

    Args:
        time: 计算卫星位置的时刻
        sat_id: 低轨卫星的序号（0~N-1之间）
        low_walker: 低轨星座对象
        high_walker: 高轨星座对象
        # low_ang: 低轨卫星覆盖张角
        # high_ang: 高轨卫星发改张角
        max_dist: 两星相连最长距离
        existed_sat_id: 卫星id平移
        
    Returns:
        dict，key是卫星id列出所有可见卫星，value是星地距离
    """
    # 较低轨卫星的wgs84坐标
    low_sat_pos = low_walker.get_onesat_wgs84_pos(time, sat_id)
    # 较高轨星座中所有卫星的wgs84坐标
    high_sat_poses = high_walker.get_wgs84_pos(time)
    # 返回值
    ret = {}
    # 遍历所有较高轨星座卫星
    for i, pos in enumerate(high_sat_poses):
        # 计算两星距离
        dist = sqrt((pos[0] - low_sat_pos[0])**2 + \
                    (pos[1] - low_sat_pos[1])**2 + \
                    (pos[2] - low_sat_pos[2])**2)
        # 若两星距离不超过阈值，则说明两星可见，加入返回值字典
        if dist < max_dist:
            ret[i+existed_sat_id+low_walker.N] = dist
    # 返回
    return ret


def get_best_visible_sat(time, sat_id, low_walker,
                         high_walker, max_dist, existed_sat_id):
    """
    （星座链路）walker星座不同高度轨道间，低轨卫星可见的最佳高轨卫星
    均采用“最短路径优先”

    Args:
        time: 计算卫星位置的时刻
        sat_id: 低轨卫星的局部序号（0~N-1之间）
        low_walker: 低轨星座对象
        high_walker: 高轨星座对象
        # low_ang: 低轨卫星覆盖张角
        # high_ang: 高轨卫星发改张角
        max_dist: 两星相连最长距离
        existed_sat_id: 卫星id平移
        
    Returns:
        list，第一个元素是：卫星id列出的最佳连接的可见卫星; 
              第二个元素是：两星距离，单位km
        两个None，说明无可见高轨卫星
    """
    # 获得所有可见卫星字典
    all_visible_sats = get_visible_sats(time, sat_id, low_walker, 
                                        high_walker, max_dist, existed_sat_id)
    # 若无可见卫星，则返回两个None
    if all_visible_sats == {}:
        return None, None
            
    # 选择最佳卫星，仅考虑距离最近
    min_dist = np.inf
    for sat_id, val in all_visible_sats.items():
        if val < min_dist:
            best_sat, min_dist = sat_id, val
    return [best_sat, min_dist]


class Walker():
    """
    单层的Walker卫星对象
    """
    def __init__(self, N, P, i, h, F, sensor_angle=170) -> None:
        """
        walker星座初始化
        """
        self.N = N         # 卫星总数
        self.P = P         # 星座轨道面数
        self.i = i         # 卫星轨道倾角，单位：°
        self.h = h         # 卫星轨道半径，单位：km
        self.F = F         # 相位因子，0~(S-1)间的整数，代表相邻两轨道面星间相位关系
        self.S = int(N/P)  # 每轨道卫星数
        self.sat_ang = sensor_angle  # 天线张角
        # walker星座各卫星每天绕地圈数
        self.circles = sqrt(PROJ_CONFIG.GM) * 12 * 3600 / pi / pow(h*1000, 1.5)
        # 邻轨星间链路天线旋转的最大角度，体现在两星距离上
        self.dist_min = \
            cos(acos(sin(i*pi/180)**2 * cos(pi/P) + cos(i*pi/180)**2) / 2) \
            * F * h / N / sin(sensor_angle/360*pi) * 2 * pi

    def _satellite_run(self, yr, mon, day, hr, mins, sec, satellite):
        """
        计算某时刻卫星对象的位置

        Args:
            yr, mon, day, hr, mins, sec: 年月日时分秒
            satellite: 卫星对象

        Return: 
            (lon, lat, alt, r)，分别为经度、纬度、高度、WGS84三维坐标
        """
        # sgp4（废弃）
        # r, _ = satellite.propagate(yr, mon, day, hr, mins, sec)
        # lon, lat, alt = self._wgs84_to_spotdown(r)
        # skyfield
        t = load.timescale().utc(yr, mon, day, hr, mins, sec)
        geocentric = satellite.at(t)
        # 转化为wgs84
        wgs84_pos = wgs84.geographic_position_of(geocentric)
        lon = wgs84_pos.longitude.degrees
        lat = wgs84_pos.latitude.degrees
        alt = wgs84_pos.elevation.km
        # 转化为xyz，单位km
        r = [(alt + wgs84.radius.km) * cos(lat/180*pi) * cos(lon/180*pi),
             (alt + wgs84.radius.km) * cos(lat/180*pi) * sin(lon/180*pi),
             (alt + wgs84.radius.km) * sin(lat/180*pi)]
        # 返回
        return (lon, lat, alt+wgs84.radius.km, r)

    def _get_tles(self):
        """
        生成walker星座中所有卫星的TLE星历
        
        Returns:
            list，包含所有卫星的两行TLE，例：
            [['1 48580U 21041AD  23059.20970124  .00000000  00000+0  00000+0 0  000',
              '2 48580 070.0000 270.0000 0000000 000.0000 459.0000 12.13298926 99386']]
        """
        def _tle_format(num, all=8, dec=4):
            return str(f"%.{dec}f"%num).zfill(all)
        
        tles = []                      # 计算各卫星tle，并加入该列表
        det_u = 360 / self.N * self.F  # 邻轨对应卫星间的相位差
        for sat_id in range(self.N):
            Pm = int(sat_id / self.S)  # 轨道面编号，0 ~ P-1
            Nm = sat_id % self.S       # 轨道内编号，0 ~ S-1
            omega_m = 180 / self.P * Pm           # 升交点赤经
            u_m = 360 / self.S * Nm + det_u * Pm  # 升交点角距
            tles.append([
                '1 48580U 21041AD  23059.20970124  .00000000  00000+0  00000+0 0  000',
                f'2 48580 {_tle_format(self.i)} {_tle_format(omega_m)} 0000000 000.0000 {_tle_format(u_m)} {_tle_format(self.circles,11,8)} 99386'
            ])
        return tles
    
    def get_links(self, time):
        """
        获取walker星座中所有卫星某一时刻的所有需要连接的链路
        完全使用+grid方式：同轨直连，临轨和差一个相位的卫星相连
        
        Args:
            time: 获得卫星关系的时刻

        Returns: 
            links: list，星间连接关系
                         如[[1, 2, 2000], [2, 3, 3000], [3, 1, 4000]]
                         说明三者依次连接，前两个数字代表卫星ID，
                         最后的数字代表星间距离，单位km
            no_link: list，无星间连接
                           如[[2, 4], [3, 6]]
                           说明卫星ID之间不存在链路
        """
        wgs84_pos = self.get_wgs84_pos(time)
        def _sat_dist(id1, id2):
            """
            计算卫星间的角度
            Args: 卫星轨道半径，两个卫星的id序号
            Returns: 卫星s1、s2之间的角度，不可见则返回inf
            """
            # 两卫星的wgs84坐标
            pos1 = wgs84_pos[id1]
            pos2 = wgs84_pos[id2]
            # 计算向量夹角，保证acos不出错
            cosL = (pos1[0]*pos2[0]+pos1[1]*pos2[1]+pos1[2]*pos2[2])\
                    /np.linalg.norm(pos1)/np.linalg.norm(pos2)
            if cosL <= -1:
                L = pi
            elif cosL >= 1:
                L = 0
            else:
                L = acos(cosL)
            # 若被地球挡住，则不可见
            if self.h * cos(L/2) <= PROJ_CONFIG.earth_r:
                return np.inf
            # 正常返回两星距离
            return 2*self.h*sin(L/2)
        
        # 0、返回值
        links = []
        no_link = []

        # 1、同轨顺次连接
        for i in range(self.P):
            for j in range(self.S):
                a = i*self.S+j              # 同轨两颗卫星 a
                b = i*self.S+(j+1)%self.S   # 同轨两颗卫星 b
                if a == b:                  # 若两星相同，则不连接
                    continue
                dist = _sat_dist(a, b)
                if dist != np.inf:
                    links.append([a, b, dist])
                else:
                    no_link.append([a, b])

        # # 2、邻轨相互连接：确定每对邻轨间的卫星id差，称为det
        # # 临轨互联规则（1）：“太远” 最少，即不可见的卫星对数最小
        # # 临轨互联规则（2）：满足（1）时，让“距离之和”最小
        # for i in range(self.P-1):  # 遍历所有轨道（跳过最后一个轨道），i为轨道id
        #     min_inf_cnt = np.inf   # 最小的“太远”数
        #     min_dist_sum = np.inf  # 最小的“距离之和”

        #     # 遍历所有临轨间，可能的det
        #     for det in range(self.S, 2 * self.S):
        #         inf_cnt = 0               # 本次的“太远”数
        #         dist_sum = 0              # “距离之和”
        #         dist_list = []            # 距离列表
        #         # 信息统计
        #         for j in range(self.S):            # 遍历轨道id为i的所有卫星
        #             id1 = i * self.S + j           # 本轨道卫星
        #             id2 = (id1 + det) % self.N     # 下一轨道（临轨）卫星
        #             dist = _sat_dist(id1, id2)     # 两个卫星之间的距离
        #             dist_list.append(dist)         # 加入本次距离列表
        #             if dist == np.inf:             # 当存在一对卫星不可见
        #                 inf_cnt += 1
        #             else:
        #                 dist_sum += dist
        #         # 按照规则判断是否是当前的 best_det
        #         if inf_cnt <= min_inf_cnt and dist_sum < min_dist_sum:
        #             min_inf_cnt = inf_cnt
        #             min_dist_sum = dist_sum
        #             min_dist_list = dist_list      # 用于记录距离的最佳距离列表
        #             best_det = det                 # 最佳det
            
        #     # 已知 best_det 后，记录 best_det 入返回值
        #     dets.append(best_det)
        #     for k in range(self.S):  # 连接该轨每个卫星
        #         # 源卫星id
        #         source_id = k + i*self.S
        #         # 目的卫星id
        #         target_id = source_id + best_det
        #         if target_id >= (i+2)*self.S:
        #             target_id -= self.S
        #         target_id = target_id % self.N
        #         # 若星间距离不是无穷，则记录链路连接
        #         if min_dist_list[k] != np.inf:
        #             links2.append([source_id, target_id, min_dist_list[k]])

        # 2、邻轨相互连接：卫星和临轨差一个相位且可见的卫星相连
        for sat_id in range(self.N):
            if int(sat_id / self.S) == self.P-1:
                break
            target_id = sat_id + self.S
            dist = _sat_dist(sat_id, target_id)
            if dist != np.inf and dist > self.dist_min:
                links.append([sat_id, target_id, dist])
            else:
                no_link.append([sat_id, target_id])
        
        # 3、返回有连接链路和无连接链路
        return links, no_link

    def get_spot_down(self, time):
        """
        获取walker星座中所有卫星某一时刻的星下点，包括：经度+纬度+高度

        Args:
            time: 获得卫星关系的时刻

        Returns: 
            list，包含所有卫星的这三个量，这三个量用list呈现
        """
        spot_down = []
        for tle in self._get_tles():
            # satellite = twoline2rv(tle[0], tle[1], wgs84)  # sgp4
            satellite = EarthSatellite(tle[0], tle[1])  # skyfield
            r = self._satellite_run(*time, satellite)
            spot_down.append(r[:-1])
        return spot_down

    def get_wgs84_pos(self, time):
        """
        获取walker星座中所有卫星某一时刻的wgs84位置，包括xyz三维

        Args:
            time: 获得卫星关系的时刻

        Returns: 
            list，包含所有卫星的这三个量，这三个量用list呈现
        """
        wgs84_pos = []
        for tle in self._get_tles():
            # satellite = twoline2rv(tle[0], tle[1], wgs84)  # sgp4
            satellite = EarthSatellite(tle[0], tle[1])  # skyfield
            r = self._satellite_run(*time, satellite)
            wgs84_pos.append(r[-1])
        return wgs84_pos

    def get_onesat_wgs84_pos(self, time, sat_id):
        """
        获取walker星座中单个卫星某一时刻的wgs84位置，包括xyz三维

        Args:
            time: 获得卫星关系的时刻
            sat_id: 卫星编号

        Returns: 
            list，包含卫星的xyz三个量
        """
        tle = self._get_tles()[sat_id]
        # satellite = twoline2rv(tle[0], tle[1], wgs84)  # sgp4
        satellite = EarthSatellite(tle[0], tle[1])  # skyfield
        return self._satellite_run(*time, satellite)[-1]
        
    def get_visible_sats(self, time, gnd_dev_para, method=1):
        """
        获取walker星座中某一时刻，可连接到地面设备的卫星们

        Args:
            time: 获得卫星关系的时刻
            gnd_dev_para: 包含地面站经纬度和可视角度的list
            method: 1或2，若为2，则进行剩余可见时间计算

        Returns: 
            dict，key是卫星id列出所有可见卫星，value是星地距离
        """
        lon = gnd_dev_para[0] / 180 * pi  # 地面站经度，单位rad
        lat = gnd_dev_para[1] / 180 * pi  # 地面站纬度，单位rad
        min_zeta = get_limit_elevation_ang_or_dist(  # 地面站最小仰角，单位度
            PROJ_CONFIG.earth_r, self.h, gnd_dev_para[2], self.sat_ang)
        ###kc-print
        #print(f"地面站最小仰角，单位度:{min_zeta}")
        ret = {}
        tles = self._get_tles()
        for i in range(len(tles)):  # 遍历所有卫星id
            # satellite = twoline2rv(tles[i][0], tles[i][1], wgs84)  # sgp4
            satellite = EarthSatellite(tles[i][0], tles[i][1])  # skyfield
            r = self._satellite_run(*time, satellite)
            # 计算高度角
            L = acos(sin(lat) * sin(r[1]/180*pi) \
                     + cos(lat) * cos(r[1]/180*pi) * cos(lon - r[0]/180*pi))
            alt_zeta = atan((cos(L) - PROJ_CONFIG.earth_r / r[2]) / sin(L)) * 180 / pi
            ###kc-print
            # print(f'角度L:{L* 180 / pi},r[2]为{r[2]},sin(L)为{sin(L)}')
            # print(f"卫星{i}高度角{alt_zeta}")
            # 若高度角大于等于最小仰角，说明卫星可见
            if alt_zeta >= min_zeta:
                # 计算方位角
                # direct_alpha = asin(sin(lon - r[0]/180*pi) * sin(pi/2 - lat) / sin(L))
                
                # 计算星地距离
                dist = sqrt(PROJ_CONFIG.earth_r ** 2 + self.h ** 2 \
                            - 2 * PROJ_CONFIG.earth_r * self.h * cos(L))
                
                # 计算剩余可见时间
                if method == 2:
                    ######################### 解析法 #########################
                    # def _vector_ang(v1, v2, d1=self.h, d2=1):
                    #     """
                    #     求两向量夹角，返回角度单位为度

                    #     Args:
                    #         v1 / v2: 两个向量
                    #         d1 / d2: 两个向量的模长，用于本应用的快速计算

                    #     Returns: 
                    #         向量夹角，单位为度
                    #     """
                    #     # 对应分量相乘再相加，得内积
                    #     inner_product = 0
                    #     for i in range(len(v1)):
                    #         inner_product += v1[i] * v2[i]
                    #     # 内积除以两个模长，得夹角余弦值
                    #     return acos(inner_product / d1 / d2) * 180 / pi

                    # # 1、确定卫星所在轨道的升交点赤经和轨道倾角，单位rad
                    # omega = 2 * pi / self.P * int(i / self.S)
                    # rad_i = self.i / 180 * pi

                    # # 2、解方程，获得卫星升起和落下时，卫星的WGS-84位置(x, y, z)
                    # x, y, z = symbols('x y z', real=True)
                    # # 三个方程（f1~f3均等于0）含义分别是：
                    # # （1）卫星在地平线上
                    # # （2）卫星在轨道平面上
                    # # （3）卫星轨道半径是self.h
                    # f1 = x * cos(lon) * cos(lat) + y * cos(lon) * sin(lat) \
                    #     + z * sin(lon) - PROJ_CONFIG.earth_r
                    # f2 = x * sin(rad_i) * sin(omega) - y * sin(rad_i) \
                    #     * cos(omega) + z * cos(rad_i)
                    # f3 = x ** 2 + y ** 2 + z ** 2 - self.h ** 2
                    # # 因为已有可见卫星，故方程组有两个实数解
                    # solution = solve([f1, f2, f3])
                    # # 提取两组解的x、y、z，组成数组ans1和ans2
                    # ans1, ans2 = [[ans[x], ans[y], ans[z]] for ans in solution]                    
                    
                    # # 3、确定两个解中，哪个是卫星落下的点
                    # # 平行于轨道平面的两个特殊方向K和M，K沿卫星绕行方向90度到M
                    # M = (-cos(omega), -sin(omega), 0)
                    # K = (-sin(rad_i) * sin(omega), sin(rad_i) * cos(omega), cos(rad_i))
                    # # 计算ans1和ans2相对K绕行的角度位置pos1和pos2
                    # K1 = _vector_ang(ans1, K)
                    # pos1 = K1 if _vector_ang(ans1, M) <= 90 else 360 - K1 
                    # K2 = _vector_ang(ans2, K)
                    # pos2 = K2 if _vector_ang(ans2, M) <= 90 else 360 - K2
                    # # 判断ans1和ans2的先后关系，得到ans为卫星落下的点
                    # if 0 < abs(pos1 - pos2) < 180:
                    #     ans = ans1 if pos1 == max(pos1, pos2) else ans2
                    # else:
                    #     ans = ans1 if pos1 == min(pos1, pos2) else ans2

                    # # 4、计算当前卫星位置和落下的点之间的夹角，该值和剩余时间成正比
                    # angle_left = _vector_ang(ans, r[3], d2=self.h)

                    # # 5、将距离和剩余可见时间加入字典
                    # # 通过万有引力和匀速圆周运动公式，周期和轨道半径的1.5次幂成正比
                    # ret[i] = [dist, angle_left * pow(sat_height, 1.5)]

                    ######################### 试探法 #########################
                    for k in range(1, PROJ_CONFIG.max_try_visible_time_left):
                        # 试探时刻
                        time_try = time[:]
                        time_try[-2] += k * PROJ_CONFIG.det_t_visible_time_left
                        # 试探时刻的卫星位置
                        r = self._satellite_run(*time_try, satellite)
                        # 试探时刻的卫星高度角
                        L = acos(sin(lat) * sin(r[1]/180*pi) \
                                + cos(lat) * cos(r[1]/180*pi) * cos(lon - r[0]/180*pi))
                        alt_zeta = atan((cos(L) - PROJ_CONFIG.earth_r / r[2]) / sin(L)) * 180 / pi
                        # 试探时刻的卫星高度角小于最小仰角，说明不可见
                        if alt_zeta < min_zeta:
                            ret[i] = [dist, k * PROJ_CONFIG.det_t_visible_time_left]
                            break
                    # 所有尝试都可见时，赋予卫星最大剩余可见时长
                    if i not in ret.keys():
                        ret[i] = [dist, PROJ_CONFIG.max_try_visible_time_left * PROJ_CONFIG.det_t_visible_time_left]

                else:
                    # 仅将距离加入字典
                    ret[i] = [dist]
        
        return ret
    
    def get_best_visible_sat(self, time, gnd_position, method=1):
        """
        基于一定选择策略，获取walker星座中某一时刻，可连接到地面设备的最佳卫星们

        Args:
            time: 获得卫星关系的时刻
            gnd_dev_para: 包含地面站经纬度和可视角度的list
            method: 寻找最优星地连接的策略，1（默认）为距离最近，2为可见时间最长
        
        Returns:
            list，第一个元素是：卫星id列出的最佳连接的可见卫星; 
                  第二个元素是：星地距离，单位km
            两个None，说明无可见卫星
        """
        # 获得所有可见卫星字典
        all_visible_sats = self.get_visible_sats(time, gnd_position, method)
        
        # 若地面站无可见卫星，则返回两个None
        if all_visible_sats == {}:
            return None, None
                
        # 使用不同策略判断最佳卫星
        # 策略1：距离最近，即高度角最大
        if method == 1:
            min_dist = np.inf
            for sat_id, val in all_visible_sats.items():
                if val[0] < min_dist:
                    min_dist = val[0]
                    best_sat = sat_id
            return [best_sat, min_dist]
        
        # 策略2：可见时间最长
        else:
            max_time = 0
            for sat_id, val in all_visible_sats.items():
                if val[1] > max_time:
                    dist = val[0]
                    max_time = val[1]
                    best_sat = sat_id
            return [best_sat, dist, max_time]

############## 和星座拓扑总部署相关 ##############

def _check_sat_para(walkers, devs,
                    t0, t_speed, sat_identity, method, rs, bw,
                    mode, topo_json):
    """
    检查卫星参数

    Args:
        walkers: list，包含若干不同高度的wallker星座
        devs: 地面站设备及参数
        t0: 初始时刻
        t_speed: 时间加速速度
        sat_identity: 卫星身份
        method: 选星策略
        rs: 星间路由延迟
        bw: 链路带宽
        mode: 星间路由转发模式
        topo_json: 拓扑json，用以检查地面站是否存在
        
    Returns:
        字典，包括code字段和msg字段
    """
    ###################### 检查walker星座参数 ######################
    # 检查1 - walker星座个数不超过3
    if len(walkers) > 3:
        return {'code': 0, 'msg': '不同高度的轨道数不可超过3'}
    existed_orbits = []
    for walker in walkers:
        # 参数提取
        orbit, N, P, F, i, h, sensor_ang = walker["orbit"], walker["N"], \
            walker["P"], walker["F"], walker["i"], walker["h"], walker["sensor_angle"]
        # 检查2 - 轨道名称不重复
        if orbit in existed_orbits:
            return {'code': 0, 'msg': '存在相同轨道高度的walker'}
        else:
            existed_orbits.append(orbit)
        # 检查3 - 轨道名称合法，且每个walker的高度在所属轨道的范围内
        if orbit == 'LEO':
            if not 400 <= h-6372 <= 2000:
                return {'code': 0, 'msg': 'LEO轨道高度超过范围'}
        elif orbit == 'MEO':
            if not 2000 <= h-6372 <= 36000:
                return {'code': 0, 'msg': 'MEO轨道高度超过范围'}
        elif orbit == 'GEO':
            if h != 42164:
                return {'code': 0, 'msg': 'GEO轨道高度超过范围，必须是42164km'}
        else:
            return {'code': 0, 'msg': 'walker的轨道名称不正确'}
        # 检查4 - 每个walker星座的参数正确
        if N % P != 0:
            return {'code': 0, 'msg': '星座参数错误，卫星总数不能被轨道数整除'}
        if not 0 <= i < 360:
            return {'code': 0, 'msg': '星座参数错误，轨道倾角范围在0~180度间'}
        if h < 6372:
            return {'code': 0, 'msg': '星座参数错误，轨道半径大于地球半径'}
        if not 1<= F <= P-1 and P != 1:
            return {'code': 0, 'msg': '星座参数错误，相位因子范围在1~P-1间'}
        # 检查5 - 卫星可视角度
        if not 0 <= sensor_ang <= 180:
            return {'code': 0, 'msg': '卫星可视角度在0°~180°间'}

    ####################### 检查星座公共参数 #######################
    # 检查1 - 卫星节点的身份，仅可是router(路由器)或switch(交换机)
    if sat_identity not in ["router", "switch"]:
        return {'code': 0, 'msg': '卫星节点身份不是router或switch'}
    # 检查2 - 初始时间限制
    if not 0 <= t0 <= PROJ_CONFIG.max_time_start:
        return {'code': 0, 'msg': '星座运行的初始时间超过范围'}
    # 检查3 - 时间加速速度限制
    if not 1 <= t_speed <= PROJ_CONFIG.max_time_speed:
        return {'code': 0, 'msg': f'星座运行的时间加速速度在1~{PROJ_CONFIG.max_time_speed}间'}
    # 检查4 - 选星策略
    if method not in [1, 2]:
        return {'code': 0, 'msg': '选星策略取值为1 (最短距离) 或2 (最长可见时间)'}
    # 检查5 - 星间路由延迟
    if not 1 <= rs <= PROJ_CONFIG.max_rs:
        return {'code': 0, 'msg': '星间路由延迟超过范围'}
    # 检查6 - 链路带宽
    if set(bw.keys()) != {"sat-sat", "sat-gnd up", "sat-gnd down"}:
        return {'code': 0, 'msg': '链路带宽仅存在字段"sat-sat"、"sat-gnd up"和"sat-gnd down"'}
    if not all([val >= 1 for val in bw.values()]):
        return {'code': 0, 'msg': '链路带宽值为正整数'}

    ######################## 检查地面站参数 ########################
    # 检查1 - 设备是存在的主机或路由器
    hosts = topo_json['networks']['hosts'].keys()
    routers = topo_json['networks']['routers'].keys()
    for dev in devs.keys():
        if dev not in hosts and dev not in routers:
            return {'code': 0, 'msg': '请求与星座连接的设备不存在'}
    # 检查2 - 地面站的经纬度、天线等级
    for val in devs.values():
        if not -180 <= val['position'][0] <= 180:
            return {'code': 0, 'msg': '用户站经度范围在-180°~180°间'}
        if not -90 <= val['position'][1] <= 90:
            return {'code': 0, 'msg': '用户站纬度范围在-90°~90°间'}
        if not 1 <= val['antenna_level'] <= len(PROJ_CONFIG.gnd_dev_level):
            return {'code': 0,
                    'msg': f'用户站天线等级在1~{len(PROJ_CONFIG.gnd_dev_level)}间'}
    # 检查3 - 各地面站是否在同一子网
    all_ip_data = [[val['ip'], val['netmask']] for val in devs.values()]
    # router且不是tunnel模式，不在同一子网
    if sat_identity == "router":
        if mode != 'IP-TUNNEL' and _subnet_count(all_ip_data) != len(all_ip_data):
            return {'code': 0, 'msg': '卫星是路由器时，地面站应配置于不同子网'}
    # switch，在同一子网
    else:
        if _subnet_count(all_ip_data) != 1:
            return {'code': 0, 'msg': '卫星是交换机时，地面站应配置于同一子网'}
    # 检查4 - 若地面站主机有配置网关，则网关和网卡ip在同一子网
    for dev, val in devs.items():
        if dev[0] == 'h' and _subnet_count([[val['ip'], val['netmask']],
                                            [val['gateway'], val['netmask']]]) != 1:
            return {'code': 0, 'msg': '地面是主机时，网关和网卡ip应配置于同一子网'}
    
    ######################### 检查配置参数 #########################
    # 检查1 - 星间转发模式
    if mode not in ['SDN', 'STP', 'NO-STP', 'DHCP', 'IP-NO-MODIFY', 'IP-MODIFY', 'IP-TUNNEL']:
        return {'code': 0, 'msg': f'星间转发模式取值为 SDN/STP/NO-STP/DHCP/IP-MODIFY/IP-NO-MODIFY/IP-TUNNEL'}
    if mode in ['SDN', 'STP', 'NO-STP'] and sat_identity == 'router':
        return {'code': 0, 'msg': f'{mode}模式仅当卫星是交换机时可开启'}
    if mode in ['DHCP', 'IP-MODIFY', 'IP-NO-MODIFY', 'IP-TUNNEL'] and sat_identity == 'switch':
        return {'code': 0, 'msg': f'{mode}模式仅当卫星是路由器时可开启'}
    # 检查2 - DHCP和IP-TUNNRL模式下，所有地面站都是主机
    if mode in ['DHCP', 'IP-TUNNEL'] and \
       not all([dev[0] == 'h' for dev in devs.keys()]):
        return {'code': 0, 'msg': 'DHCP和IP-TUNNRL模式下，所有地面站都是主机'}

    ####################### 检查没问题，返回 #######################
    return {'code': 1, 'msg': '卫星参数正确'}


def sat_topo_config(user_topo_info, user_db_cli, topo):
    """
    部署拓扑时，若发现是含有星座的拓扑，将调用该函数进行卫星拓扑配置

    Args:
        user_topo_info: 原始拓扑描述json
        user_db_cli: 用户数据库DB
        topo: 拓扑名称
        ovs_below_enable: 星下ovs使能
        ne_up_down_enable: 使用网卡启停模拟链路通断
        
    Returns:
        字典，包括code字段、msg字段、json字段
    """

    # （0）提取卫星信息，并从json去掉satellite字段
    sat = user_topo_info['networks']['satellite']
    user_topo_info['networks'].pop('satellite')

    # （1）参数提取
    try:
        # 1）星座参数提取，可包含三个高度（LEO、MEO、GEO）星座各一个
        walkers = sat['walkers']
        # 2）公用参数提取
        ts0, t_speed, sat_identity, method, rs, bw = \
            sat['time_start'], sat['time_speed'], sat['sat_identity'], \
            sat['select_sat_method'], sat['rs'], sat['bw']
        # 3）配置参数提取
        # 网卡启停使能（默认false）
        ne_up_down_enable = sat['nic_up_down_enable'] \
            if 'nic_up_down_enable' in sat else False
        # 星间转发模式（必须指定），包括：
        # switch - SDN / STP / NO-STP
        # router - DHCP / IP-MODIFY / IP-NO-MODIFY / IP-TUNNEL
        mode = sat['mode']
        # 4）地面站参数提取
        devices = sat['devices']
    except KeyError as e:
        return {'code': 0, 'msg': f'星座参数缺失，{e.args[0]}'}
    
    # （2）参数检查
    ret = _check_sat_para(walkers,                                     # 星座
                          devices,                                     # 地面站
                          ts0, t_speed, sat_identity, method, rs, bw,  # 公共参数
                          mode,                                        # 配置
                          user_topo_info)
    if ret['code'] == 0:
        return ret

    # （3）准备工作
    # 1）初始时间计算
    t0 = timestamp2date(ts0)
    # 2）从低轨到高轨对星座排序
    sorted_walkers = []
    for orbit in ['LEO', 'MEO', 'GEO']:
        for walker in walkers:
            if walker['orbit'] == orbit:
                sorted_walkers.append(walker)
                break
    walkers = sorted_walkers
    # 3）需记录的重要变量
    link_cnt_dict = {}  # 字典，星间链路编号（数字）-> 包含两端卫星节点名的列表
    ip_dict = {}  # 字典，链路编号（数字）-> 整数对应的ip网络号，仅卫星为路由器有效
    sat_ovs_gnd = {}            # 【星地链路】字典，卫星 -> 用于连接地面站的星下ovs（或地面站本身，若无星下ovs）
    sat_ovs_walker = {}         # 【星座链路】字典，卫星 -> 用于连接星座的星下ovs
    all_sat_gnd_links = {}      # 【星地链路】星地链路字典，地面站 -> 卫星
    all_sat_highsat_links = {}  # 【星座链路】星座链路字典，较低轨卫星 -> 较高轨卫星
    sat_gnd_nets = {}   # 字典，表示每个卫星分配的对地小网段
    # 4）卫星身份复数
    pl_sat_identity = "routers" if sat_identity == "router" else "switches"
    # 5）链路网段ip分配
    if sat_identity == "router":
        ip_splited = PROJ_CONFIG.sat_link_ip.split('/')
        ip_next = ip2int(ip_splited[0])
        ip_last = ip_next + 2 ** (32 - int(ip_splited[1])) - 1
    # 6）模式参数化转换
    # STP功能
    stp_enable = mode == 'STP'
    # SDN功能
    sdn_enable = mode == 'SDN'
    # 星下ovs规则：DHCP或IP-TUNNEL模式下
    ovs_below_enable = mode == 'IP-TUNNEL' or mode == 'DHCP'
    # 7）前端卫星节点展示
    # 各星座中心
    walker_center = [(PROJ_CONFIG.frontend_sat_a, (2*i+1)*PROJ_CONFIG.frontend_sat_b)
                     for i in range(len(walkers))][::-1]
    # 地面网络所有网元，向下平移
    modify_2d_front_node_y(user_topo_info, 
                           len(walkers) * 2 * PROJ_CONFIG.frontend_sat_b)
    
    # （4）星下ovs建立
    if ovs_below_enable:
        # ovs初始编号
        s_id = len(user_topo_info['networks']["switches"].keys()) + 1
        # 已有卫星编号偏移
        existed_sat_id = 0
        # 对每层walker星座
        for cnt, walker in enumerate(walkers):
            N = walker["N"]  # 本层星座卫星数
            P = walker["P"]  # 本层星座轨道数
            S = N / P        # 本层星座每轨道卫星数
            for i in range(N):
                # 卫星全局编号
                sat_id = existed_sat_id + i
                # 进行卫星节点到ovs节点的映射，方便在ovs网桥上完成链路切换
                sat_ovs_gnd[sat_id] = ovs_gnd = f's{s_id}'
                s_id += 1  # ovs编号自增
                # 计算卫星在2d前端的位置
                radius = int((int(i / S) + 1)*PROJ_CONFIG.frontend_sat_a/P)
                ang = i % S * 2 * pi / S
                postion = [radius*cos(ang) + walker_center[cnt][0] + 2*PROJ_CONFIG.frontend_sat_offset,
                           radius*sin(ang)*PROJ_CONFIG.frontend_sat_b/PROJ_CONFIG.frontend_sat_a + walker_center[cnt][1] + 2*PROJ_CONFIG.frontend_sat_offset]
                # json里建立连接地面站星下ovs
                user_topo_info['networks']["switches"][ovs_gnd] = \
                    get_node_json(ovs_gnd, sdn=sdn_enable, stp=stp_enable,
                                  position=postion)
                # 若未开启隧道且不是最下层星座，则建立星座星下ovs【默认不建立】
                # if cnt != 0:
                #     sat_ovs_walker[sat_id] = ovs_walker = f's{s_id}'
                #     s_id += 1
                #     user_topo_info['networks']["switches"][ovs_walker] = \
                #         get_node_json(ovs_walker, sdn=sdn_enable, stp=stp_enable, 
                #                       position=postion)
            # 从低轨到高轨进行卫星编号
            existed_sat_id += N

    # （5）卫星节点建立
    # 卫星初始编号
    sat_id1 = len(user_topo_info['networks'][pl_sat_identity].keys()) + 1
    # 链路初始编号
    l_id = len(user_topo_info['networks']['links'].keys()) + 1
    # 已有卫星编号偏移
    existed_sat_id = 0
    # 对每层walker星座
    for cnt, walker in enumerate(walkers):
        # 参数提取，创建单轨道星座对象
        N, P, i, F, h, ang = get_walker_para(walker)
        walker = Walker(N, P, i, h, F, ang)
        # 获得初始星间链路，记录星间连接关系
        all_sats_links, no_sats_links = walker.get_links(t0)
        # 对每个卫星
        for i in range(N):
            # 卫星全局编号
            sat_id = existed_sat_id + i
            # 卫星节点名称
            sat_name = f'{sat_identity[0]}{sat_id1 + sat_id}'
            # 存在星下ovs时，考虑链路建立
            if ovs_below_enable:
                # 地面站星下ovs，对所有卫星均建立
                ovs_name = sat_ovs_gnd[sat_id]  # 星下ovs名称
                link_to_ovs = f'l{l_id}'        # 链路名称
                l_id += 1  # 链路编号编号自增
                # 建立连接到地面站星下ovs的链路
                user_topo_info['networks']['links'][link_to_ovs] = \
                    get_link_json(link_to_ovs, sat_name, ovs_name, rs, bw)
                # 若未开启隧道且不是最下层星座，则将星座星下ovs加入卫星连接列表
                if cnt != 0:
                    ovs_walker_name = sat_ovs_walker[sat_id]  # 星座星下ovs名称
                    link_to_ovs_walker = f'l{l_id}'           # 链路名称
                    l_id += 1  # 链路编号编号自增
                    # 建立连接到星座星下ovs的链路
                    user_topo_info['networks']['links'][link_to_ovs_walker] = \
                        get_link_json(link_to_ovs_walker, sat_name, 
                                      ovs_walker_name, rs, bw)
                else:
                    sat_ovs_walker[sat_id] = sat_name
            # 不存在星下ovs时，考虑星下ovs映射字典
            else:
                sat_ovs_gnd[sat_id] = sat_ovs_walker[sat_id] = sat_name
            # 卫星在2d前端的位置
            S = N / P
            radius = int((int(i / S) + 1)*PROJ_CONFIG.frontend_sat_a/P)
            ang = i % S * 2 * pi / S
            postion = [radius*cos(ang) + walker_center[cnt][0] + PROJ_CONFIG.frontend_sat_offset,
                       radius*sin(ang)*PROJ_CONFIG.frontend_sat_b/PROJ_CONFIG.frontend_sat_a + walker_center[cnt][1] + PROJ_CONFIG.frontend_sat_offset]
            # 建立卫星设备节点的json
            user_topo_info['networks'][pl_sat_identity][sat_name] = \
                get_node_json(sat_name, sdn=sdn_enable, stp=stp_enable,
                              position=postion)
            # 若卫星为路由器，做对地小网段分配
            # 因为存在模式转换，故只要是路由器均需划分小子网
            if sat_identity == "router":
                # 记录网段里的下一个可用地面站IP
                sat_gnd_nets[sat_id] = ip_next + 2
                # 配置卫星路由器连接到星下ovs的网卡ip
                user_topo_info['networks']['routers'][sat_name]['interfaces'].append({
                    'name': f'{sat_name}{sat_ovs_gnd[sat_id]}',
                    'ip': int2ip(ip_next + 1),
                    'netmask': PROJ_CONFIG.sat_gnd_subnet_mask
                })
                # 链路ip自增
                ip_next += 2 ** (32 - netmask2cidr(PROJ_CONFIG.sat_gnd_subnet_mask))
                if ip_next >= ip_last:
                    return {'code': 0, 'msg': f'星座可分配IP不足'}
        # 从低轨到高轨进行卫星编号
        existed_sat_id += N

    # （6）预备主机建立、ryu控制器建立
    # 预备主机编号
    spare_conn = f'h{len(user_topo_info["networks"]["hosts"].keys()) + 1}'
    # 计算预备主机在2d前端的位置，在最低轨的中心
    postion = [walker_center[0][0] + PROJ_CONFIG.frontend_sat_offset,
               walker_center[0][1] + PROJ_CONFIG.frontend_sat_offset]
    # 在拓扑json里建立一个主机
    user_topo_info['networks']["hosts"][spare_conn] = \
        get_node_json(spare_conn, position=postion)
    # 添加sdn控制器
    postion = [walker_center[0][0] + PROJ_CONFIG.frontend_sat_offset + 2*PROJ_CONFIG.frontend_sat_offset,
               walker_center[0][1] + PROJ_CONFIG.frontend_sat_offset + 2*PROJ_CONFIG.frontend_sat_offset]
    if sdn_enable:
        user_topo_info['networks']["controllers"][PROJ_CONFIG.default_ryu_name] = \
            get_node_json(PROJ_CONFIG.default_ryu_name, position=postion)

    # （7）星间链路
    # 已有卫星编号偏移
    existed_sat_id = 0
    # 对每层walker星座
    for cnt, walker in enumerate(walkers):
        # 参数提取，创建单轨道星座对象
        N, P, i, F, h, ang = get_walker_para(walker)
        walker = Walker(N, P, i, h, F, ang)
        # 获得初始星间链路，记录星间连接关系和无星间连接
        all_sats_links, no_sats_links = walker.get_links(t0)
        # 已存在链路和不存在链路均分配link_id
        for link in all_sats_links+no_sats_links:
            # 链路名称
            link_name = f'l{l_id}'
            # 链路两端卫星1
            sat1 = f'{sat_identity[0]}{existed_sat_id + sat_id1 + link[0]}'
            # 链路两端卫星2
            sat2 = f'{sat_identity[0]}{existed_sat_id + sat_id1 + link[1]}'
            # 对连接的星间链路
            if link in all_sats_links:
                # 写入json，并考虑链路质量
                user_topo_info['networks']['links'][link_name] = get_link_json(
                    link_name, sat1, sat2, rs, bw, dist=link[2])
                # 当卫星作为路由器时，配置网卡ip
                if sat_identity == "router":
                    # 卫星id大者，赋予更大的ip地址
                    if int(sat1[1:]) > int(sat2[1:]):
                        ip1 = int2ip(ip_next + 2)
                        ip2 = int2ip(ip_next + 1)
                    else:
                        ip1 = int2ip(ip_next + 1)
                        ip2 = int2ip(ip_next + 2)
                    # 配置网卡ip
                    user_topo_info['networks']['routers'][sat1]['interfaces'].append({
                        'name': f'{sat1}{sat2}',
                        'ip': ip1,
                        'netmask': PROJ_CONFIG.link_subnet_mask
                    })
                    user_topo_info['networks']['routers'][sat2]['interfaces'].append({
                        'name': f'{sat2}{sat1}',
                        'ip': ip2,
                        'netmask': PROJ_CONFIG.link_subnet_mask
                    })
            if sat_identity == "router":
                # 写入ip字典
                ip_dict[l_id] = ip_next
                # 链路ip自增
                ip_next += 4
                if ip_next >= ip_last:
                    return {'code': 0, 'msg': f'星座可分配IP不足'}
            # 写入链路字典
            link_cnt_dict[l_id] = [sat1, sat2]
            # 链路编号自增
            l_id += 1
        # 从低轨到高轨进行卫星编号
        existed_sat_id += N

    # （8）星地链路：每个地面站 -> 卫星
    # 第一个星地链路，用于持久化
    link_gnd_id1 = l_id
    # 对每个地面站
    for dev, para in devices.items():
        # 0）地面站设备类型
        dev_type = 'routers' if dev[0]=='r' else 'hosts'
        
        # 1）获得地面站连接的全局最优卫星
        # [全局最优卫星, 星地距离(越小越好), 最长可见时间(越大越好)]
        global_best_sat = [None, np.inf, 0]  
        # 遍历各高度轨道
        existed_sat_id = 0
        for walker in walkers:
            # 参数提取，单轨道星座对象
            N, P, i, F, h, ang = get_walker_para(walker)
            walker = Walker(N, P, i, h, F, ang)
            # 地面站连至本高度轨道的最佳卫星（局部最优）
            local_best_sat = walker.get_best_visible_sat(
                t0, [para['position'][0], para['position'][1],
                     PROJ_CONFIG.gnd_dev_level[para['antenna_level']-1][2]],
                method)
            # 更新全局最优，优先低轨
            # 若局部无最优卫星，则不更新
            if local_best_sat[0] == None:
                continue
            # 若局部有最优卫星，则按策略对比并更新
            if method == 1 and global_best_sat[1] > local_best_sat[1] or \
               method == 2 and global_best_sat[2] < local_best_sat[2]:
                local_best_sat[0] += existed_sat_id
                global_best_sat = local_best_sat
            # 从低轨到高轨进行卫星编号
            existed_sat_id += N
        # 全局最优存入字典
        all_sat_gnd_links[dev] = global_best_sat
        
        # 2）新增链路
        link_name = f'l{l_id}'
        l_id += 1  # 链路编号自增
        # 有相连卫星
        if global_best_sat[0] != None:
            # 连接到星下ovs
            connect_to = sat_ovs_gnd[global_best_sat[0]]
            # 生成链路json
            user_topo_info['networks']['links'][link_name] = get_link_json(
                link_name, connect_to, dev, rs, bw,
                target_para=PROJ_CONFIG.gnd_dev_level[devices[dev]['antenna_level']-1][:-1],
                dist=global_best_sat[1], place='sat-gnd')
        # 无可连卫星
        else:
            # 连接到预备主机
            connect_to = spare_conn
            # 生成链路json
            user_topo_info['networks']['links'][link_name] = get_link_json(
                link_name, connect_to, dev, rs=0, bw=bw)
        
        # 3）地面站网卡配置
        # 若为IP-TUNNEL模式，统一分配ip（暂不用DHCP）
        if mode == 'IP-TUNNEL':
            # 有卫星连接，配置ip
            if global_best_sat[0] != None:
                # 小子网掩码
                mask = ip2int(PROJ_CONFIG.sat_gnd_subnet_mask)
                # 连接到的卫星设备名
                connect_to_sat = global_best_sat[0]
                # 地面站网关配置
                user_topo_info['networks'][dev_type][dev]['gateway'] = \
                    int2ip((sat_gnd_nets[connect_to_sat] & mask) + 1)
                # 地面站网卡配置
                user_topo_info['networks'][dev_type][dev]['interfaces'].append({
                    'name': f'{dev}{connect_to}',
                    'ip': int2ip(sat_gnd_nets[connect_to_sat]),
                    'netmask': int2ip(mask)
                })
                # 子网内下一可用ip自增
                sat_gnd_nets[connect_to_sat] += 1
            # 无卫星连接，不配置ip
            else:
                # 地面站网卡配置
                user_topo_info['networks'][dev_type][dev]['interfaces'].append({
                    'name': f'{dev}{connect_to}',
                    'ip': '', 'netmask': ''
                })

        # 若为DHCP模式模式，不配置地面站的ip、掩码、网关
        elif mode == 'DHCP':
            # 地面站网卡配置
            user_topo_info['networks'][dev_type][dev]['interfaces'].append({
                'name': f'{dev}{connect_to}',
                'ip': '', 'netmask': ''
            })
        
        # 若为其他模式
        else:
            # 地面站网卡配置
            user_topo_info['networks'][dev_type][dev]['interfaces'].append({
                'name': f'{dev}{connect_to}',
                'ip': devices[dev]['ip'],
                'netmask': devices[dev]['netmask']
            })
            # 若卫星为路由器且有卫星连接，配置卫星网卡IP和地面站网关
            if sat_identity == "router" and global_best_sat[0] != None:
                # 地面站为主机
                if dev_type == "hosts":
                    sat_ip = user_topo_info['networks'][dev_type][dev]['gateway'] = \
                        devices[dev]['gateway']
                # 地面站为路由器
                else:
                    # 链路地面站侧已占用的ip
                    occupied_ip = ip2int(devices[dev]['ip'])
                    # 子网号
                    net = occupied_ip & ip2int(devices[dev]['netmask'])
                    # 第一个子网内可用ip，作为链路卫星侧的ip
                    for ip in range(net+1, net+2**(32-netmask2cidr(devices[dev]['netmask']))-1):
                        if ip != occupied_ip:
                            sat_ip = int2ip(ip)
                            break
                # 配置连接至卫星路由器网卡的ip
                user_topo_info['networks']['routers'][connect_to]['interfaces'].append({
                    'name': f'{connect_to}{dev}',
                    'ip': sat_ip,
                    'netmask': devices[dev]['netmask']
                })

    # （9）星座链路：对每个下层轨道，连接到上层卫星的对应星下ovs
    # 对每个较低层星座
    existed_sat_id = 0
    for i in range(len(walkers)-1):
        # 1）参数提取，建立两个星座对象
        N1, P1, i1, F1, h1, ang1 = get_walker_para(walkers[i])
        N2, P2, i2, F2, h2, ang2 = get_walker_para(walkers[i+1])
        walker1, walker2 = Walker(N1, P1, i1, h1, F1, ang1), \
            Walker(N2, P2, i2, h2, F2, ang2)
        # 2）建立星座链路的最大距离
        max_dist = get_limit_elevation_ang_or_dist(h1, h2, ang1, ang2,
                                                   output="dist")
        # 3）对每个下层卫星，看连接到哪个上层卫星
        for j in range(N1):
            # 下层卫星id
            sat_id = existed_sat_id + j
            # 下层卫星设备名
            sat = f'{sat_identity[0]}{sat_id1 + sat_id}'
            # 链路名称
            link_name = f'l{l_id}'
            l_id += 1  # 链路编号自增
            # 上层最佳卫星
            all_sat_highsat_links[sat_id] = connect_to_sat = \
                get_best_visible_sat(t0, j, walker1, walker2, 
                                     max_dist, existed_sat_id)
            # 有相连卫星
            if connect_to_sat[0]:
                # 连接到星下ovs
                connect_to = sat_ovs_walker[connect_to_sat[0]]
                # 生成链路json
                user_topo_info['networks']['links'][link_name] = get_link_json(
                    link_name, connect_to, sat, rs, bw, dist=connect_to_sat[1])
            # 无可连卫星
            else:
                # 连接到预备主机
                connect_to = spare_conn
                # 生成链路json
                user_topo_info['networks']['links'][link_name] = get_link_json(
                    link_name, connect_to, sat, 0, bw)
            # 当卫星作为路由器时，配置网卡IP
            if sat_identity == "router":
                # （较小IP）下层卫星侧
                user_topo_info['networks']['routers'][sat]['interfaces'].append({
                    'name': f'{sat}{connect_to}',
                    'ip': int2ip(ip_next + 1),
                    'netmask': PROJ_CONFIG.link_subnet_mask
                })
                # （较大IP）上层卫星侧，仅对有卫星连接进行配置
                if connect_to_sat[0] != None:
                    user_topo_info['networks']['routers'][connect_to]['interfaces'].append({
                        'name': f'{connect_to}{sat}',
                        'ip': int2ip(ip_next + 2),
                        'netmask': PROJ_CONFIG.link_subnet_mask
                    })
                # 下一链路ip自增
                ip_next += 4
                if ip_next >= ip_last:
                    return {'code': 0, 'msg': f'星座可分配IP不足'}   
        # 4）从低轨到高轨进行卫星编号，因此需进行已有编号的偏移
        existed_sat_id += N1

    # （10）路由配置
    if sat_identity == "router":
        # 对于每颗卫星
        for i in range(sum([walker['N'] for walker in walkers])):
            # 卫星设备名
            sat = f'r{sat_id1 + i}'
            # 路由协议使用的路由器标识符
            router_id = int2ip(i)
            # 对该卫星进程路由协议配置
            if ospf:
                user_topo_info['networks']['routers'][sat]['config']['ospf'] = {
                    "enable": True,
                    "areas": {},
                    "networks":[[f"{int2ip(ip2int(intf['ip']) & ip2int(intf['netmask']))}/{netmask2cidr(intf['netmask'])}",
                                 "0.0.0.0"] for intf in user_topo_info['networks']['routers'][sat]['interfaces']],
                    "router_id": router_id
                }
            if rip:
                user_topo_info['networks']['routers'][sat]['config']['rip'] = {
                    'enable': True,
                    'neighbors': [],
                    'networks':[f"{int2ip(ip2int(intf['ip']) & ip2int(intf['netmask']))}/{netmask2cidr(intf['netmask'])}"
                                for intf in user_topo_info['networks']['routers'][sat]['interfaces']],
                    'version': 2
                }
            if bgp:
                user_topo_info['networks']['routers'][sat]['config']['rip'] = {
                    'enable': True,
                    'asn': '',
                    'neighbors': [],
                    'networks': [],
                    'router_id': router_id
                }
       
    # （11）持久化
    sat_table_name = f'{topo}{PROJ_CONFIG.sat_table_name}'
    # 星座参数
    user_db_cli.set_value(sat_table_name, 'walkers', walkers)
    # 定时参数
    refresh_interval = PROJ_CONFIG.refresh_interval_para * t_speed \
        * sum([walker['N']*2-walker['N']/walker['F'] for walker in walkers])
    user_db_cli.set_value(sat_table_name, 'timer',
                          [ts0+refresh_interval, t_speed, time(), refresh_interval])
    # 星地链路
    user_db_cli.set_value(sat_table_name, 'sat-gnd links', all_sat_gnd_links)
    # 星座链路
    user_db_cli.set_value(sat_table_name, 'sat-highsat links', all_sat_highsat_links)
    # 临时存储
    user_db_cli.set_value(sat_table_name, 'temp', {})
    # 卫星身份, 星间转发模式
    user_db_cli.set_value(sat_table_name, 'mode', 
                          [sat_identity, mode])
    # 编号偏移                                                     
    user_db_cli.set_value(sat_table_name, 'virtual-para', 
                          [sat_id1,        # 第一个卫星设备编号
                           spare_conn,     # 预备主机
                           link_gnd_id1])  # 第一个星地链路(星座链路继续往后编号)
    # 星地链路连接, 星座链路连接
    user_db_cli.set_value(sat_table_name, 'sat-ovs', 
                            [sat_ovs_gnd, sat_ovs_walker])
    # 星间链路连接
    user_db_cli.set_value(sat_table_name, 'links2dev', link_cnt_dict)
    # 星间转发延迟, 链路带宽配置
    user_db_cli.set_value(sat_table_name, 'link-config', [rs, bw])
    # 星间链路IP, IP隧道小子网映射(网段里的下一个可用地面站IP)
    user_db_cli.set_value(sat_table_name, 'ip-net', [ip_dict, sat_gnd_nets])
    # 网卡启停
    user_db_cli.set_value(sat_table_name, 'ne-up-down', ne_up_down_enable)
    # 地面站设备信息, 选星策略
    user_db_cli.set_value(sat_table_name, 'gnd-dev', [devices, method])
    # 地面站换星日志
    user_db_cli.set_value(
        sat_table_name, 'sat log', 
        [f"初始时戳{round(ts0)}秒，地面站{dev}连接卫星: {all_sat_gnd_links[dev][0]}"
         for dev in devices.keys()])

    # （12）信息打印
    print("< 配置信息 >")
    print(f" 🛰 卫星节点身份: {sat_identity}")
    print(f" 🛰 网卡up/down开启: {ne_up_down_enable}")
    print(f" 🛰 星间转发模式: {mode}")
    print("< 卫星信息 >")
    print(f" 🛰 初始时间: {round(ts0)}秒 ({t0[0]}年{t0[1]}月{t0[2]}日{t0[3]}时{t0[4]}分{t0[5]}秒)")
    print(f" 🛰 星地连接: " + ", ".join([f"{dev}-{all_sat_gnd_links[dev][0]}"
                                        for dev in devices.keys()]))
    
    # （13）函数返回
    return {
        'code': 1,
        'msg': '卫星json修改成功',
        'json': user_topo_info
    }
