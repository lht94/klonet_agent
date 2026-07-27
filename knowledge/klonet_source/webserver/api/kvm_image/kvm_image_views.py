from flask.views import MethodView
from flask import request
from ....Service_layer.mysql_api.user_login import get_user_id_by_user_name
from ....Service_layer.mysql_api.kvm_image import get_all_kvm_image_by_user_id
import json
import os
import traceback
class KVMImageAPI(MethodView):
    '''
    GET /my/kvm_image/  获取用户可用的镜像信息
    
    参数为：
    "user": "", # 用户名
    '''
    
    def get(self):
        try:
            user = request.args.get('user')
            user_id = get_user_id_by_user_name(user)
            # 镜像实例列表
            image_obj_list = get_all_kvm_image_by_user_id(user_id)
            
            # 将model实例处理成json
            image_list = {"routers":[], "hosts":[], "switches":[]}
            
            # 合并平台提供的默认镜像信息
            root = os.getcwd()
            with open(f'{root}/vemu_uestc/webserver/api/kvm_image/kvm_image_list.json', "r") as f:
                image_list.update(json.loads(f.read()))
            
            for image in image_obj_list:
                # 对数据库中的路径进行转化
                # 数据库中的所有镜像均是用户自己上传镜像
                # path=default表示是用户web端上传的镜像，不是平台默认镜像
                if image.path == "default":
                    image_path = f"self_upload_image:{image.image_name}"
                else:
                    image_path = image.path
                if image.type == "router":
                    image_list["routers"].append({
                        "image_name": image.image_name,
                        "type": image.type,
                        "subtype": image.image_name.split(".")[0], # 暂时复用image_name（文件名）的前缀
                        "service": "kvm",
                        "gateway": "",
                        "interfaces":[],
                        "intname":{},
                        "nic":[],
                        "portname": [
                            "nic_1",
                            "nic_2",
                            "nic_3",
                            "nic_4",
                            "nic_5",
                            "nic_6",
                            "nic_7",
                            "nic_8"
                        ],
                        "resource_limit": {
                        "cpu": image.cpu,
                        "mem": image.memory_requirements,
                        },
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
                        },
                        "linestyle":"solid",
                        "vm_config": {
                            "kvm_image": {
                                "image_path": image_path,
                                "qcow2_size": -1
                            },
                            "image_name": image.image_name,     # vm_config里再次记录了image_name
                            "type": image.type,
                            "port_num": 8,
                            "kvm_config": {}
                        }
                    })
                # TODO：暂时各类型操作是一样的
                if image.type == "host":
                    image_list["hosts"].append({
                        "image_name": image.image_name,
                        "type": image.type,
                        "subtype": image.image_name,
                        "service": "kvm",
                        "gateway": "",
                        "interfaces":[],
                        "intname":{},
                        "nic":[],
                        "portname": [
                            "nic_1"    
                        ],
                        "resource_limit": {
                        "cpu": image.cpu,
                        "mem": image.memory_requirements,
                        },
                        "vm_config": {
                        "kvm_image": {
                            "image_path": image_path,
                            "qcow2_size": -1
                        },
                        "image_name": image.image_name,
                        "type": image.type,
                        "port_num": 1,
                        "kvm_config": {}
                        },
                        "linestyle":"solid",
                        "config": {}
                    })
                if image.type == "switch":
                    image_list["switches"].append({
                        "image_name": image.image_name,
                        "type": image.type,
                        "subtype": image.image_name,
                        "service": "kvm",
                        "gateway": "",
                        "interfaces":[],
                        "intname":{},
                        "nic":[],
                        "portname": [
                            "nic_1",
                            "nic_2",
                            "nic_3",
                            "nic_4",
                            "nic_5",
                            "nic_6",
                            "nic_7",
                            "nic_8"
                        ],
                        "resource_limit": {
                        "cpu": image.cpu,
                        "mem": image.memory_requirements,
                        },
                        "vm_config": {
                        "kvm_image": {
                            "image_path": image_path,
                            "qcow2_size": -1
                        },
                        "image_name": image.image_name,
                        "type": image.type,
                        "port_num": 8,
                        "kvm_config": {}
                        },
                        "linestyle":"solid",
                        "config": {}
                    })
            # print(image_list)
            # return image_list
            return {"public": image_list}   # 为了适配前端的取值格式，仿照docker的镜像操作
        
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}