from flask import Blueprint, request
from flask.views import MethodView
import json 

from ....Service_layer.redisAPI import UserMapRedis
from ....Service_layer.worker_business_deploy import TrafficManager
from ....tools import get_host_ip




local_ip = get_host_ip()

# TODO
# master 先向 worker 下发服务器相关的服务创建
# 再向 worker 下发客户端相关的服务创建

class TrafficAPI(MethodView):
    '''
    流量服务的创建/删除

    请求的url如下:
    GET /traffic/ 不需要实现
    DELETE /traffic/
    POST /traffic/
    POST /worker/traffic/traffic_server/
    POST /worker/traffic/traffic_client/
    POST /worker/traffic/pkt_gen2/
    POST /worker/traffic/pkt_gen1/

    DELETE /worker/traffic/traffic_server/ 
    DELETE /worker/traffic/traffic_client/ 
    DELETE /worker/traffic/pkt_gen2/
    DELETE /worker/traffic/pkt_gen1/

    POST 的请求体中包含 
    {
        'user': 'xc',
        'topo': 'test_topo1',
        'app_seq': 'app_seq1'
    }
    '''
    def post(self, role):
        data = json.loads(request.get_data(as_text=True))
        user, topo, appname = data['user'], data['topo'], data['app_seq']
        user_db_map = UserMapRedis()
        user_db_cli = user_db_map.get_user_db(user)
        user_db_map.close()
        traffic_manager = TrafficManager(role, user, topo, appname)  # pkt_gen1 tb liuliang f1
        try:
            if role == 'traffic_server':
                s_table = f'{topo}_{appname}_{local_ip}_s'
                s_info = user_db_cli.get_all_values(s_table)
                user_db_cli.close()
                traffic_manager.traffic_gen_business_deploy(s_info['traffic_gen'])
                # user_db_cli.set_value(s_table, 'traffic_gen_s_pid', con_pid_map)
                return {'code': 1, 'msg': '流发生器服务端创建成功'}
            elif role == 'traffic_client' or role == 'pkt_gen2' or role == 'pkt_gen1':
                c_table = f'{topo}_{appname}_{local_ip}_c'
                c_info = user_db_cli.get_all_values(c_table)
                user_db_cli.close()
                if role == 'traffic_client':
                    traffic_manager.traffic_gen_business_deploy(c_info['traffic_gen'])
                    return {'code': 1, 'msg': '流发生器客户端创建成功'}
                elif role == 'pkt_gen2':
                    traffic_manager.pkt_gen2_business_deploy(c_info['pkt_gen2'])
                    return {'code': 1, 'msg': 'pkt_gen2包发生器创建成功'}
                elif role == 'pkt_gen1':
                    traffic_manager.pkt_gen1_business_deploy(c_info['pkt_gen1'])
                    return {'code': 1, 'msg': 'pkt_gen1包发生器创建成功'}
            else:
                return {'code': 0, 'msg': 'role参数不对，其为server或者client'}
        except Exception as e:
            return {'code': 0, 'msg': f'worker创建流量服务失败, 由于{e.args[0]}'}

    def delete(self, role):
        data = json.loads(request.get_data(as_text=True))
        user, topo, appname = data['user'], data['topo'], data['app_seq']
        user_db_map = UserMapRedis()
        user_db_cli = user_db_map.get_user_db(user)
        user_db_map.close()
        traffic_manager = TrafficManager(role, user, topo, appname)
        try:
            if role == 'traffic_server':
                s_table = f'{topo}_{appname}_{local_ip}_s'
                s_info = user_db_cli.get_all_values(s_table)
                user_db_cli.close()
                # traffic_gen_s_pid表: {container1: [pid1,pid2,...];container2:[]}
                traffic_manager.traffic_stop(s_info['traffic_gen'])
                return {'code': 1, 'msg': '流量发生器服务端删除成功'}
            elif role == "traffic_client" or role == 'pkt_gen2' or role == 'pkt_gen1':
                c_table = f'{topo}_{appname}_{local_ip}_c'
                c_info = user_db_cli.get_all_values(c_table)
                user_db_cli.close()
                if role == 'traffic_client':
                    traffic_manager.traffic_stop(c_info['traffic_gen'])
                else:
                    traffic_manager.traffic_stop(c_info[role])
                return {'code': 1, 'msg': '流量发生器客户端删除成功'}
            else:
                return {'code': 0, 'msg': 'role参数不对，其为server或者client'}
        except Exception as e:
            return {'code': 0, 'msg': f'worker创建流量服务失败, 由于{e.args[0]}'}
        

    def get(self):
        return {'msg': 'this url can be routed', 'code': 1}