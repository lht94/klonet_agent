# 此文件位于master上，负责接收来自前端的监控服务定义，并分发监控服务至各worker
from logging import error
import pprint
import uuid
import requests
import time
import copy
from pprint import pprint
from celery.result import AsyncResult

from ..vemu_config.config import PROJ_CONFIG
from ..Service_layer.redisAPI import USER_DB_COUNT, UserMapRedis
from ..webserver.socketio_handlers import push_msg
from ..webserver import celery


LOOP_INTERVAL_S = 1  # 轮询间隔（秒）

# 可以写一个显示关闭数据库连接的装饰器

def handle_monitor_info(monitor_info:dict) -> None:
    '''
    处理前端传来的json文件，将事件转换为在各worker上创建的子事件，并将子事件存入对应
    worker的创建列表

    Args:
        monitor_info: 前端传来的json文件，例如：
            {    
                {    
                    "user":"用户名",   
                    "expr":"实验名，同一用户的多次实验，实验名不得重复 ",  
                    "topo":"拓扑名",    
                    "events_to_monitor":[   
                        {
                            "performace":"有一系列的可选项，目前支持为
                                         throughput / delay / loss / srtt ",  
                            "params":{ # 本性能指标的特有参数  
                            } 
                        }   
                    ]        
                }    
            }  


    Returns:
        workers_which_have_job: 创建了监控程序的worker的列表
    '''
    user = monitor_info["user"]
    user_db_map = UserMapRedis()
    user_db_cli = user_db_map.get_user_db(user)
    user_db_map.close()

    expr = monitor_info["expr"]
    topo = monitor_info["topo"]
    events_to_monitor = user_db_cli.get_value(f"{topo}_monitor", expr)
    monitor_info["events_to_monitor"] = events_to_monitor

    # 存储events_to_monitor的key-value于redis中
    del monitor_info["user"]
    del monitor_info["expr"]
    del monitor_info["topo"]
    table = f"{topo}_{expr}_monitor"
    user_db_cli.set_all_values(table, monitor_info)

    sub_events = _convert_events_to_subevents(user_db_cli, topo, 
                                            events_to_monitor)
    workers_which_have_job = _save_sub_events_to_db(user_db_cli, topo, 
                                                    expr, sub_events)
    print("saving expr data to redis...")
    user_db_cli.close()
    return workers_which_have_job
    # except:
    #     raise
    # finally:
    #     user_db_cli.close()


def _convert_events_to_subevents(user_db_cli, topo, 
                                 events_to_monitor:list) -> list:
    '''
    处理前端发来的json文件，将事件转换为子事件列表

    Args:
        user_db_cli: 用户数据库管理实例
        topo: 拓扑名
        events_to_monitor: 要监控的事件列表，即前端传来的json的
                           ["events_to_monitor"]里的内容

    Returns:
        sub_events: 子事件列表，包含所有事件的子事件，列表的元素为一个字典：
            {
                "event_seq":子事件序号, 从1开始递增,
                "performance": 要监控的性能指标,
                "owned_worker":子事件所在worker的ip地址,
                "params": {针对每个指标子事件参数不一样}, 
            }
    '''


    sub_events = []

    for event_seq, event in enumerate(events_to_monitor, 1):
        sub_events_ = _convert_to_sub_events_by_perf(
            user_db_cli, topo, str(event_seq), 
            event["performance"], event["params"])
        sub_events.extend(sub_events_)

    return sub_events

def _convert_to_sub_events_by_perf(user_db_cli, topo:str, event_seq:str, 
                                   performance:str, params:dict) -> list:
    '''
    根据性能指标的不同将各个事件转换为数个子事件

    Args:
        user_db_cli: 用户数据库管理实例
        event_seq: 事件序号
        performance: 该事件要监控的性能指标
        params: 该性能指标的参数

    Returns:
        sub_events_: 子事件列表，包含当前事件的子事件，列表的元素为一个字典：
            {
                "seq": 子事件序号, 
                "performance": 要监控的性能指标,
                "owned_worker": 子事件所在worker的ip地址,
                "params": {针对每个指标子事件参数不一样}, 
            }    
    '''
    sub_events_ = []
    sub_event = {"seq":event_seq, "performance":performance}

    if performance == "throughput":      
        sub_event["params"] = params
        sub_event["owned_worker"] = user_db_cli.get_worker_ip_by_ne_name(
            topo, params["dst"]["ne_name"])
        # 需要在params中加入deploy_list字段，来让worker知道自己需要创建pcap抓包程序至
        # 哪个节点上的网卡(而不需要再查找一次已经查过的信息)
        sub_event["params"]["deploy_list"] = [params["dst"]["ne_name"]] 
        ###kc-test
        print("吞吐子事件sub_event：",sub_event)
        sub_events_.append(sub_event)
    elif performance == "loss" or performance == "delay":
        sub_event["params"] = params

        worker_ip_of_src = user_db_cli.get_worker_ip_by_ne_name(
            topo, params["src"]["ne_name"])
        worker_ip_of_dst = user_db_cli.get_worker_ip_by_ne_name(
            topo, params["dst"]["ne_name"])   

        sub_event["params"]["deploy_list"] = []
        if worker_ip_of_src == worker_ip_of_dst:
            # 源和目的在同一worker上
            sub_event["owned_worker"] = worker_ip_of_src
            sub_event["params"]["deploy_list"].append(params["src"]["ne_name"])
            sub_event["params"]["deploy_list"].append(params["dst"]["ne_name"])
            
            sub_events_.append(sub_event)
        else:
            # 源和目的在不同worker上
            sub_event["owned_worker"] = worker_ip_of_src
            sub_event["params"]["deploy_list"] = [params["src"]["ne_name"]]
            
            sub_events_.append(sub_event)

            sub_events_ = copy.deepcopy(sub_events_)

            sub_event["owned_worker"] = worker_ip_of_dst
            sub_event["params"]["deploy_list"] = [params["dst"]["ne_name"]]
            
            sub_events_.append(sub_event)
    elif performance == "srtt":
        sub_event["params"] = params
        sub_event["owned_worker"] = user_db_cli.get_worker_ip_by_ne_name(
            topo, params["src"]["ne_name"]
        )
        sub_events_.append(sub_event)
    else:
        # TODO(MaTie): raise一个异常
        print("This performance is not currently supported")
       
    return sub_events_

def _save_sub_events_to_db(user_db_cli, topo:str, 
                           expr:str, sub_events:list) -> None:
    '''
    将子事件存至数据库中对应worker的创建列表，存储的格式为：
        topo_expr_monitor:{
            "worker_ip_1":[
                {
                    "seq": 子事件序号, 
                    "performance": 要监控的性能指标,
                    "params": {针对每个指标子事件参数不一样},
                },
                ... 
            ]
        }

    Args:
        user_db_cli: 用户数据库管理实例
        topo: 拓扑名
        expr: 实验名
        sub_events: 子事件列表，包含所有事件的子事件，列表的元素为一个字典：
            {
                "seq": 子事件序号, 
                "performance": 要监控的性能指标,
                "owned_worker": 子事件所在worker的ip地址,
                "params": {针对每个指标子事件参数不一样}, 
            }  

    Returns:
        workers_which_have_job: 有创建任务的worker_ip列表
    '''
    table = '{}_{}_monitor'.format(topo, expr)
    workers_which_have_job = []
    print('sub_events is:\n ')
    pprint(sub_events)
    temp_value = {}
    for sub_event in sub_events:
        print('sub_event is:\n')
        pprint(sub_event)
        worker = sub_event["owned_worker"]
        workers_which_have_job.append(worker)
        temp_list = temp_value.setdefault(worker, [])  
        temp_list.append(sub_event)

    pprint(temp_value)
    user_db_cli.set_all_values(table, temp_value)

    return workers_which_have_job

def _send_deploy_signal_to_workers(user, topo, expr, workers_which_have_job):
    '''
    向有监控创建任务的worker发送开始创建信号

    Args:
        user: 用户数据库管理实例
        topo: 拓扑名
        expr: 实验名
        workers_which_have_job: 有创建任务的worker_ip列表

    Returns:
        None
    '''
    info_dict = {
         'user': user, 
         'topo': topo, 
         'expr': expr,
    }
    for worker in workers_which_have_job:
        # TODO(MaTie):数据库地址、端口等的全局配置文件？
        print(f"send deploy signal to {worker}")
        worker_url = f'http://{worker}:{PROJ_CONFIG.worker_port}/worker/monitor/'
        requests.post(worker_url, json=info_dict)    

def handle_user_terminal_signal(signal):
    '''
    前端发来结束实验信号时调用此函数，向各创建过本次实验的监控程序的worker
    发送结束实验信号，信号为json格式

    Args:
        signal: 前端发来的结束信号，为json格式，例如：
            {
                "user": "test",
                "expr": "expr1",
                "topo": "test_topo"
            }        

    Returns:
        workers_which_have_job: 创建了监控程序的worker的列表
    '''
    user_db_map = UserMapRedis()
    user_db_cli = user_db_map.get_user_db(signal["user"])
    user_db_map.close()
    try:
        table = '{}_{}_monitor'.format(signal["topo"], signal["expr"])
        all_values = user_db_cli.get_all_values(table)
        workers_which_have_job = list(all_values.keys())
        # 获取的列表中有"events_to_monitor"这一项，因此需要去除
        try:
            workers_which_have_job.remove("events_to_monitor")
        except:
            pass
        return workers_which_have_job
    except:
        raise
    finally:
        user_db_cli.close()

def _send_calc_signal_to_data_server(user, topo, expr):
    '''
    发送开始计算信号至数据存储与计算服务器，数据存储与计算服务器收到此信号后
    会对task_ids的状态进行轮询，待该次实验下所有的存储任务都执行完毕后，数据
    存储与计算服务器将开始指标数据的计算工作。

    Args:
        user: 用户名
        topo: 拓扑名
        expr: 实验名
        
    Returns:
        resp: HTTP请求的响应
    '''
    info_dict = {
         'user': user, 
         'topo': topo, 
         'expr': expr,
    }
    data_server_url = (f"http://{PROJ_CONFIG.data_server_ip}:"
                       f"{PROJ_CONFIG.data_server_port}"
                       "/data-server/expr/")
    try:
        resp = requests.post(data_server_url, json=info_dict)
        print("send calc signal to data server.")
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"Detect error: {e}, "
            "Do you forget to start data_server_main?")

    return resp

def _check_task_status(task_ids):
    '''
    对task_ids的状态进行轮询，直至任务全部完成或有某一任务失败。

    Args:
        task_ids: 任务id的集合
        
    Returns:
        result: true则任务全部成功，false则有任务失败
    '''
    print("check task status...")
    master_ip = PROJ_CONFIG.master_ip
    task_failed = False
    while task_ids: # TODO：有无死循环风险？
        # 遍历拷贝的task_ids，操作原始的task_ids
        for i, task_id in enumerate(task_ids[:]):
            async_result = AsyncResult(id=task_id, app=celery)
            if async_result.status == "SUCCESS":
                task_ids.remove(task_id)
                async_result.forget()
            elif async_result.status == "FAILURE":
                task_failed = True  # 只有有一个任务失败就返回失败
                async_result.forget()
        
        if task_failed:
            print("some task failed.")
            # 释放剩余资源
            for task_id in task_ids:
                async_result = AsyncResult(task_id, app=celery)
                async_result.forget()
            break

        time.sleep(LOOP_INTERVAL_S)

    if not task_failed: 
        print("All tasks are successful!")
    return not task_failed

def send_calc_signal_until_save_done(user, topo, expr, task_ids):
    '''
    轮询存储任务的task_id，待所有存储任务完成后，向数据服务器发送开始计算信号
    
    Args:
        user: 用户名
        topo: 拓扑名
        expr: 实验名    
        task_ids: 任务id的集合
    Returns:
        true则任务全部成功，false则有任务失败 # TODO：不应该只有这么简单的返回值，
        若发送信号失败怎么办？
    '''
    print("start send_calc_signal_until_save_done process")
    push_msg("开始将网络实验监控原始数据保存至数据服务器")
    print(f"task_ids: {task_ids}")
    if _check_task_status(task_ids):
        print('计算原始指标。。。')
        push_msg("网络实验监控原始数据已保存至数据服务器，您现在可以下载原始数据。"
                 "开始将原始数据计算为指标数据...") # TODO(MaTie, 20210416): 会有广播问题
        print("all save tasks done!")
        _send_calc_signal_to_data_server(user, topo, expr)
        return True
    else:
        print("有任务失败")
        return False

def clear_divide_info(user, topo, expr):
    '''
    清除redis中监控的切分信息

    Args:
        user: 用户名
        topo: 拓扑名
        expr: 实验名
    Returns:
        None，若出错则异常
    '''
    user_db_map = UserMapRedis()
    user_db_cli = user_db_map.get_user_db(user)
    user_db_map.close()
    try:
        table = f'{topo}_{expr}_monitor'
        user_db_cli.del_table(table)
        return True
    except:
        raise
    finally:
        user_db_cli.close()

def expr_monitor_master_main():
    '''
    仅用于测试，请忽略本函数
    '''
    pass


if __name__ == '__main__':
    pass