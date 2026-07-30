import json
from flask_login import login_required
import requests
from vemu_uestc.Function_layer.expr_monitor_master import clear_divide_info

from flask import Blueprint, request
from flask.views import MethodView
import grequests
import threading

from ....vemu_config.config import PROJ_CONFIG
from ....Function_layer import master_expr_monitor
from ....tools import get_host_ip

from ....tools.log_tools import UserLogLevel, UserLogger, FLASK_LOGGER

# 自定义一个测试用的json描述文件
one_multi = {
    "user":"xc",
    "expr":"expr1",
    "topo":"test_topo1",
    "events_to_monitor":{
        1:{
            "performance": "throughput",
            "src":{
                "ne_name":"h1",
                "nic_ip":"192.168.1.2",
                "port":"",
                },
            "dst":{
                "ne_name":"h4",
                "nic_ip":"192.168.1.5", 
                "port":"",
                },
            "proto_type":"tcp",
        }, 
        2:{
            "performance": "throughput",
            "src":{
                "ne_name":"h2",
                "nic_ip":"192.168.1.3",
                "port":"",
                },
            "dst":{
                "ne_name":"h4",
                "nic_ip":"192.168.1.5", 
                "port":"",
                },
            "proto_type":"tcp",
        }, 
    },
}

multi_one = {
    "user":"xc",
    "expr":"expr2",
    "topo":"test_topo1",
    "events_to_monitor":{
        1:{
            "performance": "throughput",
            "src":{
                "ne_name":"h1",
                "nic_ip":"192.168.1.2",
                "port":"",
                },
            "dst":{
                "ne_name":"h3",
                "nic_ip":"192.168.1.4", 
                "port":"",
                },
            "proto_type":"tcp",
        }, 
        2:{
            "performance": "throughput",
            "src":{
                "ne_name":"h1",
                "nic_ip":"192.168.1.2",
                "port":"",
                },
            "dst":{
                "ne_name":"h4",
                "nic_ip":"192.168.1.5", 
                "port":"",
                },
            "proto_type":"tcp",
        }, 
    },
}


# 暂时使用的是分布式队列， 如果后续有问题
# 则优先改回来原来的版本
class MonitorAPI(MethodView):
    '''
    POST    /master/monitor/
    DELETE  /master/monitor/
    前端传来的json描述文件如下:
    '''

    def post(self):
        data = json.loads(request.get_data(as_text=True))
        info = {'user': data['user'], 'topo': data['topo'], 'expr': data['expr']}
        # try:
        #     worker_ip_set = set(master_expr_monitor.handle_monitor_info(data))
        # except:
        #     return {'code': 0, 'msg': '监控实验数据库信息写入失败'}
        
        worker_ip_set = set(master_expr_monitor.handle_monitor_info(data))
        FLASK_LOGGER.debug(worker_ip_set)
        # 并发请求
        req_urls = []
        for worker_ip in  worker_ip_set:
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/monitor/' # 写死了
            req_urls.append((req_url, info))
        # try:
        # rs= (async_requests.post(url, json=info) for url, info in req_urls)
        # print('mapping request to workers...')
        # resp_result = async_requests.map(rs)
        rs= (grequests.post(url, json=info) for url, info in req_urls)
        FLASK_LOGGER.debug('mapping request to workers...')
        FLASK_LOGGER.debug(f'请求的url为 {req_urls}')
        resp_result = grequests.map(rs)
        resp_status = [resp for resp in resp_result]
        FLASK_LOGGER.debug('监控请求完成, requests done! 返回的响应是 resp_status '
              f'is {resp_status}')
        resp_status_code = [resp.json()['code'] for resp in resp_result]
        FLASK_LOGGER.debug(f'resp_status_code is: {resp_status_code}')
        # except AttributeError:
        #     return {'code': 0, 'msg': '链路监控创建失败'}
        if not all(resp_status_code):
            return {'code': 0, 'msg': '链路监控创建失败'}
       

        # 日志输出
        user,topo,expr = info['user'], info['topo'], info['expr']
        logger = UserLogger(user, UserLogLevel.Second, topo)
        logger.log_to_mysql(f'启动监控服务{expr}')

        return {'code': 1, 'msg': '链路监控创建成功'}
        # 顺序请求
        ##########################################################################
        # resp_result = []
        # for worker_ip in worker_ip_set:
        #     print(worker_ip)
        #     req_url = f'http://{worker_ip}:5001/worker/monitor/'
        #     result = requests.post(req_url, json=info)
        #     resp_result.append(result.json()['code'])
        # if not all(resp_result):
        #     return {'code': 0, 'msg': '链路监控创建失败'}
        # return {'code': 1, 'msg': '链路监控创建成功'}
        ##########################################################################
        

    # 删除时，需要传入的是 {'user': 'xc', 'expr': 'expr1', 'topo': 'test_topo1'}
 
    def delete(self):
        '''
        {
            "tasks": [
                {"task_id": ""},
                {"task_id": ""}
            ],
            "code": 1,
            "msg": ""   
        }
        '''
        data = json.loads(request.get_data(as_text=True))
        worker_ip_set = set(master_expr_monitor.handle_user_terminal_signal(data))
        FLASK_LOGGER.debug(f'data in master_monitor: {data}')
        FLASK_LOGGER.debug(worker_ip_set)
        resp_result = []
        
        # 异步停止
        req_urls = []
        for worker_ip in worker_ip_set:
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/monitor/'
            req_urls.append(req_url)
        # rs = (async_requests.delete(url, json=data) for url in req_urls)
        # resp_result = async_requests.map(rs)
        rs = (grequests.delete(url, json=data) for url in req_urls)
        resp_result = grequests.map(rs)
        FLASK_LOGGER.debug(f'发送监控停止请求后的结果\n')
        FLASK_LOGGER.debug(resp_result)
        FLASK_LOGGER.debug(rs)
        resp_status = [resp.json()['code'] for resp in resp_result]
        resp_task_ids = [resp.json()['task_id'] for resp in resp_result]
        # ????   结果的存储？？？？？
        FLASK_LOGGER.debug(resp_status)
        FLASK_LOGGER.debug(resp_task_ids)
        if not all(resp_status):
            return {'code': 0, 'msg': '链路监控停止失败'}
        resp_info = dict(tasks=[])

        for resp in resp_result:
            resp_json = resp.json()
            resp_info['tasks'].append({
                'task_id': resp_json['task_id']})

        p = threading.Thread( # 使用多进程的话会报change parent错误，然后停止轮询
            target=master_expr_monitor.send_calc_signal_until_save_done, 
            args=(data['user'], data['topo'], data['expr'], resp_task_ids))
        p.start()

        # clear_divide_info(data['user'], data['topo'], data['expr'])

        resp_info.update({'code': 1, 'msg': '链路监控停止成功'})
        #日志输出
        user,topo,expr= data['user'],data['topo'], data['expr']
        logger = UserLogger(user, UserLogLevel.Second, topo)
        logger.log_to_mysql(f'停止监控服务{expr}')

        return resp_info
        #顺序停止
        # for worker_ip in worker_ip_set:
        #     print(worker_ip)
        #     req_url = f'http://{worker_ip}:5001/worker/monitor/'
        #     resp = requests.delete(req_url, json=data).json()
        #     print(f'resp is {resp}')
        #     resp_result.append(resp['code'])
        #     resp_info['tasks'].append({'task_id': resp['task_id'], 'parent_id': resp['parent_id']})
        # if not all(resp_result):
        #     return {'code': 0, 'msg': '链路监控停止失败'}
        # resp_info.update({'code':1, 'msg': '链路监控停止成功'})
        # return resp_info


    def get(self):
        return {'msg': 'this url can be routed'}

class MonitorTcQueueAPI(MethodView):
    '''
    POST /master/monitor/tc/queue/     启动端口监控程序
    DELETE /master/monitor/tc/queue/   停止端口监控程序
    监控端口tc队列长度
    {
        "user": "wtx",  
	    "topo": "628",  
	    "interfaces": [
                {
                    "source_ne": "h1",
                    "target_ne": "s1"     
                },
                {
                    "source_ne": "h1",
                    "target_ne": "s2"     
                }
                ...
        ]
    }

    '''

    def post(self):
        return self._handle_req_info(request, 'post')

    def delete(self):
        return self._handle_req_info(request, 'delete')

    def _handle_req_info(self, request, method: str):
        data = json.loads(request.get_data(as_text=True))
        user, topo = data['user'], data['topo']
        # 确定节点所在worker的ip
        try:
            worker_monitor_tc_queue_dict = self._category_ne_worker(user, topo, data)
        except ValueError as e:
            return {'code': 0, 'msg':e.args[0]}
        # 转发请求至worker
        # return worker_monitor_tc_queue_dict
        resp_result = []
        for worker_ip, monitor_tc_queue_tasks in worker_monitor_tc_queue_dict.items():
            info_dict = {'user': user, 'topo': topo, 'interfaces': monitor_tc_queue_tasks}
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/monitor/tc/queue/'
            req_method = getattr(requests, method)
            resp_result.append(req_method(req_url, json=info_dict))
        error_msgs_dict = {}
        error_msgs_dict['worker_url'] = []
        error_msgs_dict['worker_msg'] = []
        resp_result_list = [] 
        for i,resp in enumerate(resp_result, 1):
            resp_result_list.append(resp.json()["code"])
            if resp.json()["code"] != 1:
                return_msg = resp.json()["msg"]
                error_msgs_dict['worker_url'].append(f"请求url：{req_url}") 
                error_msgs_dict['worker_msg'].append(f"{return_msg}")
                
        # print(resp_result_list)
        if not all(resp_result_list):
            FLASK_LOGGER.debug(error_msgs_dict)
            if method == 'delete':
                return {'code': 0, 'msg':  " ".join(error_msgs_dict['worker_msg'])}
            else:
                return {'code': 0, 'msg':  " ".join(error_msgs_dict['worker_msg'])}
        if method == 'delete':
            return {'code': 1, 'msg': '端口监控停止成功'}
        else:
            return {'code': 1, 'msg': '端口监控启动成功'}


    def _category_ne_worker(self, user, topo, data:dict):
        '''
        根据source_ne进行初步检查筛选，后面还需要实现对target_ne的检查
        Args:
            data: 前端传来的TC监控的API dict
        Returns:
            info_dict： 将ne信息根据worker_ip分类并查询补充数据库相关信息的字典
                                    {'worker_ip': [temp_list]}                         
        '''
        user_map_redis = UserMapRedis()
        user_db_cli = user_map_redis.get_user_db(user)
        info_dict = {}
        interfaces = data['interfaces']
        for interface in interfaces:
            try:
                worker_ip = user_db_cli.get_worker_ip_by_ne_name(topo, interface['source_ne'])
                temp_list = info_dict.setdefault(worker_ip, [])
                temp_list.append(interface)
            except:
                raise ValueError(f"拓扑{topo}或者源节点{interface['source_ne']}信息出错")
        user_map_redis.close()
        user_db_cli.close()
        return info_dict