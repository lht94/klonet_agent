import traceback
from flask.views import MethodView
from flask import request, current_app
from flask_login import login_required, current_user
from ....Function_layer.deployed_proj_manager import retrieve_project_json


class DeployedProjectAPI(MethodView):
    '''
    GET /re/project/{project_name}/
        获取已创建项目的拓扑，流量，监控信息。
    '''

    def get(self, project_name):
        try:
            if current_app.config.get('LOGIN_DISABLED'):
                data = request.args
                user_name = data['user']
            else:
                user_name = current_user.name
            return(retrieve_project_json(user_name, project_name))
        except Exception as e:
            traceback.print_exc()
            return {"code":0, "msg":str(e), "project":{}}