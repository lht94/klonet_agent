import json
from flask.views import MethodView
from flask import request, jsonify
import requests

from ....Function_layer.deployed_proj_manager import retrieve_topo
from ....vemu_config.config import PROJ_CONFIG
from ....Service_layer.redisAPI import UserMapRedis
from flask_cors import CORS
from flask_cors import cross_origin

class TopoRecoverAPI(MethodView):
    """
    拓扑恢复API
    /topo/recover/
    """
    @cross_origin()
    def post(self):
        """
        处理拓扑恢复的HTTP请求
        请求参数：{
            "user": "用户名",
            "topo": "拓扑名/项目名"
        }
        """
        data = request.get_json()
        # data = json.loads(request.get_data(as_text=True))
        if not data:
            return {'code': 0, 'msg': 'Invalid JSON data or missing Content-Type application/json'}
        user = data.get('user')
        topo = data.get('topo')
        if not user or not topo:
            return {'code': 0, 'msg': '缺少user或topo参数'}

        # 1. 获取拓扑结构
        topo_info = retrieve_topo(user, topo)
        if topo_info.get('code') != 1:
            return {'code': 0, 'msg': f"获取拓扑信息失败: {topo_info.get('msg', '')}"}
        net = topo_info.get('networks', {}) # 包含了拓扑内的所有节点与链路信息

        # return {'code':1,'msg':'获取拓扑结构成功','net':net}

        # 2. 恢复节点
        """
        因为虚拟机做了nat,故config里的url均为本机IP
        """
        node_results = []
        user_db = UserMapRedis().get_user_db(user)
        for ne_type in ['hosts', 'switches', 'routers', 'controllers']:
            nodes = net.get(ne_type, {})
            for node_name, node_info in nodes.items():
                # a. 获取容器ID(NAMES)
                try:
                    container_id = user_db.get_value(f"{topo}_{node_name}", "NEid")
                except Exception as e:
                    node_results.append({'node': node_name, 'code': 0, 'msg': f'获取容器ID失败: {str(e)}'})
                    continue
                # b. 获取worker IP
                try:
                    worker_ip = user_db.get_worker_ip_by_ne_name(topo, node_name)
                except Exception as e:
                    node_results.append({'node': node_name, 'code': 0, 'msg': f'获取worker_ip失败: {str(e)}'})
                    continue
                # c. 调用worker端API启动所有容器（包括OVS交换机）
                url = f"http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/container/start/"
                payload = {"container_id": container_id}
                try:
                    resp = requests.post(url, json=payload, timeout=10)
                    res_json = resp.json()
                except Exception as e:
                    node_results.append({'node': node_name, 'code': 0, 'msg': f'容器启动请求异常: {str(e)}'})
                    continue
                node_results.append({'node': node_name, 'code': res_json.get('code', 0), 'msg': res_json.get('msg', '')})
                # d. 调用worker端API启动OVS交换机服务
                # ovs节点必须初始化服务。初始化后stp配置自动会恢复
                # 没有恢复服务时无法创建链路，也无法配置交换机
                if ne_type == 'switches':
                    url = f"http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/ovs/start/"
                    payload = {
                        "user":user,
                        "topo":topo,
                        "ovs_name":node_name
                    }
                    try:
                        # 实际上就是下发指令 service openvswitch-switch start
                        # ovs-vsctl list bridge init-br0 检测服务是否启动成功
                        resp = requests.post(url, json=payload, timeout=10)
                        res_json = resp.json()
                    except Exception as e:
                        node_results.append({'node':node_name,'code':0,'msg':f'ovs服务启动异常: {str(e)}'})
                    node_results.append({'node':node_name,'code':res_json.get('code',0),'msg':res_json.get('msg','')})

                # d. 恢复节点网关，暂未实现

        
        
        # return {'code':1,'msg':'恢复节点成功','results':node_results}
        
        # 3. 恢复链路
        link_results = []
        links = net.get('links',{}) 
        for link_name,link_info in links.items():
            # a. 先删除链路
            delete_payload = {
                "user":user,
                "topo":topo,
                "info":link_info
            }
            url = f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/modification/link/"
            try:
                resp = requests.delete(url,json=delete_payload,timeout=10)
                resp_json = resp.json() # 这里的响应是0，但不影响
            except Exception as e:
                link_results.append({'link' : link_name, 'code' : 0, 'msg' : f'删除链路失败 : {str(e)}'})
                continue
            # link_results.append({'link' : link_name, 'code' : 1 , 'msg' : f'删除链路成功'})
            # b. 再创建参数相同的链路
            # 链路创建成功时，主机便多了一个网卡。该网卡用来配置IP，故恢复链路时即恢复了IP(link_info里有IP)
            create_payload = delete_payload
            url = f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/modification/link/"
            try:
                resp = requests.post(url,json=delete_payload,timeout=10)
                resp_json = resp.json() # 这里的响应是0，但不影响
            except Exception as e:
                link_results.append({'link' : link_name, 'code' : 0, 'msg' : f'创建链路失败 : {str(e)}'})
                continue
            link_results.append({'link' : link_name, 'code' : resp_json.get('code',0) , 'msg' : resp_json.get('msg', '')})
            # c. 接着配置链路参数
            # get能够防止由于键不存在而出现的报错
            if link_info.get('config',None) and link_info['config']['flag'] == True:
                if link_info['config']['src_con_flag'] == True:
                    source_config = link_info['config']['source']
                if link_info['config']['trg_con_flag'] == True:
                    target_config = link_info['config']['target']
                config_payload = {
                    "user": user,
                    "topo": topo,
                    "links":[source_config,target_config]
                }
                url = f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/link/"
                try:
                    resp = requests.post(url,json=config_payload,timeout=10)
                    resp_json = resp.json()
                except Exception as e:
                    link_results.append({'link' : link_name, 'code' : 0, 'msg' : f'链路配置失败 : {str(e)}'})
                    continue
                link_results.append({'link': link_name, 'code': resp_json.get('code', 0), 'msg': '链路配置成功'})
                    


        return {'code':1,'msg':'拓扑恢复成功','node_results':node_results,'link_results':link_results}





   

"""
附：
API负载与响应示例

1. topo_info = retrieve_topo(user, topo)
响应如下：
{
    "code": 1,
    "msg": "获取拓扑成功",
    "networks": {
        "controllers": {},
        "dpdks": {},
        "hosts": {
            "h1": {
                "gateway": "192.168.1.254",
                "image_name": "host/ubuntu",
                "interfaces": [
                    {
                        "ip": "192.168.1.1",
                        "name": "h1s1_1",
                        "netmask": "255.255.255.0"
                    }
                ],
                "linestyle": "solid",
                "name": "h1",
                "resource_limit": {
                    "cpu": "8",
                    "mem": "20"
                },
                "service": "docker",
                "subtype": "ubuntu",
                "type": "host",
                "x": 112,
                "y": 399
            },
        "links": {
            "l1": {
                "config": {
                    "flag": false,
                    "src_con_flag": false,
                    "trg_con_flag": false
                },
                "name": "l1",
                "source": "h1",
                "sourceIP": "192.168.1.1/24",
                "sourceType": "host",
                "target": "s1",
                "targetIP": "",
                "targetType": "switch"
            }
        },
        "routers": {},
        "switches": {
            "s1": {
                "config": {
                    "controllers": [],
                    "dpid": "00002e532ebdcb48",
                    "stp": true
                },
                "image_name": "switch/ovs",
                "linestyle": "solid",
                "name": "s1",
                "resource_limit": {
                    "cpu": "100",
                    "mem": "1000"
                },
                "service": "docker",
                "subtype": "ovs",
                "type": "switch",
                "x": 266,
                "y": 326
            }
        }
    },
    "topo": "a",
    "user": "lht"
}

2. modification/link/  DELETE/POST
二者负载相同，如下：
{
    "user": "lht",
    "topo": "a",
    "info": {
        "config": {
        "flag": false,
        "src_con_flag": false,
        "trg_con_flag": false
        },
        "name": "l1",
        "source": "h1",
        "sourceIP": "192.168.1.2/24", #存储的有IP信息
        "sourceType": "host",
        "target": "s1",
        "targetIP": "",
        "targetType": "switch"
    }
}

3. 数据库中tcConfig的值
{
  "flag": true,
  "source": {
    "bw_kbps": "10000",
    "correlation": "10",
    "delay_distribution": "uniform",
    "delay_us": "0",
    "jitter_us": "0",
    "loss": "0",
    "queue_size_bytes": "100000",
    "linkchoice": "static",
    "link": "link_l1",
    "ne": "h1"
  },
  "src_con_flag": true,
  "target": {
    "bw_kbps": "10000",
    "correlation": "0",
    "delay_distribution": "uniform",
    "delay_us": "0",
    "jitter_us": "0",
    "loss": "0",
    "queue_size_bytes": "100000",
    "linkchoice": "static",
    "link": "link_l1",
    "ne": "s1"
  },
  "trg_con_flag": true
}

4. /master/link参数
{
  "user": "lht",
  "topo": "a",
  "links": [
    {
      "bw_kbps": "10000",
      "correlation": "10",
      "delay_distribution": "uniform",
      "delay_us": "10",
      "jitter_us": "10",
      "loss": "10",
      "queue_size_bytes": "100000",
      "linkchoice": "static",
      "link": "link_l5",
      "ne": "s1"
    },
    {
      "bw_kbps": "10000",
      "correlation": "0",
      "delay_distribution": "uniform",
      "delay_us": "0",
      "jitter_us": "0",
      "loss": "0",
      "queue_size_bytes": "100000",
      "linkchoice": "static",
      "link": "link_l5",
      "ne": "h2"
    }
  ]
}
"""