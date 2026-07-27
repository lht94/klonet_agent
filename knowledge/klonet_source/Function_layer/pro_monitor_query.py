import requests
import time
import copy
from pprint import pprint
from ..vemu_config.config import PROJ_CONFIG
from ..Service_layer.redisAPI import UserMapRedis

# from redisAPI import UserMapRedis
# import json

# Prometheus所在主机的IP和端口
MASTER_IP = PROJ_CONFIG.master_ip
PROMETHEUS_PORT = PROJ_CONFIG.prometheus_port
CONFIG = {"PROMETHEUS_URL": 'http://' + MASTER_IP + ':' + str(PROMETHEUS_PORT)}

# 查询语句字典,每一个列表本可以写多个表达式以实现多个表达式同时查询。但为了查询和绘制便利，暂且仅写一个。
# 宿主机的常用查询
QUERY_DICT = {
    # 宿主机节点查询
    'node_cpu': ['100 - avg(irate(node_cpu_seconds_total{mode="idle"}[1m])) by (instance) * 100', ],  # 单位：%
    'node_mem': ['(node_memory_MemTotal_bytes{ } - node_memory_MemAvailable_bytes{ }) / (1024 * 1024)', ],  # 单位：Mbytes
    'node_load': ['node_load1{ }', ],  # 'node_load5{ }', 'node_load15{ }'],  单位：无
}

# 用户拓扑节点的常用查询
USER_TOPO_QUERY_DICT = {
    # 原表达式:表示容器在过去10s内，在每个CPU内的核上的累积占用时间平均值 (单位：秒)；函数计算后，单位：%
    "cpu": "sum(rate(container_cpu_usage_seconds_total{name=\"<name>\", name!=\"\"}[20s])) by (name) * 100", 
    # 原表达式:表示容器当前的内存使用量（单位：bytes），换算后，单位：Mbytes
    "mem": "container_memory_usage_bytes{name=\"<name>\", name!=\"\"} / (1024 * 1024)",
    "traffic_sent": "irate(container_network_transmit_bytes_total{name!=\"\",name=\"<name>\"}[1m])*8",
    "traffic_received": "irate(container_network_receive_bytes_total{name!=\"\",name=\"<name>\"}[1m])*8"
}

# 其他Prometheus查询
OTHER_QUERY_DICT = {
    # 一、宿主机相关Prometheus表达式
    # host_boot_time:运行时间，单位：s
    # host_cpu_cores:CPU核数，单位：个
    # host_total_mem:内存总量，单位：Mbytes
    # host_mem_usage:内存使用率，单位：%
    # host_disk_write:磁盘写速率，单位：iops
    # host_disk_read:磁盘读速率，单位：iops
    # host_net_download:节点流量下载速率，单位：bps
    # host_net_upload:网络流量上传速率，单位：bps
    "host_boot_time": ["sum(time() - node_boot_time_seconds{ }) by (instance)"],
    "host_cpu_cores": ["count(node_cpu_seconds_total{mode='system'}) by (instance)"],
    "host_total_mem": ["sum(node_memory_MemTotal_bytes{ }) by (instance) / 1024 / 1024"],
    "host_mem_usage": ["(1 - (node_memory_MemAvailable_bytes{ } / (node_memory_MemTotal_bytes{ })))* 100"],
    "host_disk_write": ["irate(node_disk_writes_completed_total{ }[1m])"],
    "host_disk_read": ["irate(node_disk_reads_completed_total{ }[1m])"],
    "host_net_download": ["irate(node_network_receive_bytes_total{device!~'tap.*|veth.*|br.*|docker.*|virbr*|lo*'}[30m]) *8"],
    "host_net_upload": ["irate(node_network_transmit_bytes_total{device!~'tap.*|veth.*|br.*|docker.*|virbr*|lo*'}[30m])*8"],
    # 二、容器相关
    # container_num: 容器数量，单位：个
    # container_transmit：容器发送流量，单位：bps
    # container_receive: 容器接受流量，单位：bps
    "container_num": "count(rate(container_last_seen{id=~\"/docker/.*\"}[5m])) by (instance)",
    "container_transmit": "irate(container_network_transmit_bytes_total{name!=\"\",name=\"<name>\"}[1m])*8",
    "contaienr_received": "irate(container_network_receive_bytes_total{name!=\"\",name=\"<name>\"}[1m])*8"

}

user_map_redis = UserMapRedis()


def timestamp_to_date(timestamp, format_string="%Y-%m-%d,%H:%M:%S"):
    local_time = time.localtime(timestamp)
    date = time.strftime(format_string, local_time)
    return date


def date_to_timestamp(date, format_string="%Y-%m-%d %H:%M:%S"):
    '''
    @description: 将日期“%Y-%m-%d %H:%M:%S”转换为格林威治时间戳
    @param:
    :param date: 日期
    :param format_string: 输入日期的格式
    @return: 格林威治时间戳
    '''
    time_array = time.strptime(date, format_string)
    time_stamp = int(time.mktime(time_array))
    return time_stamp


def get_instant_metric_data(config, metric_list) -> list:
    '''
        输入:
            config:访问prometheus服务器的配置
            metric_list:指标列表
        输出：
            metric_datas:未处理格式的指标数据
        功能描述：
            利用Prometheus的HTTP API获取当前时刻的时序数据
    '''
    metric_datas = []
    query_time = time.time()

    for m in metric_list:
        try:
            params = {
                "query": m,
                "time": query_time
            }
            url = config["PROMETHEUS_URL"] + "/api/v1/query"
            response = requests.get(url, params=params)
            if response.status_code == 200:
                res = response.json()
                if res and res.get('status') == 'success':
                    datas = res.get('data', {}).get('result', [])
                    metric_datas.extend(datas)
        except Exception as e:
            print(e)

    return metric_datas


def get_range_metric_data(config, metric_dict, start_time, end_time, step):        
    '''
    @description: 利用Prometheus的HTTP API获取一个时间段内的时序数据
    @param:
    :param config: 访问prometheus服务器的配置
    :param metric_dict: 指标字典
    :param start_time: 时间段查询的开始时间
    :param end_time: 时间段查询的结束时间
    :param step: 时间段查询的步长
    @return: 未处理格式的指标数据
    '''    
    metric_datas = []

    # end_time = time.time()
    # start_time = end_time - float(history_len) # 300s数据

    for m in metric_dict:
        try:
            params = {
                "query": m,
                "start": start_time,
                "end": end_time,
                "step": step + 's', # 数据步长
            }
            url = config["PROMETHEUS_URL"] + "/api/v1/query_range"
            response = requests.get(url, params=params)
            if response.status_code == 200:
                res = response.json()
                if res and res.get('status') == 'success':
                    datas = res.get('data', {}).get('result', [])
                    metric_datas.extend(datas)
        except Exception as e:
            print(e)

    return metric_datas


def query_host_info_metric(metric, **time_args):
    """
    用于查询宿主机的cpu、内存情况
    Args:
        metric: 查询指标 cpu/mem
        time_args: 历史数据查询需要的时间段信息

    Returns:
        result: 宿主机的性能情况
    """
    # print(worker_avail_mem)
    query_list = []
    if metric == "cpu":
        query_list.append(QUERY_DICT['node_cpu'])
    elif metric == "mem":
        query_list.append(QUERY_DICT['node_mem'])
    elif metric == "load":
        query_list.append(QUERY_DICT['node_load'])
    if time_args == dict():
        raw_host_info = get_instant_metric_data(CONFIG, query_list)
        result = handle_host_info(raw_host_info, "available_" + metric, "instant")
    else:
        start_time = time_args["start_time"]
        end_time = time_args["end_time"]
        step = time_args["step"]
        start_timestamp = date_to_timestamp(start_time)
        end_timestamp = date_to_timestamp(end_time)
        raw_host_info = get_range_metric_data(CONFIG, query_list, start_timestamp, end_timestamp, step)
        pprint(raw_host_info)
        result = handle_host_info(raw_host_info, "available_" + metric, "range")
        pprint(result)
    return result


def query_all_ne_info_metric(user, topo, metric_name, **time_args):
    """
    用于查询特定用户特定拓扑下所有节点的资源使用情况,
    调用get_instant_metric_data获取当前时刻的资源使用情况
    Args:
        user: 用户名 string
        topo: 拓扑名  string
        metric_name: 查询指标类型(cpu/mem/traffic_sent/traffic_received)
        time_args: 时间段查询需要的start_time、end_time、step字典 dict,
                   若非时间段查询，则为空
    
    Returns:
        ne_info: 用户该拓扑下所有节点的特定指标情况，由metric_name决定 dict
    Examples:
        {
            'h1': {
                'value': '0'
            }
            's1': {
                'value': '0.4413776610710877'
            },
            's2': {
                'value': '0.5077254201762847'
            }
            ...
        }
    """
    id_name_dict = get_user_topo_all_ne(user, topo)
    query_list = []
    if metric_name == "cpu":
        for ne_id in id_name_dict:
            query_list.append(USER_TOPO_QUERY_DICT["cpu"].replace("<name>", ne_id))
    elif metric_name == "mem":
        for ne_id in id_name_dict:
            query_list.append(USER_TOPO_QUERY_DICT["mem"].replace("<name>", ne_id))
    elif metric_name == "traffic_sent":
        for ne_id in id_name_dict:
            query_list.append(USER_TOPO_QUERY_DICT["traffic_sent"].replace("<name>", ne_id))
    elif metric_name == "traffic_received":
        for ne_id in id_name_dict:
            query_list.append(USER_TOPO_QUERY_DICT["traffic_received"].replace("<name>", ne_id))
    start_time = time_args.get("start_time", 0)
    end_time = time_args.get("end_time", 0)
    step = time_args.get("step", 0)
    if all([start_time, end_time, step]):
        # 全不为0, 查询时间段
        start_timestamp = date_to_timestamp(start_time)
        end_timestamp = date_to_timestamp(end_time)
        ne_info = get_range_metric_data(CONFIG, query_list, start_timestamp, end_timestamp, step)
        ne_info = handle_container_info(ne_info, metric_name, id_name_dict, "range")
    elif not any([start_time, end_time, step]):
        # 全为0，则查询当前时刻
        ne_info = get_instant_metric_data(CONFIG, query_list)
        ne_info = handle_container_info(ne_info, metric_name, id_name_dict, "instant")
    return ne_info

def query_topo_info_metric(user, topo, metric_list):
    """
    用于查询特定用户特定拓扑下所有节点的资源使用情况,
    调用get_instant_metric_data获取当前时刻的资源使用情况
    Args:
        user: 用户名 string
        topo: 拓扑名  string
        metric_name: 查询指标类型(cpu/mem/traffic_sent/traffic_received)
    
    Returns:
        topo_info: 用户该拓扑下所有节点的特定指标情况，由metric_name决定 dict
    Examples:
        {
            'topo1': {
                'value': 100
            }
        }
    """
    topo_info = {
        topo: {
            'cpu': '',
            'mem': ''
        }
    }
    for metric_name in metric_list:
        id_name_dict = get_user_topo_all_ne(user, topo)
        query_list = []
        if metric_name == "cpu":
            for ne_id in id_name_dict:
                query_list.append(USER_TOPO_QUERY_DICT["cpu"].replace("<name>", ne_id))
        elif metric_name == "mem":
            for ne_id in id_name_dict:
                query_list.append(USER_TOPO_QUERY_DICT["mem"].replace("<name>", ne_id))
        elif metric_name == "traffic_sent":
            for ne_id in id_name_dict:
                query_list.append(USER_TOPO_QUERY_DICT["traffic_sent"].replace("<name>", ne_id))
        elif metric_name == "traffic_received":
            for ne_id in id_name_dict:
                query_list.append(USER_TOPO_QUERY_DICT["traffic_received"].replace("<name>", ne_id))
        ne_info = get_instant_metric_data(CONFIG, query_list)
        metric_used = sum_container_info(ne_info)
        topo_info[topo][metric_name] = metric_used
    return topo_info


def query_one_ne_info_metric(user, topo, ne_name, metric_name, **time_args):
    """
    用于查询特定用户特定拓扑下某个节点的资源使用情况
    Args:
        user: 用户名 string
        topo: 拓扑名  string
        ne_name: 节点名 string
        metric_name: 查询指标类型(cpu/mem/traffic_sent/traffic_received)
        time_args: 时间段查询需要的start_time、end_time、step字典 dict
                   若非时间段查询，则为空
    
    Returns:
        ne_info: 用户该拓扑下所有节点的特定指标情况，由metric_name决定 dict
    Example:
        {
            "h1": {'value': '0'}
        }
    """
    ne_id = get_user_topo_one_ne(user, topo, ne_name)
    # print("ne_id:", ne_id)
    id_name_dict = {ne_id: ne_name}
    query_list = [USER_TOPO_QUERY_DICT[metric_name].replace("<name>", ne_id)]
    start_time = time_args.get("start_time", 0)
    end_time = time_args.get("end_time", 0)
    step = time_args.get("step", 0)
    if all([start_time, end_time, step]):
        # 全不为0
        start_timestamp = date_to_timestamp(start_time)
        end_timestamp = date_to_timestamp(end_time)
        # TODO(sw): 一次查不了太多数据，可能需要一个小时查一次，然后合并
        ne_info = get_range_metric_data(CONFIG, query_list, start_timestamp, end_timestamp, step)
        # print("raw_ne_info:", ne_info)
        ne_info = handle_container_info(ne_info, metric_name, id_name_dict, "range")
    elif not any([start_time, end_time, step]):
        ne_info = get_instant_metric_data(CONFIG, query_list)
        ne_info = handle_container_info(ne_info, metric_name, id_name_dict, "instant")
    if ne_info == list():
        print("NO SUCH CONTAINER.MAYBE CONTAINER IS DOWN.")
        # raise ValueError("NO SUCH CONTAINER.MAYBE CONTAINER IS DOWN.")
    return ne_info


def query_ne_info(user, topo, ne_name, metric_list, **time_args):
    all_metric_info = {}
    # metric_dict = {
    #     "cpu": "", 
    #     "mem": "", 
    #     "traffic_sent": "", 
    #     "traffic_received": ""
    # }
    metric_dict = {
        "cpu": "", 
        "mem": ""
    }
    # 不为空，特定查询
    if ne_name != "":
        for metric in metric_list:
            one_metric_info = query_one_ne_info_metric(
                user, topo, ne_name, metric, **time_args)
            print(one_metric_info)
            for container in one_metric_info:
                container_info = all_metric_info.setdefault(
                    container, copy.deepcopy(metric_dict))
                container_info[metric] = one_metric_info[container]["value"]
    # 为空则所有节点查询
    else:
        for metric in metric_list:
            one_metric_info = query_all_ne_info_metric(
                user, topo, metric, **time_args)
            print(one_metric_info)
            for container in one_metric_info:
                container_info = all_metric_info.setdefault(
                    container, copy.deepcopy(metric_dict))
                container_info[metric] = one_metric_info[container]["value"]
    return all_metric_info


def query_host_info(metric_list, **time_args):
    all_host_info = {}
    metric_dict = {"cpu": "", "mem": "", "load": ""}
    for metric in metric_list:
        one_metric_info = query_host_info_metric(metric, **time_args)
        for host in one_metric_info:
            # print("host:", host, "value:", one_metric_info[host]["value"])
            print("metric_dict:", metric_dict)
            # 要使用深拷贝，否则会指向同一个对象metric_dict
            host_info = all_host_info.setdefault(host, copy.deepcopy(metric_dict))
            host_info[metric] = one_metric_info[host]["value"]
    return all_host_info
    

def query_other_metrics(metric_name, container_info={}):
    """
    用于查询OTHER_QUERY_DICT中的指标信息
    Args:
        metric_name: 指标名 string
        container_info: 如果是容器相关的查询，则传入容器所在的拓扑信息  dict
    
    Returns:
        final_datas: 查询结果 dict
    """
    # final_datas = {
    #    'metirc_datas': {
    #        'data0': {
    #            'label': {xxx}, # 不同指标label不同,通过label反映相同指标查询的不同对象(如不同容器) dict
    #            'name':'data0', # 返回数据的计数名，即data0指第一个数据 string
    #            'time':xxx, # 查询指标的时间 string
    #            'value':xxx, # 数据的 string
    #         },
    #        'data1': {}
    #        ...
    #    },
    #    'metric_name': xxx, # 查询的指标名
    # }
    if "host" in metric_name:
        metric_list = OTHER_QUERY_DICT[metric_name]
    elif "container" in metric_name and metric_name != "container_num":
        user = container_info['user']
        topo = container_info['topo']
        ne_name = container_info['ne_name']
        ne_id = get_user_topo_one_ne(user, topo, ne_name)
        metric_list = [OTHER_QUERY_DICT[metric_name].replace("<name>", ne_id)]
    elif metric_name == "container_num":
        metric_list = [OTHER_QUERY_DICT[metric_name]]
    metric_datas = get_instant_metric_data(CONFIG, metric_list)
    final_datas = json_change(metric_name, metric_datas)
    pprint(final_datas)
    return final_datas


def get_user_topo_all_ne(user, topo):
    """
    从Redis数据库中获取某个用户某个拓扑的节点id（雪花算法得到的节点ID）
    Args:
        user: 用户名 string
        topo: 拓扑名  string
    
    Returns:
        id_name_dict： 节点id(uuid)与节点名的对应字典
    """
    user_db_cli = user_map_redis.get_user_db(user)
    user_map_redis.close()
    # 从plane_topo_list中找到该拓扑所有节点名，并根据此找到uuid
    plane_topo_list = user_db_cli.get_value("plane_topo_list", topo)
    ne_list = plane_topo_list["NEs"]
    id_name_dict = {}
    for ne_name in ne_list:
        table_name = '{}_{}'.format(topo, ne_name)
        ne_id = user_db_cli.get_value(table_name, "NEid")
        id_name_dict[ne_id] = ne_name
    return id_name_dict


def get_user_topo_one_ne(user, topo, ne_name):
    """
    从Redis数据库中获取某个用户某个拓扑特定节点的节点ID
    Args:
        user: 用户名 string
        topo: 拓扑名  string
    
    Returns:
        ne_id： 节点id string
    """
    user_db_cli = user_map_redis.get_user_db(user)
    user_map_redis.close()
    table_name = '{}_{}'.format(topo, ne_name)
    ne_id = user_db_cli.get_value(table_name, "NEid")
    return ne_id

def sum_container_info(raw_info):
    '''
    处理容器查询原始数据，返回特定结构的数据格式
    Args:
        raw_info: 原始查询数据 string
    Returns:
        metric_used: 该拓扑所有节点的资源用量之和
    
    eg:
    '''
    # print(raw_info)
    # handle_info["metric"] = metric_name
    metric_used = 0
    for container in raw_info:
        metric_used += float(container['value'][1])
    return metric_used

def handle_container_info(raw_info, metric_name, id_name_dict, choice):
    # 似乎metric_name写到info？前端知道自己在查什么
    '''
    处理容器查询原始数据，返回特定结构的数据格式
    Args:
        raw_info: 原始查询数据 string
        metric_name: 指标名  string
        id_name_dict: 前端容器名与后端容器名的对应关系 dict
        choice: 历史数据查询(range)or当前时刻查询(instant)
    Returns:
        handle_info
    
    eg:

    handle_info = {
        "s1": {
            "cpu": [
                "0.3750724444444131",
                "0.375096222221474"
            ], # choice为range返回list, choice为instant返回字符串
            "mem": "", # 不查询该指标返回空字符串
            "traffic_received": "",
            "traffic_sent": ""
        },
        "s2":{
            "cpu":xxx,
            "mem":xxx,
            "traffic_received": xxx,
            "traffic_sent": xxx,
        }
      ...
    }
    '''
    handle_info = {}
    # print(raw_info)
    # handle_info["metric"] = metric_name
    for container in raw_info:
        container_name = id_name_dict[container['metric']['name']]
        container_info = handle_info.setdefault(container_name, {})
        if choice == "instant":
            if "traffic" in metric_name:
                # 添加网卡信息
                container_info["interface"] = container['metric']['interface']
            container_info["value"] = container['value'][1]
        elif choice == "range":
            if "traffic" in metric_name:
                container_info["interface"] = container['metric']['interface']
            value_list = []
            for v in container['values']:
                value_list.append(v[1])
            container_info["value"] = value_list
    return handle_info


def handle_host_info(raw_info, metric_name, choice):
    '''
    处理宿主机查询的原始数据，返回特定结构的数据格式
    Args:
        raw_info: 用户名 string
        metric_name: 拓扑名  string
        id_name_dict: 前端容器名与后端容器名的对应关系
        choice: 历史数据查询(range)or当前时刻查询(instant)
    Returns:
        handle_info
    
    eg:
    handle_info = {
        "10.1.1.117": {
            "cpu": "",
            "load": "",
            "mem": "30505.0625"
        },
        "10.1.1.118": {
            "cpu": "",
            "load": "",
            "mem": "30505.0625"
        },
      ...
    }
    '''
    handle_info = {}
    # handle_info["metric"] = metric_name
    for host in raw_info:
        host_name = host['metric']['instance'].split(':')[0]
        host_info = handle_info.setdefault(host_name, {})
        if choice == "instant":
            host_info["value"] = host['value'][1]
        elif choice == "range":
            value_list = []
            for v in host["values"]:
                value_list.append(v[1])
            host_info["value"] = value_list
    return handle_info


# TODO:容器存活,通过能否从数据库里查到数据判断？执行特定命令判断(docker ps)？
def is_container_alive(user, topo, ne_name):
    """
    从Prometheus数据库中获取当前时刻某个用户某个拓扑特定节点的内存
    情况以检查容器是否存活
    Args:
        user: 用户名 string
        topo: 拓扑名  string
        ne_name: 节点名 string
    
    Returns:
        is_alive: 是否存活 bool
    """
    is_alive = True
    ne_id = get_user_topo_one_ne(user, topo, ne_name)
    query_list = [USER_TOPO_QUERY_DICT['mem'].replace("<name>", ne_id)]
    ne_info = get_instant_metric_data(CONFIG, query_list)
    if ne_info == list():
        print("NO SUCH CONTAINER(%s).MAYBE CONTAINER IS DOWN." % ne_name)
        is_alive = False
        return is_alive
    return is_alive


def json_change(metric_name, metric_datas) -> dict:
    """
    用于处理Prometheus rest api的原始数据,可根据前端需求灵活修改
    Args:
        metric_name: 指标查询的选择 string
        metrics_datas: 未处理的指标 list
    
    Returns:
        metric_dict: 处理后的数据格式 dict
    """
    if metric_datas == list():
        return metric_datas
    metric_dict = {}
    metric_dict['metric_name'] = metric_name
    metric_dict['metric_datas'] = {}
    for num, m in enumerate(metric_datas):
        metric_dict['metric_datas'].setdefault('data' + str(num), {}).setdefault('name', 'data' + str(num))
        metric_dict['metric_datas']['data' + str(num)]['time'] = timestamp_to_date(m['value'][0])
        metric_dict['metric_datas']['data' + str(num)]['value'] = m['value'][1]
        metric_dict['metric_datas']['data' + str(num)]['label'] = m['metric']
    return metric_dict


if __name__ == "__main__":
    # host query test
    print("-----host query test-----")
    # query_host_info()
    # container query test
    print("-----container query test-----")
    # query_one_ne_instant_info("xz", "topo1", "r1", "mem")
    # query_all_ne_instant_info("xz", "topo1", 'cpu')
    # is_container_alive('xz', 'topo1', 'r1')
    # loadbe start 2021-06-15 21:30:48, end 2021-06-15 21:37:28 
    # sighop start 2021-06-15 21:44:17, end 2021-06-15 21:50:57
    # loadbe start 2021-06-16 09:24:03, end 2021-06-16 09:30:53
    # sighop start 2021-06-16 09:45:30, end 2021-06-16 09:52:20
    # time_args = {
    #     "start_time": "2021-06-30 21:21:55",
    #     "end_time": "2021-06-30 21:22:10",
    #     "step": "1"
    # }

    # result = query_one_ne_info('sw', 'test1', 'h2', 'mem')
    # pprint(result)
    # json_str = json.dumps(result, indent=4)
    # with open('5gb5g_monitor_cpu.json', 'w') as f:
    #     f.write(json_str)
    # result = query_all_ne_range_info('sw', 'topo2', 'mem', "2021-06-16 09:45:30", "2021-06-16 09:52:20", "1")
    # json_str = json.dumps(result, indent=4)
    # with open('5gb5g_monitor_mem.json', 'w') as f:
    #     f.write(json_str)

    # other query test
    # container_info = {
    #     "user": "xzz",
    #     "topo": "topo1",
    #     "ne_name": "h1"
    # }
    # result = query_other_metrics("container_transmit", container_info)
    # metric_list = [USER_TOPO_QUERY_DICT['traffic_sent'].replace("<name>", "prometheus")]
    # raw_info = get_instant_metric_data(CONFIG, metric_list)
    # pprint(raw_info)
    # metric_name = 'traffic_sent'
    # handle_info = {}
    # for container in raw_info:
    #     container_name = container['metric']['name']
    #     container_info = handle_info.setdefault(container_name, {})
    #     # TODO:metric_name为network相关，把网卡提出
    #     if "traffic" not in metric_name:
    #         container_info[metric_name] = container['value'][1]
    #     else:
    #         interface_info = container_info.setdefault(metric_name, {})
    #         interface_info[container['metric']['interface']] = container['value'][1]
    #     # pprint(handle_info)
    # pprint(handle_info)

    # 写入csv
    def range_csv_write(user, topo, ne_name, metric_name, **time_args):
        import csv
        ne_id = get_user_topo_one_ne(user, topo, ne_name)
        print("ne_id:", ne_id)
        id_name_dict = {ne_id: ne_name}
        query_list = [USER_TOPO_QUERY_DICT[metric_name].replace("<name>", ne_id)]
        start_time = time_args.get("start_time", 0)
        end_time = time_args.get("end_time", 0)
        step = time_args.get("step", 0)
        start_timestamp = date_to_timestamp(start_time)
        end_timestamp = date_to_timestamp(end_time)
        print("start_timestamp:", start_timestamp, "end_timestamp:", end_timestamp)
        # TODO(sw): 一次查不了太多数据，可能需要一个小时查一次，然后合并
        round = 64
        time_list = []
        value_list = []
        for i in range(round):
            temp_end_timestamp = start_timestamp + 3600 # 每次取一小时的数据
            if temp_end_timestamp >= end_timestamp:
                temp_end_timestamp = end_timestamp
            ne_info = get_range_metric_data(CONFIG, query_list, start_timestamp, temp_end_timestamp, step)
            # print(ne_info)
            for container in ne_info:
                print("append")
                for v in container['values']:
                    time_list.append(v[0])
                    value_list.append(v[1])
            if temp_end_timestamp == end_timestamp: 
                with open(f"{user}_{topo}_{ne_name}_{metric_name}.csv", "a+") as f:
                    csv_write = csv.writer(f)
                    print(len(time_list), len(value_list))
                    csv_write.writerow(time_list)
                    csv_write.writerow(value_list)
                break
            start_timestamp = temp_end_timestamp + 1


    time_args = {
        "start_time": "2021-07-15 11:04:00",
        "end_time": "2021-07-15 11:05:00",
        "step": "20"
    }
    # range_csv_write("sw", "task2_topo", "s1", "cpu", **time_args)
    # 
    # 一次查不了太多
    # result = query_one_ne_info("sw", "task2_topo", "s1", "cpu", **time_args)
    # pprint(result)
    # result = query_one_ne_info("sw", "task2_test", "s1", "cpu")
    # pprint(result)

    print("-----container query test-----")
    # 用户拓扑节点查询：特定节点，多指标，时间段
    result = query_ne_info("sw", "task2_topo", "s1", ["cpu", "mem"], **time_args)
    print("用户拓扑节点查询：特定节点，多指标，时间段")
    pprint(result)
    # 用户拓扑节点查询：所有节点，多指标，时间段
    result = query_ne_info("sw", "task2_topo", "", ["cpu", "mem"], **time_args)
    print("用户拓扑节点查询：所有节点，多指标，时间段")
    pprint(result)
    # 用户拓扑节点查询：所有节点，多指标，时刻
    time_args = {}
    print("用户拓扑节点查询：所有节点，多指标，时刻")
    result = query_ne_info("sw", "task2_topo", "", ["cpu", "mem"], **time_args)
    pprint(result)


    print("-----host query test-----")
    # 宿主机资源查询
    print("宿主机资源查询")
    result = query_host_info(["cpu", "mem", "load"], **time_args)
    print(result)
