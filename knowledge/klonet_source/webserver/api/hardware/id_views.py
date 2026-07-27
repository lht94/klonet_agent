from flask.views import MethodView
from flask import request
from ....vemu_config.config import PROJ_CONFIG



from ....Service_layer.redisAPI import HardwareRedis


IMAGE_REGISTRY_DIR = PROJ_CONFIG.kvm_image_registry_dir
WORKER_LIST = PROJ_CONFIG.worker_list

class GetIdAPI(MethodView):

    def get(self):
        id = request.args.get('id')
        type = request.args.get('type')
        redis = HardwareRedis()

        id_info = {}
        hardware_info = redis.get_hardware_in_type(type)
        info = hardware_info.get(id,'')
        id_info['IP'] = info['IP']
        id_info['user'] = info['user']
        id_info['password'] = info['password']
        id_info['id'] = id
        switch = redis.get_switch_in_id(id)
        print(switch)
        if len(switch) != 1:
            return{"code":0, "msg": "请求失败，请确定设备id信息是否正确"}
        else:
            id_info['switch'] = switch[0]['switch']
            id_info['vlan'] = switch[0]['vlan']
        return id_info

        