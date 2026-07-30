import json
from flask import request
from flask.views import MethodView
from ....Function_layer.deployed_proj_manager import create_all_traffic
from ....Function_layer.deployed_proj_manager import create_traffic_template
from ....Function_layer.deployed_proj_manager import delete_all_traffic
from ....Function_layer.deployed_proj_manager import delete_traffic_app
from ....Function_layer.deployed_proj_manager import delete_traffic_template
from ....Function_layer.deployed_proj_manager import delete_all_template
from ....Function_layer.deployed_proj_manager import update_traffic_app
from ....Function_layer.deployed_proj_manager import retrieve_all_traffic
from ....Function_layer.deployed_proj_manager import retrieve_traffic_app


'''
流量格式：

total_traffic = {
    "user": "xc",
    "traffics": {
        "app1": {
            "traffic_gen": [
                {
                    "mode": "0",
                    "server_list": [
                        "h2:192.168.1.3:5001", 
                        "h3:192.168.1.4:5001", 
                        "h4:192.168.1.5:5001"
                    ],
                    "client": {
                        "client_name": "h1",
                        "client_config": {
                            "server_list": [
                                "h2:192.168.1.3:5001", 
                                "h3:192.168.1.4:5001", 
                                "h4:192.168.1.5:5001"
                            ],
                            "req_size_dist": {
                                "100": "0.1",  # 大小分布CDF
                                "200": "0.4",
                                "1000": "0.7",
                                "10000": "1"
                            },
                            "dscp": {
                                "0": "25",
                                "1": "25",
                                "2": "50"
                            },
                            "rate": {
                                "1Mbps": "50",
                                "2Mbps": "50"
                            }
                        },
                        "cli_param": {
                            "-b": "1",  # 以Mbps为单位 
                            "-t": "",
                            "-n": "100",
                            "-s": "12",
                        }
                    }
                },
                {
                    "mode": "1",
                    "server_list": [
                        "h1:192.168.1.2:5001", 
                        "h2:192.168.1.3:5001"
                    ],
                    "client": {
                        "client_name": "h4",
                        "client_config": {
                            "server_list": [
                                "h1:192.168.1.2:5001", 
                                "h2:192.168.1.3:5001"
                            ],
                            "req_size_dist": {
                                "100": "0.1",  # 大小分布CDF
                                "500": "0.4",
                                "2000": "0.7",
                                "10000": "1"
                            },
                            "dscp": {
                                "0": "25",
                                "1": "25",
                                "2": "50"
                            },
                            "rate": {
                                "1Mbps": "50",
                                "2Mbps": "50"
                            },
                            "fanout": {
                                "1": "10",  # 这个根据client的mode读取
                                "2": "50",
                                "3": "40"
                            },
                        },
                        "cli_param": {
                            "-b": "1",  # 以Mbps为单位 
                            "-t": "",
                            "-n": "200",
                            "-s": "20",
                        }
                    }
                }
            ],
            "pkt_gen2": [
                {
                    "src": "h1",
                    "dst": "h2",
                    "src_ip": "192.168.1.2",
                    "dst_ip": "192.168.1.3",
                    "rate": "10",
                    "pkt_length": {
                        "40": "0.7",
                        "200": "0.9",
                        "500": "1"
                    }, 
                    "duration": "40",
                    "on_k": "2",
                    "on_min": "1",
                    "off_k": "2",
                    "off_min": "2"
                },
                {
                    "src": "h2",
                    "dst": "h4",
                    "src_ip": "192.168.1.3",
                    "dst_ip": "192.168.1.5",
                    "rate": "20",
                    "pkt_length": {
                        "50": "0.7",
                        "300": "0.9",
                        "500": "1"
                    }, 
                    "duration": "60",
                    "on_k": "2",
                    "on_min": "1",
                    "off_k": "2",
                    "off_min": "2"
                },
                {
                    "src": "h3",
                    "dst": "h1",
                    "src_ip": "192.168.1.4",
                    "dst_ip": "192.168.1.2",
                    "rate": "20",
                    "pkt_length": {
                        "40": "0.7",
                        "200": "0.9",
                        "500": "1"
                    },
                    "duration": "30",
                    "on_k": "2",
                    "on_min": "1",
                    "off_k": "2",
                    "off_min": "2"
                },
            ],
            "pkt_gen1": [
                {
                    "src": "h1",
                    "dst": "h3",
                    "src_ip": "192.168.1.2",
                    "dst_ip": "192.168.1.4",
                    "rate": "10",
                    "duration": "30", 
                    "pkt_length": "1000", 
                    "dist": "normal", 
                    "normal_scale": "0.1", 
                    "ip_tos": "0", 
                    "ip_ttl": "64", 
                    "ip_id": "1", 
                    "proto": "tcp",
                    "tcp_header": {
                        "sport": "10000",
                        "dport": "10000",
                        "tcp_window": "1000"
                    },
                    "udp_header": {
                        "sport": "",
                        "dport": ""
                    }
                }
            ]
        }
    }
}


'''


class TrafficRedisAPI(MethodView):
    '''
    存储流量服务信息到redis数据库
    
    POST    /re/project/{project_name}/traffic_app/
    DELETE  /re/project/{project_name}/traffic_app/
            /re/project/{project_name}/traffic_app/{app_name}
    PUT     /re/project/{project_name}/traffic_app/{app_name}
    GET     /re/project/{project_name}/traffic_app/
            /re/project/{project_name}/traffic_app/{app_name}
    '''
    def post(self, project_name):
        data = json.loads(request.get_data(as_text=True))
        user = data["user"]
        traffic_info = data['traffics']
        return create_all_traffic(user, project_name, traffic_info)

    def delete(self, project_name, app_name=None):
        data = json.loads(request.get_data(as_text=True))
        user = data['user']
        if app_name is None:
            return delete_all_traffic(user, project_name)
        else:
            return delete_traffic_app(user, project_name, app_name)
    
    def get(self, project_name, app_name=None):
        data = json.loads(json.dumps(request.args))
        user = data['user']
        if app_name is None:
            return retrieve_all_traffic(user, project_name)
        else:
            return retrieve_traffic_app(user, project_name, app_name)
    
    def put(self, project_name, app_name=None):
        data = json.loads(request.get_data(as_text=True))
        user = data['user']
        traffic_info = data['traffics']
        if app_name is None:
            return {'code': 0, 'msg': '更新流量服务时缺少应用名！'}
        else:
            return update_traffic_app(user, project_name, app_name, traffic_info)
        

class TrafficTemplateAPI(MethodView):
    """
    POST    /re/project/{project_name}/traffic_templates/
    PUT     /re/project/{project_name}/traffic_templates/<template_name>
    GET     /re/project/{project_name}/traffic_templates/<template_name>
    DELETE  /re/project/{project_name}/traffic_templates/<template_name>

    用于流量模板的上传、修改、查询和删除功能：
    模板数据结构:
    {
        "template_name": "",    # 模板名称
        "traffic_gen": {...},   # traffic_gen 模板配置
        "pkt_gen1": {...},      # pkt_gen1 模板配置
        "pkt_gen2": {...}       # pkt_gen2 模板配置
    }
    """
    
    def post(self, project_name):
        data = json.loads(request.get_data(as_text=True))
        user = data["user"]
        traffic_info = data['traffics']
        return create_traffic_template(user, project_name, traffic_info)

    def delete(self, project_name, app_name=None):
        data = json.loads(request.get_data(as_text=True))
        user = data['user']
        if app_name is None:
            return delete_all_template(user, project_name)
        else:
            return delete_traffic_template(user, project_name, app_name)