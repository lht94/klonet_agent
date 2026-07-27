from flask.views import MethodView
from flask import request
from ....vemu_config.config import PROJ_CONFIG
import json

from ....Service_layer.redisAPI import HardwareRedis
from ....tools.generate_ne_id import SnowFlake

IMAGE_REGISTRY_DIR = PROJ_CONFIG.kvm_image_registry_dir
WORKER_LIST = PROJ_CONFIG.worker_list

class HardwareUploadAPI(MethodView):
    '''
    POST /master/hardware/upload/  备案硬件设备
    
    参数为:
    "user": "", 用户名
    "type": "", 硬件类型
    "device_name":"", 设备的名字（用户可以命名）
    "IP":"",管控ip
    "switch:"", 接到了哪个交换机上（交换机的管控ip）
    "switch_port":{'port1':'vlan1', 'port2':'vlan2', ......}, 接到了交换机的哪个端口上，对应的vlan设置
    "hardware_port":[] ，真实设备的哪些端口与交换机相连,与switch_port对应
    "name":设备的用户名
    "password":设备的管控密码
    '''
    def post(self):
        snow = SnowFlake()
        data = json.loads(request.get_data(as_text=True))
        user = data["user"]
        type = data["type"]
        device = data["device_name"]
        IP = data["IP"]
        switch = data["switch"]
        switch_port = data["switch_port"]
        hardware_port = data["hardware_port"]
        name = data["name"]
        password = data["password"]
        id = snow.get_id()

        redis = HardwareRedis()

        for index, (key, value) in enumerate(switch_port.items()):
            switch_info = redis.get_hardware_in_switch(switch)
            if switch_info == None:
                return {"code": 0, "msg": "识别交换机IP失败，请确定交换机已进行备案"}
            if key in switch_info.keys():
                return {"code": 0, "msg": "备案失败，交换机端口重复，请确定端口输入是否正确或者该硬件已被备案"}
            eth_info = {}
            eth_info['vlan'] = value
            eth_info['id'] = id
            eth_info['nic_index'] = hardware_port[index]
            switch_info[key] = eth_info
            redis.add_hardware_to_switch(switch, switch_info)

        hardware_info = redis.get_hardware_in_type(type)
        if hardware_info ==None:
            return {"code": 0, "msg": "识别硬件类型失败"}
        id_info = {}
        id_info['name'] = device
        id_info['IP'] = IP
        id_info['user'] = name
        id_info['password'] = password
        id_info['state'] = f'{user}_0'
        hardware_info[str(id)] = id_info
        redis.add_hardware_to_type(type, hardware_info)
        return {"code": 1, "msg": "设备备案成功!"}

                       