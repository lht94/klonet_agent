import json
import traceback
from flask.views import MethodView
from flask import request
import os
from ....Service_layer.mysql_api.user_login import get_user_id_by_user_name,get_user_name_by_user_id
from ....Service_layer.mysql_api.image import get_image_by_user_id, get_image_by_is_public, get_image_cpu_and_memory
from ....Function_layer.deployed_proj_manager import retrieve_topo
from ....vemu_config.config import  PROJ_CONFIG
from ....tools.context import redis_context
from flask_login import login_required


class ImageAPI(MethodView):

    def get(self):
        try:
            user = request.args.get('username')

            # mysql根据用户名聚合该用户的私有镜像json
            image_list_privite={'controllers':[],'switches':[],'routers':[],'hosts':[]}
            
            userid = get_user_id_by_user_name(user)
        
            user_privite_image=get_image_by_user_id(userid)
    
            #私有镜像json
            for privite_image in user_privite_image:
                image_full_name = (f"{PROJ_CONFIG.image_registry_ip}:{PROJ_CONFIG.image_registry_port}"
                                f"/{user}/{privite_image.image_name}:{privite_image.tag}")
                cpu, mem = get_image_cpu_and_memory(image_full_name)
                
                if privite_image.is_public == False:
                    if privite_image.type == "host":
                        image_list_privite["hosts"].append({"image_name":image_full_name,
                                                            "type":privite_image.type,
                                                            "service": "docker",
                                                            "portname": None,
                                                            "subtype":privite_image.subtype,
                                                            "interfaces": [],
                                                            "gateway": "",
                                                            "config": {},
                                                            "linestyle":"solid", 
                                                            "resource_limit":{'cpu':cpu, 'mem':mem}})
                    if privite_image.type == "router":
                        image_list_privite["routers"].append({"image_name":image_full_name,
                                                            "type":privite_image.type, 
                                                            "service": "docker",
                                                            "portname": None,
                                                            "subtype":privite_image.subtype,
                                                            "interfaces": [],
                                                            "gateway": "", 
                                                            "resource_limit":{'cpu':cpu, 'mem':mem}, 
                                                            "linestyle":"solid",
                                                            "config": {
                                                                    "rip": {
                                                                        "networks": [],
                                                                        "neighbors": [],
                                                                        "version": 2,
                                                                        "enable": 0
                                                                    },
                                                                    "ospf": {
                                                                        "router_id": "",
                                                                        "networks": [],
                                                                        "areas": {},
                                                                        "enable": 0
                                                                    },
                                                                    "bgp": {
                                                                        "asn": "",
                                                                        "router_id": "",
                                                                        "networks": [],
                                                                        "neighbors": [],
                                                                        "enable": 0
                                                                    }
                                                                }})
                    if privite_image.type == "switch":
                        image_list_privite["switches"].append({"image_name":image_full_name,
                                                            "resource_limit":{'cpu':cpu, 'mem':mem}, 
                                                            "type":privite_image.type,
                                                            "service": "docker",
                                                            "portname": None,
                                                            "subtype":privite_image.subtype,
                                                            "linestyle":"solid",
                                                            "config": {
                                                                    "stp": True,
                                                                    "controllers": []
                                                                }})
                    if privite_image.type == "controller":
                        image_list_privite["controllers"].append({"image_name":image_full_name,
                                                                "resource_limit":{'cpu':cpu, 'mem':mem}, 
                                                                "type":privite_image.type,
                                                                "service": "docker",
                                                                "portname": None,
                                                                "subtype":privite_image.subtype,
                                                                "linestyle":"solid",
                                                                "config": {
                                                                    "port": 6653
                                                                }})
            


            #mysql根据is_pubilc聚合公有镜像json
            root = os.getcwd()
            with open(f'{root}/vemu_uestc/webserver/api/image/image_list.json', "r") as f:
                info = json.loads(f.read())

            public_image=get_image_by_is_public(is_public=1)
            #公有镜像
            for image in public_image:
                user_name=get_user_name_by_user_id(image.user_id)
                public_image_full_name = (f"{PROJ_CONFIG.image_registry_ip}:{PROJ_CONFIG.image_registry_port}"
                                        f"/{user_name}/{image.image_name}:{image.tag}")
                cpu, mem = get_image_cpu_and_memory(public_image_full_name)
                if image.type == "host":
                        info["hosts"].append({"image_name":public_image_full_name, 
                                            "resource_limit":{'cpu':cpu, 'mem':mem}, 
                                            "type":image.type,
                                            "service": "docker",
                                            "portname": None,
                                            "subtype":image.subtype,
                                            "interfaces": [],
                                            "gateway": "",
                                            "config": {},
                                            "linestyle":"solid"})
                if image.type == "router":
                        info["routers"].append({"image_name":public_image_full_name, 
                                                "resource_limit":{'cpu':cpu, 'mem':mem}, 
                                                "type":image.type,
                                                "service": "docker",
                                                "portname": None,
                                                "subtype":image.subtype,
                                                "interfaces": [],
                                                "gateway": "",
                                                "linestyle":"solid",
                                                "config": {
                                                    "rip": {
                                                        "networks": [],
                                                        "neighbors": [],
                                                        "version": 2,
                                                        "enable": 0
                                                    },
                                                    "ospf": {
                                                        "router_id": "",
                                                        "networks": [],
                                                        "areas": {},
                                                        "enable": 0
                                                    },
                                                    "bgp": {
                                                        "asn": "",
                                                        "router_id": "",
                                                        "networks": [],
                                                        "neighbors": [],
                                                        "enable": 0
                                                    }
                                                }})
                if image.type == "switch":
                        info["switches"].append({"image_name":public_image_full_name, 
                                                "resource_limit":{'cpu':cpu, 'mem':mem}, 
                                                "type":image.type,
                                                "service": "docker",
                                                "portname": None,
                                                "subtype":image.subtype,
                                                "linestyle":"solid",
                                                "config": {
                                                    "stp": True,
                                                    "controllers": []
                                                }})
                if image.type == "controller":
                        info["controllers"].append({"image_name":public_image_full_name, 
                                                    "resource_limit":{'cpu':cpu, 'mem':mem}, 
                                                    "type":image.type,
                                                    "service": "docker",
                                                    "portname": None,
                                                    "subtype":image.subtype,
                                                    "linestyle":"solid",
                                                    "config": {
                                                        "port": 6653
                                                    }})    


            image_list = {'public': info, 'private': image_list_privite}
            return image_list
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}

    def post(self):
        pass

    def delete(self):
        pass

    def put(self):
        pass


class EditAPI(MethodView):


    def get(self):
        user = request.args.get('username')

        ## mysql根据用户名聚合该用户的私有镜像edit_json
        edit_list_privite={'controllers':[],'switches':[],'routers':[],'hosts':[]}
        
        userid = get_user_id_by_user_name(user)
      
        user_privite_image=get_image_by_user_id(userid)


        for privite_image in user_privite_image:
             #私有镜像
            if privite_image.is_public == False:
                if privite_image.type == "host":
                    edit_list_privite["hosts"].append({
                "interface": [
                    {
                        "config_name": "name",
                        "config_description": "接口",
                        "value_method": "disabled",
                        "default_value": None
                    },
                    {
                        "config_name": "nic",
                        "config_description": "网卡",
                        "value_method": "disabled",
                        "default_value": None
                    },
                    {
                        "config_name": "ip",
                        "config_description": "IP",
                        "value_method": "input",
                        "default_value": None
                    },
                    {
                        "config_name": "netmask",
                        "config_description": "网络掩码",
                        "value_method": "input",
                        "default_value": None
                    }
                ],
                "gateway": {
                        "config_name": "gateway",
                        "config_description": "网关",
                        "value_method": "input",
                        "default_value": None
                    },
                "resource_limit": [
				{
	    			"config_name": "cpu",
		            "config_description": "cpu限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: %, 范围: 0 < cpu <= 1000"
	    		},
				{
	    			"config_name": "mem",
		            "config_description": "mem限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: Mbytes, 范围: 10 < mem <= 1000"
	    		}
			],
            })
                if privite_image.type == "router":
                    edit_list_privite["routers"].append({
                "interface": [
                    {
                        "config_name": "ip",
                        "config_description": "IP",
                        "value_method": "input",
                        "default_value": None
                    },
                    {
                        "config_name": "netmask",
                        "config_description": "网络掩码",
                        "value_method": "input",
                        "default_value": None
                    }
                ],
                "resource_limit": [
				{
	    			"config_name": "cpu",
		            "config_description": "cpu限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: %, 范围: 0 < cpu <= 1000"
	    		},
				{
	    			"config_name": "mem",
		            "config_description": "mem限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: Mbytes, 范围: 10 < mem <= 1000"
	    		}
			],
                "rip": [
                    {
                        "config_name": "enable",
                        "config_description": "是否启用",
                        "value_method": "boolean",
                        "default_value": False,
                        "value_list": [True,False]
                    },
                    {
                        "config_name": "networks",
                        "config_description": "网络段配置",
                        "value_method": "multi",
                        "default_value": []
                    },
                    {
                        "config_name":  "neighbors",
                        "config_description":  "网络邻域配置",
                        "value_method":  "multi",
                        "default_value": []
                    }
                ],
                "ospf": [
                    {
                        "config_name": "enable",
                        "config_description": "是否启用",
                        "value_method": "boolean",
                        "default_value": False,
                        "value_list": [False,True]
                    },
                    {
                        "config_name": "router_id",
                        "config_description": "路由器ID",
                        "value_method": "input",
                        "default_value": None
                    },
                    {
                        "config_name": "networks",
                        "config_description": "网络段配置",
                        "value_method": "multi",
                        "default_value": []
                    },
                    {
                        "config_name": "areas",
                        "config_description": "区域信息配置",
                        "config_details": [
                            {
                                "config_name": "<area_id>",
                                "config_description": "区域信息ID",
                                "value_method": "multi",
                                "default_value": []
                            }
                        ]
                    }
                ],
                "bgp": [
                    {
                        "config_name": "enable",
                        "config_description": "是否启用",
                        "value_method": "boolean",
                        "default_value": False,
                        "value_list":[False,True]
                    },
                    {
                        "config_name": "asn",
                        "config_description": "ASN编号",
                        "value_method": "input",
                        "default_value": None
                    },
                    {
                        "config_name": "router_id",
                        "config_description": "路由器id",
                        "value_method": "input",
                        "default_value": None
                    },
                    {
                        "config_name": "networks",
                        "config_description": "网络段配置",
                        "value_method": "multi",
                        "default_value": []
                    },
                    {
                        "config_name": "neighbors",
                        "config_description": "邻域配置",
                        "value_method": "multi",
                        "default_value": []
                    }
                ]
            })
                if privite_image.type == "switch":
                    edit_list_privite["switches"].append({
                "stp": {
                        "config_name": "stp",
                        "config_description": "是否启用stp",
                        "value_method": "boolean",
                        "value_list": [True,False],
                        "default_value": True
                    },
                "controllers": {
                        "config_name": "controllers",
                        "config_description": "OVS控制器",
                        "value_method": "multi",
                        "default_value": []
                    },
                "resource_limit": [
				{
	    			"config_name": "cpu",
		            "config_description": "cpu限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: %, 范围: 0 < cpu <= 1000"
	    		},
				{
	    			"config_name": "mem",
		            "config_description": "mem限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: Mbytes, 范围: 10 < mem <= 1000"
	    		}
			],
                "interface": [
                    {
                        "config_name": "name",
                        "config_description": "接口",
                        "value_method": "disabled",
                        "default_value": None
                    },
                    {
                        "config_name": "nic",
                        "config_description": "网卡",
                        "value_method": "disabled",
                        "default_value": None
                    }
                ],
            })
                if privite_image.type == "controller":
                    edit_list_privite["controllers"].append({
                "port": {
                        "config_name": "port",
                        "config_description": "控制器端口",
                        "value_method": "disabled",
                        "default_value": 6653
                    },
                "resource_limit": [
				{
	    			"config_name": "cpu",
		            "config_description": "cpu限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: %, 范围: 0 < cpu <= 1000"
	    		},
				{
	    			"config_name": "mem",
		            "config_description": "mem限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: Mbytes, 范围: 10 < mem <= 1000"
	    		}
			],
            })


        #mysql根据is_pubilc聚合公有镜像edit_json
        root = os.getcwd()
        with open(f'{root}/vemu_uestc/webserver/api/image/edit_list.json', "r") as f:
            info = json.loads(f.read())

        public_image=get_image_by_is_public(is_public=1)
        #公有镜像
        for image in public_image:
            if image.is_public == True:
                if image.type == "host":
                        info["hosts"].append({
                    "interface": [
                        {
                            "config_name": "name",
                            "config_description": "接口",
                            "value_method": "disabled",
                            "default_value": None
                        },
                        {
                            "config_name": "nic",
                            "config_description": "网卡",
                            "value_method": "disabled",
                            "default_value": None
                        },
                        {
                            "config_name": "ip",
                            "config_description": "IP",
                            "value_method": "input",
                            "default_value": None
                        },
                        {
                            "config_name": "netmask",
                            "config_description": "网络掩码",
                            "value_method": "input",
                            "default_value": None
                        }
                    ],
                    "gateway": {
                            "config_name": "gateway",
                            "config_description": "网关",
                            "value_method": "input",
                            "default_value": None
                        },
                    "resource_limit": [
				{
	    			"config_name": "cpu",
		            "config_description": "cpu限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: %, 范围: 0 < cpu <= 1000"
	    		},
				{
	    			"config_name": "mem",
		            "config_description": "mem限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: Mbytes, 范围: 10 < mem <= 1000"
	    		}
			],
                })
                if image.type == "router":
                        info["routers"].append({
                    "interface": [
                        {
                            "config_name": "ip",
                            "config_description": "IP",
                            "value_method": "input",
                            "default_value": None
                        },
                        {
                            "config_name": "netmask",
                            "config_description": "网络掩码",
                            "value_method": "input",
                            "default_value": None
                        }
                    ],
                    "resource_limit": [
				{
	    			"config_name": "cpu",
		            "config_description": "cpu限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: %, 范围: 0 < cpu <= 1000"
	    		},
				{
	    			"config_name": "mem",
		            "config_description": "mem限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: Mbytes, 范围: 10 < mem <= 1000"
	    		}
			],
                    "rip": [
                        {
                            "config_name": "enable",
                            "config_description": "是否启用",
                            "value_method": "boolean",
                            "default_value": False,
                            "value_list": [True,False]
                        },
                        {
                            "config_name": "networks",
                            "config_description": "网络段配置",
                            "value_method": "multi",
                            "default_value": []
                        },
                        {
                            "config_name":  "neighbors",
                            "config_description":  "网络邻域配置",
                            "value_method":  "multi",
                            "default_value": []
                        }
                    ],
                    "ospf": [
                        {
                            "config_name": "enable",
                            "config_description": "是否启用",
                            "value_method": "boolean",
                            "default_value": False,
                            "value_list": [False,True]
                        },
                        {
                            "config_name": "router_id",
                            "config_description": "路由器ID",
                            "value_method": "input",
                            "default_value": None
                        },
                        {
                            "config_name": "networks",
                            "config_description": "网络段配置",
                            "value_method": "multi",
                            "default_value": []
                        },
                        {
                            "config_name": "areas",
                            "config_description": "区域信息配置",
                            "config_details": [
                                {
                                    "config_name": "<area_id>",
                                    "config_description": "区域信息ID",
                                    "value_method": "multi",
                                    "default_value": []
                                }
                            ]
                        }
                    ],
                    "bgp": [
                        {
                            "config_name": "enable",
                            "config_description": "是否启用",
                            "value_method": "boolean",
                            "default_value": False,
                            "value_list":[False,True]
                        },
                        {
                            "config_name": "asn",
                            "config_description": "ASN编号",
                            "value_method": "input",
                            "default_value": None
                        },
                        {
                            "config_name": "router_id",
                            "config_description": "路由器id",
                            "value_method": "input",
                            "default_value": None
                        },
                        {
                            "config_name": "networks",
                            "config_description": "网络段配置",
                            "value_method": "multi",
                            "default_value": []
                        },
                        {
                            "config_name": "neighbors",
                            "config_description": "邻域配置",
                            "value_method": "multi",
                            "default_value": []
                        }
                    ]
                })
                if image.type == "switch":
                    info["switches"].append({
                    "stp": {
                            "config_name": "stp",
                            "config_description": "是否启用stp",
                            "value_method": "boolean",
                            "value_list": [True,False],
                            "default_value": True
                        },
                    "controllers": {
                            "config_name": "controllers",
                            "config_description": "OVS控制器",
                            "value_method": "multi",
                            "default_value": []
                        },
                    "resource_limit": [
				{
	    			"config_name": "cpu",
		            "config_description": "cpu限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: %, 范围: 0 < cpu <= 1000"
	    		},
				{
	    			"config_name": "mem",
		            "config_description": "mem限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: Mbytes, 范围: 10 < mem <= 1000"
	    		}
			],
                    "interface": [
                {
                    "config_name": "name",
                    "config_description": "接口",
                    "value_method": "disabled",
                    "default_value": None
                },
                {
                    "config_name": "nic",
                    "config_description": "网卡",
                    "value_method": "disabled",
                    "default_value": None
                }
            ],
                })
                if image.type == "controller":
                    info["controllers"].append({
                    "port": {
                            "config_name": "port",
                            "config_description": "控制器端口",
                            "value_method": "disabled",
                            "default_value": 6653
                        },
                    "resource_limit": [
				{
	    			"config_name": "cpu",
		            "config_description": "cpu限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: %, 范围: 0 < cpu <= 1000"
	    		},
				{
	    			"config_name": "mem",
		            "config_description": "mem限制",
		            "value_method": "input",
		            "default_value": None,
					"reminder_text": "单位: Mbytes, 范围: 10 < mem <= 1000"
	    		}
			],
                })

        # 整合数据库的链路信息
        nic_data = {}                                  # 网卡、链路信息，用于返回
        if 'toponame' in request.args:                 # 若前端请求里包含topo名，才进行链路信息整合
            topo = request.args.get('toponame')
            with redis_context(user) as user_db_cli:
                if retrieve_topo(user, topo)['code']:  # 若拓扑已经部署
                    links = user_db_cli.get_value('plane_topo_list', topo)['links']  # 从数据库读出链路列表 
                    nodes = user_db_cli.get_value('plane_topo_list', topo)['NEs']    # 从数据库读出节点列表
                    for node in nodes:
                        nic_data[node] = {}
                        for link in links:
                            node_table = topo + "_" + node
                            if user_db_cli.check_exist(node_table, 'link_' + link):             # 对某节点，查看链路是否相关
                                port_data = user_db_cli.get_value(node_table, 'link_' + link)   # 获得链路和节点相交端口信息
                                nic_data[node][port_data['name']] = port_data['nic']            # 在node下建立端口的别名到真名的映射
        
        edit_list = {'public': info, 'private': edit_list_privite, 'static': nic_data}
        return edit_list
