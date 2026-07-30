from flask.views import MethodView
from flask import request
import json
import requests

from .traffic_gen import traffic_generate, traffic_delete
from ....Service_layer.redis_error import *
from ....Service_layer.redisAPI import UserMapRedis
from ....vemu_config.config import PROJ_CONFIG

class MasterTrafficGenAPI(MethodView):
    def post(self):
        '''
        产生从一个节点到另一个节点的流量
        利用iperf3和dpkt进行流量生成

        POST master/traffic_gen/

        input:
            {
                "user": username,
                "project": projectname,
                "traffic_name": 流量名称,
            }

        output:
            {
                "code": 0,
                "message": "success",
                "run_time": 运行时间,
                "performance_indicators": 其他性能指标
            }

        Example:
            {
                "user": "admin",
                "project": "test",
                "traffic_name": "traffic1"
            }
        '''
        try:
            data = json.loads(request.get_data(as_text=True))
            user = data['user']
            topo = data['topo']
            traffic_name = data['traffic_name']
            user_db_map = UserMapRedis()
            db_cli = user_db_map.get_user_db(user)
            if not db_cli.check_exist(f'{topo}_newtraffic_configs', traffic_name):
                return {'code': 0, 'message': 'traffic name does not exist'}
            traffic_config = db_cli.get_value(f'{topo}_newtraffic_configs', traffic_name)
            src_node = traffic_config['src_node']
            dst_node = traffic_config['dst_node']
            data_size = traffic_config.get('data_size', None)
            if data_size != None and ' 'in data_size:
                return {'code': 0, 'message': 'data_size cannot contain space'}
            run_time = traffic_config.get('run_time', None)
            CONFIG = traffic_config.get('CONFIG', {})
            bandwidth = CONFIG.get('bandwidth', '')
            if ' ' in bandwidth:
                return {'code': 0, 'message': 'bandwidth cannot contain space'}
            if run_time and data_size:
                return {'code': 0, 'message': 'cannot set both run_time and data_size'}
            if not run_time and not data_size:
                return {'code': 0, 'message': 'must set either run_time or data_size'}
            running = traffic_config.get('running', False)
            if running == True:
                return {'code': 0, 'message': 'cannot run while running'}
        except Exception as e:
            return {'code': 0, 'message': str(e)}
        
        try:
            result = traffic_generate(traffic_name, user, topo, src_node, dst_node, data_size, run_time, CONFIG)
            # 修复一下code的一致性bug
            result['code'] += 1 
            return result
        except Exception as e:
            return {'code': 0, 'message': str(e)}

    def delete(self):
        try:
            data = json.loads(request.get_data(as_text=True))
            user = data['user']
            topo = data['topo']
            traffic_name = data['traffic_name']
            user_db_map = UserMapRedis()
            db_cli = user_db_map.get_user_db(user)
            if not db_cli.check_exist(f'{topo}_newtraffic_configs', traffic_name):
                return {'code': 0, 'message': 'traffic name does not exist'}
            traffic_config = db_cli.get_value(f'{topo}_newtraffic_configs', traffic_name)
            src_node = traffic_config['src_node']
            dst_node = traffic_config['dst_node']
            port = traffic_config['CONFIG']['port']
            if 'traffic_distribution' not in traffic_config['CONFIG'] or traffic_config['CONFIG']['traffic_distribution'] == 'constant':
                constant = True
            else:
                constant = False
            
            result = traffic_delete(user, topo, src_node, dst_node, port, constant)
            # 修改code一致性bug
            result['code'] += 1
            # 修改running状态
            traffic_config['running'] = False
            db_cli.set_value(f'{topo}_newtraffic_configs', traffic_name, traffic_config)
            return result
        except Exception as e:
            return {'code': 0, 'message': str(e)}
        
class MasterTrafficSaveAPI(MethodView):  
    def post(self):
        '''
        生成流量生成配置

        POST master/traffic_save/

        input:
            {
                "traffic_name": 流量名称,
                "user": username,
                "project": projectname,
                "src_node": 源节点,
                "dst_node": 目的节点,
                "data_size": 总数据大小,
                "run_time": 运行时间,
                "CONFIG": 其他配置
            }

        output:
            {
                "code": 0,
                "message": "success",
            }

        Example:
            {
                "user": "admin",
                "project": "test",
                "src_node": "node1",
                "dst_node": "node2",
                "bandwidth": 100,
                "data_size": 1000,
                "run_time": "",
                "CONFIG": {
                    "traffic_distribution": "constant"
                }
            }
        '''
        try:
            data = json.loads(request.get_data(as_text=True))
            traffic_name = data['traffic_name']
            user = data['user']
            topo = data['topo']
            port = data['port']
            if 'CONFIG' not in data:
                data['CONFIG'] = {}
            data['CONFIG']['port'] = port
            data_size = data.get('data_size', None)
            if data_size != None and ' 'in data_size:
                return {'code': 0, 'message': 'data_size cannot contain space'}
            run_time = data.get('run_time', None)
            CONFIG = data.get('CONFIG', {})
            bandwidth = CONFIG.get('bandwidth', '')
            if ' ' in bandwidth:
                return {'code': 0, 'message': 'bandwidth cannot contain space'}

            if run_time and data_size:
                return {'code': 0, 'message': 'cannot set both run_time and data_size'}
            if not run_time and not data_size:
                return {'code': 0, 'message': 'must set either run_time or data_size'}
            user_db_map = UserMapRedis()
            db_cli = user_db_map.get_user_db(user)
        except Exception as e:
            return {'code': 0, 'message': str(e)}
        
        try:
            if db_cli.check_exist(f'{topo}_newtraffic_configs', traffic_name):
                return {'code': 0, 'message': 'traffic name already exists'}
            data["running"] = False
            db_cli.set_value(f'{topo}_newtraffic_configs', traffic_name, data)
            return {'code': 1, 'message': 'success'}
        except Exception as e:
            return {'code': 0, 'message': str(e)}

    def put(self):
        '''
        修改流量生成配置

        PUT master/traffic_save/

        input:
            {
                "traffic_name": 流量名称,
                "user": username,
                "project": projectname,
                "src_node": 源节点,
                "dst_node": 目的节点,
                "data_size": 总数据大小,
                "run_time": 运行时间,
                "CONFIG": 其他配置
            }

        output:
            {
                "code": 0,
                "message": "success",
            }

        Example:
            {
                "user": "admin",
                "project": "test",
                "src_node": "node1",
                "dst_node": "node2",
                "bandwidth": 100,
                "data_size": 1000,
                "run_time": "",
                "CONFIG": {
                    "traffic_distribution": "constant"
                }
            }
        '''
        try:
            data = json.loads(request.get_data(as_text=True))
            traffic_name = data['traffic_name']
            user = data['user']
            topo = data['topo']
            port = data['port']
            if 'CONFIG' not in data:
                data['CONFIG'] = {}
            data['CONFIG']['port'] = port
            data_size = data.get('data_size', None)
            if data_size != None and ' 'in data_size:
                return {'code': 0, 'message': 'data_size cannot contain space'}
            run_time = data.get('run_time', None)
            CONFIG = data.get('CONFIG', {})
            bandwidth = CONFIG.get('bandwidth', '')
            if ' ' in bandwidth:
                return {'code': 0, 'message': 'bandwidth cannot contain space'}

            if run_time and data_size:
                return {'code': 0, 'message': 'cannot set both run_time and data_size'}
            if not run_time and not data_size:
                return {'code': 0, 'message': 'must set either run_time or data_size'}
            user_db_map = UserMapRedis()
            db_cli = user_db_map.get_user_db(user)
        except Exception as e:
            return {'code': 0, 'message': str(e)}
        
        try:
            if not db_cli.check_exist(f'{topo}_newtraffic_configs', traffic_name):
                return {'code': 0, 'message': 'traffic name does not exist'}
            old_data = db_cli.get_value(f'{topo}_newtraffic_configs', traffic_name)
            if old_data.get('running', False):
                return {'code': 0, 'message': 'cannot change config while running'}
            data["running"] = False
            db_cli.set_value(f'{topo}_newtraffic_configs', traffic_name, data)
            return {'code': 1, 'message': 'success'}
        except Exception as e:
            return {'code': 0, 'message': str(e)}
    
    def get(self):
        '''
        获取流量生成配置

        GET master/traffic_save/

        input:
            {
                "user": username,
                "project": projectname,
                "traffic_name": 流量名称
            }

        output:
            {
                "code": 0,
                "message": "success",
                "CONFIG": 其他配置
            }

        Example:
            {
                "user": "admin",
                "project": "test",
                "src_node": "node1",
                "dst_node": "node2",
            }
        '''
        try:
            data = request.args.to_dict()
            traffic_name = data['traffic_name']
            user = data['user']
            topo = data['topo']
            user_db_map = UserMapRedis()
            db_cli = user_db_map.get_user_db(user)
        except Exception as e:
            return {'code': 0, 'message': str(e)}
        
        try:
            traffic_config = db_cli.get_value(f'{topo}_newtraffic_configs', traffic_name)
            return {'code': 1, 'message': 'success', 'config': traffic_config}
        except Exception as e:
            return {'code': 0, 'message': str(e)}
        
    def delete(self):
        '''
        删除流量生成配置

        DELETE master/traffic_save/

        input:
            {
                "user": username,
                "project": projectname,
                "traffic_name": 流量名称
            }

        output:
            {
                "code": 0,
                "message": "success",
            }

        Example:
            {
                "user": "admin",
                "project": "test",
                "src_node": "node1",
                "dst_node": "node2",
            }
        '''
        try:
            data = json.loads(request.get_data(as_text=True))
            traffic_name = data['traffic_name']
            user = data['user']
            topo = data['topo']
            user_db_map = UserMapRedis()
            db_cli = user_db_map.get_user_db(user)
        except Exception as e:
            return {'code': 0, 'message': str(e)}
        
        try:
            if not db_cli.check_exist(f'{topo}_newtraffic_configs', traffic_name):
                return {'code': 0, 'message': 'traffic name does not exist'}


            # 调用 DELETE /master/redis_traffic_gen/
            try:
                db_cli.check_table_exist(f'{topo}_flows{traffic_name}_client')
                db_cli.check_table_exist(f'{topo}_flows{traffic_name}_server')

                data = {
                    "user": user,
                    "topo": topo,
                    "traffic_name": traffic_name
                }
                
                wait_time = 4
                req_url = f'http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/redis_traffic_gen/'
                result = requests.delete(req_url, json=data, timeout=(wait_time)).json()

                if result['code'] != 1:
                    raise Exception(result['message'])
            except:
                pass


            db_cli.del_value(f'{topo}_newtraffic_configs', traffic_name)
            return {'code': 1, 'message': 'success'}
        except Exception as e:
            return {'code': 0, 'message': str(e)}