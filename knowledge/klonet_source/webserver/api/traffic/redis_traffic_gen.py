import re
import json

from flask.views import MethodView
from flask import request
import requests

from ....Service_layer.redis_error import *
from ....Service_layer.redisAPI import UserMapRedis
from ....vemu_config.config import PROJ_CONFIG

class RedisTrafficGenAPI(MethodView):

    def get(self):
        """
        获取流量生成的信息

        GET /master/redis_traffic_gen/

        input:
            {
                "user": username,
                "topo": projectname,
                "traffic_name': traffic_name
                "client_index": client_index, # 客户端查询信息索引
                "server_index": server_index # 服务端查询信息索引
            }

        output:
            {
                "code": code,
                "message": message,
                "client_info": client_info, # 客户端信息
                "server_info": server_info # 服务端信息
            }
        """
        def value_transform(value, unit, ratio):
            if 'K' in unit[0] or 'k' in unit[0]:
                return float(value) * ratio
            elif 'M' in unit[0] or 'm' in unit[0]:
                return float(value) * ratio * ratio
            elif 'G' in unit[0] or 'g' in unit[0]:
                return float(value) * ratio * ratio * ratio
            elif 'T' in unit[0] or 't' in unit[0]:
                return float(value) * ratio * ratio * ratio * ratio
            else:
                return float(value)
        
        def return_klonetpktgen_sum_info(info):
            sum_info = {}
            for line in info:
                if 'Duration:' in line:
                    time_range = f'0.00-{line.split()[1]}'
                    sum_info["time_range"] = time_range
                elif 'Total Data:' in line:
                    transfer_info = line.replace('\r', '').split()
                    transfer = value_transform(float(transfer_info[2]), transfer_info[3], 1024)
                    sum_info["transfer"] = transfer
                elif 'Average Bandwidth:' in line:
                    bandwidth_info = line.replace('\r', '').split()
                    bandwidth = value_transform(float(bandwidth_info[2]), bandwidth_info[3], 1000)
                    sum_info["bandwidth"] = bandwidth
                # 目前不要暂时保留（lzl）
                # elif 'Max_cwnd:' in line:
                #     cwnd_info = line.replace('\r', '').split()
                #     cwnd = value_transform(float(cwnd_info[1]), cwnd_info[2], 1024)
                #     sum_info["cwnd"] = cwnd
                # 目前不要暂时保留（lzl）
                # elif 'Mean_RTT:' in line:
                #     rtt_info = line.replace('\r', '').split()
                #     rtt = rtt_info[1]
                #     sum_info["rtt"] = rtt
                elif 'Retransmissions:' in line:
                    retr_info = line.replace('\r', '').split()
                    retr = retr_info[1]
                    sum_info["retr"] = int(retr)
                elif 'Lost/Total Datagrams:' in line:
                    lost_datagrams_info = line.replace('\r', '').split()
                    lost_rate = float(lost_datagrams_info[3][1:-2])
                    sum_info["lost_rate"] = lost_rate
                elif 'Jitters:' in line:
                    jitter_info = line.replace('\r', '').split()
                    jitter = float(jitter_info[1])
                    sum_info["jitter"] = jitter
            return sum_info

        def return_klonetpktgen_detail_info(info):
            info = info.replace('\r', '').split()   
            detail_info = {}
            time_range = info[1]
            detail_info["time_range"] = time_range
            if 'Datagrams:' in info:
                total_datagrams_index = info.index('Datagrams:')
                total_datagrams = info[total_datagrams_index + 1]
                if '/'  not in total_datagrams:
                    detail_info["total_datagrams"] = int(total_datagrams)
            if 'Transfer:' in info:
                transfer_index = info.index('Transfer:')
                transfer = value_transform(float(info[transfer_index + 1]), info[transfer_index + 2], 1024)
                detail_info["transfer"] = transfer
            if 'Received:' in info:
                received_index = info.index('Received:')
                transfer = value_transform(float(info[received_index + 1]), info[received_index + 2], 1024)
                detail_info["transfer"] = transfer
            if 'Bitrate:' in info:
                bitrate_index = info.index('Bitrate:')
                bandwidth = value_transform(float(info[bitrate_index + 1]), info[bitrate_index + 2], 1000)
                detail_info["bandwidth"] = bandwidth
            if 'Bandwidth:' in info:
                bandwidth_index = info.index('Bandwidth:')
                bandwidth = value_transform(float(info[bandwidth_index + 1]), info[bandwidth_index + 2], 1000)
                detail_info["bandwidth"] = bandwidth
            if 'Retr:' in info:
                retr_index = info.index('Retr:')
                retr = info[retr_index + 1]
                detail_info["retr"] = int(retr)
            if 'Cwnd:' in info:
                cwnd_index = info.index('Cwnd:')
                cwnd = info[cwnd_index + 1]
                detail_info["cwnd"] = float(cwnd)
            # 暂时不要但是保留（lzl）
            # if 'RTT:' in info:
            #     rtt_index = info.index('RTT:')
            #     rtt = info[rtt_index + 1]
            #     detail_info["rtt"] = rtt
            if 'Jitters:' in info:
                jitter_index = info.index('Jitters:')
                jitter = float(info[jitter_index + 1])
                detail_info["jitter"] = jitter
            if 'Lost/Total' in info:
                lost_datagrams_index = info.index('Lost/Total')
                lost_rate = float(info[lost_datagrams_index + 3][1:-2])
                detail_info["lost_rate"] = lost_rate
            return detail_info
        
        try:
            data = request.args.to_dict()
            user, topo, flow_id = data['user'], data['topo'], data['traffic_name']
            client_index = data.get('client_index', 0)
            server_index = data.get('server_index', 0)
            user_db_map = UserMapRedis()
            db_cli = user_db_map.get_user_db(user)

            try:
                db_cli.check_table_exist(f'{topo}_flows{flow_id}_client')
                db_cli.check_table_exist(f'{topo}_flows{flow_id}_server')
            except:
                return {'code': 0, 'message': 'result not exist'}

            client_protocol = db_cli.get_value(f'{topo}_flows{flow_id}_client', "protocol")

            client_index = int(client_index)
            if client_index < 0:
                raise Exception("client_index must be greater than or equal to 0")
            all_client_info = {}
            all_client_info["sum_info"] = []
            all_client_info["detail_info"] = []
            client_info = ''
            while True:
                try:
                    client_info += db_cli.get_value(f'{topo}_flows{flow_id}_client', client_index)
                    client_index += 1
                except KeyNotExistError:
                    break
            client_info = client_info.split('\n')
            
            constant_client  = db_cli.get_value(f'{topo}_flows{flow_id}_client', "constant")
            constant_server  = db_cli.get_value(f'{topo}_flows{flow_id}_server', "constant")
            if constant_client != constant_server:
                raise Exception("constant not equal")
            
            if constant_client == True:
                # 简单匹配 [数字] 开头的行且包含 MBytes 的行
                pattern_simple = r'^\[\s*\d+\].*Bytes.*bits/sec'
            else:
                # 匹配 [数字] 开头的行且包含 MBytes 的行
                pattern_simple = r'^\[\s*\d+\.\d+-\d+\.\d+ s\].*B.*bps.*'

            # 使用列表推导式找出所有匹配的行
            matched_lines = [line for line in client_info if re.match(pattern_simple, line)]
            
            if constant_client == True: # constant
                for line in matched_lines:
                    if 'sender' in line:
                        parts = ' '.join(line.split()).split()
                        time_range = parts[2]
                        transfer = value_transform(float(parts[4]), parts[5], 1024)
                        bandwidth = value_transform(float(parts[6]), parts[7], 1000)
                        retr = int(parts[8])
                        all_client_info["sum_info"].append({"time_range": time_range, "transfer": transfer, "bandwidth": bandwidth, "retr": retr, "type": "sender"})
                    elif 'receiver' in line:
                        parts = ' '.join(line.split()).split()
                        time_range = parts[2]
                        transfer = value_transform(float(parts[4]), parts[5], 1024)
                        bandwidth = value_transform(float(parts[6]), parts[7], 1000)
                        all_client_info["sum_info"].append({"time_range": time_range, "transfer": transfer, "bandwidth": bandwidth, "type": "receiver"})
                    else:
                        parts = ' '.join(line.split()).split()
                        if client_protocol == "tcp":
                            time_range = parts[2]
                            transfer = value_transform(float(parts[4]), parts[5], 1024)
                            bandwidth = value_transform(float(parts[6]), parts[7], 1000)
                            retr = int(parts[8])
                            cwnd = value_transform(float(parts[9]), parts[10], 1024)
                            all_client_info["detail_info"].append({"time_range": time_range, "transfer": transfer, "bandwidth": bandwidth, "retr": retr, "cwnd": cwnd})
                        else: # udp
                            if len(parts) == 9: # detail info
                                time_range = parts[2]
                                transfer = value_transform(float(parts[4]), parts[5], 1024)
                                bandwidth = value_transform(float(parts[6]), parts[7], 1000)
                                total_datagrams = int(parts[8])
                                all_client_info["detail_info"].append({"time_range": time_range, "transfer": transfer, "bandwidth": bandwidth, "total_datagrams": total_datagrams})
                            else: # sum info
                                time_range = parts[2]
                                transfer = value_transform(float(parts[4]), parts[5], 1024)
                                bandwidth = value_transform(float(parts[6]), parts[7], 1000)
                                lost_rate = float(parts[11][1:-2])
                                all_client_info["sum_info"].append({"time_range": time_range, "transfer": transfer, "bandwidth": bandwidth, "lost_rate": lost_rate, "type": "sender"})
            else: # non-constant
                if '=== Test Summary ===\r' in client_info:
                    summary_index = client_info.index('=== Test Summary ===\r')
                    sum_info = return_klonetpktgen_sum_info(client_info[summary_index + 1:])
                    sum_info["type"] = "sender"
                    all_client_info["sum_info"].append(sum_info)
                    
                for line in matched_lines:
                    all_client_info["detail_info"].append(return_klonetpktgen_detail_info(line))


            server_protocol = db_cli.get_value(f'{topo}_flows{flow_id}_server', "protocol")

            server_index = int(server_index)
            if server_index < 0:
                raise Exception("server_index must be greater than or equal to 0")
            all_server_info = {}
            all_server_info["sum_info"] = []
            all_server_info["detail_info"] = []
            server_info_str = ''
            while True:
                try:
                    server_info_str += db_cli.get_value(f'{topo}_flows{flow_id}_server', server_index)
                    server_index += 1
                except KeyNotExistError:
                    break
            server_info = server_info_str.split('\n')
            if constant_server == True: # constant
                pattern_simple = r'^\[\s*\d+\].*Bytes.*bits/sec'
            else:
                pattern_simple = r'^\[\s*\d+\.\d+-\d+\.\d+ s\].*B.*bps.*'
            matched_lines = [line for line in server_info if re.match(pattern_simple, line)]
            
            if constant_server == True: # constant
                for line in matched_lines:
                    if 'sender' in line:
                        parts = ' '.join(line.split()).split()
                        time_range = parts[2]
                        transfer = value_transform(float(parts[4]), parts[5], 1024)
                        bandwidth = value_transform(float(parts[6]), parts[7], 1000)
                        all_server_info["sum_info"].append({"time_range": time_range, "transfer": transfer, "bandwidth": bandwidth, "type": "sender"})
                    elif 'receiver' in line:
                        parts = ' '.join(line.split()).split()
                        time_range = parts[2]
                        transfer = value_transform(float(parts[4]), parts[5], 1024)
                        bandwidth = value_transform(float(parts[6]), parts[7], 1000)
                        all_server_info["sum_info"].append({"time_range": time_range, "transfer": transfer, "bandwidth": bandwidth, "type": "receiver"})
                    else:
                        parts = ' '.join(line.split()).split()
                        if server_protocol == "tcp":
                            time_range = parts[2]
                            transfer = value_transform(float(parts[4]), parts[5], 1024)
                            bandwidth = value_transform(float(parts[6]), parts[7], 1000)
                            all_server_info["detail_info"].append({"time_range": time_range, "transfer": transfer, "bandwidth": bandwidth})
                        else:
                            time_range = parts[2]
                            transfer = value_transform(float(parts[4]), parts[5], 1024)
                            bandwidth = value_transform(float(parts[6]), parts[7], 1000)
                            jitter = float(parts[8])
                            lost_rate = float(parts[11][1:-2])
                            if '- - - - - - - - - - - - - - - - - - - - - - - - -' in server_info_str and line == matched_lines[-1]: # udp sum info
                                all_server_info["sum_info"].append({"time_range": time_range, "transfer": transfer, "bandwidth": bandwidth, "jitter": jitter, "lost_rate": lost_rate, "type": "receiver"})
                            else:
                                all_server_info["detail_info"].append({"time_range": time_range, "transfer": transfer, "bandwidth": bandwidth, "jitter": jitter, "lost_rate": lost_rate})
            else: # non-constant
                if '=== Test Summary ===\r' in server_info:
                    summary_index = server_info.index('=== Test Summary ===\r')
                    sum_info = return_klonetpktgen_sum_info(server_info[summary_index + 1:])
                    sum_info["type"] = "receiver"
                    all_server_info["sum_info"].append(sum_info)
                for line in matched_lines:
                    all_server_info["detail_info"].append(return_klonetpktgen_detail_info(line))

            db_cli.close()
            if all_client_info["sum_info"] != [] and all_server_info["sum_info"] != []:
                traffic_done = True
            else:
                traffic_done = False
            next_client_index = client_index 
            next_server_index = server_index 
            interval = db_cli.get_value(f"{topo}_newtraffic_configs", flow_id)['CONFIG']['interval']
            return {'code':  1, 'message': 'success', 'detail_time_interval':interval, 'client_info': all_client_info, 'server_info': all_server_info, 'traffic_done': traffic_done, 'next_client_index': next_client_index, 'next_server_index': next_server_index}
        except Exception as e:
            return {'code': 0, 'message': str(e)}
        
    def delete(self):
        """
        删除流量生成的信息

        DELETE /master/redis_traffic_gen/

        input:
            {
                "user": username,
                "topo": projectname,
                "traffic_name': traffic_name
            }

        output:
            {
                "code": code,
                "message": message
            }
        """
        try:
            data = json.loads(request.get_data(as_text=True))
            user, topo, flow_id = data['user'], data['topo'], data['traffic_name']
            user_db_map = UserMapRedis()
            db_cli = user_db_map.get_user_db(user)

            try:
                db_cli.check_table_exist(f'{topo}_flows{flow_id}_client')
                db_cli.check_table_exist(f'{topo}_flows{flow_id}_server')
            except:
                return {'code': 0, 'message': 'result not exist'}

            client_done = db_cli.get_value(f'{topo}_flows{flow_id}_client', 'done')
            server_done = db_cli.get_value(f'{topo}_flows{flow_id}_server', 'done')
            
            if client_done == False or server_done == False:    
                # 发送停止流量的命令
                data = {
                    "user": user,
                    "topo": topo,
                    "traffic_name": flow_id
                }
                
                wait_time = 3
                req_url = f'http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/traffic_gen/'
                result = requests.delete(req_url, json=data, timeout=(wait_time)).json()

                if result['code'] != 1:
                    raise Exception(result['message'])

            db_cli.del_all_values(f'{topo}_flows{flow_id}_client')
            db_cli.del_all_values(f'{topo}_flows{flow_id}_server')
            db_cli.close()

            return {'code':  1, 'message': 'success'}
        
        except Exception as e:
            return {'code': 0, 'message': str(e)}
