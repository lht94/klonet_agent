import json
import docker
import time
import re
import multiprocessing as mp
from vemu_uestc.Implement_layer import LinkManager
from vemu_uestc.Service_layer.redisAPI import WorkerRedis
from flask import request
from flask.views import MethodView
from ....Service_layer.LinkManager import deploy_mmlink
from ....Service_layer.LinkManager import terminate_mmlink_processings
from ....Service_layer.LinkManager import WorkerLinkManager
from ....Service_layer.deploy_error import LinkConfigError, LinkInterfaceDeleteError
from ....Service_layer.redisAPI import UserMapRedis
from ....tools import get_host_ip
from ....tools.log_tools import FLASK_LOGGER


local_ip = get_host_ip()
client = docker.from_env()

class StLinkConfigAPI(MethodView):
    '''静态链路
    
    详细参考master下同名API
    '''
    __postexample__ = {
        "user":"user",
        "topo":"user",  
        "links": [
                {'linkchoice': 'static', 
                 'link': 'link_l1', 
                 'ne': 's1', 
                 'bw_kbps': 10000, 
                 'queue_size_bytes': 10000000, 
                 'delay_us': 15, 
                 'loss': 10.0, 
                 'jitter_us': 16, 
                 'correlation': 0.0, 
                 'delay_distribution': 'normal'}]
        }
    def post(self):
        """配置静态链路属性

        POST /worker/stlink/

        Returns:
            dict: 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        link_manager = WorkerLinkManager(data)
        try:
            link_manager.config_links(operate='replace')
        except LinkConfigError as e:
            FLASK_LOGGER.error(e.args[0])
            return {'code': 0, 'msg': e.args[0]}
        finally:
            link_manager.close()
        return {'code': 1, 'msg': '静态链路配置成功'}

    def delete(self):
        """重置静态链路属性

        DELETE /worker/stlink/
        
        Returns:
            dict: 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        link_manager = WorkerLinkManager(data)
        try:
            link_manager.clear_qdisc()
        except LinkInterfaceDeleteError as e:
            FLASK_LOGGER.warning(e)
            # 可忽略该错误，重复删除就是会导致报错
            return {'code': 1, 'msg': '静态链路重置失败'}
        finally:
            link_manager.close()
        return {'code': 1, 'msg': '静态链路重置成功'}


class MmlinkConfigAPI(MethodView):
    '''动态链路
    
    详细参考master下同名API
    '''
    __postexample__ = {
        "user":"user",
        "topo":"user",  
        "links": [{
            'linkchoice': 'mmwave', 
            'link': 'link_l1', 
            'ne': 'h1', 
            'queue_type': 'pie', 
            'link_scenario': 'lb', 
            'loss': 10.0, 
            'bandwidth_scaling': 1.0}]
        }
    def post(self):
        """配置毫米波链路

        POST /worker/mmlink/ 

        Returns:
            dict: 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, links = data['user'], data['topo'], data['links']
        link = links[0]['link'] 
        ne = links[0]['ne'] 
        try:
            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(user)
            # 执行脚本文件，记录进程号
            mmlink_process_list = deploy_mmlink(user, topo, links, user_db_cli)
            # 向数据库写入进程信息，以便后续关闭
            table_name1 = f'{topo}_mmlink_process'
            key =  f'{link}_{ne}'
            user_db_cli.set_value(table_name1, key, mmlink_process_list)
            return {'code': 1, 'msg': '毫米波链路创建成功'}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': '毫米波链路配置失败'}
        finally: 
            user_db_cli.close()
            user_map_redis.close()

    def delete(self):
        """停止毫米波链路

        DELETE /worker/mmlink/
        
        Returns:
            dict: 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo = data['user'], data['topo']
        try:
            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(user)
            for link_conf in data['links']:
                table_name = f'{topo}_mmlink_process'
                key = f'{link_conf["link"]}_{link_conf["ne"]}'
                if not user_db_cli.check_exist(table_name,key):
                    return { 'code':1, 'msg':'毫米波链路重置成功'}
                else:
                    mmlink_processing_list = user_db_cli.get_value(table_name, key)
                result = terminate_mmlink_processings(mmlink_processing_list)
            if result:
                user_db_cli.del_value(table_name, key)
                return {'code': 1, 'msg':'毫米波链路重置成功'}
            else:
                return {'code': 0, 'msg':'毫米波链路重置失败'}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg':'毫米波链路重置失败'}
        finally:
            user_db_cli.close()
            user_map_redis.close()


class ThroughputQueriesAPI(MethodView):
    '''吞吐量查询API
    
    吞吐量查询API
    用于查询链路的吞吐量，通过调用worker的API来实现。
    '''
    def post(self):
        """查询链路吞吐量

        POST /worker/throughput/

        Returns:
            dict: 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, info, wait_time = data['user'], data['topo'], data['info'], data['wait_time']

        def get_throughput(data, wait_time, queue):

            # 转换单位
            def convert_unit(bps):
                if bps < 1000:
                    return f'{bps} '
                elif bps < 1000000:
                    return f'{bps/1000:.3f} K'
                elif bps < 1000000000:
                    return f'{bps/1000000:.3f} M'
                elif bps < 1000000000000:
                    return f'{bps/1000000000:.3f} G'
                else:
                    return f'{bps}'
                
            try:
                ne = data['NE']
                ne_id = data['ID']
                port = data['Port']
                link = data['Link']

                command = f"""sh -c '
                    rx1=$(ifconfig {port} | grep "RX packets" | awk "{{print \\$5}}")
                    tx1=$(ifconfig {port} | grep "TX packets" | awk "{{print \\$5}}")
                    sleep {wait_time}
                    rx2=$(ifconfig {port} | grep "RX packets" | awk "{{print \\$5}}")
                    tx2=$(ifconfig {port} | grep "TX packets" | awk "{{print \\$5}}")
                    rx_bps=$(awk "BEGIN {{printf \\"%.0f\\", ($rx2-$rx1)*8/{wait_time}}}")
                    tx_bps=$(awk "BEGIN {{printf \\"%.0f\\", ($tx2-$tx1)*8/{wait_time}}}")
                    echo "$rx_bps"
                    echo "$tx_bps"
                '"""
                
                container = client.containers.get(ne_id)
                result = container.exec_run(command)

                result = result.output.decode('utf-8')
                result = result.strip().split('\n')
                rx_bps, tx_bps = int(result[0]), int(result[1])
                rx_throughput = convert_unit(rx_bps)
                tx_throughput = convert_unit(tx_bps)
                queue.put({'link': link, 'ne': ne, 'throughput': {'rx': f'{rx_throughput}bps', 'tx': f'{tx_throughput}bps'}})
            except:
                queue.put({'link': link, 'ne': ne, 'throughput': None})


        processes = []
        queue = mp.Queue()
        for data in info:
            processes.append(mp.Process(target=get_throughput, args=(data, wait_time, queue)))
        for process in processes:
            process.start()
        for process in processes:
            process.join()

        results = []
        while not queue.empty():
            result = queue.get()
            if result['throughput'] is None:
                return {'code': 0, 'msg': '吞吐量查询失败'}
            results.append(result)

        return {'code': 1, 'msg': '吞吐量查询成功', 'result': results}
    
    
class DelayAPI(MethodView):
    '''时延查询API
    
    时延查询API
    用于查询链路的时延。
    '''
    def post(self):
        """查询链路吞吐量

        POST /worker/delay/

        Returns:
            dict: 执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        container, IP = data['container'], data['IP']

        command = f'ping -i 0.1 -c 10 {IP}'
                
        container = client.containers.get(container)
        result = container.exec_run(command, stream=False)

        exit_code = result.exit_code
        if exit_code == 0: 
            output = result.output.decode('utf-8')
            pattern = r'(?:rtt|round-trip).*min/avg/max/.*=\s*([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)\s*ms'
            for line in output.splitlines():
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    res = float(match.group(2))  # 第二个值为平均值
                    return {'code': 1, 'msg': str(res) + "ms"}
        else:
            return {'code': 0, 'msg': "容器操作失败"}