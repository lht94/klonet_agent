"""
卫星相关接口
"""

from flask.views import MethodView
from flask import request
import json, time

from ....vemu_config.config import PROJ_CONFIG
from ....satellite.satool import shell_execute
from ....satellite.worker_eventset import docker_exec


class SatelliteGenerateTraffic(MethodView):
    """
    /satellite/traffic/

    由于星座倍速，需进行流量生成包装

    使用iperf指令生成流量，有关命令如下：
        -c <server>：指定客户端模式，并指定服务器的地址。
        -s：指定服务器模式，在指定的端口上等待客户端连接。
        -t <time>：指定测试运行的时间，单位为秒。
        -w <window>：设置TCP窗口大小。
        -n <bytes>：指定发送的总字节数。
        -b <bandwidth>：设置发送的带宽限制。
        -u：使用UDP协议进行测试。
        -l <length>：设置UDP数据包的长度
    """
    def post(self):
        """
        操作（产生/删除）地面站之间的流量。
        """
        try:
            data = json.loads(request.get_data(as_text=True))
            
            # 开启iperf
            if data["action"] == "start":
                # server端
                if data["server_client"] == "s":
                    docker_exec(data["dev_id"], 'nohup iperf -s &')
                # client端
                else:
                    docker_exec(
                        data["dev_id"],
                        f"iperf -c {data['ip']} -t {data['last_t']}"
                        f" {data['bw']} {data['bytes']}")
            
            # 关闭iperf
            else:
                pid = docker_exec(
                    data["dev_id"],
                    "ps -ef | grep iperf | grep -v grep  | awk '{print $2}'")
                if pid:
                    docker_exec(data["dev_id"], f"kill -9 {pid}")
            return {'code': 1, 'msg': '流量操作成功'}
        
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}      


class MonitorRealtime(MethodView):
    """
    /satellite/monitor-realtime/
    
    实时监控、获得端到端路径
    """
    def post(self):
        """
        实时监控端到端的时延、丢包
        """
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            dev_id = data['dev_id']
            ip = data['ip']
            pkt_num = data['pkt_num'] if 'pkt_num' in data \
                else PROJ_CONFIG.ping_pkt_num
            
            # ping
            ping_result = shell_execute(
                f"sudo docker exec {dev_id} ping {ip}"
                f" -c {pkt_num} -i .1 | tail -n 2")
            
            # 统计丢包
            loss = ping_result.split(' packet loss')[0].split(', ')[-1] \
                if ping_result.startswith(f"{PROJ_CONFIG.ping_pkt_num} packets") \
                else '100%'
            
            # 统计时延，为RTT/2的最小值
            delay = float(ping_result.split(' = ')[1].split('/')[0]) / 2 \
                if loss != '100%' else None
            
            return {'code': 1,
                    'msg': '实时监控返回成功',
                    'loss': loss,
                    'delay_ms': delay}

        except Exception as e:
            return {"code": 0, "msg": str(e)}

    def get(self):
        """
        获得端到端路径
        """
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            sat_identity = data['sat_identity']
            dev_id = data['dev_id']
            target_para = data['target_para']

            if sat_identity == "switch":
                # 获取下一跳卫星
                port = docker_exec(
                    dev_id, "ovs-appctl fdb/show init-br0"
                            f" | grep {target_para}"
                            f" | awk '{{print $1}}'")
                next_sat = docker_exec(
                    dev_id, "ovs-ofctl show init-br0"
                            f" | grep \"{port}(to\""
                            f" | grep -oP '\((.*?)\)' | sed 's/[(|)]//g'")[2:]
            else:
                next_sat = docker_exec(
                    dev_id, f"route -n | grep {target_para}").split()[-1][2:]

            return {"code": 1,
                    "msg": "获取下一跳成功",
                    "next_sat": next_sat}

        except Exception as e:
            return {"code": 0, "msg": str(e)}
