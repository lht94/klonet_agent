import json
from urllib.error import HTTPError
import requests
import threading
from ..tools.upper_level_redis_API import get_workers_to_nes
from ..tools.log_tools import FLASK_LOGGER
from ..vemu_config.config import PROJ_CONFIG

def node_exec_cmd_in_workers(json_dict):
    '''
    解析容器列表，确定要执行命令的容器所在worker，并使用异步请求使这些worker对命令进行
    处理

    Args:
        json_dict = {
                'user': "用户名",
                'topo': "项目名（拓扑名）",
                'node_and_cmd': {
                    "h1":["route add -host 192.168.1.2 dev eth0:0"," route add default gw 192.168.1.1"],    #要执行的多条命令
                    "h2":["route add –net 10.0.0.0/24 gw 192.168.1.129","route add default gw 192.168.1.1"]
                }
                'cmd_timeout_s':"1"(命令结果获取的超时时间，默认值为1s，可配置范围为(0,300]s)
                "block":"true/false"，是否阻塞执行命令
            }
            
    Returns:
        返回各worker上的各容器的命令执行情况
    '''
    user, topo, node_and_cmd = json_dict["user"], json_dict["topo"], json_dict["node_and_cmd"]
    cmd_timeout_s, block = json_dict["cmd_timeout_s"], json_dict["block"]
    workers2node = get_workers2node(user, topo, node_and_cmd)
    FLASK_LOGGER.debug(workers2node)

    responses = send_requests_to_workers(user, topo, workers2node, node_and_cmd, cmd_timeout_s, block)

    return responses


def get_workers2node(user, topo, node_and_cmd):
    '''
    获取要执行cmd的worker及其容器列表

    Args:
        'node_and_cmd': {
                    "h1":["route add -host 192.168.1.2 dev eth0:0"," route add default gw 192.168.1.1"],    #要执行的多条命令
                    "h2":["route add –net 10.0.0.0/24 gw 192.168.1.129","route add default gw 192.168.1.1"]
                }

    Returns:
        'workers2node': {
            "worker ip": [容器列表],
            ...
        }
        如'workers2node': {
            "10.1.1.105": [h1, h2, h3],
            ...
        }
    '''

    workers2node = {}

    workers_to_nes = get_workers_to_nes(user, topo)

    node_list=[]
    for node,_ in node_and_cmd.items():
        node_list.append(node)

    node_set = set(node_list)
    for worker_ip, nes_data in workers_to_nes.items():
        workers2node[worker_ip] = []
        # 每个worker上的容器列表与指定容器列表取交集，以获取每个worker上的指定容器列表
        for _, ne_list in nes_data.items():
            workers2node[worker_ip].extend(list(node_set & set(ne_list)))
    
    return workers2node


def send_requests_to_workers(user, topo, workers2node, node_and_cmd, cmd_timeout_s, block):
    '''
    多线程向worker发送同步请求，使其在指定容器列表中执行任务（worker执行完毕后返回）

    Returns:
        返回各worker上各容器命令的执行情况
        responses = {
            "worker_ip": {
                "code": 0/1, # 1成功，0失败
                "msg": "success"/"失败原因"
                "exec_results": {
                    "容器名": {
                        "exit_code": "容器命令执行返回码"
                        "output": "容器命令执行输出"
                    },
                    ...
                }
            },
            ...
        }
    '''
    threads = []
    responses = {}
    for worker_ip, node_list in workers2node.items():
        t = threading.Thread(
            target=send_req_to_worker, 
            args=(user, topo, worker_ip, node_list, node_and_cmd, cmd_timeout_s, block, responses,))
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    for worker_ip, resp in responses.items():
        if resp["code"] == 0:
            raise RuntimeError(resp["msg"])

    return responses

def send_req_to_worker(user, topo, worker_ip, node_list, node_and_cmd, cmd_timeout_s, block, responses):
    '''
    向worker发送同步请求，使其在指定容器列表中执行任务
    '''
    try:
        req_json = {"user":user, "topo": topo, "node_list": node_list, 
                    "node_and_cmd": node_and_cmd, "cmd_timeout_s":cmd_timeout_s,
                    "block": block}
        resp = requests.post(
            f"http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/node_exec_cmd/",
            json=req_json)
        FLASK_LOGGER.debug(resp.status_code)
        if resp.status_code == 200:
            responses[worker_ip] = resp.json()
        else:
            raise RuntimeError(f"resp.status_code={resp.status_code}")
    except Exception as e:
        responses[worker_ip] = {"code":0, "msg": repr(e)}