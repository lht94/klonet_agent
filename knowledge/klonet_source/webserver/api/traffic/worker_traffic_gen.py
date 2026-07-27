import docker
import json
import os
import random
from flask.views import MethodView
from flask import request
from multiprocessing import Process

from ....Service_layer.redisAPI import UserMapRedis
from ....Implement_layer.LinkManager import shell_execute

class WorkerTrafficGenAPI(MethodView):
    def post(self):
        '''
        产生从一个节点到另一个节点的流量
        分为客户端和服务端两部分

        POST worker/traffic_gen/

        input:
            {
                "user": username,
                "topo": projectname,
                "node": 节点,
                "node_id": 节点id,
                "target_node_ip": 目标节点ip,
                "type": 类型（客户端还是服务端）,
                "data_size": 总数据大小,
                "run_time": 运行时间,
                "traffic_distribution": 流量分布,
                "CONFIG": 其他配置
            }

        output:
            {
                "code": 0,
                "message": "success",
                "run_time": 运行时间,
                "performance_indicators": 其他性能指标
            }
        '''
        def create_traffic_process(db_cli, topo, CONFIG, flow_id, container, cmd, type, node, docker_client):

            result = container.exec_run(cmd, stdin=True, tty=True, stream=True)
            index = 0
            db_cli.set_value(f'{topo}_flows{flow_id}_{type}', "node", node)
            protocol = "udp" if CONFIG.get('udp') == True else "tcp"
            port = CONFIG.get('port')
            constant = True if CONFIG.get('traffic_distribution') == 'constant' else False
            db_cli.set_value(f'{topo}_flows{flow_id}_{type}', "protocol", protocol)
            db_cli.set_value(f'{topo}_flows{flow_id}_{type}', "constant", constant)
            db_cli.set_value(f'{topo}_flows{flow_id}_{type}', "port", port)
            db_cli.set_value(f'{topo}_flows{flow_id}_{type}', "done", False)
            for output in result.output:
                if output:
                    db_cli.set_value(f'{topo}_flows{flow_id}_{type}', index, output.decode())
                    index += 1 
            db_cli.set_value(f'{topo}_flows{flow_id}_{type}', "done", True)
            config_data = db_cli.get_value(f'{topo}_newtraffic_configs', flow_id)
            config_data['running'] = False
            db_cli.set_value(f'{topo}_newtraffic_configs', flow_id, config_data)
            db_cli.close()
            docker_client.close()

        try:
            data = request.get_json()
            traffic_name = data['traffic_name']
            user = data['user']
            topo = data['topo']
            node = data['node']
            node_id = data['node_id']
            type = data['type']
            src_node_ip = data.get('src_node_ip')
            dst_node_ip = data.get('dst_node_ip')
            data_size = data.get('data_size')
            run_time = data.get('run_time')
            traffic_distribution = data.get('traffic_distribution')
            CONFIG = data.get('CONFIG')
            port = CONFIG.get('port')

            # 利用iperf3
            docker_client = docker.from_env()
            container = docker_client.containers.get(node_id)
            if traffic_distribution == 'constant':

                if type == 'server':
                    # 服务端
                    cmd = f'iperf3 -p {port} -s -1 -B {dst_node_ip}' # 仅仅接收一次连接
                    if CONFIG.get('udp') == True and CONFIG.get('interval'):
                        cmd += f' -i {CONFIG.get("interval")}'
                    if CONFIG.get('interval'):
                        interval = CONFIG.get('interval')
                        if interval < 0.1:
                            raise Exception('interval must be greater than 0.1')
                        cmd += f' -i {interval}'
                    if not CONFIG.get('save_to_redis'):
                        cmd += ' -J'
                elif type == 'client':
                    # 客户端
                    cmd = f'iperf3 -p {port} -c {dst_node_ip} -B {src_node_ip}'
                    if data_size:
                        cmd += f' -n {data_size}'
                    if run_time:
                        cmd += f' -t {run_time}'
                    if CONFIG.get('interval'):
                        interval = CONFIG.get('interval')
                        if interval < 0.1:
                            raise Exception('interval must be greater than 0.1')
                        cmd += f' -i {interval}'
                    if CONFIG.get('package_size'):
                        package_size = CONFIG.get('package_size')
                        cmd += f' -l {package_size}'
                    if CONFIG.get('bandwidth'):
                        bandwidth = CONFIG.get('bandwidth')
                        cmd += f' -b {bandwidth}'
                    if CONFIG.get('udp') == True:
                        cmd += ' -u'
                    if not CONFIG.get('save_to_redis'):
                        cmd += ' -J'

                if CONFIG.get('save_to_redis'):
                    flow_id = traffic_name
                    user_db_map = UserMapRedis()
                    db_cli = user_db_map.get_user_db(user)

                    traffic_process = Process(target=create_traffic_process, args=(db_cli, topo, CONFIG, flow_id, container, cmd, type, node, docker_client))
                    traffic_process.daemon = False  # 设置为非守护进程，使得即使主进程结束，子进程也能继续执行
                    traffic_process.start()
                else:
                    result = container.exec_run(cmd, detach=False)
                    docker_client.close()
            else:
                if traffic_distribution == 'multiple':
                    pps_distribution = CONFIG.get('pps_distribution', None)
                    pkt_distribution = CONFIG.get('pkt_distribution', None)
                    bandwidth_distribution = CONFIG.get('bandwidth_distribution', None)
                else:
                    pps_distribution = traffic_distribution
                    pkt_distribution = traffic_distribution
                    bandwidth_distribution = traffic_distribution
                if type == 'server':
                    cmd = f'Klonetpktgen -p {port} -s -1 -B {dst_node_ip}' # 仅仅接收一次连接
                    if CONFIG.get('udp') == True:
                        cmd += ' -u'
                    if CONFIG.get('interval'):
                        interval = CONFIG.get('interval')
                        if interval < 0.1:
                            raise Exception('interval must be greater than 0.1')
                        cmd += f' -i {interval}'
                    if not CONFIG.get('save_to_redis'):
                        cmd += ' -J'
                elif type == 'client':
                    cmd = f'Klonetpktgen -p {port} -c {dst_node_ip} -B {src_node_ip}'
                    if data_size:
                        cmd += f' -n {data_size}'
                    if run_time:
                        cmd += f' -t {run_time}'
                    if CONFIG.get('interval'):
                        interval = CONFIG.get('interval')
                        if interval < 0.1:
                            raise Exception('interval must be greater than 0.1')
                        cmd += f' -i {interval}'
                    else:
                        interval = 1
                    if CONFIG.get('package_size'):
                        package_size = CONFIG.get('package_size')
                        cmd += f' -l {package_size}'
                    if CONFIG.get('bandwidth'):
                        bandwidth = CONFIG.get('bandwidth')
                        cmd += f' -b {bandwidth}'
                    if CONFIG.get('udp') == True:
                        cmd += ' -u'
                    if not CONFIG.get('save_to_redis'):
                        cmd += ' -J'
                    if pps_distribution:
                        cmd += f' -dpps {pps_distribution}'
                    if pkt_distribution:
                        cmd += f' -dl {pkt_distribution}'
                    if bandwidth_distribution:
                        cmd += f' -db {bandwidth_distribution}'
                        bandwidth_reset_interval = CONFIG.get('bw_reset_interval', None)
                        if bandwidth_reset_interval == None:
                            bandwidth_reset_interval = interval
                        cmd += f' -bri {bandwidth_reset_interval}'

                if CONFIG.get('save_to_redis'):
                    flow_id = traffic_name
                    user_db_map = UserMapRedis()
                    db_cli = user_db_map.get_user_db(user)

                    traffic_process = Process(target=create_traffic_process, args=(db_cli, topo, CONFIG, flow_id, container, cmd, type, node, docker_client))
                    traffic_process.daemon = False  # 设置为非守护进程，使得即使主进程结束，子进程也能继续执行
                    traffic_process.start()
                else:
                    result = container.exec_run(cmd, detach=False)
                    docker_client.close()       

            if traffic_distribution == 'constant':
                if CONFIG.get('save_to_redis'):
                    return {'code': 0, 'message': 'success to start traffic generator'}
                
                # 解析结果
                result = result.output.decode('utf-8')
                result = json.loads(result)

                if CONFIG.get('udp') != True:
                    if type == 'server':
                        return {'code': 0, 'message': 'success'}
                    else:
                        simple_info = {
                            'time': result['end']['sum_sent']['seconds'],
                            'transfer': result['end']['sum_sent']['bytes'],
                            'throughput': result['end']['sum_sent']['bits_per_second'],
                        }

                        detail_info = {}
                        if CONFIG.get('detail') == True:
                            for flow in result['end']['streams']:
                                flow_id = flow['sender']['socket']
                                detail_info[flow_id] = {'sum_info':{
                                    'time': flow['sender']['seconds'],
                                    'transfer': flow['sender']['bytes'],
                                    'throughput': flow['sender']['bits_per_second'],
                                    'retr': flow['sender']['retransmits'],
                                    'max_cwnd': flow['sender']['max_snd_cwnd'],
                                    'mean_rtt': flow['sender']['mean_rtt']
                                }, 'interval_info': []}
                            for interval in result['intervals']:
                                for flow in interval['streams']:
                                    detail_info[flow['socket']]['interval_info'].append({
                                        'time': f"{flow['start']}-{flow['end']}",
                                        'transfer': flow['bytes'],
                                        'throughput': flow['bits_per_second'],
                                        'retr': flow['retransmits'],
                                        'cwnd': flow['snd_cwnd'],
                                        'rtt': flow['rtt']
                                    })

                else:
                    if type == 'client':
                        detail_info = {}
                        simple_info = {
                            'time': result['end']['sum']['seconds'],
                            'transfer': result['end']['sum']['bytes'],
                            'throughput': result['end']['sum']['bits_per_second'],
                            'jitter_ms': result['end']['sum']['jitter_ms'],
                            'lost_percent': result['end']['sum']['lost_percent']
                        }

                    else:
                        simple_info = {}
                        detail_info = {}
                        if CONFIG.get('detail') == True:
                            for flow in result['end']['streams']:
                                flow_id = flow['udp']['socket']
                                detail_info[flow_id] = {'sum_info':{
                                    'time': flow['udp']['seconds'],
                                    'transfer': flow['udp']['bytes'],
                                    'throughput': flow['udp']['bits_per_second'],
                                    'jitter_ms': flow['udp']['jitter_ms'],
                                    'lost_percent': flow['udp']['lost_percent']
                                }, 'interval_info': []}
                            for interval in result['intervals']:
                                for flow in interval['streams']:
                                    detail_info[flow['socket']]['interval_info'].append({
                                        'time': f"{flow['start']}-{flow['end']}",
                                        'transfer': flow['bytes'],
                                        'throughput': flow['bits_per_second'],
                                        'jitter_ms': flow['jitter_ms'],
                                        'lost_percent': flow['lost_percent']
                                    })

            else:
                if CONFIG.get('save_to_redis'):
                    return {'code': 0, 'message': 'success to start traffic generator'}
                
                result = result.output.decode('utf-8')
                result = json.loads(result)

                if CONFIG.get('udp') != True:
                    if type == 'server':
                        return {'code': 0, 'message': 'success'}
                    else:
                        simple_info = {
                            'time': result['end']['seconds'],
                            'transfer': result['end']['bytes'],
                            'throughput': result['end']['bits_per_second'],
                        }

                        detail_info = {}
                        if CONFIG.get('detail') == True:
                            flow_id = str(1)
                            detail_info[flow_id] = {'sum_info':{
                                'time': result['end']['seconds'],
                                'transfer': result['end']['bytes'],
                                'throughput': result['end']['bits_per_second'],
                                'retr': result['end']['retransmits'],
                                'max_cwnd': result['end']['max_snd_cwnd'],
                                'mean_rtt': result['end']['mean_rtt']
                            }, 'interval_info': []}

                            for interval in result['intervals']:
                                detail_info[flow_id]['interval_info'].append({
                                    'time': interval['times'],
                                    'transfer': interval['bytes'],
                                    'throughput': interval['bandwidth'],
                                    'retr': interval['retr'],
                                    'cwnd': interval['cwnd'],
                                    'rtt': interval['rtt']
                                })

                else:
                    if type == 'client':
                        detail_info = {}
                        simple_info = {
                            'time': result['end']['seconds'],
                            'transfer': result['end']['bytes'],
                            'throughput': result['end']['bits_per_second']
                        }

                    else:
                        simple_info = {
                            'jitter_ms': result['end']['jitter_ms'],
                            'lost_percent': result['end']['lost_percent']
                        }
                        detail_info = {}
                        if CONFIG.get('detail') == True:
                            flow_id = str(1)
                            detail_info[flow_id] = {'sum_info':{
                                'time': result['end']['seconds'],
                                'transfer': result['end']['bytes'],
                                'throughput': result['end']['bits_per_second'],
                                'jitter_ms': result['end']['jitter_ms'],
                                'lost_percent': result['end']['lost_percent']
                            }, 'interval_info': []}

                            for interval in result['intervals']:
                                detail_info[flow_id]['interval_info'].append({
                                    'time': interval['times'],
                                    'transfer': interval['bytes'],
                                    'throughput': interval['bandwidth'],
                                    'jitter_ms': interval['jitter_ms'],
                                    'lost_percent': interval['lost_percent']
                                })

            return {'code': 0, 'message': 'success', 'simple_info':simple_info, 'detail_info': detail_info}
        except Exception as e:
            try:
                # 删除之前的iperf3进程，关闭端口为port的iperf3
                cmd = f'pkill -f "iperf3.*-p {port}"'
                container.exec_run(cmd, detach=False)
                docker_client.close()
            except:
                pass
            return {'code': -1, 'message': str(e)}

    def put(self):
        """
        预备工作
        1. 检查是否有iperf3进程
        2. 如果没有，下载iperf3
        """
        try:
            data = json.loads(request.get_data(as_text=True))
            type = data['type']
            CONFIG = data['CONFIG']
            node_id = data['node_id']
            docker_client = docker.from_env()
            container = docker_client.containers.get(node_id)
            if type == 'server':
                if 'port' in CONFIG:
                    port = CONFIG['port']
                else:
                    # 先检测是否安装netstat
                    cmd = 'netstat -tunlp'
                    result = container.exec_run(cmd, detach=False)
                    if result.exit_code != 0:
                        cmd = "cat /etc/debian_version"
                        result = container.exec_run(cmd, detach=False)
                        if result.exit_code == 0 and result.output.decode('utf-8') != '':
                            dir_path = os.path.dirname(os.path.abspath(__file__))
                            dir_path = f'{dir_path}/source_pkt/net-tools-deb'
                            container.exec_run(f'mkdir -p /root/source_pkt/net-tools-deb')
                            files = os.listdir(dir_path)
                            for file in files:
                                shell_execute(f'docker cp {dir_path}/{file} {node_id}:/root/source_pkt/net-tools-deb')
                                cmd = f'dpkg -i /root/source_pkt/net-tools-deb/{file}'
                                container.exec_run(cmd, detach=False)
                        else:
                            return {'code': -1, 'message': 'netstat not found'}

                    for i in range(50):
                        port = random.randint(5000, 7000)
                        # 利用 netstat 查看端口是否被占用
                        cmd = f'/bin/sh -c "netstat -tunlp | grep {port}"'
                        result = container.exec_run(cmd, detach=False)
                        result = result.output.decode('utf-8')
                        if result == '':
                            break
                        if i == 49:
                            return {'code': -1, 'message': 'no available port'}

            # 检测是否有占用iperf3的进程, 如果占用就根据force来决定是否删除
            if CONFIG.get('traffic_distribution') == 'constant':
                # 首先检查是否有iperf3
                if type == 'server':
                    if CONFIG.get('force') == True:
                        # 删除之前的iperf3进程，关闭端口为port的iperf3
                        cmd = f'pkill -f "iperf3.*-p {port}"'
                        container.exec_run(cmd, detach=False)
                    else:
                        cmd = f"/bin/sh -c \"ps -ef | grep 'iperf3.*-p {port}'\""
                        result = container.exec_run(cmd, detach=False)
                        result = result.output.decode('utf-8').split('\n')
                        if len(result) > 3:
                            return {'code': -1, 'message': f'iperf3 -p {port} is already in use'}

                # 检测并下载资源
                cmd = 'iperf3 -v'
                result = container.exec_run(cmd, detach=False)
                if result.exit_code != 0:
                    cmd = "cat /etc/debian_version"
                    result = container.exec_run(cmd, detach=False)
                    if result.exit_code == 0 and result.output.decode('utf-8') != '':
                        # 下载iperf
                        dir_path = os.path.dirname(os.path.abspath(__file__))
                        dir_path = f'{dir_path}/source_pkt/iperf3-deb'
                        container.exec_run(f'mkdir -p /root/source_pkt/iperf3-deb')
                        files = os.listdir(dir_path)
                        sorted_files = [None, None, None]
                        for file in files:
                            if 'libsctp1' in file:
                                sorted_files[0] = file
                            elif 'libiperf0' in file:
                                sorted_files[1] = file
                            elif 'iperf3' in file:
                                sorted_files[2] = file
                        for file in sorted_files:
                            shell_execute(f'sudo docker cp {dir_path}/{file} {node_id}:/root/source_pkt/iperf3-deb')
                            cmd = f'dpkg -i /root/source_pkt/iperf3-deb/{file}'
                            container.exec_run(cmd, detach=False)
                        container.exec_run('ln -s /usr/bin/iperf3 /usr/local/bin/iperf3')
                        
                        # 再次检查是否有iperf3
                        cmd = 'iperf3 -v'
                        result = container.exec_run(cmd, detach=False)
                        if result.exit_code != 0:
                            return {'code': -1, 'message': 'iperf3 not found'}
                    else:
                        return {'code': -1, 'message': 'iperf3 not found'}
            else:
                # 检查是否有Klonetpktgen
                if type == 'server':
                    if CONFIG.get('force') == True:
                        cmd = f'pkill -f "Klonetpktgen.*-p {port}"'
                        container.exec_run(cmd, detach=False)
                    else:
                        cmd = f"/bin/sh -c \"ps -ef | grep 'Klonetpktgen.*-p {port}'\""
                        result = container.exec_run(cmd, detach=False)
                        result = result.output.decode('utf-8').split('\n')
                        if len(result) > 3:
                            return {'code': -1, 'message': f'Klonetpktgen -p {port} is already in use'}
                        
                cmd = 'Klonetpktgen -v'
                result = container.exec_run(cmd, detach=False)
                if result.exit_code != 0:
                    cmd = "cat /etc/debian_version"
                    result = container.exec_run(cmd, detach=False)
                    if result.exit_code == 0 and result.output.decode('utf-8') != '':
                        # 下载Klonetpktgen
                        dir_path = os.path.dirname(os.path.abspath(__file__))
                        dir_path = f'{dir_path}/source_pkt/Klonetpktgen'
                        shell_execute(f'sudo docker cp {dir_path}/Klonetpktgen {node_id}:/usr/local/bin/')
                        
                        # 再次检查是否有Klonetpktgen
                        cmd = 'Klonetpktgen -v'
                        result = container.exec_run(cmd, detach=False)
                        if result.exit_code != 0:
                            return {'code': -1, 'message': 'Klonetpktgen not found'}
                    else:
                        return {'code': -1, 'message': 'Klonetpktgen not found'}
            
            docker_client.close()
            if type == 'server':
                return {'code': 0, 'message': 'success', 'port': port}
            else:
                return {'code': 0, 'message': 'success'}
        except Exception as e:
            return {'code': -1, 'message': str(e)}

    def delete(self):
        '''
        删除流量生成器
        '''
        try:
            data = request.get_json()
            user = data['user']
            topo = data['topo']
            node_id = data['node_id']
            port = data['port']
            constant = data['constant']
            docker_client = docker.from_env()
            container = docker_client.containers.get(node_id)
            
            if constant:
                cmd = f'pkill -f "iperf3.*-p {port}"'
                container.exec_run(cmd, detach=False)
                docker_client.close()
            else:
                cmd = f'pkill -f "Klonetpktgen.*-p {port}"'
                container.exec_run(cmd, detach=False)
                docker_client.close()
            return {'code': 0, 'message': 'success'}
        except Exception as e:
            return {'code': -1, 'message': str(e)}