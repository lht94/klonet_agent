import requests
import time
import threading
import queue

from ....vemu_config.config import PROJ_CONFIG
from ....Service_layer.redisAPI import UserMapRedis

def traffic_generate(traffic_name, user, topo, src_node, dst_node, data_size, run_time, CONFIG):
    '''
    产生从一个节点到另一个节点的流量，利用iperf3和dpkt进行流量生成

    '''
    try:
        traffic_distribution = CONFIG.get('traffic_distribution', 'constant')
        CONFIG['traffic_distribution'] = traffic_distribution

        user_db_map = UserMapRedis()
        db_cli = user_db_map.get_user_db(user)

        try:
            db_cli.check_table_exist(f'{topo}_flows{traffic_name}_client')
            db_cli.check_table_exist(f'{topo}_flows{traffic_name}_server')
            return {"code": -1, "message": "traffic_name already exists"}
        except:
            pass
            
        # 修改running状态
        data = db_cli.get_value(f'{topo}_newtraffic_configs', traffic_name)
        data['running'] = True
        db_cli.set_value(f'{topo}_newtraffic_configs', traffic_name, data)

        src_NEloc = db_cli.get_value(f'{topo}_{src_node}', 'NEloc')
        dst_NEloc = db_cli.get_value(f'{topo}_{dst_node}', 'NEloc')

        src_workerip = db_cli.get_value('subtopo2worker', src_NEloc)
        dst_workerip = db_cli.get_value('subtopo2worker', dst_NEloc)

        src_node_id = db_cli.get_value(f'{topo}_{src_node}', 'NEid')
        dst_node_id = db_cli.get_value(f'{topo}_{dst_node}', 'NEid')

        if 'src_node_ip' in CONFIG:
            src_node_ip = CONFIG['src_node_ip']
        else:
            src_node_ip = db_cli.get_value(f'{topo}_{src_node}', 'NEinterface')[0]['ip']
        if 'dst_node_ip' in CONFIG:
            dst_node_ip = CONFIG['dst_node_ip']
        else:
            dst_node_ip = db_cli.get_value(f'{topo}_{dst_node}', 'NEinterface')[0]['ip']

        def check_traffic_gen(type, worker_ip, node_id, CONFIG, queue):
            wait_time = 20
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/traffic_gen/'
            queue.put(requests.put(req_url, json={'node_id': node_id, 'type': type, 'CONFIG': CONFIG}, timeout=(wait_time)).json())

        def post_to_worker(traffic_name, user, topo, node, worker_ip, node_id, type, src_node_ip, dst_node_ip, data_size, run_time, traffic_distribution, CONFIG, queue):
            data = {
                "traffic_name": traffic_name,
                "user": user,
                "topo": topo,
                "node": node,
                "node_id": node_id,
                "type": type,
                'src_node_ip': src_node_ip,
                "dst_node_ip": dst_node_ip,
                "data_size": data_size,
                "run_time": run_time,
                "traffic_distribution": traffic_distribution,
                "CONFIG": CONFIG
            }
            wait_time = 3000
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/traffic_gen/'
            queue.put(requests.post(req_url, json=data, timeout=(wait_time)).json())

        # 运行两个线程，一个在源节点，一个在目的节点
        src_queue = queue.Queue()
        dst_queue = queue.Queue()

        # 向目的节点发送put请求检测两节点是否有正在占用端口的iperf3
        src_thread = threading.Thread(target=check_traffic_gen, args=('client', src_workerip, src_node_id, CONFIG, src_queue))
        dst_thread = threading.Thread(target=check_traffic_gen, args=('server', dst_workerip, dst_node_id, CONFIG, dst_queue))
        src_thread.start()
        dst_thread.start()

        src_thread.join()
        src_result = src_queue.get()
        if src_result['code'] != 0:
            return src_result
        
        dst_thread.join()
        dst_result = dst_queue.get()
        if dst_result['code'] != 0:
            return dst_result
        else:
            CONFIG['port'] = dst_result['port']

        # 运行实际的流量生成
        src_thread = threading.Thread(target=post_to_worker, args=(traffic_name, user, topo, src_node, src_workerip, src_node_id, 'client', src_node_ip, dst_node_ip, data_size, run_time, traffic_distribution, CONFIG, src_queue))
        dst_thread = threading.Thread(target=post_to_worker, args=(traffic_name, user, topo, dst_node, dst_workerip, dst_node_id, 'server', src_node_ip, dst_node_ip, data_size, run_time, traffic_distribution, CONFIG, dst_queue))

        # 首先启动服务端
        dst_thread.start()
        time.sleep(0.2)
        src_thread.start()

        src_thread.join()
        src_result = src_queue.get()
        if src_result['code'] != 0:
            return src_result
        
        dst_thread.join()
        dst_result = dst_queue.get()
        if dst_result['code'] != 0:
            return dst_result
        
        if not CONFIG.get('save_to_redis'):
            # 修改running状态
            data['running'] = False
            db_cli.set_value(f'{topo}_newtraffic_configs', traffic_name, data)
            
        if traffic_distribution == 'constant':
            if CONFIG.get('udp') == True:
                if CONFIG.get('save_to_redis'):
                    return src_result
                return {"code": 0, "message": "success", 'simple_info':src_result['simple_info'], 'detail_info':dst_result['detail_info']}
            else:
                return src_result
        else:
            if CONFIG.get('udp') == True:
                if CONFIG.get('save_to_redis'):
                    return src_result
                simple_info = {}
                simple_info.update(src_result['simple_info'])
                simple_info.update(dst_result['simple_info'])
                return {"code": 0, "message": "success", 'simple_info':simple_info, 'detail_info':dst_result['detail_info']}
            else:
                return src_result

    except Exception as e:
        return {"code": -1, "message": str(e)}

def traffic_delete(user, topo, src_node, dst_node, port, constant):
    '''
    删除从一个节点到另一个节点的流量
    '''
    try:
        user_db_map = UserMapRedis()
        db_cli = user_db_map.get_user_db(user)

        src_NEloc = db_cli.get_value(f'{topo}_{src_node}', 'NEloc')
        dst_NEloc = db_cli.get_value(f'{topo}_{dst_node}', 'NEloc')

        src_workerip = db_cli.get_value('subtopo2worker', src_NEloc)
        dst_workerip = db_cli.get_value('subtopo2worker', dst_NEloc)
        
        src_node_id = db_cli.get_value(f'{topo}_{src_node}', 'NEid')
        dst_node_id = db_cli.get_value(f'{topo}_{dst_node}', 'NEid')
        
        def delete_to_worker(user, topo, worker_ip, node_id, port, constant, queue):
            data = {
                "user": user,
                "topo": topo,
                "node_id": node_id,
                "port": port,
                "constant": constant
            }
            wait_time = 3000
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/traffic_gen/'
            queue.put(requests.delete(req_url, json=data, timeout=(wait_time)).json())
            
        src_queue = queue.Queue()
        dst_queue = queue.Queue()
        src_thread = threading.Thread(target=delete_to_worker, args=(user, topo, src_workerip, src_node_id, port, constant, src_queue))
        dst_thread = threading.Thread(target=delete_to_worker, args=(user, topo, dst_workerip, dst_node_id, port, constant, dst_queue))
        
        src_thread.start()
        dst_thread.start()
        
        src_thread.join()
        src_result = src_queue.get()
        
        dst_thread.join()
        dst_result = dst_queue.get()
        
        if src_result['code'] == 0 and dst_result['code'] == 0:
            return {"code": 0, "message": "success"}
        else:
            return {"code": -1, "message": "fail"}
    except Exception as e:
        return {"code": -1, "message": str(e)}