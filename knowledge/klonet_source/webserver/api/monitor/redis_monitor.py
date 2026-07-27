import json
import traceback
from flask import request
from flask.views import MethodView
from ....Function_layer.deployed_proj_manager import delete_monitor_event
from ....Function_layer.deployed_proj_manager import retrieve_monitor_event
from ....Function_layer.deployed_proj_manager import update_monitor_event
from ....Function_layer.deployed_proj_manager import create_monitor_events
from ....Function_layer.deployed_proj_manager import retrieve_monitor_events


class redis_monitor_info(MethodView):
    '''
        POST    /re/project/<project_name>/monitor/
        DELETE  /re/project/<project_name>/monitor/<monitor_name>/
                /re/project/<project_name>/monitor/
        PUT     /re/project/<project_name>/monitor/<monitor_name>/
        GET     /re/project/<project_name>/monitor/<monitor_name>/
                /re/project/<project_name>/monitor/
    '''

    def post(self, project_name):
        try:
            data = json.loads(request.get_data(as_text=True))
            user = data['user']
            monitor_events_dict = data['monitors']
            return create_monitor_events(user, project_name, monitor_events_dict)
        except Exception as e:
            traceback.print_exc()
            return {'code': 0, 'msg': str(e)}

    def delete(self, project_name, monitor_name=None):
        try:
            data = json.loads(request.get_data(as_text=True))
            user = data['user']
            if monitor_name:
                return delete_monitor_event(user, project_name, monitor_name)
            else:
                return delete_monitor_event(user, project_name)
        except Exception as e:
            traceback.print_exc()
            return {'code': 0, 'msg': str(e)}

    def get(self, project_name, monitor_name=None):
        try:
            data = request.args
            user = data['user']
            if monitor_name:
                return retrieve_monitor_event(user, project_name, monitor_name)
            else:
                return retrieve_monitor_events(user, project_name)
        except Exception as e:
            traceback.print_exc()
            return {'code': 0, 'msg': str(e)}

    def put(self, project_name, monitor_name=None):
        try:
            data = json.loads(request.get_data(as_text=True))
            user = data['user']
            if monitor_name:
                expr = monitor_name
                monitor_info = data['monitors'][expr]
                return update_monitor_event(user, project_name, monitor_name,
                    monitor_info)
            else:
                return {'code': 0, 'msg': '更新监控服务时缺少监控服务名！'}
        except Exception as e:
            traceback.print_exc()
            return {'code': 0, 'msg': str(e)}
