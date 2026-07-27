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


def compat_node_json(user_topo_info):
    '''
    虚机-卫星-容器代码合并时的json兼容处理函数，动态增加节点时的兼容
    Args:
        user_topo_info: 包含用户和动态节点的json信息
    
    Returns：
        res: 适配后的json
    '''
    # wudx
    # 老版本json兼容增加key
    # 为了适配老版本json中的容器节点没有service字段，为容器节点增加service字段
    node_info = user_topo_info['info']
    # 没有service字段证明是老版本json
    if "service" not in node_info.keys():
        node_info["service"] = "docker"
        node_info["portname"] = None    # 目前仅为无效填充字段
    # 还要为配置有interface字段的节点加入平行边的后缀_1，否则该字段后续不能写入数据库
    if "interfaces" in node_info.keys():
        for link_ip_config in node_info["interfaces"]:
            # 以下代码为了兼容性略显丑陋
            # 为应对json的调整
            # 某些情况存在有service字段，但interface错误，依靠字符串分割多层判断逻辑来确定是否要追加后缀
            check_str = link_ip_config["name"][len(node_info["name"]):] # 去掉节点开头的名称，然后尝试匹配后面的对端节点
            for ops_node_name, _ in v.items():
                if check_str == ops_node_name:  # 匹配到证明该字符串就是某节点名称，是老前端，缺少_1后缀
                    link_ip_config["name"] = link_ip_config["name"] + "_1"
                else:   # 未匹配到就说明已经是新前端，存在后缀了
                    pass
    return user_topo_info

def get_ne_types(ne_type: str):
    """
    根据节点的type字段返回复数形式type的字段
    Args:
        ne_type (str): 节点类型
        
    Returns:
        节点类型(ne_type)的复数形式
        
    Rasies:
        TypeError: 无法匹配该节点类型时触发
    """
    if ne_type in ['host', 'router', 'floodlight', 'controller']:
        return ne_type + 's'
    elif ne_type in ['switch',]:
        return ne_type + 'es'
    else:
        raise TypeError('wrong ne type')


class DynamicContainerAPI(MethodView):
    """
    处理动态创建容器的HTTP请求
    """


    def post(self):
        """
        处理动态增加新节点请求
        
        POST /modification/container/
        
        Args:
            data (json): 动态增加节点的相关信息
            {
                user (str): 用户名
                topo (str): 拓扑名
                info (dict): 动态增加的节点信息
            }
            
        Returns:
            dict: 执行结果字典
        """        
        data = json.loads(request.get_data(as_text=True))
        # 卫星-虚机代码兼容预处理
        data = compat_node_json(data)
        
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
        req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/modification/container/'
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
            # wudx
            # 为什么发送了两次一样的容器创建请求？
            data_handler = NECreateDataHandler(data, db_cli)
            worker_ip = data_handler.modify_db()[0]
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/modification/container/'
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
        
        PUT /modification/container/
        
        Args:
            data (json): 动态修改节点的相关信息
            {
                user (str): 用户名
                topo (str): 拓扑名
                info (dict): 动态修改的节点信息
            }
            
        Returns:
            dict: 执行结果字典
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
        req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/modification/container/'
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
        
        DELETE /modification/container/
        
        Args:
            data (json): 动态删除节点的相关信息
            {
                user (str): 用户名
                topo (str): 拓扑名
                info (dict): 动态删除的节点信息
            }
            
        Returns:
            dict: 执行结果字典
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
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/modification/container/'
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
