import traceback
from flask.views import MethodView
from flask import request
from ....Function_layer.deployed_proj_manager import get_monitor_event_types

class MonitorEventTypesAPI(MethodView):
    '''
    GET re/project/<project_name>/monitor/<monitor_name>/types/
        获取某次监控服务的所有子事件监控的性能指标
    '''
    def get(self, project_name, monitor_name):
        data = request.args
        try:
            type2subevent = get_monitor_event_types(
                data["user"], project_name, monitor_name)
            return {
                "code": 1,
                "msg": "success",
                "type2subevent": type2subevent
            }
        except Exception as e:
            traceback.print_exc()
            return {
                "code": 0,
                "msg": str(e),
                "types": []
            }