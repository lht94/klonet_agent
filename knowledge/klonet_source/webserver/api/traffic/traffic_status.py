import traceback
from flask.views import MethodView
from flask import request
from ....Function_layer.deployed_proj_manager import is_traffic_running

class TrafficStatusAPI(MethodView):
    '''
    获取某个流量服务的运行状态

    GET re/project/<project_name>/traffic/<traffic_name>/status/
    '''
    def get(self, project_name, traffic_name):
        data = request.args
        try:
            is_running = is_traffic_running(data["user"], project_name, 
                traffic_name)
            if is_running:
                return {
                    "code": 1,
                    "msg": "success",
                    "status": "running"
                }
            else:
                return {
                    "code": 1,
                    "msg": "success",
                    "status": "stopped"
                }
        except Exception as e:
            traceback.print_exc()
            return {
                "code": 0,
                "msg": str(e),
                "status": None
            }