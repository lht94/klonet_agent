import json
from flask_login import login_required
from flask import request
from flask.views import MethodView
import requests

from ....vemu_config.config import PROJ_CONFIG
from .data_handler import NECreateDataHandler, NEDeleteDataHandler, NeModifyHandler
from ....Service_layer.redisAPI import UserMapRedis
from ....tools.log_tools import UserLogger, UserLogLevel, FLASK_LOGGER
from ....Function_layer.resource_manager import ResourceNotEnoughError

user_db_map = UserMapRedis()


class DynamicKvmAPI(MethodView):
    """
    处理动态创建容器的HTTP请求
    """


    def post(self):
        """
        处理动态增加新kvm节点请求
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, info = data['user'], data['topo'], data['info']
        name = info['name']
        
        #检查源和目的容器名是否为数字字母组合，且是否超过13位
        if len(name) > 13 or len(name) >13:
            return {'code': 0,'msg': f"容器名长度超过13位，请重新命名！"}
        if name.isalnum() == False or name.isalnum() == False:
            return {'code': 0,'msg': f"容器名中包含除字母或数字以外的符号，请重新命名！"}

        try:
            db_cli = user_db_map.get_user_db(user)
        except Exception as e:
            return {'code': 0, 'msg': e.args[0]}
        finally:
            user_db_map.close()
        # 检查是否重名
        plane_topo_list = db_cli.get_value('plane_topo_list', topo)
        if name in plane_topo_list['NEs']:
            return {'code': 0, 'msg': '该名称的节点已存在，请勿重复添加'}
        # try:
        data_handler = NECreateDataHandler(data, db_cli)
        FLASK_LOGGER.debug('modifying redis..')
        try:
            worker_ip = data_handler.modify_db()[0]
        except ResourceNotEnoughError as e:
            return {'code': 0, 'msg': f'由于物理资源不足, 节点创建失败, 其他信息{e.args}'}
        req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/modification/kvm/'
        req_data = {'user': user, 'topo': topo, 'name': name}
        result = requests.post(req_url, json=req_data)
        FLASK_LOGGER.debug(result.json())
        if result.json()['code']:

            #日志输出
            logger = UserLogger(user, UserLogLevel.Second, topo)
            logger.log_to_mysql(f'创建节点{name}')

            return {'code': 1, 'msg': '节点创建成功'}

        else:
            data_handler.rollback_db()
        
        try:
            data_handler = NECreateDataHandler(data, db_cli)
            worker_ip = data_handler.modify_db()[0]
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/modification/kvm/'
            req_data = {'user': user, 'topo': topo, 'name': name}
            result = requests.post(req_url, json=req_data)
            if result.json()['code']:

                # 日志输出
                logger = UserLogger(user, UserLogLevel.Second, topo)
                logger.log_to_mysql(f'创建节点{name}')

                return {'code': 1, 'msg': '节点创建成功'}
            else:
                data_handler.rollback_db()
                return {'code': 0, 'msg': '节点创建失败'}
        except:
            return {'code': 0, 'msg': '节点创建失败'}
        finally:
            db_cli.close()


    def put(self):
        """
        处理动态修改节点请求
        """
        data = json.loads(request.get_data(as_text=True))
        topo, user, info = data['topo'], data['user'], data['info']
        ne_name = info['name']
        try:
            db_cli = user_db_map.get_user_db(user)
        except Exception as e:
            return {'code': 0, 'msg': e.args[0]}
        finally:
            user_db_map.close()
        data_handler = NeModifyHandler(topo, ne_name, info, db_cli)
        result = data_handler.modify_db()
        # 若只改变了xy的信息, result为空， 并直接返回
        if not result:
            return {'code': 1, 'msg': "节点配置成功"}
        worker_ip = result['ip']
        changed = result['changed']
        req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/modification/kvm/'
        req_para = {'name': ne_name, 'topo': topo, 'user': user, 'changed': changed}
        try:
            result = requests.put(req_url, json=req_para)
            if result.json()['code']:

                # 日志输出
                logger = UserLogger(user, UserLogLevel.Second, topo)
                logger.log_to_mysql(f'配置节点{ne_name}')

                return {'code': 1, 'msg': '节点配置成功'}
            else:
                return {'code': 0, 'msg': '节点配置失败'}
        except:
            return {'code': 0, 'msg': '节点配置失败'}
        finally:
            db_cli.close()


    def delete(self):
        """
        处理动态删除节点请求
        """
        data = json.loads(request.get_data(as_text=True))
        try:
            db_cli = user_db_map.get_user_db(data['user'])
        except Exception as e:
            return {'code': 0, 'msg': e.args[0]}
        finally:
            user_db_map.close()
        # 得到节点所在worker
        try:
            topo, user = data['topo'], data['user']
            name = data['info']['name']
            worker_ip = db_cli.get_worker_ip_by_ne_name(topo, name)
            FLASK_LOGGER.debug(worker_ip)
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/modification/kvm/'
            req_para = {'name': name, 'user': user, 'topo': topo}
            result = requests.delete(req_url, json=req_para)
            if not result.json()['code']:
                return {'code': 0, 'msg': '节点删除失败'}
            # 删除链路的时候， 就算是vxlan，也应该是只删除worker上的ovs
            FLASK_LOGGER.debug('detect vxlan info... and delete... ')
            ip_ovs_map = db_cli.get_ne_vxlan_info(topo, name)
            if not ip_ovs_map:
                data_handler = NEDeleteDataHandler(data, db_cli)
                data_handler.modify_db()

                #日志输出
                logger = UserLogger(user, UserLogLevel.Second, topo)
                logger.log_to_mysql(f'删除节点{name}')

                return {'code': 1, 'msg': '节点删除成功'}
            else:
                results = []
                for worker_ip, ovs_lst in ip_ovs_map.items():
                    req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/modification/vxlanlink/'
                    req_para = {'ovs_lst': ovs_lst, 'name': name, 'user': user, 'topo': topo}
                    results.append(requests.delete(req_url, json=req_para).json()['code'])
                data_handler = NEDeleteDataHandler(data, db_cli)
                data_handler.modify_db()
            if not all(results):
                return {'code': 0, 'msg': '节点删除失败'}
            else:
                #日志输出
                logger = UserLogger(user, UserLogLevel.Second, topo)
                logger.log_to_mysql(f'删除节点{name}')
                return {'code': 1, 'msg': '节点删除成功'}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': '节点删除失败'}
