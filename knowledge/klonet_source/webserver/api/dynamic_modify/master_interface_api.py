import json
from flask import request
from flask.views import MethodView
from ....Service_layer.redisAPI import UserMapRedis

from ....tools.log_tools import *

user_db_map = UserMapRedis()
class DynamicInterfaceAPI(MethodView):
    """
    动态命名虚机节点端口API
    纯数据库操作 不涉及worker
    """


    def post(self):
        """
        动态修改虚机节点端口命名
        """
        data = json.loads(request.get_data(as_text=True))
        try:
            db_cli = user_db_map.get_user_db(data['user'])
        except Exception as e:
            return {'code': 0, 'msg': e.args[0]}
        finally:
            user_db_map.close()
        
        try:
            topo, user, info = data['topo'], data['user'], data['info']
            # 检查端口是否存在
            # 取出端口名称 如eth1
            service = db_cli.get_value(f"{topo}_{info['ne']}", 'NEservice')
            if service != 'kvm':
                return {'code': 0, 'msg': f"节点{info['ne']}不是虚机节点，无法进行端口命名"}
            interface = info['interface']
            is_exist = db_cli.check_exist(f"{topo}_{info['ne']}_nodetointerface", interface)
            if not is_exist:
                return {'code': 0, 'msg': f"端口{interface}不存在，请重新请求"}
            else:
                # 修改端口名称
                br_name = db_cli.get_value(f"{topo}_{info['ne']}_nodetointerface", interface)
                db_cli.set_value(f"{topo}_{info['ne']}_interfacetoname", br_name, info['name'])
                return {'code': 1, 'msg': f"虚机{info['ne']}端口{interface}名称修改为{info['name']}"}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': f'端口名称修改失败，错误信息：{str(e)}'} 
    
    
    def delete(self):
        return {'code': 0, 'msg': 'method not allowed', 'status': 405}