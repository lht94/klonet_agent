import traceback
from flask.views import MethodView
from flask import request
from ....Function_layer.deployed_proj_manager import is_monitor_running

class MonitorStatusAPI(MethodView):
    '''
    GET re/project/<project_name>/monitor/<monitor_name>/status/
        获取某个监控服务的运行状态
    '''
    def get(self, project_name, monitor_name):
        data = request.args
        try:
            is_running = is_monitor_running(data["user"], project_name, 
                monitor_name)
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