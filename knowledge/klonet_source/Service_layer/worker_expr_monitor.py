# 此文件位于worker上，负责接收来自master的监控服务创建信号，并根据数据库中的创建列表创建pcap程序
# import subprocess
import random
import re
from nsenter import Namespace
from ..Service_layer.LoadManager import UploadFile
from gevent import subprocess
import os
# import multiprocessing
import billiard as multiprocessing
from time import sleep
import psutil
from pprint import pprint
import time
import csv

from .deploy_error import *
from .redisAPI import UserMapRedis
from ..tools import get_host_ip
from ..Implement_layer.LinkManager.link_operate import shell_execute, get_pid
from ..vemu_config.config import PROJ_CONFIG
from .influxAPI import read_influx, write_influx
from ..Function_layer.deployed_proj_manager import delete_monitor_data


# 得到自己此运行程序所在worker的局域网IP地址
local_ip = get_host_ip()

# 参数配置
PARENT_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
IMPLEMENT_LAYER_EXPR_M_DIR = PARENT_DIR + "/Implement_layer/ExprMonitorManager"
USER_DATA_DIR = PROJ_CONFIG.user_data_dir
DATA_SERVER_FLASK_PORT = PROJ_CONFIG.data_server_flask_port
DATA_SERVER_IP = PROJ_CONFIG.data_server_ip
INFLUX_DB_NAME = PROJ_CONFIG.influxdb_name
INFLUX_DB_PORT = PROJ_CONFIG.influxdb_port
SRTT_FILE_NAME = PROJ_CONFIG.srtt_file_name


def _is_br_exists(ne_pid, br_name) -> bool:
    '''
    查看指定节点的指定网桥/网卡是否存在

    Args:
        ne_pid: 节点的pid
        br_name: 要查找的网桥/网卡名
        
    Returns:
        bool: 网桥/网卡存在则返回True, 不存在则返回False
    '''
    try:
        if shell_execute("sudo nsenter -t "+ ne_pid +" --net "+ 
                        IMPLEMENT_LAYER_EXPR_M_DIR +
                         "/is_br_exists.sh " + br_name) == "1":
            return True
        else:
            return False
    except subprocess.CalledProcessError as e:
        raise ExprMonitorWorkerError(
            "Execute command '" + e.cmd + "' failed,\nexit code: " 
            + str(e.returncode) + ",\nstderr: " + e.stderr.rstrip() 
            + ",\nstdout: " + e.stdout.rstrip() + ".")

def _add_br_to_nic(ne_pid, nic_name) -> str:
    '''
    在指定网卡前添加网桥

    Args:
        ne_pid: 节点的pid
        nic_name: 要在前面添加网桥的网卡名
        
    Returns:
        None
    '''
    try:
        cidr = get_CIDR(ne_pid, nic_name)

        # 获得原始路由表
        original_routes = shell_execute("sudo nsenter -t "+ ne_pid +" --net " + 
                      "route -n").split('\n')[2:]

        shell_execute("sudo nsenter -t "+ ne_pid +" --net " + 
                      IMPLEMENT_LAYER_EXPR_M_DIR + "/add-br-to-nic.sh " +
                      nic_name + " " + cidr)
        
        # 恢复原网卡的路由表
        for route in original_routes:
            if route.endswith(nic_name):
                route_data = route.split()
                dest = route_data[0]
                gw = route_data[1]
                shell_execute("sudo nsenter -t "+ ne_pid +" --net " + 
                      f"route add -net {dest} gw {gw} dev {nic_name}-br")
                
    except subprocess.CalledProcessError as e:
        raise ExprMonitorWorkerError(
            "Execute command '" + e.cmd + "' failed,\nexit code: " 
            + str(e.returncode) + ",\nstderr: " + e.stderr.rstrip() 
            + ",\nstdout: " + e.stdout.rstrip() + ".")

def get_CIDR(ne_pid, nic_name) -> str:
    '''
    根据节点名和网卡名获取CIDR形式的网卡地址

    Args:
        ne_pid: 节点的pid
        br_name: 网卡名
        
    Returns:
        CIDR形式(xxx.xxx.xxx.xxx/yy)的网卡地址
    '''
    ip = get_nic_ip(ne_pid, nic_name)
    subnet_mask = get_nic_subnet_mask(ne_pid, nic_name)
    print("Get nic ip: " + ip)
    print("Get nic subnet_mask: " + subnet_mask)

    return ip + "/" + str(convert_to_mask_int(subnet_mask))

def get_nic_ip(ne_pid, nic_name) -> str:
    '''
    根据节点名和网卡名获取网卡ip

    Args:
        ne_pid: 节点的pid
        nic_name: 网卡名
        
    Returns:
        网卡的ip地址
    '''
    print("get " + nic_name + "'s ip")
    nic_ip = ""
    try:
        nic_ip = shell_execute("sudo nsenter -t "+ ne_pid + 
                               " --net ifconfig " + nic_name +
                               " | grep \"inet \" | awk '{print $2}'")
        '''
        防止以下情况
        tos1      Link encap:Ethernet  HWaddr ba:f3:c2:16:bc:2d  
          inet addr:192.168.1.2  Bcast:192.168.1.255  Mask:255.255.255.0
          UP BROADCAST RUNNING MULTICAST  MTU:1500  Metric:1
          RX packets:186534 errors:0 dropped:0 overruns:0 frame:0
          TX packets:14362 errors:0 dropped:0 overruns:0 carrier:0
          collisions:0 txqueuelen:1000 
          RX bytes:9893100 (9.8 MB)  TX bytes:41552553 (41.5 MB)
        '''
        if nic_ip.startswith("addr:"):
            nic_ip = nic_ip.lstrip("addr:")
    except subprocess.CalledProcessError as e:
        raise ExprMonitorWorkerError(
            "Execute command '" + e.cmd + "' failed,\nexit code: " 
            + str(e.returncode) + ",\nstderr: " + e.stderr.rstrip() 
            + ",\nstdout: " + e.stdout.rstrip() + ".")
    
    return nic_ip

def get_nic_subnet_mask(ne_pid, nic_name) -> str:
    '''
    根据节点名和网卡名获取网卡的地址形式的子网掩码

    Args:
        ne_pid: 节点的pid
        nic_name: 网卡名
        
    Returns:
        网卡的地址形式的子网掩码
    '''

    subnet_mask = ""
    try:
        subnet_mask = shell_execute("sudo nsenter -t "+ ne_pid + 
                                    " --net ifconfig " + nic_name + 
                                    " | grep \"inet \" | awk '{print $4}'")
        '''
        防止以下情况
        tos1      Link encap:Ethernet  HWaddr ba:f3:c2:16:bc:2d  
          inet addr:192.168.1.2  Bcast:192.168.1.255  Mask:255.255.255.0
          UP BROADCAST RUNNING MULTICAST  MTU:1500  Metric:1
          RX packets:186534 errors:0 dropped:0 overruns:0 frame:0
          TX packets:14362 errors:0 dropped:0 overruns:0 carrier:0
          collisions:0 txqueuelen:1000 
          RX bytes:9893100 (9.8 MB)  TX bytes:41552553 (41.5 MB)
        '''
        if subnet_mask.startswith("Mask:"):
            subnet_mask = subnet_mask.lstrip("Mask:")
    except subprocess.CalledProcessError as e:
        raise ExprMonitorWorkerError(
            "Execute command '" + e.cmd + "' failed,\nexit code: " 
            + str(e.returncode) + ",\nstderr: " + e.stderr.rstrip() 
            + ",\nstdout: " + e.stdout.rstrip() + ".")
    
    return subnet_mask

def convert_to_mask_int(subnet_mask) -> int:
    '''
    将地址形式(xxx.xxx.xxx.xxx)的子网掩码转换为位长形式(int)的子网掩码

    Args:
        subnet_mask: 地址形式的子网掩码
        
    Returns:
        位长形式的子网掩码
    ''' 
    # 计算二进制字符串中 '1' 的个数
    count_bit = lambda bin_str: len([i for i in bin_str if i=='1'])
 
    # 分割字符串格式的子网掩码为四段列表
    mask_splited = subnet_mask.split('.')
    print(mask_splited)
    # 转换各段子网掩码为二进制, 计算十进制
    mask_count = [count_bit(bin(int(i))) for i in mask_splited]
 
    return sum(mask_count)

def deploy_monitor(user, topo, expr):
    '''
    根据master的创建信号，在数据库中查询创建列表，并创建监控程序，最终返回一
    个监控程序的进程列表供后续关闭进程。

    Args:
        user: 用户名
        topo: 拓扑名
        expr: 实验名
        
    Returns:
        processing_list: 各监控程序的进程号列表
    '''
    user_db_map = UserMapRedis()
    user_db_cli = user_db_map.get_user_db(user)
    user_db_map.close()
    try:
        table = '{}_{}_monitor'.format(topo, expr)
        current_worker_deploy_list = user_db_cli.get_value(table, local_ip)    
        print('current_worker_deploy_list is\n ')
        pprint(current_worker_deploy_list)
        processing_list = []
        temp_list = []
        
        #del monitoring data
        delete_monitor_data(user, topo, expr)

        for event in current_worker_deploy_list:
            performance = event["performance"]
            if (performance == "throughput" or performance == "delay" 
                or performance == "loss"):
                temp_list = _deploy_pcap_monitor(
                    user, topo, expr, performance, event["seq"], event["params"])
            elif performance == "srtt":
                temp_list = _deploy_ebpf_monitor(user, topo, expr, event["seq"], 
                                                event["params"])
            else:
                raise ValueError(f'目前不支持监控性能指标{performance}')
            processing_list.extend(temp_list)
        # print(processing_list)
        return processing_list
    except:
        raise
    finally:
        user_db_cli.close()


def _calc_filter_expression(params):
    '''
    根据事件参数中的五元组，计算出pcap过滤表达式

    Args:
        params: 性能指标为throughput/delay/loss时，前端传来的特有参数。例如：
            {
                'deploy_list': ['h1', 'h4'], # 性能指标为吞吐时，没有此项
                'proto_type': 'tcp',
                'src': {'ne_name': 'h1',
                        'nic_ip': '192.168.1.2',
                        'port': ''}},
                'dst': {'ne_name': 'h4',
                        'nic_ip': '192.168.1.5',
                        'port': ''},
            }
        
    Returns:
        filter: 根据五元组计算出的pcap过滤表达式
    '''
    def _add_module_to_filter(filter_module, filter):
        if filter == "":
            return "(" + filter_module + ")"
        else:
            return filter + " and " + "(" + filter_module + ")"
    
    filter = ""

    if params["proto_type"] != "":
        filter = _add_module_to_filter(params["proto_type"], filter.lower())

    if params["src"]["port"] != "":
        filter = _add_module_to_filter(
            "src host " + params["src"]["nic_ip"] + " and src port "
            + params["src"]["port"], filter)
    else:
        filter = _add_module_to_filter("src host " + params["src"]["nic_ip"],
                                       filter)

    if params["dst"]["port"] != "":
        filter = _add_module_to_filter(
            "dst host " + params["dst"]["nic_ip"] + " and dst port "
            + params["dst"]["port"], filter)
    else:
        filter = _add_module_to_filter("dst host " + params["dst"]["nic_ip"],
                                       filter)

    return filter

def _deploy_pcap_monitor(user, topo, expr, performance, seq, params):
    '''
    创建libpcap监控程序

    Args:
        user: 用户名
        topo: 拓扑名
        expr: 实验名
        performance: 要测量的性能指标
        seq: 监控服务序号
        params: 性能指标为throughput/delay/loss时，前端传来的特有参数。例如：
            {
                'deploy_list': ['h1', 'h4'], # 性能指标为吞吐时，没有此项
                'proto_type': 'tcp',
                'src': {'ne_name': 'h1',
                        'nic_ip': '192.168.1.2',
                        'port': ''}},
                'dst': {'ne_name': 'h4',
                        'nic_ip': '192.168.1.5',
                        'port': ''},
            }
        
    Returns:
        pcap_processing_list: 各pcap程序的进程号列表
    '''
    user_db_map = UserMapRedis()
    user_db_cli = user_db_map.get_user_db(user)
    user_db_map.close()
    filter = _calc_filter_expression(params)
    print('创建 pcapmeasurement...')
    src_nic_name = user_db_cli.get_nic_by_ne_name_and_ip(
        topo, params["src"]["ne_name"], params["src"]["nic_ip"])
    dst_nic_name = user_db_cli.get_nic_by_ne_name_and_ip(
        topo, params["dst"]["ne_name"], params["dst"]["nic_ip"])

    ne_name_2_nic_name = {
        params["src"]["ne_name"]:src_nic_name, 
        params["dst"]["ne_name"]:dst_nic_name,
        }
    print(f'performance is {performance}')
    # pprint(params)
    pcap_processing_list = []
    try:
        for ne_name in params["deploy_list"]:
            con_name = user_db_cli.get_value('{}_{}'.format(topo, ne_name),
                                             'NEid')
            pid = get_pid(con_name)
            # print(ne_name + "_pid = " + str(pid))
            nic_name = ne_name_2_nic_name[ne_name]
            
            # 测量丢包和时延需要在源端网卡前加网桥，并在网桥上创建pcap
            # 否则会出现pcap抓到的是tc进行丢包时延处理后的包，无法计算丢包率或时延
            if performance == "loss" or performance == "delay":
                if ne_name == params["src"]["ne_name"]:
                    if not _is_br_exists(pid, nic_name + "-br"):
                        _add_br_to_nic(pid, nic_name)
                        print(f"add br to {con_name}")
                    nic_name += "-br"

            # 创建时，通过目录来区分用户-某次实验-某个事件-某张网卡
            print("_-----------userdatadir------------",USER_DATA_DIR)
            file_path = f"{USER_DATA_DIR}/{user}/{topo}/{expr}/{seq}"
            if not os.path.exists(file_path):
                os.makedirs(file_path)
            file_name = file_path + "/" + ne_name + ".pcap"

            print(file_name)
            t = multiprocessing.Process(
                target=shell_execute, 
                args=("sudo nsenter -t "+ pid + " --net "+ 
                      IMPLEMENT_LAYER_EXPR_M_DIR + "/pcapMeasurement " +
                      nic_name + " " +  "\"" + filter +
                      "\"" + " " + file_name, ))
            t.start()
            print(f'starting pcap in {ne_name} on nic {nic_name} process is {t.pid}')
            pcap_processing_list.append(t.pid)
    except subprocess.CalledProcessError as e:
        raise ExprMonitorWorkerError(
            "Execute command '" + e.cmd + "' failed,\nexit code: " 
            + str(e.returncode) + ",\nstderr: " + e.stderr.rstrip() 
            + ",\nstdout: " + e.stdout.rstrip() + ".")
    finally:
        user_db_cli.close()

    return pcap_processing_list

def _deploy_ebpf_monitor(user, topo, expr, event_seq, params):
    '''
    创建eBPF监控程序

    Args:
        user: 用户名
        expr: 实验名
        event_seq: 监控服务序号
        params: 性能指标为srtt时，前端传来的特有参数。例如：
            {
                'src': {'ne_name': 'h1',
                        'nic_ip': '192.168.1.2',
                        'port': ''}},
                'dst': {'ne_name': 'h4',
                        'nic_ip': '192.168.1.5',
                        'port': ''},
            }
        
    Returns:
        ebpf_processing_list: 各pcap程序的进程号列表
    '''    
    ebpf_processing_list = []

    src_port_filter = ""
    dst_port_filter = ""
    if params["src"]["port"] != "":
        src_port_filter = " -p " + params["src"]["port"]
    if params["dst"]["port"] != "":
        dst_port_filter = " -P " + params["dst"]["port"]

    file_path = f"{USER_DATA_DIR}/{user}/{topo}/{expr}/{event_seq}"
    if not os.path.exists(file_path):
        os.makedirs(file_path)

    try:
        t = multiprocessing.Process(
            target=shell_execute, 
            args=("sudo "+ IMPLEMENT_LAYER_EXPR_M_DIR + "/get_srtt/tcprtt.py" + 
                " -S -i 1 -l " + file_path + "/" + SRTT_FILE_NAME + " -a " + 
                params["src"]["nic_ip"] + " -A " + params["dst"]["nic_ip"] +
                src_port_filter + dst_port_filter,
                ))
        t.start()

        ebpf_processing_list.append(t.pid)
    except subprocess.CalledProcessError as e:
        raise ExprMonitorWorkerError(
            "Execute command '" + e.cmd + "' failed,\nexit code: " 
            + str(e.returncode) + ",\nstderr: " + e.stderr.rstrip() 
            + ",\nstdout: " + e.stdout.rstrip() + ".")

    return ebpf_processing_list

def terminate_monitor(user, topo, expr, processing_list):
    '''
    结束实验监控。包括结束监控进程，存储原始数据，计算指标数据并存储。

    Args:
        user: 用户名
        topo: 拓扑名
        expr: 实验名
        processing_list: 各监控程序的进程号列表
        
    Returns:
        bool: 结束成功返回True，结束失败返回False
    '''
    print("terminate")
    terminate_processings(processing_list)
    save_raw_data_to_db(user, topo, expr)

    # TODO(MaTie): 结束成功与否的判断，并返回
    return True

def terminate_processings(processing_list:list) -> bool:
    '''
    结束监控进程

    Args:
        processing_list: 各监控程序的进程列表
        
    Returns:
        bool: 结束成功返回True，结束失败返回False
    '''
    # TODO(tie): 僵尸进程问题还没解决
    # TODO(tie): 也许这个连接里的方法可以解决https://www.jianshu.com/p/7ac73e9c7150
    for pid in processing_list:
        try:
            p = psutil.Process(pid)
        except psutil.Error as e:
            raise
        childrens = p.children(recursive=True)
        print(childrens)  # TODO(MaTie): 打印时各进程状态都是sleeping?
        for child in childrens:
            if child.name() == "pcapMeasurement":
                os.kill(child.pid, 10)  # SIGUSR1
            elif child.name() == "tcprtt.py":
                os.kill(child.pid, 2)  # SIGINT
            else:
                os.kill(child.pid, 9)  # SIGKILL
        os.kill(pid, 9)
    print('terminate monitor done')

    return True  # TODO(MaTie): 是否杀成功还需要判断，来给一个返回值

def save_raw_data_to_db(user, topo, expr):
    '''
    存储监控程序捕获到的原始数据至influx db

    Args:
        user: 用户名
        topo: 拓扑名
        expr: 实验名
        
    Returns:
        None
    '''
    user_db_map = UserMapRedis()
    user_db_cli = user_db_map.get_user_db(user)
    user_db_map.close()
    table = '{}_{}_monitor'.format(topo, expr)
    current_worker_deploy_list = user_db_cli.get_value(table, local_ip)

    processes = []

    print("wait for pcap file writing...")
    time.sleep(10)
    
    print("start save raw data to db...")
    print("Get current_worker_deploy_list:")
    print(current_worker_deploy_list)
    for event in current_worker_deploy_list:
        print("save event " + str(event["seq"]))
        
        performance = event["performance"]
        processes_ = []

        if (performance == "throughput" or performance == "delay" 
            or performance == "loss"):
            processes_ = _save_pcap_raw_data_to_db(
                user, topo, expr, event["seq"], event["performance"], 
                event["params"])
        if performance == "srtt":
            p = multiprocessing.Process(
                target=_save_ebpf_raw_data_to_db, 
                args=(user, topo, expr, event["seq"],))
            p.start()
            processes_.append(p)

        processes.extend(processes_)

    print("save processes:")
    print(processes)
    print("saving...")
    for p in processes:
        p.join()
    
    # 检查进程状态，若非正常退出则抛出异常
    for p in processes:
        if p.exitcode != 0:
            # 异常信息位于子进程中，日志中可见
            raise ExprMonitorWorkerError("Subprocess of save_raw_data_to_db "
                                         f"error. Subprocess name={p.name}, "
                                         f"pid={p.pid}. Please check log for "
                                         "more information.")

    user_db_cli.close()

def _save_pcap_raw_data_to_db(user, topo, expr, event_seq, performance, params):
    '''
    存储pcap监控程序捕获到的原始数据至influx db的raw_data表

    Args:
        user: 用户名
        topo: 拓扑名
        expr: 实验名
        performance: 测量的性能指标
        params: 性能指标为throughput/delay/loss时，前端传来的特有参数。例如：
            {
                'deploy_list': ['h1', 'h4'], # 性能指标为吞吐时，没有此项
                'proto_type': 'tcp',
                'src': {'ne_name': 'h1',
                        'nic_ip': '192.168.1.2',
                        'port': ''}},
                'dst': {'ne_name': 'h4',
                        'nic_ip': '192.168.1.5',
                        'port': ''},
            }
        
    Returns:
        processes_: 存储pcap原始数据程序的进程列表
    '''
    def get_node_type(ne_name):
        return "src" if ne_name == params["src"]["ne_name"] else "dst"

    def analyse_pacp(ne_name):
        try:
            shell_execute(
                IMPLEMENT_LAYER_EXPR_M_DIR
                + "/analyse_pcap "
                + str(DATA_SERVER_IP) + " "
                + str(INFLUX_DB_PORT) + " "
                + str(INFLUX_DB_NAME) + " "
                + USER_DATA_DIR + " "
                + user + " " 
                + topo + " "
                + expr + " " 
                + str(event_seq) + " " 
                + performance + " "
                + get_node_type(ne_name) + " "
                + ne_name + ".pcap")
        except subprocess.CalledProcessError as e:
            raise ExprMonitorWorkerError(
                "Execute command '" + e.cmd + "' failed,\nexit code: " 
                + str(e.returncode) + ",\nstderr: " + e.stderr.rstrip() 
                + ",\nstdout: " + e.stdout.rstrip() + ".")

    processes_ = []
    for ne_name in params["deploy_list"]:
        print(ne_name)
        p = multiprocessing.Process(target=analyse_pacp, args=(ne_name,))
        p.start()
        processes_.append(p)

    return processes_

def _save_ebpf_raw_data_to_db(user, topo, expr, event_seq):
    '''
    存储eBPF监控程序捕获到的sRTT数据至influx db的perf_data表

    Args:
        user: 用户名
        expr: 实验名
        event_seq: 监控服务序号
        
    Returns:
        None
    '''
    print("save_ebpf_raw_data_to_db")
    csv_path = USER_DATA_DIR + "/" + user + "/" + expr + "/" + str(event_seq)
    print(csv_path + "/" + SRTT_FILE_NAME)
    with open(csv_path + "/" + SRTT_FILE_NAME, newline="", 
              encoding="utf-8") as f:
        csv_reader = csv.reader(f)

        # ebpf程序接收到打断信号后，对csv文件的写入并不会立即结束，此时是读取不出来数
        # 据的。因此需要等待csv文件写入结束
        while csv_reader.line_num == 0:  # csv_reader读取到文件内容后line_num会变
                                         # 为正数
            try:
                for row in csv_reader:
                    time_ns = int(float(row[0]) * (10**9))
                    write_influx(f"perf_data,user_name={user},"
                                 f"expr={topo}_{expr},event_seq={event_seq},"
                                 f"perf=srtt srtt_us={row[1]} {time_ns}")
            except csv.Error as e:
                raise ExprMonitorWorkerError('file {}, line {}: {}'.format(
                    csv_path + "/" + SRTT_FILE_NAME, csv_reader.line_num, e))

def expr_monitor_worker_main():
    '''
    仅用于测试，请忽略本函数
    '''
    start_time = time.time()
    user = "tie"
    topo = "topo1"
    expr = "expr6"

    return 0
    # r = read_influx("select * from perf_data where \"expr\"=\'exp1\' limit 10;"
    #                 "select * from perf_data where \"expr\"=\'exp1\' limit 1;"
    #                 "select * from perf_data where \"expr\"=\'exp1\' limit 1")
    start_raw_data_calc(user, topo, expr)
    # _throughput_raw_data_calc(user, expr, 2)
    end_time = time.time()
    print(end_time-start_time)

def _deploy_tc_queue_monitor(user, topo, container_id, pid, nic, source_ne, target_ne, timethreshold = 1800):
    '''
    Args:
        container_id: 容器id
        pid: 容器pid
        nic: 端口网卡
        timethreshold: 时间阈值，到期自动关闭进程 
    '''
    # 这个进程一直不关的话需要解决内存溢出的问题
    # 暂时用时间阈值去限制
    tc_queue_lens = []
    tc_queue_bytes = []
    times = []
    datafile = f"{topo}_{source_ne}{target_ne}_queue_data.csv"
    with open(datafile,'w') as f:
        f.writelines(f"time queue_bytes queue_lens\n")
    cur_time = start_time = time.time()
    # 每 10~30s写入一次文件，时间随机，防止同时多个端口达到触发条件造成资源紧张
    loop_count = 0 
    random_c = random.randint(50,150)
    while cur_time - start_time <= timethreshold:
        try:
            loop_count += 1
            # print(loop_count, random_c)
            with Namespace(pid, 'net'):
            # output network interfaces as seen from within the pid's net NS:
                cmd = f"tc -s qdisc show dev {nic} | grep backlog | awk {{'print $2,$3'}}"
                output = subprocess.check_output(cmd, shell=True).decode("utf-8")      
            # mystr = '1234b 12345p\n'
            # tc_queue_byte = int(re.search(r"(\d*)b", mystr).group(1))
            # tc_queue_len = int(re.search(r"(\d*)p\n", mystr).group(1))
            tc_queue_byte = int(re.search(r"(\d*)b", output).group(1))
            tc_queue_len = int(re.search(r"(\d*)p\n", output).group(1))
            cur_time = time.time()
            tc_queue_bytes.append(tc_queue_byte)
            tc_queue_lens.append(tc_queue_len)
            times.append(cur_time) 
            # print(f'当前时间{cur_time} 队列字节数{tc_queue_byte} 队列长度{tc_queue_len}')
            time.sleep(0.2)
        except:
            raise RuntimeError('容器运行异常')
        finally:
            # 达到触发条件打开一次文件，并读写
            while loop_count >= random_c:
                with open(datafile,'a') as f:
                    for i in range(len(times)):
                        f.writelines(f"{times[i]} {tc_queue_bytes[i]} {tc_queue_lens[i]}\n")
                tc_queue_bytes.clear()
                tc_queue_lens.clear()
                times.clear()
                loop_count = 0
    # 进程超时，挂载文件，删除本地文件与数据库中的进程信息       
    data_file_path = os.path.abspath(datafile)
    upload_manager = UploadFile(container_id = container_id, file_path = data_file_path)
    result = upload_manager.cp_file() 
    os.remove(data_file_path)
    if not result:
        raise RuntimeError("文件存储失败")
    try:
        precess = os.getpid()
        user_db_map = UserMapRedis()
        user_db_cli = user_db_map.get_user_db(user)
        user_db_map.close()  
        table_name = f'{topo}_tc_queue_monitor_process'
        
        target_id = -1
        process_dict_list = user_db_cli.get_value(table_name,local_ip)
        for i in range(len(process_dict_list)):
            if process_dict_list[i]['process_id'] == precess:
                target_id = i
                break
        process_dict_list.pop(target_id)
        user_db_cli.set_value(table_name,local_ip,process_dict_list)
    except:  
        raise
    finally:
        user_db_cli.close()

def stop_tc_queue_monitor(user, topo, interfaces:dict) -> bool:
    '''
    首先需要检查是否存在此端口的进程，若没有则返回提示信息，若有则停止，返回停止操作的标志
    Args:
        user: 用户名
        topo: 拓扑名
        interfaces: 链路两端端口
        
    Returns:
        stop_flag: 监控程序停止成功与否的标志
    '''
    user_db_map = UserMapRedis()
    user_db_cli = user_db_map.get_user_db(user)
    user_db_map.close()
    for interface in interfaces:
        try:
            source_ne = interface['source_ne']
            target_ne = interface['target_ne']
            table = f"{topo}_{source_ne}"
            ne_id  = user_db_cli.get_all_values(table)['NEid']
            table_name = f'{topo}_tc_queue_monitor_process'
            # 首先检查进程表是否存在
            if user_db_cli.check_exist(table_name, local_ip):
                process_dict_list = user_db_cli.get_value(table_name,local_ip)
            else:
                raise ValueError
            # print(process_dict_list)
            # 在进程表中查询进程号，选择用索引号是为了后续删除的方便
            target_id = -1
            for i in range(len(process_dict_list)):
                if process_dict_list[i]['port'] == f'{source_ne}{target_ne}':
                    target_id = i
                    break
            if target_id == -1:
                raise ValueError
        except ValueError as e:
            raise ValueError(f"{source_ne}{target_ne}端口监控进程不存在")    
        finally:
            user_db_cli.close() 

        try:
            # print(process_dict_list[target_id]['process_id'])
            os.kill(process_dict_list[target_id]['process_id'],9)
        except:
            raise ValueError(f"{source_ne}{target_ne}端口监控进程异常结束") 
        # 在数据库中清理结束的进程
        finally:
            process_dict_list.pop(target_id)
            user_db_cli.set_value(table_name,local_ip,process_dict_list)
            # 将文件挂载至容器
            datafile = f"{topo}_{source_ne}{target_ne}_queue_data.csv"
            data_file_path = os.path.abspath(datafile)
            # print(data_file_path + " " + ne_id )
            upload_manager = UploadFile(container_id = ne_id, file_path = data_file_path)
            result = upload_manager.cp_file() 
            if not result:
                raise RuntimeError("文件存储失败")
            # 挂载成功删除本地文件
            os.remove(data_file_path)
    return True

def deploy_tc_queue_monitor(user, topo, interfaces:dict):
    '''
    根据master的创建信号，在数据库中查询创建列表，并创建监控程序，最终返回一
    个监控程序的进程列表供后续关闭进程。

    Args:
        user: 用户名
        topo: 拓扑名
        interfaces: 链路两端端口
        
    Returns:
        processing_dict: 各监控程序的进程号字典
    '''
    user_db_map = UserMapRedis()
    user_db_cli = user_db_map.get_user_db(user)
    user_db_map.close()
    processing_list = []
    container_id = None
    for interface in interfaces:
        start_signal = True
        try:
            # 通过source_ne与target_ne找到网卡接口，容器PID
            source_ne = interface['source_ne']
            target_ne = interface['target_ne']
            table_name = f'{topo}_{source_ne}'
            dict = user_db_cli.get_all_values(table_name)
            nic = ''
            for key, value in dict.items():
                if key.startswith('li') and value['name'] == f'{source_ne}{target_ne}':
                    nic = value['nic']
            table = f"{topo}_{source_ne}"
            container_id  = user_db_cli.get_all_values(table)['NEid']
            pid = get_pid(container_id)
            if not nic or not container_id:
                raise ValueError(f'链路端口{source_ne}{target_ne}中，节点信息出错')
            
            # 检查端口是否已经存在进程，若有则跳过
            table_name = f'{topo}_tc_queue_monitor_process'
            if user_db_cli.check_exist(table_name, local_ip):
                monitor_list = user_db_cli.get_value(table_name, local_ip)
                for info_dict in monitor_list:
                    if info_dict['port'] == f'{source_ne}{target_ne}':
                        start_signal = False
                        break

        except:
            raise
        finally:
            user_db_cli.close()  
        
        if not start_signal: continue
            
        try:
            # 根据节点以及网卡信息，启动监控进程
            t = multiprocessing.Process(
                target=_deploy_tc_queue_monitor, 
                args=(user, topo, container_id, pid, nic, source_ne, target_ne)
                )
            t.start()
            info_dict = {'ne': source_ne,  'port': f'{source_ne}{target_ne}', 'nic_intf': nic, 'process_id': t.pid}
            # 保存进程号列表，以便后续停止
            processing_list.append(info_dict)
        except RuntimeError as e:
            raise RuntimeError(e.args[0])
        finally:
            pass
    return processing_list

if __name__ == "__main__":
    pass
