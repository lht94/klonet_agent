import json
from flask_login import login_required
from  flask import request
from flask.views import MethodView
import requests
import threading
import queue

from ....vemu_config.config import PROJ_CONFIG
from ....Service_layer.redisAPI import UserDB, UserMapRedis
from ....Service_layer.redis_error import *
from ....tools.log_tools import *
from ....tools.schema.schema import parameter_check
from ....tools.schema.link_schema import st_link_post_schema, mm_link_post_schema,top_link_post_schem
        
# 还回给前端渲染的数据
link_dict = {
    "config_flag":False,
	"mmwave": {
		"bandwidth_scaling": {
			"config_description": "带宽缩放因子",
			"config_name": "bandwidth_scaling",
			"default_value": 1,
			"value_method": "input",
			"necessity": False,
            "reminder_text": "需为正数，如0.1、10，默认值：1"
		},
		"link_scenario": {
			"config_description": "链路场景",
			"config_name": "link_scenario",
			"default_value": "lb",
			"value_list": [
				"lb",
				"mobb",
				"sb",
				"sl"
			],
			"value_method": "select",
			"necessity": True
		},
		"loss": {
			"config_description": "链路丢包率(%)",
			"config_name": "loss",
			"default_value": 0,
			"value_method": "input",
			"necessity": False,
            "reminder_text": "0~100的非负数，如0、50、100，默认值：0"
		},
		"queue_type": {
			"config_description": "队列类型",
			"config_name": "queue_type",
			"default_value": "largefifo",
			"value_list": [
				"largefifo",
				"fq_codel",
				"pie",
				"smallfifo"
			],
			"value_method": "select",
			"necessity": True
		}
	},
	"static": {
		"bw_kbit": {
			"config_description": "链路带宽（kbps）",
			"config_name": "bw_kbps",
			"default_value": 10000,
			"value_method": "input",
			"necessity": True,
            "reminder_text": "必填项，正数，如10000"
		},
        "delay_us": {
			"config_description": "链路时延(us)",
			"config_name": "delay_us",
			"default_value": 0,
			"value_method": "input",
			"necessity": False,
            "reminder_text": "非负整数，如30000。默认值：0"
		},
        "jitter_us": {
			"config_description": "时延抖动(us)",
			"config_name": "jitter_us",
			"default_value": 0,
			"value_method": "input",
			"necessity": False,
            "reminder_text": "非负整数，如10000。默认值：0"
		},
		"correlation": {
			"config_description": "抖动相关率(%)",
			"config_name": "correlation",
			"default_value": "0",
			"value_method": "input",
			"necessity": False,
            "reminder_text": "0~100的非负数，如0、50、100，默认值：0"
		},
		"delay_distribution": {
			"config_description": "时延抖动分布",
			"config_name": "delay_distribution",
			"default_value": "uniform",
			"value_list": [
				"uniform",
				"normal",
				"pareto",
				"paretonomal"
			],
			"value_method": "select",
			"necessity": False
		},
		"loss": {
			"config_description": "链路丢包率(%)",
			"config_name": "loss",
			"default_value": 0,
			"value_method": "input",
			"necessity": False,
            "reminder_text": "0~100的非负数，如0、50、100，默认值：0"
		},
		"queue_size_bytes": {
			"config_description": "队列大小(字节)",
			"config_name": "queue_size_bytes",
			"default_value": 100000,
			"value_method": "input",
			"necessity": False,
            "reminder_text": "非负整数，如10000。默认值：100000"
		}
	}
}

class LinkQueryAPI(MethodView):
    '''查询链路属性'''
    def post(self):
        """查询链路属性

        POST /master/linkquery/，如果已配置则返回配置信息，否则返回link_dict。

        Returns:
            dict: 默认链路属性字典或者已配置的链路属性
        """
        data = json.loads(request.get_data(as_text=True))
        try:
            user, topo = data['user'], data['topo']
            link = data['links'][0]['link']
            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(user)
            table_name = f'{topo}_{link[5:]}'
            key = f'tcConfig'
        except:
            return {'code':0 , 'msg': '获取配置信息失败'}

        # 参数错误
        try:
            user_db_cli.get_value('plane_topo_list', topo)
            user_db_cli.get_value('topo_service', topo)
            value = user_db_cli.get_value(table_name, key)
            if "flag" not in value.keys():
                raise ValueError("参数错误")
        except TableNotExistError:
            return link_dict
        except KeyNotExistError:
            return link_dict
        except:
            traceback.print_exc()
            return {'code':0 , 'msg': '获取配置信息失败'}
        
        # flag 为 false 表明链路未配置
        if not user_db_cli.get_value(table_name, key)['flag']:
            return link_dict
        else:
            dict = user_db_cli.get_value(table_name,key)
            info_list = [dict['source'], dict['target']]
            info_dict = {'config_flag':True,'config':info_list}
            # 由于历史原因，以下代码是和老版本兼容，老版本彻底淘汰后不需要此处的代码
            try:
                # 更换key
                link_dict['static']["bw_kbps"] = link_dict['static'].pop(
                    "bw_kbit")
            except KeyError:
                pass
            info_dict["link_dict"] = link_dict
        user_map_redis.close()
        user_db_cli.close()
        return info_dict
   

class LinkConfigAPI(MethodView):
    '''链路配置API
    
    链路属性如时延，丢包率等是通过Linux TC队列属性来实现的，由于TC实现原理的限制，该属性
    配置只能对链路两端接节点的发包行为进行控制，因此整个链路的最终性能实际上是由“网络”与
    “TC”两部分组成，本系统只能对TC做出约束，对于中间的网络性能暂不能友好的控制。
    '''
    # post请求示例
    __posexample__ = {
                        "user": "admin",  
                        "topo": "1",  
                        "links": [
                            {
                                "linkchoice":"mmwave",
                                "link": "link_l1",
                                "ne": "h1",
                                "queue_type": "pie",
                                "link_scenario":"sb",
                                "loss": "10",
                                "bandwidth_scaling":"1"
                            },
                            {
                                "linkchoice":"static",
                                "link": "link_l1",
                                "ne": "s1",
                                "bw_kbps": "100",
                                "queue_size_bytes": "10000000",
                                "delay_us": "15",
                                "loss": "0",
                                "jitter_us": "16",
                                "correlation": "10",
                                "delay_distribution": "pareto"
                            }
                        ]
    }
    
    # del请求示例
    __delexample__ = {
                        "user": "admin",  
                        "topo": "1",  
                        "links": [
                            {
                                "linkchoice":"mmwave",
                                "link": "link_l1",
                                "ne": "h1"
                            },
                            {
                                "linkchoice":"static",
                                "link": "link_l1",
                                "ne": "s1"
                            }
                        ]
    }
    def post(self):
        """配置链路

        POST /master/link/

        Returns:
            dict : 执行结果字典
        """
        # TODO: 原本设想链路需要同时配置两端节点的TC规则，实际中似乎也存在只配置一方的情况
        data = json.loads(request.get_data(as_text=True))
        # 参数检查
        res = parameter_check(data, top_link_post_schem)
        if not res['code']: 
            return {'code': 0 , 'msg': res['msg']}
        user, topo = data['user'], data['topo']
        # 跨条目的schema检测是不支持的，只能单独写
        if data['links'][0]['link'] == data['links'][1]['link']:
            link_name = data['links'][0]['link'] 
        else:
            return {"code": 0 , "msg":"链路错误"}
        table_name = f'{topo}_{link_name[5:]}'
        key = f'tcConfig'
        try:
            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(user)
            # flag：整条链路是否完整配置，src_con_flag: 源节点是否配置，trg_con_flag：目的节点是否配置
            db_value = {"flag":False, "source":'',"src_con_flag":False, "target":'',"trg_con_flag":False}
            for index, link_conf_dict in enumerate(data['links']):
                # 根据用户选择，初始化静态链路或者毫米波链路配置类
                choice = StLinkConfigAPI() if link_conf_dict['linkchoice'] == 'static' else MmlinkConfigAPI()
                pre_config_dict = user_db_cli.get_value(table_name, key)
                # 重复配置，也可以不用判断，只要配置就先重置，但感觉不是很好
                if pre_config_dict['src_con_flag'] or pre_config_dict['trg_con_flag']:
                    _del_resp= choice._handle_req_info(request, 'delete', index, user_db_cli)
                    if not _del_resp['code']: FLASK_LOGGER.warning(_resp['msg'])
                _resp = choice._handle_req_info(request, 'post', index, user_db_cli)['code']
                if _resp:
                    if link_conf_dict['ne'] == user_db_cli.get_value(table_name, 'sourceNE'):
                        db_value['src_con_flag'] = True
                        db_value['source'] = link_conf_dict
                    else:
                        db_value['trg_con_flag'] = True
                        db_value['target'] = link_conf_dict
            if db_value["src_con_flag"] and db_value["trg_con_flag"]:
                db_value['flag'] = True
                logger = UserLogger(user, UserLogLevel.Second, topo)
                logger.log_to_mysql(f"配置链路{data['links'][0]['ne']}-{data['links'][1]['ne']}")
                return {'code': 1, 'msg': '链路配置成功'}
            else:
                return {'code': 0, 'msg': '链路配置失败'}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': '链路配置失败'}
        finally:
            user_db_cli.set_value(table_name, key, db_value)
            user_map_redis.close()
            user_db_cli.close()

    def delete(self):
        """重置链路

        DELETE /master/link/
        
        Returns:
            dict : 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        res = parameter_check(data, top_link_post_schem)
        if not res['code']: 
            return {'code': 0 , 'msg': res['msg']}
        user, topo = data['user'], data['topo']
        if data['links'][0]['link'] == data['links'][1]['link']:
            link_name = data['links'][0]['link'] 
        else:
            return {"code": 0 , "msg":"链路错误"}
        table_name = f'{topo}_{link_name[5:]}'
        key = f'tcConfig'
        try:
            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(user)
            db_value = {"flag":False, "source":'',"src_con_flag":False, "target":'',"trg_con_flag":False}
            resp = []
            for index, link_conf_dict in enumerate(data['links']):
                choice = StLinkConfigAPI() if link_conf_dict['linkchoice'] == 'static' else MmlinkConfigAPI()
                _resp= choice._handle_req_info(request, 'delete', index, user_db_cli)
                resp.append(_resp['code'])
                if not _resp['code']: FLASK_LOGGER.warning(_resp['msg'])
            if all(resp):
                user_db_cli.set_value(table_name, key, db_value)
                logger = UserLogger(user, UserLogLevel.Second, topo)
                logger.log_to_mysql(f"重置链路{data['links'][0]['ne']}-{data['links'][1]['ne']}")
                return {'code': 1, 'msg': '链路重置成功'}
            else:
                return {'code': 0, 'msg': '链路重置失败'}
        except Exception as e:
            traceback.print_exc()
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': '链路重置失败'}
        finally:
            user_map_redis.close()
            user_db_cli.close()

class StLinkConfigAPI(MethodView):
    '''静态链路处理类
    
    该API原本为直接暴露的API接口，现已隐藏，通过LinkConfigAPI间接调用。静态链路即指链路
    关联节点的TC属性为一个相对稳定的恒定值。
    '''
    # TODO: 不支持批量，但是worker的API实际是可以支持批量的
    def _handle_req_info(self, request, method: str, num:int, user_db_cli:UserDB):
        """处理并转发请求

        Args:
            request : request对象
            method : Restful接口
            num : 链路列表下标标识
            user_db_cli (UserDB):用户数据库连接实例

        Returns:
            dict: 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo = data['user'], data['topo']
        if method == 'post':
            # 参数预处理及默认值设置
            # TODO：前端完全没有做检查，空格也会发送
            try:
                links = data['links'][num]
                data['links'][num]['queue_size_bytes'] = int(links['queue_size_bytes'].replace(" ","")) or 100000
                data['links'][num]['delay_us'] = int(links['delay_us'].replace(" ","")) or 0
                data['links'][num]['loss'] = float(links['loss'].replace(" ","") or 0) 
                data['links'][num]['jitter_us'] = int(links['jitter_us'].replace(" ","") or 0 )  
                #data['links'][num]['correlation'] = float(links['correlation'].replace(" ","") or 0)
                data['links'][num]['correlation'] = float(links['correlation'].replace(" ","").replace("%","") or 0)
                data['links'][num]['bw_kbps'] = int(links['bw_kbps'].replace(" ",""))
            except ValueError as e:
                FLASK_LOGGER.error(e)
                return {'code': 0 , 'msg': '参数格式错误'}
            res = parameter_check(data['links'][num], st_link_post_schema)
            if not res['code']: 
                return {'code': 0 , 'msg': res['msg']}
        try:
            worker_ip = user_db_cli.get_worker_ip_by_ne_name(topo, data['links'][num]['ne'])
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code':0, 'msg':'数据库查询失败'}
        # 没有去掉这个多余的列表是因为前端已经做了，并且后期批量可以在这个基础上改
        info_dict = {'user': user, 'topo': topo, 'links':[data['links'][num]]}
        req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/stlink/'
        req_method = getattr(requests, method)
        resp = req_method(req_url, json=info_dict).json()
        if not resp['code']:
            FLASK_LOGGER.warning(resp['msg'])
            return {'code': 0, 'msg': f'静态链路{"配置" if method =="post" else "重置"}失败'}
        else:
            return {'code': 1, 'msg': f'静态链路{"配置" if method =="post" else "重置"}成功'}

class MmlinkConfigAPI(MethodView):
    '''毫米波链路处理类
    
    该API原本为直接暴露的API接口，现已隐藏，通过LinkConfigAPI间接调用。毫米波链路可理
    解为动态链路，即链路所关联的节点TC属性是动态变换的。这一过程是通过脚本来实现，基于预
    先收集的动态数据，在链路生效过程中定时更新TC的属性而做到的“伪动态”。
    '''
    def _handle_req_info(self, request, method: str, num:int, user_db_cli:UserDB):
        """处理并转发请求

        Args:
            request : request对象
            method : Restful接口
            num : 链路列表下标标识
            user_db_cli (UserDB):用户数据库连接实例

        Returns:
            dict: 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo = data['user'], data['topo']
        if method == 'post':
            link = data['links'][num]
            try:
                data['links'][num]['loss'] = float(link['loss'].replace(" ","") or 0) 
                data['links'][num]['bandwidth_scaling'] = float(link['bandwidth_scaling'].replace(" ","") or 1)
            except ValueError as e:
                FLASK_LOGGER.error(e)
                return {'code': 0 , 'msg': '参数格式错误'}
            res = parameter_check(data['links'][num], mm_link_post_schema)
            if not res['code']: 
                return {'code': 0 , 'msg': res['msg']}
        try:
            worker_ip = user_db_cli.get_worker_ip_by_ne_name(topo, data['links'][num]['ne'])
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code':0, 'msg':'数据库查询失败'}
        # 没有去掉这个多余的列表是因为前端已经做了
        info_dict = {'user': user, 'topo': topo, 'links':[data['links'][num]]}
        req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/mmlink/'
        req_method = getattr(requests, method)
        resp = req_method(req_url, json=info_dict).json()
        if not resp['code']:
            FLASK_LOGGER.warning(resp['msg'])
            return {'code': 0, 'msg': f'毫米波链路{"配置" if method =="post" else "重置"}失败'}
        else:
            return {'code': 1, 'msg': f'毫米波链路{"配置" if method =="post" else "重置"}成功'}

class LinkMonitorAPI(MethodView):

    def post(self):
        """查询链路指标

        POST /master/link_monitor/

        Returns:
            dict : 执行结果字典

        Examples:
            {
                "user": "admin",
                "topo": "1",
                "metric": "throughput"
            }
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, metric = data['user'], data['topo'], data['metric']

        if metric =='throughput':
            worker_need_info = {}
            try:
                user_map_redis = UserMapRedis()
                user_db_cli = user_map_redis.get_user_db(user)

                config = user_db_cli.get_value(f'{topo}_Linkmonitor', metric)
                wait_time = config['wait_time']
                links = config['links']
                running = config['running']

                if running == False:
                    return {'code': 0, 'msg': '未启动该指标监控'}

                if links == []:
                    links = user_db_cli.get_value('plane_topo_list', topo)['links']

                for link in links:
                    link_info = user_db_cli.get_all_values(f'{topo}_{link}')
                    sourceNE = link_info['sourceNE']
                    targetNE = link_info['targetNE']
                    source_NEservice = user_db_cli.get_value(f'{topo}_{sourceNE}', 'NEservice')
                    target_NEservice = user_db_cli.get_value(f'{topo}_{targetNE}', 'NEservice')
                    if source_NEservice != 'docker' or target_NEservice != 'docker':
                        return {'code': 0, 'msg': '链路节点不是docker节点, 目前只支持docker节点'}
                    source_worker_ip = user_db_cli.get_worker_ip_by_ne_name(topo, sourceNE)
                    target_worker_ip = user_db_cli.get_worker_ip_by_ne_name(topo, targetNE)
                    worker_need_info[source_worker_ip] = [] if source_worker_ip not in worker_need_info else worker_need_info[source_worker_ip]
                    worker_need_info[target_worker_ip] = [] if target_worker_ip not in worker_need_info else worker_need_info[target_worker_ip]
                    worker_need_info[source_worker_ip].append({'NE':sourceNE, 'ID': link_info['sourceID'], 'Port': link_info['sourcePort'], 'Link': link})
                    worker_need_info[target_worker_ip].append({'NE':targetNE, 'ID': link_info['targetID'], 'Port': link_info['targetPort'], 'Link': link})
            except Exception as e:
                FLASK_LOGGER.error(e)
                return {'code':0, 'msg':'数据库查询失败'}
            
            def post_to_worker(user, topo, worker_ip, data, wait_time, queue):
                info_dict = {'user': user, 'topo': topo, 'info': data, 'wait_time': wait_time}
                req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/throughput/'
                queue.put(requests.post(req_url, json=info_dict, timeout=(wait_time+2)).json())
            
            threads = []
            r_queue = queue.Queue()
            for worker_ip, data in worker_need_info.items():
                thread = threading.Thread(target=post_to_worker, args=(user, topo, worker_ip, data, wait_time, r_queue))
                threads.append(thread)
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            results = []
            while not r_queue.empty():
                results.append(r_queue.get())

            link_throughput = {}
            for result in results:
                if result['code'] == 0:
                    return {'code': 0, 'msg': '吞吐量查询失败'}
                for item in result['result']:
                    link = item['link']
                    ne = item['ne']
                    throughput = item['throughput']
                    link_throughput[link] = {} if link not in link_throughput else link_throughput[link]
                    link_throughput[link][ne] = {}
                    link_throughput[link][ne]['rx'] = throughput['rx']
                    link_throughput[link][ne]['tx'] = throughput['tx']

            user_map_redis.close()
            user_db_cli.close()

            return {'code': 1, 'msg': '吞吐量查询成功', 'throughput': link_throughput}
        else:
            return {'code': 0, 'msg': '暂不支持其他指标查询'}
    
class LinkMonitorConfigAPI(MethodView):
    '''吞吐量配置API'''

    def post(self):
        '''保存配置信息

        POST /master/link_monitor/config/

        '''                                                                                                                                                                                                                                  
        try:
            info = json.loads(request.get_data(as_text=True))
            config = info['config']
            link_monitor_metric = info['link_monitor_metric']
            user, topo = info['user'], info['topo']
            links = config.get('links', [])
            wait_time = config.get('wait_time', 2)

            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(user)

            if user_db_cli.check_exist(f'{topo}_Linkmonitor', link_monitor_metric):
                return {'code': 0, 'msg': 'link_monitor_metric already exists'}
            user_db_cli.set_value(f'{topo}_Linkmonitor', link_monitor_metric, {'links': links, 'wait_time': wait_time, "running":False})
            user_map_redis.close()
            user_db_cli.close()
            return {'code': 1, 'msg': 'success'}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': 'error'}
        
    def put(self):
        '''更新配置信息

        PUT /master/link_monitor/config/

        '''                                                                                                                                                                                                                                  
        try:
            info = json.loads(request.get_data(as_text=True))
            config = info['config']
            link_monitor_metric = info['link_monitor_metric']
            user, topo = info['user'], info['topo']
            links = config.get('links', [])
            wait_time = config.get('wait_time', 2)
            running = config.get('running', False)

            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(user)

            if not user_db_cli.check_exist(f'{topo}_Linkmonitor', link_monitor_metric):
                return {'code': 0, 'msg': 'link_monitor_metric does not exist'}
            user_db_cli.set_value(f'{topo}_Linkmonitor', link_monitor_metric, {'links': links, 'wait_time': wait_time, "running":running})
            user_map_redis.close()
            user_db_cli.close()
            return {'code': 1, 'msg': 'success'}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': 'error'}
        
    def delete(self):
        '''删除配置信息

        DELETE /master/link_monitor/config/

        '''                                                                                                                                                                                                                                  
        try:
            info = json.loads(request.get_data(as_text=True))
            link_monitor_metric = info['link_monitor_metric']
            user, topo = info['user'], info['topo']

            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(user)

            if not user_db_cli.check_exist(f'{topo}_Linkmonitor', link_monitor_metric):
                return {'code':0, 'msg': 'link_monitor_metric does not exist'}
            user_db_cli.del_value(f'{topo}_Linkmonitor', link_monitor_metric)
            user_map_redis.close()
            user_db_cli.close()
            return {'code': 1, 'msg': 'success'}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': 'error'}
        
    def get(self):
        '''获取配置信息

        GET /master/link_monitor/config/

        '''                                                                                                                                                                                                                                  
        try:
            info = request.args.to_dict()
            link_monitor_metric = info['link_monitor_metric']
            user, topo = info['user'], info['topo']

            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(user)

            if not user_db_cli.check_exist(f'{topo}_Linkmonitor', link_monitor_metric):
                return {'code':0, 'msg': 'link_monitor_metric does not exist'}
            config = user_db_cli.get_value(f'{topo}_Linkmonitor', link_monitor_metric)
            user_map_redis.close()
            user_db_cli.close()
            return {'code': 1, 'msg': 'success', 'config': config, 'link_monitor_metric':link_monitor_metric}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': 'error'}
        
class DelayAPI(MethodView):
    '''链路延迟查询API
    
    链路延迟查询API
    用于查询链路的延迟，通过调用worker的API来实现。
    '''
    def post(self):
        """查询链路延迟

        POST /master/delay/

        Returns:
            dict : 执行结果字典

        Examples:
            {
                "user": "admin",
                "topo": "1",
                "link": "l1"
            }
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, link = data['user'], data['topo'], data['link']

        try:
            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(user)

            link_info = user_db_cli.get_all_values(f'{topo}_{link}')
            sourceNE = link_info['sourceNE']
            targetNE = link_info['targetNE']
            if link_info['sourceType'] == 'switch' or link_info['targetType'] == 'switch':
                return {'code': 0, 'msg': '链路节点包含二层节点, 二层链路无法测试时延'}
            if link_info['sourceIP'] == '' or link_info['targetIP'] == '':
                return {'code': 0, 'msg': '链路节点没有正确配置IP地址，请检查'}
            source_NEservice = user_db_cli.get_value(f'{topo}_{sourceNE}', 'NEservice')
            target_NEservice = user_db_cli.get_value(f'{topo}_{targetNE}', 'NEservice')
            if source_NEservice != 'docker' or target_NEservice != 'docker':
                return {'code': 0, 'msg': '链路节点不是dockeI节点, 目前只支持docker节点'}
            source_worker_ip = user_db_cli.get_worker_ip_by_ne_name(topo, sourceNE)
            container = link_info['sourceID']
            I_mask = link_info['targetIP']
            IP = I_mask.split('/')[0]
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code':0, 'msg':'数据库查询失败'}
        

        info_dict = {'container':container, 'IP':IP}
        req_url = f'http://{source_worker_ip}:{PROJ_CONFIG.worker_port}/worker/delay/'
        rep = requests.post(req_url, json=info_dict)
  

        if rep.status_code == 200:
            return rep.json()
        else:
            return {'code':0, 'msg':'worker请求失败'}