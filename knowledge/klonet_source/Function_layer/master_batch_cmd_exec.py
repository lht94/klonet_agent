import json
from urllib.error import HTTPError
import requests
import threading
from ..tools.upper_level_redis_API import get_workers_to_nes
from ..vemu_config.config import PROJ_CONFIG

def exec_cmd_in_workers(json_dict):
    '''
    解析容器列表，确定要执行命令的容器所在worker，并使用异步请求使这些worker对命令进行
    处理

    Args:
        json_dict = {
            'user': "用户名",
            'topo': "项目名（拓扑名）",
            'ctns': {
                "list_type": "all/specified_ctn_type/specified_ctn_list",
                "list": [] # all时为空，
                        # specified_ctn_type时为类型列表，如["hosts", "controllers"]
                        # specified_ctn_list时为容器列表，如["h1", "h2", "s1"]
            },
            'cmd': "要执行的命令，如ls"
            'cmd_timeout_s':"1"(命令结果获取的超时时间，默认值为1s，可配置范围为(0,300]s)
            "block":"true/false"，是否阻塞执行命令
        }

    Returns:
        返回各worker上的各容器的命令执行情况
    '''
    user, topo, ctns, cmd, cmd_timeout_s, block = json_dict["user"], json_dict["topo"], \
        json_dict["ctns"], json_dict["cmd"], json_dict["cmd_timeout_s"], json_dict["block"]

    workers2ctns = get_workers2ctns(user, topo, ctns)
    print(workers2ctns)

    responses = send_requests_to_workers(user, topo, workers2ctns, cmd, cmd_timeout_s, block)

    return responses


def get_workers2ctns(user, topo, ctns):
    '''
    获取要执行cmd的worker及其容器列表

    Args:
        'ctns': {
            "list_type": "all/specified_ctn_type/specified_ctn_list",
            "list": [] # all时为空，
                    # specified_ctn_type时为类型列表，如["hosts", "controllers"]
                    # specified_ctn_list时为容器列表，如["h1", "h2", "s1"]
        }

    Returns:
        'workers2ctns': {
            "worker ip": [容器列表],
            ...
        }
        如'workers2ctns': {
            "10.1.1.105": [h1, h2, h3],
            ...
        }
    '''
    workers2ctns = {}

    workers_to_nes = get_workers_to_nes(user, topo)

    if ctns["list_type"] == "all":
        # 获取每个worker上的所有容器列表
        for worker_ip, ctn_data in workers_to_nes.items():
            workers2ctns[worker_ip] = []
            for _, ne_list in ctn_data.items():
                workers2ctns[worker_ip].extend(ne_list)
    elif ctns["list_type"] == "specified_ctn_type":
        # 获取每个worker上的指定类型的容器列表
        for worker_ip, ctn_data in workers_to_nes.items():
            workers2ctns[worker_ip] = []
            for ctn_type in ctns["list"]:
                workers2ctns[worker_ip].extend(ctn_data[ctn_type])
    elif ctns["list_type"] == "specified_ctn_list":
        # 获取每个worker上的指定容器列表
        ctns_set = set(ctns["list"])
        for worker_ip, ctn_data in workers_to_nes.items():
            workers2ctns[worker_ip] = []
            # 每个worker上的容器列表与指定容器列表取交集，以获取每个worker上的指定
            # 容器列表
            for _, ne_list in ctn_data.items():
                workers2ctns[worker_ip].extend(list(ctns_set & set(ne_list)))
    else:
        raise ValueError(f"Unsupported list_type: {ctns['list_type']}")

    return workers2ctns

def send_requests_to_workers(user, topo, workers2ctns, cmd, cmd_timeout_s, block):
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
    for worker_ip, ctn_list in workers2ctns.items():
        t = threading.Thread(
            target=send_req_to_worker, 
            args=(user, topo, worker_ip, ctn_list, cmd, cmd_timeout_s, block, responses,))
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    for worker_ip, resp in responses.items():
        if resp["code"] == 0:
            raise RuntimeError(resp["msg"])

    return responses

def send_req_to_worker(user, topo, worker_ip, ctn_list, cmd, cmd_timeout_s, block, responses):
    '''
    向worker发送同步请求，使其在指定容器列表中执行任务
    '''
    try:
        req_json = {"user":user, "topo": topo, "ctn_list": ctn_list, 
                    "cmd": cmd, "cmd_timeout_s":cmd_timeout_s, "block":block}
        resp = requests.post(
            f"http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/batch_exec_cmd/",
            json=req_json)
        print(resp.status_code)
        if resp.status_code == 200:
            responses[worker_ip] = resp.json()
        else:
            raise RuntimeError(f"resp.status_code={resp.status_code}")
    except Exception as e:
        responses[worker_ip] = {"code":0, "msg": repr(e)}