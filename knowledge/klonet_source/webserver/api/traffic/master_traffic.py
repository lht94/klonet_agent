from collections import namedtuple
from flask import Blueprint, request
from flask.views import MethodView
import json
import requests
import grequests
import traceback
from flask_login import login_required
from ....Service_layer import MasterTrafficManager, TRAFFIC_ROLES, template2traffic
from ....Service_layer.topo_deploy_errors import TrafficGenError, PackageGenError
from ....Service_layer.redisAPI import UserMapRedis
from ....Service_layer.redis_error import *

from ....vemu_config.config import PROJ_CONFIG
from ....tools.log_tools import UserLogger, UserLogLevel


ResultMsg = namedtuple('ResultMsg', 'success_msg error_msg')


class TrafficAPI(MethodView):
    """
    POST    /master/traffic/
    DELETE  /master/traffic/

    使用在Redis保存的流量数据，传入的data结构如下：
    {
        "user":"",     用户标识
        "topo":"",    topo标识
        "app_name":""    服务流量标识
    }
    目前有四种流量发生其角色： 
    可模拟多种网络场景流量的流发生器：接收端'traffic_server', 发送端'traffic_client'
    基于on/off模型的网络汇聚流量模拟：发送端'pkt_gen2'
    时间间隔具有概率模型的主机端发包模拟：发送端'pkt_gen1'
    }
    """
 
    def post(self):
        # 创建流量时读取数据库信息
        data = json.loads(request.get_data(as_text=True))
        traffic_manager = MasterTrafficManager(data)
        result = {}
        # 这只是将信息写入数据库的过程
        # 这里需要返回需要创建的worker_ip 吗？
        try:
            # server_worker_map 是各个发生器在哪个worker上的映射
            # {'traffic_server_workers': set(), 'traffic_client_workers': set(), 'pkt_gen2_workers': set()}
            server_worker_map = traffic_manager.set_value_to_db()
        except TrafficGenError as e:
            result['msg'] = e.args[0]
        except PackageGenError as e:
            result['msg'] = e.args[0]
        finally:
            traffic_manager.close()
        # 如果上面的已经出错了, 那就直接返回了,不需要进行向worker继续创建的操作
        if result:
            result['code'] = 0
            return result
        info_dict = {'user': data['user'], 'topo': data['topo'], 'app_seq': data['app_name']}
        # 创建发生器
        try:
            # 这里先创建traffic_server 之后
            # 创建traffict_client 和 pkt_gen2应该并行的创建
            # 也许需要改成用aiohttp
            # 或者使用celery进行统一定时之后在运行？
            for role in TRAFFIC_ROLES:
                workers_set_key = f'{role}_workers'
                self._dispatch_requests(server_worker_map[workers_set_key], role, info_dict, "post")
        except Exception as e:
            result['msg'] = e.args[0]
            result['code'] = 0
            traceback.print_exc()
            return result
        # 日志输出
        user, topo, app_name = data['user'], data['topo'], data['app_name']
        logger = UserLogger(user, UserLogLevel.Second, topo)
        logger.log_to_mysql(f'启动流量服务{app_name}')
        return {'code': 1, 'msg': '流量服务创建成功'}

    def _dispatch_requests(self, worker_ip_set:set, role, req_para:dict, choice:str):
        # 顺序请求
        ################################################
        # resp_result = []
        # for worker_ip in worker_ip_set:
        #     req_url = f'http://{worker_ip}:5001/worker/traffic/{role}/'
        #     resp_result.append(requests.post(req_url, json=req_para).json()['code'])    
        # # 反馈信息需要根据role的不同返回不同的信息
        # if not all(resp_result):
        #     raise RuntimeError(msg.error_msg)
        ################################################
        # 异步请求
        req_urls = []
        for worker_ip in worker_ip_set:
            req_urls.append(f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/traffic/{role}/')
        # rs = (async_requests.post(url, json=req_para) for url in req_urls)
        # resp_result = async_requests.map(rs)
        # 在这里请求worker
        if choice == "post":
            msg = self._get_return_deploy_msg(role)
            rs = (grequests.post(url, json=req_para) for url in req_urls)
        elif choice == "delete":
            msg = self._get_return_delete_msg(role)
            rs = (grequests.delete(url, json=req_para) for url in req_urls)
        resp_result = grequests.map(rs)
        resp_status = [resp.json()['code'] for resp in resp_result]
        if not all(resp_status):
            raise RuntimeError(msg.error_msg)

    def _get_return_deploy_msg(self, role):
        if role == 'traffic_server':
            msg = ResultMsg('创建流发生器服务端成功', '创建流发生器服务端失败')
        elif role == 'traffic_client':
            msg = ResultMsg('创建流发生器客户端成功', '创建流发生器客户端失败')
        elif role == 'pkt_gen2':
            msg = ResultMsg('创建包发生器pkt_gen2客户端成功', '创建包发生器pkt_gen2客户端失败')
        elif role == 'pkt_gen1':
            msg = ResultMsg('创建包发生器pkt_gen1客户端成功', '创建包发生器pkt_gen1客户端失败')
        else:
            raise ValueError(f'参数role的值不能为{role},应为server/client/pkt_gen1/pkt_gen2其中之一')
        return msg

    def _get_return_delete_msg(self, role):
        if role == 'traffic_server':
            msg = ResultMsg('删除流发生器服务端成功', '删除流发生器服务端失败')
        elif role == 'traffic_client':
            msg = ResultMsg('删除流发生器客户端成功', '删除流发生器客户端失败')
        elif role == 'pkt_gen2':
            msg = ResultMsg('删除包发生器pkt_gen2客户端成功', '删除包发生器pkt_gen2客户端失败')
        elif role == 'pkt_gen1':
            msg = ResultMsg('删除包发生器pkt_gen1客户端成功', '删除包发生器pkt_gen1客户端失败')
        else:
            raise ValueError(f'参数role的值不能为{role}， 应为server/client/pkt_gen1/pkt_gen2其中之一')
        return msg

    def delete(self):
        # TODO:设置停止逻辑，访问worker的traffic delete api
        # 停止之后，删除切分信息
        # 1、先停止程序
        # 2、删除切分表项
        data = json.loads(request.get_data(as_text=True))
        traffic_manager = MasterTrafficManager(data)
        # server_worker_map
        # {'traffic_server_workers': set(), 'traffic_client_workers': set(), 'pkt_gen2_workers': set()}
        server_worker_map = traffic_manager.get_value_from_db()
        result = {}
        info_dict = {'user': data['user'], 'topo': data['topo'], 'app_seq': data['app_name']}
        try:
            # 这里先创建traffic_server 之后
            # 创建traffict_client 和 pkt_gen2应该并行的创建
            # 也许需要改成用aiohttp
            # 或者使用celery进行统一定时之后在运行？
            for role in TRAFFIC_ROLES:
                workers_set_key = f'{role}_workers'
                self._dispatch_requests(server_worker_map[workers_set_key], role, info_dict, "delete")
            traffic_manager.del_value_from_db(server_worker_map)
        except Exception as e:
            result['msg'] = e.args[0]
            result['code'] = 0
            return result
        finally:
            traffic_manager.close()
        # 日志输出
        user,topo,app_name = data['user'], data['topo'], data['app_name']
        logger = UserLogger(user, UserLogLevel.Second, topo)
        logger.log_to_mysql(f'停止流量服务{app_name}')
        return {'code': 1, 'msg': '流量服务删除成功'}


        
        # data = json.loads(request.get_data(as_text=True))
        # traffic_manager = MasterTrafficManager(data)
        # result = traffic_manager.delete_traffic_info()
        # return result
        # return {'code': 0, 'msg': 'method not allowed', 'status': 405}

 
    def get(self):
        return {'msg': 'this url can be routed', 'code':0}


class TrafficRedisAPI(MethodView):
    """
    POST    /re/project/{project_name}/traffic_app/
    DELETE  /re/project/{project_name}/traffic_app/
    GET     /re/project/{project_name}/traffic_app/

    使用已经定义好了的发生器创建的描述文件
    	{  
        "app_name":"",      服务流量标识
        "pkt_gen1":[],      
        "pkt_gen2":[],
        "traffic_gen":[],    
        "back_traffic":[],    
        "real_app":{},    ???
        "trace":{}    ???
	}    
    """
    # TODO:目前delete和get还是需要通过traffic_info获取user信息

    def post(self, project_name):
        data = json.loads(request.get_data(as_text=True))
        print(project_name, data)
        traffic_redis_manager = RedisTrafficManager(project_name, data)
        result = traffic_redis_manager.save_value_to_redis()
        traffic_redis_manager.close()
        return result
  
    def delete(self, project_name):
        data = json.loads(request.get_data(as_text=True))
        traffic_redis_manager = RedisTrafficManager(project_name, data)
        result = traffic_redis_manager.del_value_from_redis()
        traffic_redis_manager.close()
        return result
    

    def get(self, project_name):
        data = json.loads(request.get_data(as_text=True))
        traffic_redis_manager = RedisTrafficManager(project_name, data)
        result = traffic_redis_manager.get_value_from_redis()
        traffic_redis_manager.close()
        return result
        

# class TrafficRedisAppAPI(MethodView):
#     """
#     PUT     /re/project/{project_name}/traffic_app/{app_name}
#     DELETE  /re/project/{project_name}/traffic_app/{app_name}
#     GET     /re/project/{project_name}/traffic_app/{app_name}

#     使用已经定义好了的发生器创建的描述文件
#     	{    
#         "user":"",     用户标识
#         "topo":"",    topo标识
#         "app_seq":"",      服务流量标识
#         "pkt_gen1":[],      
#         "pkt_gen2":[],
#         "traffic_gen":[],    
#         "back_traffic":[],    
#         "real_app":{},    ???
#         "trace":{}    ???
# 	}    
#     """
#     # TODO:目前delete和get还是需要通过traffic_info获取user信息
#     def put(self, project_name, app_name):
#         data = json.loads(request.get_data(as_text=True))
#         print(project_name, app_name, data)
#         traffic_redis_app_manager = RedisTrafficAppManager(project_name, app_name, data)
#         result = traffic_redis_app_manager.modify_app_to_redis()
#         traffic_redis_app_manager.close()
#         # save_value_to_redis 直接返回存入redis的成功与否
#         return result
    
#     def delete(self, project_name, app_name):
#         data = json.loads(request.get_data(as_text=True))
#         traffic_redis_app_manager = RedisTrafficAppManager(project_name, app_name, data)
#         result = traffic_redis_app_manager.del_app_from_redis()
#         traffic_redis_app_manager.close()
#         return result
    
#     def get(self, project_name, app_name):
#         data = json.loads(request.get_data(as_text=True))
#         traffic_redis_app_manager = RedisTrafficAppManager(project_name, app_name, data)
#         result = traffic_redis_app_manager.get_app_from_redis()
#         traffic_redis_app_manager.close()
#         return result


class TemplateUseAPI(MethodView):
    """
    POST    /master/traffic/
    DELETE  /master/traffic/

    使用在Redis保存的流量数据，传入的data结构如下：
    {
        "user":"",     用户标识
        "topo":"",    topo标识
        "app_name":"",   模板名
        "tra_name":"",  流量名
        "src_node":"",
        "dst_node":""
        "src_ip":"",
        "dst_ip":""
    }
    目前仅支持pkgen1
    }
    """
 
    def post(self):
        # 创建流量时读取数据库信息
        data1 = json.loads(request.get_data(as_text=True))
        template2traffic(data1)
        data = {"user":data1["user"],"topo":data1["topo"],"app_name":data1["tra_name"]}
        traffic_manager = MasterTrafficManager(data)
        result = {}
        # 这只是将信息写入数据库的过程
        # 这里需要返回需要创建的worker_ip 吗？
        try:
            # server_worker_map 是各个发生器在哪个worker上的映射
            # {'traffic_server_workers': set(), 'traffic_client_workers': set(), 'pkt_gen2_workers': set()}
            server_worker_map = traffic_manager.set_value_to_db()
        except TrafficGenError as e:
            result['msg'] = e.args[0]
        except PackageGenError as e:
            result['msg'] = e.args[0]
        finally:
            traffic_manager.close()
        # 如果上面的已经出错了, 那就直接返回了,不需要进行向worker继续创建的操作
        if result:
            result['code'] = 0
            return result
        info_dict = {'user': data['user'], 'topo': data['topo'], 'app_seq': data['app_name']}
        # 创建发生器
        try:
            # 这里先创建traffic_server 之后
            # 创建traffict_client 和 pkt_gen2应该并行的创建
            # 也许需要改成用aiohttp
            # 或者使用celery进行统一定时之后在运行？
            for role in TRAFFIC_ROLES:
                workers_set_key = f'{role}_workers'
                self._dispatch_requests(server_worker_map[workers_set_key], role, info_dict, "post")
        except Exception as e:
            result['msg'] = e.args[0]
            result['code'] = 0
            traceback.print_exc()
            return result
        # 日志输出
        user, topo, app_name = data['user'], data['topo'], data['app_name']
        logger = UserLogger(user, UserLogLevel.Second, topo)
        logger.log_to_mysql(f'启动流量服务{app_name}')
        return {'code': 1, 'msg': '流量服务创建成功'}

    def _dispatch_requests(self, worker_ip_set:set, role, req_para:dict, choice:str):
        # 顺序请求
        ################################################
        # resp_result = []
        # for worker_ip in worker_ip_set:
        #     req_url = f'http://{worker_ip}:5001/worker/traffic/{role}/'
        #     resp_result.append(requests.post(req_url, json=req_para).json()['code'])    
        # # 反馈信息需要根据role的不同返回不同的信息
        # if not all(resp_result):
        #     raise RuntimeError(msg.error_msg)
        ################################################
        # 异步请求
        req_urls = []
        for worker_ip in worker_ip_set:
            req_urls.append(f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/traffic/{role}/')
        # rs = (async_requests.post(url, json=req_para) for url in req_urls)
        # resp_result = async_requests.map(rs)
        # 在这里请求worker
        if choice == "post":
            msg = self._get_return_deploy_msg(role)
            rs = (grequests.post(url, json=req_para) for url in req_urls)
        elif choice == "delete":
            msg = self._get_return_delete_msg(role)
            rs = (grequests.delete(url, json=req_para) for url in req_urls)
        resp_result = grequests.map(rs)
        resp_status = [resp.json()['code'] for resp in resp_result]
        if not all(resp_status):
            raise RuntimeError(msg.error_msg)

    def _get_return_deploy_msg(self, role):
        if role == 'traffic_server':
            msg = ResultMsg('创建流发生器服务端成功', '创建流发生器服务端失败')
        elif role == 'traffic_client':
            msg = ResultMsg('创建流发生器客户端成功', '创建流发生器客户端失败')
        elif role == 'pkt_gen2':
            msg = ResultMsg('创建包发生器pkt_gen2客户端成功', '创建包发生器pkt_gen2客户端失败')
        elif role == 'pkt_gen1':
            msg = ResultMsg('创建包发生器pkt_gen1客户端成功', '创建包发生器pkt_gen1客户端失败')
        else:
            raise ValueError(f'参数role的值不能为{role},应为server/client/pkt_gen1/pkt_gen2其中之一')
        return msg

    def _get_return_delete_msg(self, role):
        if role == 'traffic_server':
            msg = ResultMsg('删除流发生器服务端成功', '删除流发生器服务端失败')
        elif role == 'traffic_client':
            msg = ResultMsg('删除流发生器客户端成功', '删除流发生器客户端失败')
        elif role == 'pkt_gen2':
            msg = ResultMsg('删除包发生器pkt_gen2客户端成功', '删除包发生器pkt_gen2客户端失败')
        elif role == 'pkt_gen1':
            msg = ResultMsg('删除包发生器pkt_gen1客户端成功', '删除包发生器pkt_gen1客户端失败')
        else:
            raise ValueError(f'参数role的值不能为{role}， 应为server/client/pkt_gen1/pkt_gen2其中之一')
        return msg

    def delete(self):
        # TODO:设置停止逻辑，访问worker的traffic delete api
        # 停止之后，删除切分信息
        # 1、先停止程序
        # 2、删除切分表项
        data = json.loads(request.get_data(as_text=True))
        traffic_manager = MasterTrafficManager(data)
        # server_worker_map
        # {'traffic_server_workers': set(), 'traffic_client_workers': set(), 'pkt_gen2_workers': set()}
        server_worker_map = traffic_manager.get_value_from_db()
        result = {}
        info_dict = {'user': data['user'], 'topo': data['topo'], 'app_seq': data['app_name']}
        try:
            # 这里先创建traffic_server 之后
            # 创建traffict_client 和 pkt_gen2应该并行的创建
            # 也许需要改成用aiohttp
            # 或者使用celery进行统一定时之后在运行？
            for role in TRAFFIC_ROLES:
                workers_set_key = f'{role}_workers'
                self._dispatch_requests(server_worker_map[workers_set_key], role, info_dict, "delete")
            traffic_manager.del_value_from_db(server_worker_map)
        except Exception as e:
            result['msg'] = e.args[0]
            result['code'] = 0
            return result
        finally:
            traffic_manager.close()
        # 日志输出
        user,topo,app_name = data['user'], data['topo'], data['app_name']
        logger = UserLogger(user, UserLogLevel.Second, topo)
        logger.log_to_mysql(f'停止流量服务{app_name}')
        return {'code': 1, 'msg': '流量服务删除成功'}


        
        # data = json.loads(request.get_data(as_text=True))
        # traffic_manager = MasterTrafficManager(data)
        # result = traffic_manager.delete_traffic_info()
        # return result
        # return {'code': 0, 'msg': 'method not allowed', 'status': 405}

 
    def get(self):
        return {'msg': 'this url can be routed', 'code':0}