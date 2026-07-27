"""
master - 卫星事件发布
"""

from .satool import *


################# 和日志记录相关 #################
def pub_ctn_satlog(event_set: EventScheduler,
                   topo, user_db_cli, dev, msg):
    """
    在容器中写入卫星相关日志信息
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
        msg: 写入字符串
    """
    event_set.register_event(Event(
        worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev),
        func="ctn_satlog", 
        para={
            "dev_id": user_db_cli.get_value(f"{topo}_{dev}", "NEid"),
            "msg": msg
        }
    ))

################## 和tc配置相关 ##################
def _get_tc_create_events(topo, user_db_cli,
                         l_name, dev1, dev2,
                         rs, bw,
                         source_para=[PROJ_CONFIG.sat_EIRP, PROJ_CONFIG.sat_GT],
                         target_para=[PROJ_CONFIG.sat_EIRP, PROJ_CONFIG.sat_GT],
                         dist=0, place='sat-sat'):
    """
    链路新增tc配置

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
    """
    ################## 指标计算 ##################
    # 延迟
    if dist:  # 若定义距离，则计算延迟
        delay_us1 = delay_us2 = \
            int(dist * 1e9 / PROJ_CONFIG.light_speed) + rs * 1000
        # 仅有星地链路的地面站处无Rs
        if place != 'sat-sat':
            delay_us2 -= rs * 1000
    else:     # 否则忽略链路延迟
        delay_us1 = delay_us2 = ""
    
    # 带宽：赋予设备频率和比特率
    if place == 'sat-sat':  # 星间链路
        freq1 = freq2 = PROJ_CONFIG.freq["sat-sat"]
        bw1 = bw2 = bw["sat-sat"]
    else:                   # 星地链路，默认第一个设备是卫星节点，第二个是地面节点
        freq1 = PROJ_CONFIG.freq["sat-gnd down"]
        freq2 = PROJ_CONFIG.freq["sat-gnd up"]
        bw1 = bw["sat-gnd down"]
        bw2 = bw["sat-gnd up"]
    
    # 丢包：若定义了设备间距离，则计算丢包率，保留五位小数
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
        if loss1 != 0:
            loss_module_1 = f"loss {loss1}"
        if loss2 != 0:
            loss_module_2 = f"loss {loss2}"
    
    # 队列大小，单位bit
    queue_size_byte = 100000

    # 更新数据库
    link_name = f"link_{l_name}"
    table_name = f"{topo}_links_config"  # tc表
    link_cfg = f"link_{l_name}_config"   # 链路名
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
    
    e1 = Event(
        worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev1),
        func="tc_create", 
        para={
            "dev_id": user_db_cli.get_value(f"{topo}_{dev1}", "NEid"),
            "ne": user_db_cli.get_value(f"{topo}_{dev1}", link_name)["nic"],
            "bw": bw1,
            "qsize": queue_size_byte,
            "delay": delay_us1,
            "loss": loss_module_1
        }
    )
    e2 = Event(
        worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev2),
        func="tc_create", 
        para={
            "dev_id": user_db_cli.get_value(f"{topo}_{dev2}", "NEid"),
            "ne": user_db_cli.get_value(f"{topo}_{dev2}", link_name)["nic"],
            "bw": bw2,
            "qsize": queue_size_byte,
            "delay": delay_us2,
            "loss": loss_module_2
        }
    )
    return [e1, e2]

def pub_tc_create(event_set: EventScheduler, tc_args: tuple, tc_kwargs: dict):
    """
    发布单独的tc事件，无依赖
    """
    if tc_args:
        event_set.register_events_without_dependency(
            _get_tc_create_events(*tc_args, **tc_kwargs)
        )

################ 和链路删除相关 ################
def _get_veth_delete_events(topo, user_db_cli, l_name,
                            dev1, dev2, ne_up_down=False):
    """
    删除 veth 链路

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        l_name: 链路名称
        dev1、dev2: 两端设备
        ne_up_down: bool，若为True，则使用网卡up/down模拟增删链路
    """
    # 返回删除veth的事件
    return [Event(
        worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev1),
        func="veth_delete", 
        para={
            "dev1_id": user_db_cli.get_value(f"{topo}_{dev1}", "NEid"),
            "dev2_id": user_db_cli.get_value(f"{topo}_{dev2}", "NEid"),
            "ne1": f"to{dev2}",
            "ne2": f"to{dev1}",
            "type1": ctn_type[dev1[0]],
            "type2": ctn_type[dev2[0]],
            "ne_up_down": ne_up_down
        }
    )]

def _get_vxlan_delete_events(topo, user_db_cli, l_name,
                             dev1, dev2, ne_up_down=False):
    """
    删除 vxlan 链路

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        l_name: 链路名称
        dev1、dev2: veth-pair两端设备
        ne_up_down: 布尔值，若为True，则使用网卡up/down模拟增删链路
    """
    # vxlan的ovs名
    ovs_name = f"{topo}_{l_name}"
    # 返回删除vxlan的一对事件
    return [
        Event(
            worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev1),
            func="vxlan_delete", 
            para={
                "dev_id": user_db_cli.get_value(f"{topo}_{dev1}", "NEid"),
                "ne": f"to{dev2}",
                "ne_up_down": ne_up_down,
                "ovs_name": ovs_name
            }
        ),
        Event(
            worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev2),
            func="vxlan_delete", 
            para={
                "dev_id": user_db_cli.get_value(f"{topo}_{dev2}", "NEid"),
                "ne": f"to{dev1}",
                "ne_up_down": ne_up_down,
                "ovs_name": ovs_name
            }
        )
    ]

def pub_link_delete(event_set: EventScheduler,
                    topo, user_db_cli, l_name, dev1, dev2, ne_up_down=False):
    """
    链路删除，继续判断是 veth-pair/vxlan
    事件均无依赖

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        l_name: 链路名称
        dev1、dev2: veth-pair两端设备
        ne_up_down: 布尔值，若为True，则使用网卡up/down模拟增删链路
    """
    # 使用veth标志位
    veth_flag = is_on_same_worker(topo, user_db_cli, dev1, dev2)

    # 数据库
    # 删除两端设备表项“topo_xx”中，“link_lxx”字段
    user_db_cli.del_value(f"{topo}_{dev1}", f"link_{l_name}")
    user_db_cli.del_value(f"{topo}_{dev2}", f"link_{l_name}")
    # 删除“topo_lxx”表项
    user_db_cli.del_table(f"{topo}_{l_name}")
    # 删除拓扑链路集中的链路信息
    topo_data = user_db_cli.get_value("plane_topo_list", topo)
    topo_data["links"].remove(l_name)
    user_db_cli.set_value("plane_topo_list", topo, topo_data)
    # 删除vxlan表
    if not veth_flag:
        user_db_cli.del_table(f'{topo}_link_{l_name}_vxlan1')
        user_db_cli.del_table(f'{topo}_link_{l_name}_vxlan2')
    
    # 删除链路事件注册
    event_set.register_events_without_dependency(
        (_get_veth_delete_events if veth_flag else _get_vxlan_delete_events)(
            topo, user_db_cli, l_name, dev1, dev2, ne_up_down
        )
    )

################ 和链路创建相关 ################
def _get_veth_create_events(topo, user_db_cli, l_name,
                            dev1, dev2,
                            ip1, ip2, mask, ne_up_down=False):
    """
    新增 veth 链路
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        l_name: 链路名称
        dev1、dev2: 两端设备
        ip1, ip2, mask: 两端网卡ip及掩码
        ne_up_down: 布尔值，若为True，则使用网卡up/down模拟增删链路
    """
    # 容器id
    dev1_id = user_db_cli.get_value(f"{topo}_{dev1}", "NEid")
    dev2_id = user_db_cli.get_value(f"{topo}_{dev2}", "NEid")
    # 网卡名
    ne1 = f"to{dev2}"
    ne2 = f"to{dev1}"
    # 卫星节点身份
    sat_identity = "switch" if dev1[0] == "s" else "router"
    
    # 新增“topo_lxx”表项
    user_db_cli.set_all_values(f"{topo}_{l_name}", {
        'sourceType': sat_identity,
        'sourcePort': ne2,
        'sourceNE': dev2,
        'sourceIP': ip2,
        'sourceID': dev2_id,
        'targetType': sat_identity,
        'targetPort': ne1,
        'targetNE': dev1,
        'targetIP': ip1,
        'targetID': dev1_id,
    })

    return [Event(
        worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev1),
        func="veth_create", 
        para={
            "dev1_id": dev1_id,
            "dev2_id": dev2_id,
            "ne1": ne1,
            "ne2": ne2,
            "ip1": ip1, 
            "ip2": ip2,
            "mask": mask,
            "sat_identity": sat_identity,
            "ne_up_down": ne_up_down
        }
    )]

def _get_vxlan_create_events(topo, user_db_cli, l_name,
                             dev1, dev2,
                             ip1, ip2, mask, ne_up_down=False):
    """
    新增 vxlan 链路
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        l_name: 链路名称
        dev1、dev2: 两端设备
        ip1, ip2, mask: 两端网卡ip及掩码
        ne_up_down: 布尔值，若为True，则使用网卡up/down模拟增删链路
    """
    # 容器id
    dev1_id = user_db_cli.get_value(f"{topo}_{dev1}", "NEid")
    dev2_id = user_db_cli.get_value(f"{topo}_{dev2}", "NEid")
    # 网卡名
    ne1 = f"to{dev2}"
    ne2 = f"to{dev1}"
    # 卫星节点身份
    sat_identity = "switch" if dev1[0] == "s" else "router"

    # 节点所在worker的ip
    worker1 = user_db_cli.get_worker_ip_by_ne_name(topo, dev1)
    worker2 = user_db_cli.get_worker_ip_by_ne_name(topo, dev2)
    # vxlan相关参数
    ovs_name = f"{topo}_{l_name}"
    vni = get_vxlan_vni()
    
    # 新增“topo_lxx”表项
    user_db_cli.set_all_values(f"{topo}_{l_name}", {
        'sourceType': sat_identity,
        'sourcePort': ne1,
        'sourceNE': dev1,
        'sourceIP': ip1,
        'sourceID': dev1_id,
        'targetType': sat_identity,
        'targetPort': ne2,
        'targetNE': dev2,
        'targetIP': ip2,
        'targetID': dev2_id,
        'vxlan': [f'link_{l_name}_vxlan1', f'link_{l_name}_vxlan2']
    })
    
    # 新增vxlan表
    user_db_cli.set_all_values(f'{topo}_link_{l_name}_vxlan1', {
        "VNI": vni,
        "remoteIP": worker2,
        "source": dev1,
        "target": ovs_name,
        "sourcePort": ne1,
        "partof": l_name,
        "sourceIP": ip1
    })
    user_db_cli.set_all_values(f'{topo}_link_{l_name}_vxlan2', {
        "VNI": vni,
        "remoteIP": worker1,
        "source": dev2,
        "target": ovs_name,
        "sourcePort": ne2,
        "partof": l_name,
        "sourceIP": ip2
    })

    return [
        Event(
            worker=worker1,
            func="vxlan_create", 
            para={
                "dev_id": dev1_id,
                "ne": ne1,
                "sat_identity": sat_identity,
                "ne_up_down": ne_up_down,
                "ovs_name": ovs_name,
                "vni": vni,
                "remote_ip": worker2,
                "ne_ip": ip1
            }
        ),
        Event(
            worker=worker2,
            func="vxlan_create", 
            para={
                "dev_id": dev2_id,
                "ne": ne2,
                "sat_identity": sat_identity,
                "ne_up_down": ne_up_down,
                "ovs_name": ovs_name,
                "vni": vni,
                "remote_ip": worker1,
                "ne_ip": ip2
            }
        )
    ]

def pub_link_create(event_set: EventScheduler,
                    topo, user_db_cli, l_name,
                    dev1, dev2, subnet_ip="", ip1="", ip2="",
                    mask=PROJ_CONFIG.link_subnet_mask,
                    ne_up_down=False, tc_args=tuple(), tc_kwargs={}):
    """
    链路新增，继续判断是 veth-pair/vxlan
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        l_name: 链路名称
        dev1、dev2: 两端设备
        subnet_ip: 链路子网网段，优先根据此信息修改链路两端ip
        ip1、ip2: 若subnet_ip没有指定，也可直接指定链路两端ip
        mask: 链路子网掩码
        ne_up_down: 布尔值，若为True，则使用网卡up/down模拟增删链路

    """
    # 根据链路子网网段划分ip
    if subnet_ip != "":
        if int(dev1[1:]) > int(dev2[1:]):
            ip1 = int2ip(subnet_ip + 2)
            ip2 = int2ip(subnet_ip + 1)
        else:
            ip1 = int2ip(subnet_ip + 1)
            ip2 = int2ip(subnet_ip + 2)
    
    # 容器id
    dev1_id = user_db_cli.get_value(f"{topo}_{dev1}", "NEid")
    dev2_id = user_db_cli.get_value(f"{topo}_{dev2}", "NEid")
    # 网卡名
    ne1 = f"to{dev2}"
    ne2 = f"to{dev1}"
    
    # 使用veth标志位
    veth_flag = is_on_same_worker(topo, user_db_cli, dev1, dev2)

    # 数据库
    # 新增两端设备表项“topo_xx”中，“link_lxx”字段
    user_db_cli.set_value(f"{topo}_{dev1}", "link_" + l_name, {
        "ip": ip1,
        "mask": mask,
        "nic": ne1,
        "name": f"{dev1}{dev2}",
        "mac": ""
    })
    user_db_cli.set_value(f"{topo}_{dev2}", "link_" + l_name, {
        "ip": ip2,
        "mask": mask,
        "nic": ne2,
        "name": f"{dev2}{dev1}",
        "mac": ""
    })
    # 新增拓扑链路集中的链路信息
    topo_data = user_db_cli.get_value("plane_topo_list", topo)
    topo_data["links"].append(l_name)
    user_db_cli.set_value("plane_topo_list", topo, topo_data)

    # 创建链路事件
    create_events = (_get_veth_create_events if veth_flag else _get_vxlan_create_events)(
        topo, user_db_cli, l_name,
        dev1, dev2,
        ip1, ip2, mask, ne_up_down
    )
    # 注册创建链路事件
    event_set.register_events_without_dependency(create_events)

    # tc配置
    if tc_args:
        # 获取tc事件
        tc_events = _get_tc_create_events(*tc_args, **tc_kwargs)
        # 对每个tc事件
        for tc_event in tc_events:
            # 统计依赖事件
            dependencies = []
            for create_event in create_events:
                if tc_event.worker == create_event.worker:
                    dependencies.append(create_event)
            # 注册tc事件
            event_set.register_event(tc_event, dependencies)

################ 和链路迁移相关 ################
def _get_veth_move_events(topo, user_db_cli, l_name,
                          dev_stable, dev_from, dev_to, ip, mask):
    """
    veth 迁移，并进行ip配置
    原来: dev_stable <--> dev_from
    后来: dev_stable <--> dev_to

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        l_name: 链路名称
        dev_stable: 链路原来一端，换绑过程中不变
        dev_from: 链路原来一端，换绑过程改变
        dev_to: 链路后来一端
        ip, mask: 迁移网卡的ip和掩码，为""说明不配置
    """
    # 容器id
    dev_from_id = user_db_cli.get_value(f"{topo}_{dev_from}", "NEid")
    dev_to_id = user_db_cli.get_value(f"{topo}_{dev_to}", "NEid")
    dev_stable_id = user_db_cli.get_value(f"{topo}_{dev_stable}", "NEid")
    # 设备类型
    type_from = ctn_type[dev_from[0]]
    type_to = ctn_type[dev_to[0]]
    # 节点所在worker的ip
    worker = user_db_cli.get_worker_ip_by_ne_name(topo, dev_from)

    # 数据库
    link_name = "link_" + l_name
    link_table = f"{topo}_{l_name}"
    # dev_to的数据库新增字段，dev_from数据库删除字段
    user_db_cli.set_value(
        f"{topo}_{dev_to}", link_name, 
        user_db_cli.get_value(f"{topo}_{dev_from}", link_name))
    user_db_cli.del_value(f"{topo}_{dev_from}", link_name)
    # 固定端和迁移端，在链路中的地位（target / source）
    move = "source"
    if dev_stable == user_db_cli.get_value(link_table, "sourceNE"):
        move = "target"
    # 更新“topo_lxx”表项
    user_db_cli.set_value(link_table, f"{move}NE", dev_to)
    user_db_cli.set_value(link_table, f"{move}Type", type_to)
    user_db_cli.set_value(link_table, f"{move}ID", dev_to_id)
    # 回调：
    # 读取链路字段
    # link_data['port'] = shell_execute(
    #     f"sudo docker exec {to_dev_id} ovs-ofctl show init-br0"
    #     f" | grep {ne} | sed 's/(.*//'")
    # 更新两端设备表项“topo_xx”中，“link_lxx”字段

    return [Event(
        worker=worker,
        func="veth_move", 
        para={
            "dev_from_id": dev_from_id,
            "dev_to_id": dev_to_id,
            "dev_stable_id": dev_stable_id,
            "ne": f"to{dev_stable}",
            "type_from": type_from,
            "type_to": type_to,
            "ip": ip,
            "mask": mask
        }
    )]

def _get_vxlan_move_events(topo, user_db_cli, l_name,
                           dev_stable, dev_from, dev_to, ip, mask):
    """
    vxlan 迁移，并进行ip配置
    原来: dev_stable <--> dev_from
    后来: dev_stable <--> dev_to
    dev_from 和 dev_to 在同宿主机

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        l_name: 链路名称
        dev_stable: 链路原来一端，换绑过程中不变
        dev_from: 链路原来一端，换绑过程改变
        dev_to: 链路后来一端
        ip, mask: 迁移网卡的ip和掩码，为""说明不配置
    """
    # 容器id
    dev_from_id = user_db_cli.get_value(f"{topo}_{dev_from}", "NEid")
    dev_to_id = user_db_cli.get_value(f"{topo}_{dev_to}", "NEid")
    # 链路名
    ovs_name = link_table = f"{topo}_{l_name}"
    # 设备类型
    type_from = ctn_type[dev_from[0]]
    type_to = ctn_type[dev_to[0]]
    # 迁移的网卡名
    ne = f"to{dev_stable}"
    # 节点所在worker的ip
    worker = user_db_cli.get_worker_ip_by_ne_name(topo, dev_from)

    # 更新数据库
    link_name = "link_" + l_name

    # 更新两端设备表项“topo_xx”中，“link_lxx”字段
    # dev_to的数据库新增字段，等于dev_from数据库删的字段
    link_data = user_db_cli.get_value(f"{topo}_{dev_from}", link_name)
    user_db_cli.set_value(f"{topo}_{dev_to}", link_name, link_data)
    user_db_cli.del_value(f"{topo}_{dev_from}", link_name)

    # 固定端和迁移端，在链路中的地位（target / source）
    move = "source"
    if dev_stable == user_db_cli.get_value(link_table, "sourceNE"):
        move = "target"
    
    # 更新“topo_lxx”表项
    user_db_cli.set_value(link_table, f"{move}NE", dev_to)
    user_db_cli.set_value(link_table, f"{move}Type", type_to)
    user_db_cli.set_value(link_table, f"{move}ID", dev_to_id)
    
    # 更新vxlan表
    vxlan_table = f'{topo}_link_{l_name}_vxlan1'
    if user_db_cli.get_value(vxlan_table, "source") == dev_stable:
        vxlan_table = f'{topo}_link_{l_name}_vxlan2'
    user_db_cli.set_value(vxlan_table, "source", dev_to)

    return [Event(
        worker=worker,
        func="vxlan_move", 
        para={
            "dev_from_id": dev_from_id,
            "dev_to_id": dev_to_id,
            "ne": ne,
            "type_from": type_from,
            "type_to": type_to,
            "ovs_name": ovs_name,
            "ip": ip,
            "mask": mask
        }
    )]

def pub_link_move(event_set: EventScheduler,
                  topo, user_db_cli, l_name,
                  dev_stable, dev_from, dev_to,
                  mode, place="sat-gnd",
                  old_con='', new_con='',
                  sat_gnd_nets={}, gnd_devices={}, l_id1=0,
                  tc_args=tuple(), tc_kwargs={}):
    """
    链路迁移，继续判断是 veth-pair/vxlan，并进行ip配置
    原来: dev_stable <--> dev_from
    后来: dev_stable <--> dev_to

    IP配置规则:
     | 星间转发模式(mode) | 地面站星下ovs |     星地链路迁移时    |  星座链路迁移时 |
     |-------------------|--------------|----------------------|---------------|
     |   IP-MODIFY       |      x       | 卫星ip与地面站网关一致 |  与固定端一致  |
     |   IP-NO-MODIFY    |      x       |      不配置卫星ip     |  不配置卫星ip  |
     |   IP-TUNNEL       |      √       |      不配置卫星ip     |  与固定端一致  |
     |   DHCP            |      √       |      不配置卫星ip     |  与固定端一致  | 

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        l_name: 链路名称
        dev_stable: 链路原来一端，换绑过程中不变
        dev_from: 链路原来一端，换绑过程改变
        dev_to: 链路后来一端
        mode: 星间转发模式
        place: 'sat-gnd'（默认）或'sat-highsat'，标定链路是星地还是星座的
        old_con: 旧连接卫星编号，仅星地链路迁移时用
        new_con: 新连接卫星编号，仅星地链路迁移时用
        sat_gnd_nets: 地面站子网映射，仅星地链路迁移时用
        gnd_devices: 地面站参数，仅星地链路迁移时用
        l_id1: 第一个星地链路，仅星地链路迁移时用
    """
    # 设备类型
    type_stable = ctn_type[dev_stable[0]]
    type_from = ctn_type[dev_from[0]]
    type_to = ctn_type[dev_to[0]]
    
    ################### 获取IP配置参数 ###################
    """
    默认不配ip
        - ip: 链路换绑网卡的ip地址，一般为较高轨卫星侧
        - ip_peer: 换绑过程中不变的一端网卡的ip地址，一般为较低轨卫星/地面站侧
        - mask: 链路网段掩码
    """
    ip = ip_peer = mask = ""
    
    # 情况1：星地链路 + IP-MODIFY
    if place == 'sat-gnd' and mode == 'IP-MODIFY':
        # 地面站ip
        ip_peer = gnd_devices[dev_stable]['ip']
        # 对端卫星ip
        ip = gnd_devices[dev_stable]['gateway'] \
            if type_stable == 'host' else int2ip(ip2int(ip_peer) + 1)
        # 链路网段掩码
        mask = gnd_devices[dev_stable]['netmask']

    # 情况2：星座链路 + IP-MODIFY/IP-TUNNEL/DHCP
    if place == 'sat-highsat' and mode in ['IP-MODIFY', 'IP-TUNNEL', 'DHCP']:
        # 低轨卫星ip
        ip_peer = user_db_cli.get_value(
            f"{topo}_{dev_stable}", f"link_{l_name}")["ip"]
        # 对端卫星ip
        ip = int2ip(ip2int(ip_peer) + 1)
        # 链路网段掩码
        mask = PROJ_CONFIG.link_subnet_mask

    ################## 链路迁移、配置IP ##################
    """
    s(stable) <--> f(from)
              <--> t(to)
    x.w := 设备x所在worker
    
    if s.w == f.w == t.w
        veth 迁移
    elif s.w != f.w == t.w
        vxlan 迁移 
    else
        链路增删
    """
    # 链路迁移事件
    move_events = []

    # 迁移两端在同宿主机，则存在迁移简化方案
    if is_on_same_worker(topo, user_db_cli, dev_from, dev_to):
        # veth标志位
        veth = is_on_same_worker(topo, user_db_cli, dev_from, dev_stable)
        # 链路迁移事件集
        move_events = (_get_veth_move_events if veth else _get_vxlan_move_events)(
            topo, user_db_cli, l_name,
            dev_stable, dev_from, dev_to, ip, mask
        )
    
    # 不存在简化方案，则先删除后新增
    else:
        # 链路删除
        # veth标志位
        veth = is_on_same_worker(topo, user_db_cli, dev_from, dev_stable)
        # 事件注册
        move_events += (_get_veth_delete_events if veth else _get_vxlan_delete_events)(
            topo, user_db_cli, l_name, dev_from, dev_stable)

        # 链路创建
        # veth标志位
        veth = is_on_same_worker(topo, user_db_cli, dev_to, dev_stable)
        # 事件注册
        move_events += (_get_veth_create_events if veth else _get_vxlan_create_events)(
            topo, user_db_cli, l_name, dev_stable, dev_to,
            ip1=ip_peer, ip2=ip, mask=mask)
    
    # 注册链路迁移事件，其均无依赖
    event_set.register_events_without_dependency(move_events)

    ###################### tc配置 #######################
    if tc_args:
        # 获取tc事件
        tc_events = _get_tc_create_events(*tc_args, **tc_kwargs)
        # 对每个tc事件
        for tc_event in tc_events:
            # 统计依赖事件
            dependencies = []
            for create_event in move_events:
                if tc_event.worker == create_event.worker:
                    dependencies.append(create_event)
            # 注册tc事件
            event_set.register_event(tc_event, dependencies)

    #################### 配置路由协议 ####################
    if ip != "" and (type_from == "router" or type_to == "router"):
        # 提取路由信息
        if type_from == "router":
            info_from = user_db_cli.get_all_values(f'{topo}_{dev_from}')
        if type_to == "router":
            info_to = user_db_cli.get_all_values(f'{topo}_{dev_to}')
        
        # 对不同协议，统计路由信息的变化
        if router_protocol == 'ospf':
            # 待增减的子网和area
            net_area = [
                f"{int2ip(ip2int(ip) & ip2int(mask))}/{netmask2cidr(mask)}",
                '0.0.0.0']
            # 修改路由信息并加入变化统计
            if type_from == "router":
                # 移除旧neighbor
                try:
                    info_from['NEconfig']['config']['ospf']['networks'].remove(net_area)
                except:
                    pass
            # to_dev，修改路由信息并加入变化统计
            if type_to == "router":
                info_to['NEconfig']['config']['ospf']['networks'].append(net_area)
        
        elif router_protocol == 'rip':
            # 待增减的子网
            net = f"{int2ip(ip2int(ip) & ip2int(mask))}/{netmask2cidr(mask)}"
            # 修改路由信息并加入变化统计
            if type_from == "router":
                # 移除旧neighbor
                try:
                    info_from['NEconfig']['config']['rip']['networks'].remove(net)
                except:
                    pass
            # to_dev，修改路由信息并加入变化统计
            if type_to == "router":
                info_to['NEconfig']['config']['rip']['networks'].append(net)
        
        elif router_protocol == 'bgp':
            pass
        
        # 修改quagga路由，写入数据库
        if type_from == "router":
            # 修改路由配置
            user_db_cli.set_value(f"{topo}_{dev_from}", 'NEconfig', info_from['NEconfig'])
            # 注册事件
            event_set.register_events_without_dependency(
                _get_rt_config_events(topo, user_db_cli, dev_from, info_from))
            
        if type_to == "router":
            # 修改路由配置
            user_db_cli.set_value(f"{topo}_{dev_to}", 'NEconfig', info_to['NEconfig'])
            # 注册事件
            event_set.register_events_without_dependency(
                _get_rt_config_events(topo, user_db_cli, dev_to, info_to))

    ################### 地面站切换特有 ###################
    if place == "sat-gnd":
        # 日志记录
        if mode == 'IP-TUNNEL':
            # 每站都记录
            for d in gnd_devices:
                pub_ctn_satlog(
                    event_set, topo, user_db_cli, d,
                    ('本' if d==dev_stable else '对端') + f'站换星 {old_con}>{new_con}')
        else:
            # 仅换星站记录
            pub_ctn_satlog(
                event_set, topo, user_db_cli, dev_stable,
                f'本站换星 {old_con}>{new_con}')
        
        # DHCP / IP-TUNNEL模式下，若存在所连卫星，需更新服务
        if type_to != 'host' and mode in ['DHCP', 'IP-TUNNEL']:
            # 地面站ip
            gnd_ip = sat_gnd_nets[new_con]
            get_next_ip(sat_gnd_nets, new_con)
            # 卫星网关ip
            sat_ip = (gnd_ip & sat_gnd_subnet_mask_int) + 1
            # DHCP：更新地面站 DHCP client
            if mode == 'DHCP':
                event_set.register_events_without_dependency(
                    _get_start_dhcp_client_events(topo, user_db_cli,
                                                    dev_stable, sat_ip))
            # IP-TUNNEL：修改隧道和换星地面站ip
            elif mode == 'IP-TUNNEL':
                event_set.register_events_without_dependency(
                    _get_change_tunnel_events(
                        topo, user_db_cli,
                        dev_stable, list(gnd_devices.keys()), 
                        int2ip(gnd_ip), int2ip(sat_ip), l_id1))
    
################# 和模式变化相关 #################
def pub_sat_change_mode(event_set: EventScheduler,
                        topo, user_db_cli,
                        old_mode: str, new_mode: str,
                        walkers, sat_gnd_links, sat_id1: int, l_id1: int,
                        sat_gnd_nets, sat_ovs_gnd,
                        dev_list, gnd_devices):
    """
    模式变更，其中所有事件均与服务相关，无依赖
    
    - 有如下模式
        - 初始模式
            - mode == ''
        - sat_identity == 'switch'
            - mode == 'NO-STP': 不配置STP
            - mode == 'STP': 配置RSTP
            - mode == 'SDN': 使用SDN控制器
        - sat_identity == 'router'
            - 无星下ovs
                - mode == 'IP-NO-MODIFY': 不自动配置IP，链路迁移后发生问题
                - mode == 'IP-MODIFY': 自动配置卫星网卡IP
            - 有星下ovs
                - mode == 'IP-TUNNEL': 为每对地面站建立隧道
                - mode == 'DHCP': 卫星网关为地面站分配IP

    - 支持的模式变更如下
        - '' --> 任意非初始模式
        - 'STP' <--> 'NO-STP'
        - 'IP-NO-MODIFY' <--> 'IP-MODIFY'
        - 'IP-TUNNEL' <--> 'DHCP'

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        old_mode, new_mode: 模式变更
        walkers: 星座参数
        sat_gnd_links: 星地链路
        sat_id1: 第一个卫星编号
        l_id: 第一个星地链路编号
        sat_gnd_nets: 小子网创建映射
        sat_ovs_gnd: 地面站星下ovs
        dev_list: 地面站列表
        gnd_devices: 地面站参数字典
    """
    if old_mode == new_mode:
        return

    ############# 卫星是交换机 #############
    if new_mode in ['STP', 'NO-STP']:
        existed_sat_id = 0
        for walker in walkers:
            N = walker['N']
            # 开启每个卫星的rstp
            for j in range(N):
                # 卫星设备名
                sat_dev = f"s{sat_id1+existed_sat_id + j}"
                event_set.register_events_without_dependency(
                    _get_start_stop_stp_events(
                        topo, user_db_cli, sat_dev, switch=(new_mode=="STP")))
            existed_sat_id += N
        return

    ############# 卫星是路由器 #############
    if old_mode == 'DHCP':
        # 卫星停止 DHCP server
        existed_sat_id = 0
        for walker in walkers:
            N = walker['N']
            # 对每个卫星，查看上层卫星连接
            for j in range(N):
                # 卫星设备名
                sat_dev = f"r{sat_id1 + existed_sat_id + j}"
                event_set.register_events_without_dependency(
                    _get_stop_dhcp_server_events(topo, user_db_cli, sat_dev))
            existed_sat_id += N
        
        # 地面站主机停止 DHCP client
        for dev in dev_list:
            event_set.register_events_without_dependency(
                _get_stop_dhcp_client_events(topo, user_db_cli, dev))
    
    elif old_mode == 'IP-TUNNEL':
        # 删除主机间每一对隧道
        for i in range(len(dev_list)):
            for j in range(i + 1, len(dev_list)):
                event_set.register_events_without_dependency(
                    _get_delete_tunnel_events(
                        topo, user_db_cli, dev_list[i], dev_list[j]))
    
    if new_mode == 'DHCP':
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
                event_set.register_events_without_dependency(
                    _get_start_dhcp_server_events(
                        topo, user_db_cli, sat_dev,
                        sat_gnd_nets[str(sat_id)],
                        sat_ovs_gnd[str(sat_id)]))
            existed_sat_id += N
        
        # 地面站主机启动 DHCP client
        for dev, val in sat_gnd_links.items():
            event_set.register_events_without_dependency(
                _get_start_dhcp_client_events(
                    topo, user_db_cli, dev, ip=sat_gnd_nets[str(val[0])]))
            
    elif new_mode == 'IP-TUNNEL':
        # 旧模式为HDCP，需为每个有卫星连接的地面站配置IP
        if old_mode == 'DHCP':
            l_id = l_id1
            for dev in dev_list:
                # 所连接卫星名
                connect_sat = str(sat_gnd_links[dev][0])
                # 地面站有所连接卫星，则配置ip
                if connect_sat != 'None':
                    ip = int2ip(sat_gnd_nets[connect_sat])
                    event_set.register_events_without_dependency(
                        _get_ip_config_events(topo, user_db_cli, dev, ip))

                    # 子网内下一可用ip自增
                    get_next_ip(sat_gnd_nets, connect_sat)
                    # 写入数据库
                    # 修改两端设备表项“topo_xx”中，“link_lxx”字段
                    tmp = user_db_cli.get_value(f"{topo}_{dev1}", f"link_l{l_id}")
                    tmp["ip"] = ip
                    tmp["mask"] = PROJ_CONFIG.sat_gnd_subnet_mask
                    user_db_cli.set_value(f"{topo}_{dev1}", f"link_l{l_id}", tmp)
                    # 修改“topo_lxx”表项
                    label = 'sourceIP' if user_db_cli.get_value(
                        f"{topo}_l{l_id}", "sourceNE") == dev else 'targetIP'
                    user_db_cli.set_value(f"{topo}_l{l_id}", label, ip)

                # 链路编号自增
                l_id += 1
        
        # 建立主机间的隧道
        for i in range(len(dev_list)):
            for j in range(i + 1, len(dev_list)):
                dev1, dev2 = dev_list[i], dev_list[j]
                event_set.register_events_without_dependency(
                    _get_create_tunnel_events(
                        topo, user_db_cli, 
                        dev1, gnd_devices[dev1]['ip'], f"link_l{l_id1+i}", 
                        dev2, gnd_devices[dev2]['ip'], f"link_l{l_id1+j}"))

################# 和交换路由相关 #################
def _get_start_stop_stp_events(topo, user_db_cli, dev, switch):
    """
    启停stp
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
        switch: bool, True意味着启动STP，否则为关闭
    """
    return [Event(                   
        worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev),
        func="docker_exec", 
        para={
            "dev_id": user_db_cli.get_value(f"{topo}_{dev}", "NEid"),
            "cmd": f'ovs-vsctl set bridge init-br0 rstp_enable='
                   f'{"true" if switch else "false"}'
        }
    )]

def _get_ip_config_events(topo, user_db_cli, dev, ip):
    """
    配置ip地址
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
        ip: 配置的ip地址
    """
    return [Event(
        worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev),
        func="ip_config", 
        para={
            "dev_id": user_db_cli.get_value(f"{topo}_{dev}", "NEid"),
            "ip": ip
        }
    )]

def _get_rt_config_events(topo, user_db_cli, dev, info):
    """
    配置路由协议
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
        info: 路由信息
    """
    return [Event(
        worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev),
        func="rt_config", 
        para={
            "dev": dev,
            "dev_id": user_db_cli.get_value(f"{topo}_{dev}", "NEid"),
            "info": info,
            "protocol": router_protocol
        }
    )]

################### 和DHCP相关 ###################
def _get_start_dhcp_server_events(topo, user_db_cli, dev: str, ip: int, ne: str):
    """
    开启dhcp服务
    预先已运行以下命令
        - chmod 777 /tmp
        - apt update
        - apt install isc-dhcp-server
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
        ip: 设备网卡ip
        ne: 网卡名
    """
    # 整数形式的网络号
    subnet_int = ip & sat_gnd_subnet_mask_int
    # 子网中最后一个可用ip
    broadcast_ip_int = ip2int('255.255.255.255') - \
        sat_gnd_subnet_mask_int + subnet_int
    
    return [Event(
        worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev),
        func="start_dhcp_server", 
        para={
            "dev_id": user_db_cli.get_value(f"{topo}_{dev}", "NEid"),
            "ne": ne,
            "subnet_int": subnet_int,
            "broadcast_ip_int": broadcast_ip_int
        }
    )]

def _get_start_dhcp_client_events(topo, user_db_cli, dev: str, ip: int):
    """
    开启dhcp客户端连接

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
        ip: 小子网中的一个ip，用来求出卫星上dhcp服务器的ip
    """
    return [Event(
        worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev),
        func="start_dhcp_client", 
        para={
            "dev_id": user_db_cli.get_value(f"{topo}_{dev}", "NEid"),
            "ip": ip
        }
    )]

def _get_stop_dhcp_server_events(topo, user_db_cli, dev: str):
    """
    关闭dhcp服务
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
    """
    return [Event(
        worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev),
        func="stop_dhcp_server", 
        para={
            "dev_id": user_db_cli.get_value(f"{topo}_{dev}", "NEid"),
        }
    )]

def _get_stop_dhcp_client_events(topo, user_db_cli, dev: str):
    """
    关闭dhcp客户端连接

    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
    """
    return [Event(
        worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev),
        func="stop_dhcp_client", 
        para={
            "dev_id": user_db_cli.get_value(f"{topo}_{dev}", "NEid")
        }
    )]

################### 和隧道相关 ###################
def _get_create_tunnel_events(
        topo, user_db_cli,
        dev1: str, ip_in1: str, link1: str, 
        dev2: str, ip_in2: str, link2: str):
    """
    创建隧道
    隧道名为: 本端设备名 + 对端设备名
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev1, dev2: 设备名称
        ip_in1, ip_in2: 设备内网IP
        link1, link2: 星地链路名，如link_l22
    """
    # 外网IP
    ip_out1 = user_db_cli.get_value(f"{topo}_{dev1}", link1)["ip"]
    ip_out2 = user_db_cli.get_value(f"{topo}_{dev2}", link2)["ip"]

    return [
        Event(
            worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev1),
            func="create_tunnel", 
            para={
                "dev_id": user_db_cli.get_value(f"{topo}_{dev1}", "NEid"),
                "tunnel_name": dev1+dev2,
                "ip_in": ip_in1,
                "ip_in_peer": ip_in2,
                "ip_out": ip_out1,
                "ip_out_peer": ip_out2
            }
        ),
        Event(
            worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev2),
            func="create_tunnel", 
            para={
                "dev_id": user_db_cli.get_value(f"{topo}_{dev2}", "NEid"),
                "tunnel_name": dev2+dev1,
                "ip_in": ip_in2,
                "ip_in_peer": ip_in1,
                "ip_out": ip_out2,
                "ip_out_peer": ip_out1
            }
        )   
    ]

def _get_change_tunnel_events(
        topo, user_db_cli,
        dev: str, dev_list: list, gnd_ip: str, gw: str, l_id: int):
    """
    修改隧道，主要修改的是外网连接的IP
    隧道名为: 本端设备名 + 对端设备名
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 换星地面站设备
        dev_list: list, 所有地面站设备列表
        gnd_ip: str, 换星地面站外网IP
        gw: str, 地面站网关
        l_id: int, 第一个星地链路编号
    """
    # 换星站容器id
    dev_id = user_db_cli.get_value(f"{topo}_{dev}", "NEid")
    # 节点所在worker的ip
    worker = user_db_cli.get_worker_ip_by_ne_name(topo, dev)
    # 事件列表
    ret_events = [
        # 修改换星地面站的网卡ip
        Event(
            worker=worker,
            func="ip_config", 
            para={
                "dev_id": dev_id,
                "ip": gnd_ip
            }
        ),
        # 修改换星地面站的网关ip
        Event(
            worker=worker,
            func="gw_config", 
            para={
                "dev_id": dev_id,
                "gw": gw,
            }
        )
    ]

    # 修改每一对隧道
    for peer in dev_list:
        # 过滤换星站
        if peer == dev:
            link_name = f"link_l{l_id}"
            link_table = f"{topo}_l{l_id}"
        else:
            # 非换星站外网ip
            peer_ip_out = user_db_cli.get_value(f"{topo}_{peer}", f"link_l{l_id}")["ip"]
            # 换星端修改隧道的事件
            ret_events.append(Event(
                worker=worker,
                func="change_tunnel", 
                para={
                    "dev_id": dev_id,
                    "tunnel_name": dev+peer,
                    "ip_out": gnd_ip,
                    "peer_ip_out": peer_ip_out
                }
            ))
            # 对端修改隧道的事件
            ret_events.append(Event(
                worker=user_db_cli.get_worker_ip_by_ne_name(topo, peer),
                func="change_tunnel", 
                para={
                    "dev_id": user_db_cli.get_value(f"{topo}_{peer}", "NEid"),
                    "tunnel_name": peer+dev,
                    "ip_out": peer_ip_out,
                    "peer_ip_out": gnd_ip
                }
            ))
        l_id += 1

    # 数据库修改
    # 更新两端设备表项“topo_xx”中，“link_lxx”字段
    link_conf = user_db_cli.get_value(f"{topo}_{dev}", link_name)
    link_conf["ip"] = gnd_ip
    user_db_cli.set_value(f"{topo}_{dev}", link_name, link_conf)
    link_conf = user_db_cli.set_value(f"{topo}_{dev}", "NEgateway", gw)
    # 更新“topo_lxx”表项
    label = 'sourceIP' \
        if dev == user_db_cli.get_value(link_table, 'sourceNE') else 'targetIP'
    user_db_cli.set_value(link_table, label, gnd_ip)

    return ret_events

def _get_delete_tunnel_events(
        topo, user_db_cli, dev1: str, dev2: str):
    """
    删除隧道
    隧道名为: 本端设备名 + 对端设备名
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev1, dev2: 设备名称
    """
    return [
        Event(
            worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev1),
            func="delete_tunnel", 
            para={
                "dev_id": user_db_cli.get_value(f"{topo}_{dev1}", "NEid"),
                "tunnel_name": dev1 + dev2 
            }
        ),
        Event(
            worker=user_db_cli.get_worker_ip_by_ne_name(topo, dev2),
            func="delete_tunnel", 
            para={
                "dev_id": user_db_cli.get_value(f"{topo}_{dev2}", "NEid"),
                "tunnel_name": dev2 + dev1
            }
        )
    ]
