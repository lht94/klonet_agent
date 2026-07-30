import json
from flask_login import login_required
from flask import request
from flask.views import MethodView
import requests

from ....Service_layer.redisAPI import UserMapRedis
from .data_handler import LinkDeleteHandler, LinkCreateHandler

from ....vemu_config.config import PROJ_CONFIG
from ....tools.log_tools import *

user_db_map = UserMapRedis()


class DynamicLinkAPI(MethodView):
    """
    动态编辑链路操作API
    需要判断链路类型   veth-pair  or  vxlan
    """

 
    def post(self):
        """
        动态创建链路API
        
        POST /modification/container/
        
        Args:
            data (json): 动态增加链路的相关信息
            {
                user (str): 用户名
                topo (str): 拓扑名
                info (dict): 动态增加的链路信息
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

        try:
            topo, user, info = data['topo'], data['user'], data['info']

            # 检查链路是否存在
            plane_topo = db_cli.get_value("plane_topo_list", data['topo'])
            links = plane_topo["links"]
            FLASK_LOGGER.debug(data['info']['name'])
            FLASK_LOGGER.debug(links)
            if data['info']['name'] in links:
                return {
                    'code': 0,
                    'msg': f"链路[{data['info']['name']}]已存在！"
                        f"链路列表：{links}"}
            # 检查kvm下端口是否有连接
            src_info = db_cli.get_all_values(f"{topo}_{info['source']}")
            src_vmport = info.get('VMsourcePort', '')
            tgt_info = db_cli.get_all_values(f"{topo}_{info['target']}")
            tgt_vmport = info.get('VMtargetPort', '')
            if src_info['NEservice'] == 'kvm':
                if src_vmport == '':
                    return {'code': 0,
                         'msg': f"虚机{info['source']}没有提供对应连接端口，请重新请求"}
                elif src_vmport > src_info['NEvmconfig']['port_num']:
                    return {'code': 0,
                            'msg': f"{info['name']}链路指定连接源端口的index超过该节点端口上限，请重新指定"}
                elif not isinstance(src_vmport, int) and src_vmport <= 0:
                    return {'code': 0,
                            'msg': f"{info['name']}链路指定连接源端口的index不是正整数，请重新指定"}
                elif src_info['NEvmconfig']['check_port'][src_vmport] == 1:
                    return {'code': 0,
                            'msg': f"虚机{info['source']}端口{src_vmport}已被占用"}
            if tgt_info['NEservice'] == 'kvm':
                if tgt_vmport == '':
                    return {'code': 0,
                         'msg': f"虚机{info['target']}没有提供对应连接端口，请重新请求"}
                elif tgt_vmport > tgt_info['NEvmconfig']['port_num']:
                    return {'code': 0,
                            'msg': f"{info['name']}链路指定连接目的端口的index超过该节点端口上限，请重新指定"}
                elif not isinstance(tgt_vmport, int) and tgt_vmport <= 0:
                    return {'code': 0,
                            'msg': f"{info['name']}链路指定连接目的端口的index不是正整数，请重新指定"}
                elif tgt_info['NEvmconfig']['check_port'][tgt_vmport] == 1:
                    return {'code': 0,
                            'msg': f"虚机{info['target']}端口{tgt_vmport}已被占用"}
            
            # 留存问题 ： topo_list没更新
            data_handler = LinkCreateHandler(data, db_cli)
            workers = data_handler.modify_db()
            # 源宿在同一台主机上
            fmt_req_url = 'http://{}:{}/modification/{}/'
            results = []
            if len(workers) == 1:
                for worker_ip, name in workers:
                    req_url = fmt_req_url.format(worker_ip, PROJ_CONFIG.worker_port, 'vethlink')
                    req_para = {'name': name, 'user': user, 'topo': topo}
                    FLASK_LOGGER.debug(req_para)
                    results.append(requests.post(req_url, json=req_para).json()['code'])
            else:
                for worker_ip, name in workers:
                    req_url = fmt_req_url.format(worker_ip, PROJ_CONFIG.worker_port, 'vxlanlink')
                    req_para = {'name': name, 'user': user, 'topo': topo}
                    FLASK_LOGGER.debug(req_para)
                    results.append(requests.post(req_url, json=req_para).json()['code'])
            if all(results):

                #日志输出
                source = info['source']
                target = info['target']
                logger = UserLogger(user, UserLogLevel.Second, topo)
                logger.log_to_mysql(f'创建链路{source}-{target}')

                return {'code': 1, 'msg': '链路创建成功'}
            else:
                return {'code': 0, 'msg': '链路创建失败'}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': f'链路创建失败，错误信息：{str(e)}'}


    def delete(self):
        """
        动态删除链路
        
        DELETE /modification/container/
        
        Args:
            data (json): 动态删除链路的相关信息
            {
                user (str): 用户名
                topo (str): 拓扑名
                info (dict): 动态删除的链路信息
            }
            
        Returns:
            dict: 执行结果字典
        """
        # delete 还要去维护相关的信息， 先不管回滚了
        data = json.loads(request.get_data(as_text=True))
        try:
            db_cli = user_db_map.get_user_db(data['user'])
        except Exception as e:
            return {'code': 0, 'msg': e.args[0]}
        finally:
            user_db_map.close()

        try:
            topo, name, user = data['topo'], data['info']['name'], data['user']
            worker_link_map = db_cli.get_worker_link_map(topo, name)    # 得到链路的对应worker等信息
            veth = worker_link_map['veth']
            vxlans = worker_link_map['vxlan']
            results = []
            # 删除普通链路
            # 这里因为是veth 就不可能是vxlan
            # 是vxlan, 就不可能是veth， 所以应该分开判断
            base_req_para = {'name': name, 'user': user, 'topo': topo}
            if veth:
                for ip in veth.keys():
                    req_url = f'http://{ip}:{PROJ_CONFIG.worker_port}/modification/vethlink/'
                    results.append(requests.delete(req_url, json=base_req_para).json()['code'])
            # 删除vxlan链路
            else:
                for ip, ovs_info in vxlans.items():
                    req_url = f'http://{ip}:{PROJ_CONFIG.worker_port}/modification/vxlanlink/'
                    base_req_para.update({'ovs_lst': [ovs_info]})
                    FLASK_LOGGER.debug(base_req_para)
                    results.append(requests.delete(req_url, json=base_req_para).json()['code'])
            # 删除表项
            del_manager = LinkDeleteHandler(topo, name, db_cli)
            del_manager.modify_db()
            db_cli.close()
            if all(results):
                
                #日志输出
                source = data['info']['source']
                target = data['info']['target']
                logger = UserLogger(user, UserLogLevel.Second, topo)
                logger.log_to_mysql(f'删除链路{source}-{target}')

                return {'code': 1, 'msg': '链路删除成功'}
            else:
                return {'code': 0, 'msg': '链路删除失败'}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': '链路删除失败'}

