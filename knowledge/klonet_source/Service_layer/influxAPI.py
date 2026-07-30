import os
import subprocess
import requests
from requests.api import get
from ..Implement_layer.LinkManager.link_operate import shell_execute
from .redisAPI import UserMapRedis
from ..tools import get_host_ip
from ..vemu_config.config import PROJ_CONFIG


# USER_DATA_DIR = settings.DEV_CONFIG["user_data_dir"]
# INFLUX_DB_NAME = settings.DEV_CONFIG["influx_db_name"]
# DATA_SERVER_IP = settings.DEV_CONFIG["data_server_ip"]
# INFLUX_DB_PORT = settings.DEV_CONFIG["influx_db_port"]

PARENT_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
USER_DATA_DIR = PARENT_DIR + "/expr_monitor_user_data"
INFLUX_DB_NAME = PROJ_CONFIG.influxdb_name
DATA_SERVER_IP = PROJ_CONFIG.data_server_ip
INFLUX_DB_PORT = PROJ_CONFIG.influxdb_port

INFLUX_QUERY_URL = (f"http://{DATA_SERVER_IP}:{INFLUX_DB_PORT}/query?")
INFLUX_WRITE_URL = (f"http://{DATA_SERVER_IP}:{INFLUX_DB_PORT}/write?")

# 得到自己此运行程序所在worker的局域网IP地址
local_ip = get_host_ip()

req_para = {
    "db": INFLUX_DB_NAME, 
    "q": "",
    "epoch": "ns"
}

def _get_measurement_name(user, topo, expr, event_seq) -> list:
    '''
    获取某用户的某实验(的某事件)在influx db中对应的原始数据表名

    Args:
        user: 用户名
        topo: 拓扑名
        expr: 实验名
        event_seq: 事件序号

    Returns:
        measurements: 一个列表，其元素为某用户的某实验(的某事件)
                      对应的表名。若未指定实验序号，则根据该次实验
                      的事件数，measurements的列表大小大于等于1；若指
                      定实验序号，则measurements的列表大小等于1。
    '''
    measurements = []
    user_db_map = UserMapRedis()
    user_db_cli = user_db_map.get_user_db(user)
    user_db_map.close()
    try:
        table = f"{topo}_monitor"
        events_to_monitor = user_db_cli.get_value(table, expr)
        if event_seq == "":
            for seq, event in enumerate(events_to_monitor):
                if event["performance"] == "throughput":
                    measurements.append(
                        f"{user}_{topo}_{expr}_{seq+1}_dst_raw_data")
                elif (event["performance"] == "delay" or
                      event["performance"] == "loss"):
                    measurements.append(
                        f"{user}_{topo}_{expr}_{seq+1}_src_raw_data")
                    measurements.append(
                        f"{user}_{topo}_{expr}_{seq+1}_dst_raw_data")
        else:
            if (events_to_monitor[int(event_seq)-1]["performance"] 
                == "throughput"):
                measurements.append(
                    f"{user}_{topo}_{expr}_{event_seq}_dst_raw_data")
            elif (events_to_monitor[int(event_seq)-1]["performance"]
                  == "delay" or 
                  events_to_monitor[int(event_seq)-1]["performance"]
                  == "loss"):
                measurements.append(
                    f"{user}_{topo}_{expr}_{event_seq}_src_raw_data")
                measurements.append(
                    f"{user}_{topo}_{expr}_{event_seq}_dst_raw_data")
    except:
        raise
    finally:
        user_db_cli.close()

    return measurements

def _get_perf_data(user, topo, expr, event_seq) -> list:
    '''
    获取用户在influxdb中的指标数据

    Args:
        user: 用户名
        topo: 拓扑名(项目名)
        expr: 实验名
        event_seq: 事件序号

    Returns:
        file_infos: 一个列表，其元素为字典，内容为文件路径及文件名。即：
                   [{"file_path":"文件路径","file_name":"文件名"}, ...]
    '''
    file_prefix = f"{user}_{topo}_{expr}"
    filter = f"WHERE \"user_name\"=\'{user}\' AND \"expr\"=\'{topo}_{expr}\'"
    if event_seq != "":
        file_prefix += f"_{event_seq}"
        filter += f" AND \"event_seq\"=\'{event_seq}\'"

    # 搞一个只有一个元素的数组是为了和_get_raw_data的返回值统一
    file_infos = [{"file_path":"","file_name":""}]
    file_infos[0]["file_path"] = f"{USER_DATA_DIR}/{user}/{topo}"
    file_infos[0]["file_name"] = f"{file_prefix}_perf_data.csv"

    file_name_with_path = (file_infos[0]["file_path"] + "/" + 
                          file_infos[0]["file_name"])

    # 若不存在csv文件，则生成
    if not os.path.exists(file_name_with_path):
        q = (f"influx -database {INFLUX_DB_NAME} -execute \"SELECT * FROM "
             f"perf_data {filter}\" -format csv >> {file_name_with_path}")
        try:
            shell_execute(q) 
            print(f"create {file_name_with_path}")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                "GET PERF DATA ERROR when execute command '" + e.cmd +
                "'.\nexit code: " + str(e.returncode) + "\nstderr: " + 
                e.stderr.rstrip() + "\nstdout: " + e.stdout.rstrip())
    else:
        print(f"{file_name_with_path} exists. Don't need to create csv file.")     

    return file_infos

def _get_raw_data(user, topo, expr, event_seq) -> list:
    '''
    获取用户在influxdb中的原始数据

    Args:
        user: 用户名
        expr: 实验名
        event_seq: 事件序号

    Returns:
        file_infos: 一个列表，其元素为字典，内容为文件路径及文件名。即：
                   [{"file_path":"文件路径","file_name":"文件名"}, ...]
    '''
    file_infos = []

    measurements = _get_measurement_name(user, topo, expr, event_seq)
    file_path = f"{USER_DATA_DIR}/{user}/{topo}"

    for measurement in measurements:
        # 若不存在csv文件，则生成
        file_name_with_path = f"{file_path}/{measurement}.csv" 
        file_infos.append(
            {"file_path":file_path, "file_name":f"{measurement}.csv"})
        if not os.path.exists(file_name_with_path):
            q = (f"influx -database {INFLUX_DB_NAME} -execute \"SELECT * FROM "
                 f"{measurement}\" -format csv >> {file_name_with_path}")
            try:
                shell_execute(q) 
                print(f"create {file_name_with_path}")
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    "GET RAW DATA ERROR when execute command '" + e.cmd +
                    "'.\nexit code: " + str(e.returncode) + "\nstderr: " + 
                    e.stderr.rstrip() + "\nstdout: " + e.stdout.rstrip())
        else:
            print(f"{file_name_with_path} exists. "
                  "Don't need to create csv file.")     

    return file_infos

def get_influx_data(data_type, user, topo, expr, event_seq="") -> list:
    '''
    获取用户在influxdb中的原始数据或指标数据

    Args:
        user: 用户名
        data_type: 数据类型，可选参数为raw或perf(原始数据或指标数据)
        expr: 实验名
        event_seq: 事件序号

    Returns:
        file_infos: 一个列表，其元素为字典，内容为文件路径及文件名。即：
                   [{"file_path":"文件路径","file_name":"文件名"}, ...]
    '''  
    file_infos = []
    if data_type == "perf":
        file_infos = _get_perf_data(user, topo, expr, event_seq)
    elif data_type == "raw":
        file_infos = _get_raw_data(user, topo, expr, event_seq)
    else:
        print("data_type should = raw/perf!")
        exit(1) # TODO(MaTie): 异常处理
    print(file_infos)
    return file_infos

def read_influx(q, method="GET"):
    '''
    读取influx db中的数据

    Args:
        q: influx db的查询语句
        method:"GET" or "POST"
        
    Returns:
        r: 若查询的数据为空，则返回{}，
        若查询的数据不为空，则返回结果，以特定格式的dict给出。
    '''
    req_para["q"] = q
    if method == "GET":
        r = requests.get(INFLUX_QUERY_URL, params=req_para).json()
    elif method == "POST":
        r = requests.post(INFLUX_QUERY_URL, params=req_para).json()
    else:
        print("please provide the right method(POST or GET) in read_influx function in influxAPI")
    return r

def write_influx(post_data):
    '''
    向influx db写入数据

    Args:
        post_data: 要向influx db写入的单行数据
        
    Returns:
        r: response
    '''
    r = requests.post(INFLUX_WRITE_URL + "db=" + INFLUX_DB_NAME, 
                    post_data.encode())
    return r

def get_rows_of_response(r_json) -> list:
    '''
    将返回的请求转换为一个列表，列表的第n个元素为第n行数据

    Args:
        r_json: requests.get()的返回值的json格式(即r.json())
        
    Returns:
        一个列表，列表的第n个元素为第n行数据
    '''  
    return r_json["results"][0]["series"][0]["values"]

def get_column_of_key(r_json, key) -> int:
    '''
    获取指定的key在数据库中的列数, 便于从数据库的行数据中找到所给的key对应的value

    Args:
        r_json: requests.get()的返回值的json格式(即r.json())
        key: 想要查询列数的键
        
    Returns:
        指定的key在数据库中的列数
    '''  
    return r_json["results"][0]["series"][0]["columns"].index(key)

