from flask.views import MethodView
from flask import request
from ....vemu_config.config import PROJ_CONFIG



from ....Service_layer.redisAPI import HardwareRedis


IMAGE_REGISTRY_DIR = PROJ_CONFIG.kvm_image_registry_dir
WORKER_LIST = PROJ_CONFIG.worker_list

class GetHardwareAPI(MethodView):

    def get(self):
        user = request.args.get('user')
        type = request.args.get('type')
        redis = HardwareRedis()

        hardware_info = redis.get_hardware_in_type(type)
        i = 0
        results = {}
        for outer_key, inner_dict in hardware_info.items():
            info_dict = {}
            state = inner_dict.get("state", "")
        
            if state == "default_0" or state == f"{user}_0":
                i=i+1
                name = inner_dict.get("name", "")
                info_dict['id'] = outer_key
                info_dict['name'] = name
                results[str(i)] =info_dict
    
        return results


        