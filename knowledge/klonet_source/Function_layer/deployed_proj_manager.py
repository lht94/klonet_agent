import json

from ..Implement_layer.LinkManager.link_operate import shell_execute
from ..tools.context import redis_context, judge_user_exist, check_table_key, check_table_existence
from .topo_aggregate import Topo_aggregate
from ..Service_layer.redis_error import *
from .topo_aggregate import Ne_re_host, Ne_re_base
from ..Service_layer.data_server_manager import DataServerManager
from ..Service_layer.influxAPI import USER_DATA_DIR, read_influx
from ..Service_layer.redisAPI import UserMapRedis
from ..tools.log_tools import UserLogger, UserLogLevel
def delete_monitor_data(user, topo, expr="all"):
    '''
        del the monitoring data including influxdb's data and csv table
        
        Args:
            user: 用户名
            topo: 拓扑名
            expr: 实验名(缺省空字符串)

        Returns:
            
    '''
    del_file_dir = USER_DATA_DIR + "/" + user + "/" + topo + "/"
    if expr == "all":
        cmp = user + "_" + topo
    else:
        cmp = user + "_" + topo + "_" + expr
    cmp_len = len(cmp)
    
    try:
        #删除csv表
        shell_execute("sudo rm -f " + del_file_dir + cmp + "*")
        shell_execute("sudo rm -rf " + del_file_dir + expr)
        if expr == "":
            shell_execute("sudo rm -rf " + del_file_dir)
            
        measurements = read_influx("show measurements")
        raw_measurements = measurements["results"][0]["series"][0]["values"][0:-1]
        print("there exit these measuerments:",raw_measurements)
        user_map_redis = UserMapRedis()
        user_db_cli = user_map_redis.get_user_db(user)
        for m in raw_measurements:
            print("m[0]:"+m[0]+" ")
            print(cmp+" "+str(cmp_len))
            if m[0][0:cmp_len] == cmp:
                print("there exit this measurement:", m[0])
                print(" so del this measurement:", m[0])
                del_measurement_q = "drop measurement \"" + m[0] + "\""
                print("q1:"+del_measurement_q)
                del_measurement_return = read_influx(del_measurement_q, method="POST")
                del_measurement_return_state_id = del_measurement_return["results"][0]["statement_id"]
                if del_measurement_return_state_id == 0:
                    print("del measurement " + m[0] + " succeed")
                print("del the series of this measurement")
            else:
                print("there have no measurement like " + cmp)    


            if expr == "all":
                # 获取该user该topo下的expr列表，然后删除所有
                table_name = topo+"_monitor"
                expr_list = []
                expr_list = user_db_cli.get_all_keys(table_name)
                print("expr_list:", expr_list)
                for expr in expr_list:
                    del_series_q = "drop series from perf_data where \"expr\"='" + topo + "_" + expr + "' and \"user_name\"='" + user + "'"
                    print("q3:"+del_series_q)
                    del_series_return = read_influx(del_series_q, method="POST") #考虑传q列表，不然每个expr就得循环一次（现在是这样）
                    del_series_return_state_id = del_series_return["results"][0]["statement_id"]
                    if del_series_return_state_id == 0:
                        print("del series in perf_data measurement succeed")
                    
            else:
                del_series_q = "drop series from perf_data where \"expr\"='" + topo + "_" + expr + "' and \"user_name\"='" + user + "'"
                print("q2:"+del_series_q)
                del_series_return = read_influx(del_series_q, method="POST")
            
    except Exception as e:
        print(e)

def retrieve_nes2interfaces_info(user, topo, ne_types:list):
    """
    在redis中获得节点列表及其接口信息
    user:用户名
    topo: 拓扑名
    ne_types: 要获取信息的节点类型，目前的类型有hosts, switches,
                     routers, controllers
    :return: 成功则返回json；code 0：失败，1：成功；msg：详细信息
            json示例：
            {
                "NEs": [
                    "h1",
                    "h2"
                ],
                "NEs_info": {
                    "h1": [
                        {
                            "ip": "192.168.0.1",
                            "name": "h1s1",
                            "netmask": "255.255.255.0"
                        }
                    ],
                    "h2": [
                        {
                            "ip": "192.168.0.2",
                            "name": "h2s1",
                            "netmask": "255.255.255.0"
                        }
                    ]
                },
                "code": 1,
                "msg": "端节点信息获取成功"
            }
    """
    with redis_context(user) as user_db_cli:
        if not check_table_key(user, 'topo_service', topo):
            return {'code': 0, 'msg': f'拓扑{topo}不存在！'}
        topo_service = user_db_cli.get_value('topo_service', topo)
        nes_list = []
        for ne_type in ne_types:
            if ne_type not in ["hosts", "switches", "routers", "controllers"]:
                return {
                    'code': 0, 
                    'msg': f'端节点信息获取失败，不支持类型[{ne_type}]信息的获取', 
                    'NEs': [], 
                    'NEs_info': []
                }
            nes_list.extend(topo_service.get(ne_type, []))
        nes2interface_table = {}
        for k in nes_list:
            table_name = f'{topo}_{k}'
            table = user_db_cli.get_all_values(table_name)
            ne_ob = Ne_re_base(k, table)
            ne_ob()
            ne_ob.update_interfaces()
            interfaces = ne_ob.table[ne_ob.name]['interfaces']
            nes2interface_table.setdefault(ne_ob.name, interfaces)
        return {'code': 1, 'msg': '端节点信息获取成功', 'NEs': nes_list, 'NEs_info': nes2interface_table}
        
def retrieve_topo(user, topo):
    """
    在redis中获得一个拓扑的json信息
    user:用户名
    topo: 拓扑名
    :return: 成功则返回拓扑的json；code 0：失败，1：成功；msg：详细信息
    """
    if not judge_user_exist(user):
        msg = {'code': 0, 'msg': f'user: {user}不存在！'}
        return msg
    # 用户已存在的情况下：
    topo_json = {'user': user, 'topo': topo}
    net = topo_json.setdefault('networks', {})
    with redis_context(user) as user_db_cli:
        try:
            plane_topo = user_db_cli.get_value('plane_topo_list', topo)
            topo_service = user_db_cli.get_value('topo_service', topo)
        except TableNotExistError:
            msg = {'code': 0, 'msg': f'topo: {topo}不存在！'}
            return msg
        except KeyNotExistError:
            msg = {'code': 0, 'msg': f'topo: {topo}不存在！'}
            return msg
        else:
            ne_list = plane_topo.get('NEs', [])
            link_list = plane_topo.get('links', [])
            # 节点表与链路表信息
            # 节点（链路）名 -> 表信息（key-value）
            nes_table = {}
            links_table = {}
            for ne in ne_list:
                table_name = f'{topo}_{ne}'
                table = user_db_cli.get_all_values(table_name)
                nes_table.setdefault(ne, table)
            for link in link_list:
                table_name = f'{topo}_{link}'
                table = user_db_cli.get_all_values(table_name)
                links_table.setdefault(link, table)
            topo_aggregated = Topo_aggregate(**nes_table, **links_table, **topo_service, **{'links': link_list})
            topo_aggregated()
            net.update(topo_aggregated.network)
            msg = {'code': 1, 'msg': '获取拓扑成功'}
            msg.update(topo_json)
            return msg

def retrieve_node_info(user,topo):
    """
    从redis中获取某topo中所有的节点信息：名称、镜像、宿主机
    return: 成功则返回json；code 0：失败，1：成功；msg：详细信息
        json示例：
            {
                "node_info": {
                    "h1": [
                        {
                            "node_name":"h1",
                            "imgae_name": "host/ubuntu",
                            "host_machine": "172.31.0.27"
                        }
                    ],
                    "h2": [
                        {
                            "node_name":"h2",
                            "imgae_name": "host/ubuntu",
                            "host_machine": "172.31.0.27"
                        }
                    ]
                },
                "code": 1,
                "msg": "端节点信息获取成功"
            }
    """
    with redis_context(user) as user_db_cli:
        if not check_table_key(user, 'topo_service', topo):
            return {'code': 0, 'msg': f'拓扑{topo}不存在！'}
        try:
            plane_topo = user_db_cli.get_value('plane_topo_list', topo)
        except TableNotExistError:
            msg = {'code': 0, 'msg': f'{topo}不存在！'}
            return msg
        except KeyNotExistError:
            msg = {'code': 0, 'msg': f'{topo}不存在！'}
            return msg
        else:
            ne_list = plane_topo.get('NEs', [])
            ne_info = {}
            for ne in ne_list:
                node_table_name = f'{topo}_{ne}'
                node_table = user_db_cli.get_all_values(node_table_name)
                host_table = user_db_cli.get_all_values("subtopo2worker")
                ne_info.setdefault(ne, {"node_name":ne,
                                        "imgae_name": node_table["NEimage"],
                                        "host_machine": host_table[node_table["NEloc"]]})
            return {'code': 1, 'msg': '获取节点信息成功',"node_info":ne_info}


def retrieve_link_info(user, topo):
    """
    从redis中获取某topo中所有的链路信息：名称、源目的节点、端口信息、tc规则
    return: 成功则返回json；code 0：失败，1：成功；msg：详细信息
            json示例：
             {
                "link_info": {
                    "l1": [
                        {
                            "link_name":"l1",
                            "sourceNE": "ovs1",
                            "targetNE": "ubuntu1",
                            "sourcePort":{
                                "ip": "",
                                "mac": "fa:76:68:a8:1d:cf",
                                "mask": "",
                                "nic": "ba47f7d231"
                            },
                            "targetPort":{
                                "ip": "192.168.1.2",
                                "mac": "86:53:26:1e:31:6e",
                                "mask": "255.255.255.0",
                                "name": "h1s1",
                                "nic": "5e627f5910"
                            },
                            "tc":{}
                                }
                            ]
                "code": 1,
                "msg": "获取链路信息成功"
            }

    """
    with redis_context(user) as user_db_cli:
        if not check_table_key(user, 'topo_service', topo):
            return {'code': 0, 'msg': f'拓扑{topo}不存在！'}
        try:
            plane_topo = user_db_cli.get_value('plane_topo_list', topo)
        except TableNotExistError:
            msg = {'code': 0, 'msg': f'{topo}不存在！'}
            return msg
        except KeyNotExistError:
            msg = {'code': 0, 'msg': f'{topo}不存在！'}
            return msg
        else:
            link_list = plane_topo.get('links', [])
            link_info = {}
            for link in link_list:
                
                link_table_name = f'{topo}_{link}'
                link_table = user_db_cli.get_all_values(link_table_name)

                link_name=f'link_{link}'
                link_source_node_tabe_name=f'{topo}_{link_table["sourceNE"]}'
                link_source_node_table=user_db_cli.get_all_values(link_source_node_tabe_name)
               
                link_target_node_tabe_name=f'{topo}_{link_table["targetNE"]}'
                link_target_node_table=user_db_cli.get_all_values(link_target_node_tabe_name)

                tc_table_name = f'{topo}_links_config'
                if user_db_cli.get_all_values(tc_table_name)=="{}":
                    tc="{}"
                else:
                    tc=user_db_cli.get_all_values(tc_table_name)
                link_info.setdefault(link, {"link_name":link,
                                            "sourceNE":link_table["sourceNE"],
                                            "targetNE":link_table["targetNE"],
                                            "sourcePort": link_source_node_table[link_name],
                                            "targetPort":link_target_node_table[link_name],
                                            "tc":tc})
            return {'code': 1, 'msg': '获取链路信息成功',"link_info":link_info}

def retrieve_worker_ip(user,topo):
    """
    从redis中获取某topo中worker的ip信息：
    return: 成功则返回json；code 0：失败，1：成功；msg：详细信息
            json示例：
             {
                {
                    "h1":"192.168.1.1", 
                    "h2": "192.168.1.2"
                }
            }

    """
    with redis_context(user) as user_db_cli:
        if not check_table_key(user, 'topo_service', topo):
            return {'code': 0, 'msg': f'拓扑{topo}不存在！'}
        try:
            plane_topo = user_db_cli.get_value('plane_topo_list', topo)
        except TableNotExistError:
            msg = {'code': 0, 'msg': f'{topo}不存在！'}
            return msg
        except KeyNotExistError:
            msg = {'code': 0, 'msg': f'{topo}不存在！'}
            return msg
        else:
            ne_list = plane_topo.get('NEs', [])
            worker_ip = {}
            for ne in ne_list:
                node_table_name = f'{topo}_{ne}'
                node_table = user_db_cli.get_all_values(node_table_name)
                worker_name = node_table["NEloc"]
                worker_ip_info = user_db_cli.get_value('subtopo2worker', worker_name)
                worker_ip.setdefault(ne,worker_ip_info)

            return {'code': 1, 'msg': '获取worker的IP地址信息成功',"worker_ip":worker_ip}

def retrieve_topo_list(user):
    """在redis中获得一个用户已创建拓扑列表标识（不包含具体json）
    
    Args:
        user:用户名
    
    Returns:
        成功则返回拓扑的列表；code 0：失败，1：成功；msg：详细信息
        topo_list:[]，shared_topo_list:[]，
    """
    if not judge_user_exist(user):
        msg = {'code': 0, 'msg': f'{user}不存在！', 'topo_list': []}
        return msg
    result = {'code': 1, 'msg': 'success'}
    with redis_context(user) as user_db_cli:
        topo_list = result.setdefault('topo_list', [])
        topo_list.extend(user_db_cli.get_all_values('plane_topo_list').keys())
        # 多人共享项目获取时，需要creator信息以便前端标识
        shared_topo_list = result.setdefault('shared_topo_list', [])
        for k,v in user_db_cli.get_all_values('shared_topo_list').items():
            creator = v['creator']
            topo = k[: -len(creator) - 1]
            shared_topo_list.append({topo:creator})
    return result

def retrieve_topo_list_and_topo_info(user):
    """
    在redis中获得一个用户的所有topo的列表及各topo的节点和链路数量以及topo创建时间
    user:用户名
    :return: 成功则返回拓扑的列表；code 0：失败，1：成功；msg：详细信息
             topo_list_and_topo_info:{}
    """
    if not judge_user_exist(user):
        msg = {'code': 0, 'msg': f'{user}不存在！', 'topo_list_and_topo_info': {}}
        return msg
    result = {'code': 1, 'msg': 'success'}
    with redis_context(user) as user_db_cli:
        topo_list_and_topo_info = result.setdefault('topo_list_and_topo_info', {})
        for topo_name in user_db_cli.get_all_values('plane_topo_list').keys():
            topo_info = topo_list_and_topo_info.setdefault(topo_name,{})
            plane_topo = user_db_cli.get_value('plane_topo_list', topo_name)
            node_number = len(plane_topo["NEs"])
            link_number = len(plane_topo["links"])
            topo_info.setdefault('link_number',link_number)
            topo_info.setdefault('node_number',node_number)
            topo_creat_tim_info = user_db_cli.get_value('topo_info', topo_name)
            topo_creat_time =  topo_creat_tim_info["topo_creat_time"]
            topo_info.setdefault('topo_creat_time',topo_creat_time)
    return result

def retrieve_monitor_event(user, topo, expr):
    """
    在redis中获取指定监控服务的信息
    user: 用户名
    topo: 拓扑名
    expr: 监控服务名
    :return: code 0：失败，1：成功；msg：详细信息;
             events_to_monitor: []（监控服务信息）
    """
    table_name = f"{topo}_monitor"
    monitor_info = []
    with redis_context(user) as user_db_cli:
        try:
            monitor_info.extend(user_db_cli.get_value(table_name, expr))
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'监控服务获取失败:{e}'}
        else:
            return {'code': 1, 'msg': '监控服务获取成功', 'events_to_monitor': monitor_info}





def update_monitor_event(user, topo, expr, monitor_info):
    """
    在redis中更新监控服务信息
    user: 用户名
    topo: 拓扑名
    expr: 监控服务名
    monitor_info: 监控服务详细信息（子事件列表）
    :return: code 0：失败，1：成功；msg：详细信息
    """
    table_name = f"{topo}_monitor"
    with redis_context(user) as user_db_cli:
        try:
            user_db_cli.del_value(table_name, expr)
            user_db_cli.set_value(table_name, expr, monitor_info)
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'监控服务更新失败:{e}'}
        else:
            #日志输出
            logger = UserLogger(user, UserLogLevel.Second, topo)
            logger.log_to_mysql(f'更新监控服务{expr}')
            
            return {'code': 1, 'msg': f'监控服务:{expr} 更新成功'}


def create_monitor_events(user, topo, monitor_events_dict):
    """
    在redis中创建用户拓扑的一个或多个监控服务
    user: 用户名
    topo: 拓扑名
    monitor_events_dict: key：value - 监控服务名：监控服务信息
    :return: code 0：失败，1：成功；msg：详细信息;
    """
    table_name = f'{topo}_monitor'
    exist_monitor_list = []
    create_monitor_list = []

    print(check_table_key(user, "topo_list", topo))
    if not check_table_key(user, "topo_list", topo):
        return {'code': 0, 'msg': f'监控服务创建失败，已创建项目[{topo}]不存在！'}
    
    # 检查需要创建的监控服务名是否已存在
    for key, value in monitor_events_dict.items():
        if check_table_key(user, table_name, key) or DataServerManager.check_expr_name(user, topo, key) == 0:
            exist_monitor_list.append(key)
        else:
            create_monitor_list.append(key)
    for k in exist_monitor_list:
        del monitor_events_dict[k]
    monitors_exist = ','.join(exist_monitor_list)
    monitors_create = ','.join(create_monitor_list)
    with redis_context(user) as user_db_cli:
        try:
            user_db_cli.set_all_values(table_name, monitor_events_dict)
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'监控服务创建失败:{e}'}
        else:
            if monitors_exist and monitors_create:
                return {'code': 0, 'msg': f'监控服务:{monitors_exist}已存在，创建失败！  监控服务：{monitors_create}创建成功！'}
            elif monitors_create:
                
                #日志输出
                logger = UserLogger(user, UserLogLevel.Second, topo)
                logger.log_to_mysql(f'创建监控服务{monitors_create}')

                return {'code': 1, 'msg': f'监控服务：{monitors_create}创建成功！'}
            elif monitors_exist:
                return {'code': 0, 'msg': f'监控服务:{monitors_exist}已存在，创建失败！'}


def retrieve_monitor_events(user, topo):
    """
    在redis中获得用户拓扑的所有监控服务信息
    user: 用户名
    topo: 拓扑名
    :return: code 0：失败，1：成功；msg：详细信息;
             monitors: 监控服务字典如{'expr1':[], 'expr2':[]}
    """
    table_name = f"{topo}_monitor"
    with redis_context(user) as user_db_cli:
        try:
            monitors_dict = user_db_cli.get_all_values(table_name)
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'监控服务获取失败:{e}'}
        else:
            return {'code': 1, 'msg': '监控服务获取成功', 'monitors': monitors_dict}

def delete_monitor_event(user, topo, expr = None):
    """
    在redis中删除监控服务
    user: 用户名
    topo: 拓扑名
    expr: 监控服务名（标识）
    :return: code 0：失败，1：成功；msg：详细信息
    """
    table_name = f"{topo}_monitor"
    with redis_context(user) as user_db_cli:
        try:
            if expr == None:
                delete_monitor_data(user, topo)
                user_db_cli.del_all_values(table_name)
            else:
                delete_monitor_data(user, topo, expr)
                user_db_cli.del_value(table_name, expr)
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'监控服务删除失败:{e}'}
        else:
            # 日志输出
            logger = UserLogger(user, UserLogLevel.Second, topo)
            logger.log_to_mysql(f'删除监控服务{expr}')
            return {'code': 1, 'msg': f'监控服务:{expr} 删除成功'}

def retrieve_link_monitor_events(user_name, project_name):
    """
    在redis中获得用户拓扑的链路监控服务信息
    user: 用户名
    topo: 拓扑名
    :return: code 0：失败，1：成功；msg：详细信息;
            link_monitors: 链路监控服务字典
    """
    with redis_context(user_name) as user_db_cli:
        try:
            link_monitors_dict = user_db_cli.get_all_values(f'{project_name}_Linkmonitor')
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'链路监控服务获取失败:{e}'}
        else:
            return {'code': 1, 'msg': '链路监控服务获取成功', 'link_monitors': link_monitors_dict}  

def retrieve_newtraffic_events(user_name, project_name):
    """
    在redis中获得用户拓扑的新流量发生器配置信息
    user: 用户名
    topo: 拓扑名
    :return: code 0：失败，1：成功；msg：详细信息;
            link_monitors: 链路监控服务字典
    """
    with redis_context(user_name) as user_db_cli:
        try:
            newtraffic_configs = user_db_cli.get_all_values(f'{project_name}_newtraffic_configs')
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'链路监控服务获取失败:{e}'}
        else:
            return {'code': 1, 'msg': '链路监控服务获取成功', 'newtraffic_configs': newtraffic_configs}      
# def delete_monitor_events(user, topo):
#     """
#     在redis中删除用户拓扑的多个监控服务
#     user: 用户名
#     topo: 拓扑名
#     :return: code 0：失败，1：成功；msg：详细信息;
#     """
#     table_name = f'{topo}_monitor'
#     with redis_context(user) as user_db_cli:
#         try:
#             user_db_cli.del_all_values(table_name)
#         except Exception as e:
#             print(e)
#             return {'code': 0, 'msg': f'所有监控服务删除失败:{e}'}
#         else:
#             return {'code': 1, 'msg': '所有监控服务删除成功'}


def create_all_traffic(user, topo, traffic_info):
    """
    在redis中批量创建流量服务信息

    Args:
        user: 用户名
        topo: 拓扑名
        app:  流量服务名
        traffic_info: 多个app的流量信息
    Returns: 
        code 0：失败，1：成功；msg：详细信息
    """
    table_name = f"{topo}_traffic"
    exist_traffic_list = []
    create_traffic_list = []

    if not check_table_key(user, "topo_list", topo):
        return {'code': 0, 'msg': f'流量服务创建失败，已创建项目[{topo}]不存在！'}

    # 判断流量服务是否已经在数据库
    for key in traffic_info.keys():
        if check_table_key(user, table_name, key):
            exist_traffic_list.append(key)
        else:
            create_traffic_list.append(key)
    for key in exist_traffic_list:
        del traffic_info[key]
    traffic_exist = ','.join(exist_traffic_list)
    traffic_create = ','.join(create_traffic_list)
    with redis_context(user) as user_db_cli:
        try:
            user_db_cli.set_all_values(table_name, traffic_info)
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'流量服务添加失败:{e}'}
        else:
            if traffic_exist and traffic_create:
                return {'code': 0, 'msg': f'流量服务:{traffic_exist}已存在，创建失败！ 流量服务：{traffic_create}创建成功！'}
            elif traffic_create:
                # 日志输出
                logger = UserLogger(user, UserLogLevel.Second, topo)
                logger.log_to_mysql(f'创建流量服务{traffic_create}')

                return {'code': 1, 'msg': f'流量服务：{traffic_create}创建成功！'}
            elif traffic_exist:
                return {'code': 0, 'msg': f'流量服务:{traffic_exist}已存在，创建失败！'}

def create_traffic_template(user, topo, traffic_info):
    """
    在redis中批量创建流量服务信息

    Args:
        user: 用户名
        topo: 拓扑名
        traffic_info: 多个app的流量信息
    Returns: 
        code 0：失败，1：成功；msg：详细信息
    """
    table_name = f"{topo}_template"
    exist_traffic_list = []
    create_traffic_list = []

    if not check_table_key(user, "topo_list", topo):
        return {'code': 0, 'msg': f'流量服务创建失败，已创建项目[{topo}]不存在！'}

    # 判断流量服务是否已经在数据库
    for key in traffic_info.keys():
        if check_table_key(user, table_name, key):
            exist_traffic_list.append(key)
        else:
            create_traffic_list.append(key)
    for key in exist_traffic_list:
        del traffic_info[key]
    traffic_exist = ','.join(exist_traffic_list)
    traffic_create = ','.join(create_traffic_list)
    with redis_context(user) as user_db_cli:
        try:
            user_db_cli.set_all_values(table_name, traffic_info)
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'流量模板添加失败:{e}'}
        else:
            if traffic_exist and traffic_create:
                return {'code': 0, 'msg': f'流量模板:{traffic_exist}已存在，创建失败！ 流量模板：{traffic_create}创建成功！'}
            elif traffic_create:
                # 日志输出
                logger = UserLogger(user, UserLogLevel.Second, topo)
                logger.log_to_mysql(f'创建流量模板{traffic_create}')

                return {'code': 1, 'msg': f'流量模板：{traffic_create}创建成功！'}
            elif traffic_exist:
                return {'code': 0, 'msg': f'流量模板:{traffic_exist}已存在，创建失败！'}

def delete_all_traffic(user, topo):
    """
    在redis中删除所有流量服务

    Args:
        user: 用户名
        topo: 拓扑名
    Returns: 
        code 0：失败，1：成功；msg：详细信息
    """
    table_name = f"{topo}_traffic"
    with redis_context(user) as user_db_cli:
        try:
            user_db_cli.del_table(table_name)
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'流量所有应用删除失败:{e}'}
        else:
            # 日志输出
            logger = UserLogger(user, UserLogLevel.Second, topo)
            logger.log_to_mysql(f'删除所有流量服务')

            return {'code': 1, 'msg': '流量所有应用删除成功'}

def delete_all_template(user, topo):
    """
    在redis中删除所有流量模板

    Args:
        user: 用户名
        topo: 拓扑名
    Returns: 
        code 0：失败，1：成功；msg：详细信息
    """
    table_name = f"{topo}_template"
    with redis_context(user) as user_db_cli:
        try:
            user_db_cli.del_table(table_name)
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'流量所有模板删除失败:{e}'}
        else:
            # 日志输出
            logger = UserLogger(user, UserLogLevel.Second, topo)
            logger.log_to_mysql(f'删除所有流量模板')

            return {'code': 1, 'msg': '流量所有模板删除成功'}

def delete_traffic_app(user, topo, app):
    """
    在redis中删除流量服务

    Args:
        user: 用户名
        topo: 拓扑名
        app: 流量服务名
    Returns: 
        code 0：失败，1：成功；msg：详细信息
    """
    if app.strip() == "":
        return {'code': 0, 'msg': '流量服务的名字不能为空'}
    table_name = f"{topo}_traffic"
    with redis_context(user) as user_db_cli:
        try:
            user_db_cli.del_value(table_name, app)
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'流量服务删除失败:{e}'}
        else:
            # 日志输出

            logger = UserLogger(user, UserLogLevel.Second, topo)
            logger.log_to_mysql(f'删除流量服务{app}')
            return {'code': 1, 'msg': '流量服务删除成功'}

def delete_traffic_template(user, topo, app):
    """
    在redis中删除流量模板

    Args:
        user: 用户名
        topo: 拓扑名
        app: 流量模板名
    Returns: 
        code 0：失败，1：成功；msg：详细信息
    """
    if app.strip() == "":
        return {'code': 0, 'msg': '流量模板的名字不能为空'}
    table_name = f"{topo}_template"
    with redis_context(user) as user_db_cli:
        try:
            user_db_cli.del_value(table_name, app)
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'流量模板删除失败:{e}'}
        else:
            # 日志输出

            logger = UserLogger(user, UserLogLevel.Second, topo)
            logger.log_to_mysql(f'删除流量模板{app}')
            return {'code': 1, 'msg': '流量模板删除成功'}

def retrieve_all_traffic(user, topo):
    """
    在redis中获得用户拓扑的所有应用信息

    Args:
        user: 用户名
        topo: 拓扑名
    Returns: 
        code 0：失败，1：成功；msg：详细信息
    """
    table_name = f"{topo}_traffic"
    with redis_context(user) as user_db_cli:
        try:
            traffic_info = user_db_cli.get_all_values(table_name)
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'流量服务获取失败:{e}'}
        else:
            return {'code': 1, 'msg': '流量服务获取成功', 'traffic_info': traffic_info}


def retrieve_traffic_app(user, topo, app):
    """
    在redis中获得用户拓扑的指定应用信息
    traffic_info: 流量服务字典如{'app1':[], 'app2':[]}
            
    Args:
        user: 用户名
        topo: 拓扑名
    Returns: 
        code 0：失败，1：成功；msg：详细信息
    
    """
    if app.strip() == "":
        return {'code': 0, 'msg': '流量服务的名字不能为空'}
    table_name = f"{topo}_traffic"
    with redis_context(user) as user_db_cli:
        try:
            traffic_info = user_db_cli.get_value(table_name, app)
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'流量服务获取失败:{e}'}
        else:
            return {'code': 1, 'msg': '流量服务获取成功', 'traffic_info': traffic_info}


def update_traffic_app(user, topo, app, traffic_info):
    """
    在redis中更新流量服务信息

    Args:
        user: 用户名
        topo: 拓扑名
        app: 流量服务名
        traffic_info: 流量服务的信息
    Returns: 
        code 0：失败，1：成功；msg：详细信息
    """
    if app.strip() == "":
        return {'code': 0, 'msg': '流量服务的名字不能为空'}
    table_name = f"{topo}_traffic"
    app_info = traffic_info.get(app, None)
    with redis_context(user) as user_db_cli:
        try:
            user_db_cli.del_value(table_name, app)
            user_db_cli.set_value(table_name, app, traffic_info[app])
        except Exception as e:
            print(e)
            return {'code': 0, 'msg': f'流量服务更新失败:{e}'}
        else:
            # 日志输出

            logger = UserLogger(user, UserLogLevel.Second, topo)
            logger.log_to_mysql(f'更新流量服务{app}')
            return {'code': 1, 'msg': '流量服务更新成功'}


def retrieve_project_json(user_name, project_name):
    """
    获取已创建项目json

    user_name: 用户名
    project_name: 项目名（拓扑名）
    
    :return: code 0：失败，1：成功；msg：详细信息；project: 项目json, 例：
        {
            "code": 1, 
            "msg": "success",
            "project": {
                "topo": topo_json,
                "traffics": traffic_json,
                "monitors": monitor_json,
            }
            "status": {
                "topo": 0/1,
                "traffics": {
                    "traffic_event1": 0/1,
                    ...
                },
                "monitors": {
                    "monitor_event1": 0/1,
                    ...
                }
            }
        }
    """
    topo = retrieve_topo(user_name, project_name)
    if topo["code"] == 0:
        return {
            "code": 0, 
            "msg": f"从redis获取拓扑失败。错误信息：{topo['msg']}",
            "project": {}
            }
    topo = topo["networks"]
    
    traffics = retrieve_all_traffic(user_name, project_name)
    if traffics["code"] == 0:
        return {
            "code":0, 
            "msg":f"从redis获取流量信息失败。错误信息：{traffics['msg']}",
            "project": {},
            "status":{}
            }
    traffics = traffics["traffic_info"]
    
    monitors = retrieve_monitor_events(user_name, project_name)
    if monitors["code"] == 0:
        return {
            "code":0, 
            "msg":f"从redis获取监控信息失败。错误信息：{monitors['msg']}",
            "project": {},
            "status": {}
            }
    monitors = monitors["monitors"]

    # 新增的链路监控
    link_monitors = retrieve_link_monitor_events(user_name, project_name)
    if link_monitors["code"] == 0:
        return {
            "code":0, 
            "msg":f"从redis获取监控信息失败。错误信息：{link_monitors['msg']}",
            "project": {},
            "status": {}
            }
    link_monitors = link_monitors["link_monitors"]

    # 新增的新流量发生器
    newtraffics = retrieve_newtraffic_events(user_name, project_name)

    if newtraffics["code"] == 0:
        return {
            "code":0, 
            "msg":f"从redis获取监控信息失败。错误信息：{link_monitors['msg']}",
            "project": {},
            "status": {}
            }
    newtraffics = newtraffics["newtraffic_configs"]

    result = {
        "code": 1, 
        "msg": "success",
        "project": {
            "topo": topo,
            "traffics": traffics,
            "monitors": monitors,
            "link_monitors":link_monitors,
            "newtraffics":newtraffics
        },
        "status": {}
    }

    try:
        result["status"] = get_project_status(user_name, project_name, result)
    except Exception as e:
        result = {
            "code": 2, 
            "msg": (f"获取项目描述信息成功, 但获取项目运行信息失败，"
                    f"error msg: {str(e)}"),
            "project": {
                "topo": topo,
                "traffics": traffics,
                "monitors": monitors,
                "link_monitors":link_monitors,
                "newtraffics":newtraffics
            },
            "status": {}
        }
    return result

def get_monitor_event_types(user, project_name, expr):
    '''
    获取某次监控服务的每种性能指标对应的子事件

    Args:
        user: 用户名
        project_name：项目名
        expr: 要获取所有性能指标的监控服务名
        
    Returns:
        type2subevent: 每种性能指标对应的子事件
            {
                "吞吐/时延/丢包":[
                    子事件(subevent x),
                    ...
                ],
                ...
            }
            例：


    Raises:
        ValueError: 监控服务不存在
    '''
    with redis_context(user) as user_db_cli:
        if not check_table_key(user, f'{project_name}_monitor', expr):
            print(f'监控服务{expr}不存在！')
            raise ValueError(f'监控服务{expr}不存在！')
        monitor_event = user_db_cli.get_value(
            f'{project_name}_monitor', expr)
        '''
        monitor_event:
        [
            {
                'params': {
                    'dst': {
                        'ne_name': 'h1',
                        'nic_ip': '192.168.1.1',
                        'port': ''
                    },
                    'proto_type': 'tcp',
                    'src': {
                        'ne_name': 'h2',
                        'nic_ip': '192.168.1.2',
                        'port': ''
                    }
                },
                'performance': 'throughput'
            },
            ...
        ]
        '''
        type2subevent = {}
        for sub_event_seq, sub_event in enumerate(monitor_event):
            type2subevent.setdefault(sub_event["performance"], [])
            # 子事件序号从1开始
            type2subevent[sub_event["performance"]].append(sub_event_seq+1)
            if sub_event["performance"] == "throughput":
                # 吞吐较为特殊
                type2subevent.setdefault("total_throughput", [])
                type2subevent["total_throughput"].append(sub_event_seq+1) 
        
        return type2subevent

def is_monitor_running(user, topo, expr):
    '''
    通过检查表项的方式判断监控是否在运行

    Args:
        user: 用户名
        topo: 拓扑名
        expr: 实验名
    Returns:
        True: 监控在运行
        False: 监控不在运行
    '''
    table = f'{topo}_{expr}_monitor'
    return check_table_existence(user, table)

def is_traffic_running(user, topo, traffic_name):
    '''
    通过检查表项的方式判断流量是否在运行

    Args:
        user: 用户名
        topo: 拓扑名
        traffic_name: 流量服务名
    Returns:
        True: 流量服务在运行
        False: 流量服务不在运行
    '''
    table = f'{topo}_{traffic_name}_to_worker'
    return check_table_existence(user, table)

def get_project_status(user, project_name, project_json):
    '''
    通过检查表项的方式判断项目（拓扑、流量、监控）是否在运行

    ！无需检查拓扑是否运行，能进到此函数，拓扑一定是运行的\n
    ！如需单独调用本接口，需加入对拓扑运行的检查

    Args:
        user: 用户名
        project_name: 项目名
        project_json: 项目json

    Returns:
        status: 一个字典，内容为
        {
            "topo": 1,
            "traffics": {
                "traffic_event1": 0/1,
                ...
            },
            "monitors": {
                "monitor_event1": 0/1,
                ...
            }
        }
    '''
    def _check_table_exist(pipe, table_name):
        # 检查数据表是否存在
        return 1 if pipe.exists(table_name) else 0

    status = {
        "topo": 1,
        "traffics": {},
        "monitors": {},
        "link_monitors": {},
        "newtraffics": {}
    }

    with redis_context(user) as user_db_cli:
        # 采用批量运行的方式，节约通信开销
        pipe = user_db_cli._db_conn.pipeline()
        
        # 无需检查拓扑是否运行，能进到此函数，拓扑一定是运行的
        # 如需单独调用本接口，需加入对拓扑运行的检查

        # 检查流量是否运行
        traffics = []
        for traffic in project_json["project"]["traffics"].keys():
            # 记录字典的遍历顺序，无论字典实现上是否有序，使用时都应假设字典是无序的。
            # 见 https://www.zhihu.com/question/289090287
            traffics.append(traffic)
            table_name = f'{project_name}_{traffic}_to_worker'
            pipe.exists(table_name)

        # 检查监控是否运行
        monitors = []
        for monitor in project_json["project"]["monitors"].keys():
            monitors.append(monitor)
            table_name = f'{project_name}_{monitor}_pcap_process'
            pipe.exists(table_name)
        
        link_monitors = []
        for link_monitor in project_json["project"]["link_monitors"].keys():
            link_monitors.append(link_monitor)

        newtraffics = []
        for newtraffic in project_json["project"]["newtraffics"].keys():
            newtraffics.append(newtraffic)

        # 分析批量执行命令结果
        execute_result = pipe.execute()

        i = 0

        for newtraffic in newtraffics:
            if project_json["project"]["newtraffics"][newtraffic]['running']:
                status['newtraffics'][newtraffic] = 1
            else:
                status['newtraffics'][newtraffic] = 0

        for link_monitor in link_monitors:
            if project_json["project"]["link_monitors"][link_monitor]['running']:
                status['link_monitors'][link_monitor] = 1
            else:
                status['link_monitors'][link_monitor] = 0

        for traffic in traffics:
            status["traffics"][traffic] = execute_result[i]
            i += 1
        
        for monitor in monitors:
            status["monitors"][monitor] = execute_result[i]
            i += 1

        return status

def get_out_link(user):
    try:
        with redis_context(user) as user_db_cli:
            out_links = user_db_cli.get_all_values("out_link")
        return out_links
    except Exception as e:
        raise e

def get_worker_ip(user, table_name):
    try:
        with redis_context(user) as user_db_cli:
            subtopo = user_db_cli.get_value(table_name, 'NEloc')
            worker_ip = user_db_cli.get_value('subtopo2worker', subtopo)
        return worker_ip
    except Exception as e:
        raise e

def delete_value(user, table_name, key):
    try:
        with redis_context(user) as user_db_cli:
            user_db_cli.del_value(table_name, key)
            return True
    except Exception as e:
        raise e