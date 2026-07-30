import json
import traceback
from flask.views import MethodView
from flask import request, current_app
from flask_login import login_required, current_user
from ...web_back.static_project import project_save_as, get_project, delete_project
from ....Service_layer.mysql_api.user_info import get_user_info_by_user_name

class StaticProjectAPI(MethodView):
    '''
    GET /my/project/{project_name}/
        获取项目的拓扑，流量，监控信息。

    DELETE /my/project/{project_name}/
        删除项目相关的数据表行及持久化文件
    '''
    # redis要调这个API的话就再写个视图函数
    # 因为需要本视图函数的认证功能，用户只读取自己名下的项目内容

    def get(self, project_name):
        try:
            if current_app.config.get('LOGIN_DISABLED'):
                data = request.args
                user = data['user']
                user_info = get_user_info_by_user_name(user)
                if user_info:
                    user_id = user_info.user_id
                else:
                    raise ValueError(f"用户 {user} 不存在！")
            else:
                user_id = current_user.user_id
            return get_project(user_id, project_name)
        except Exception as e:
            traceback.print_exc()
            return {"code":0, "msg":str(e), "project":{}}

  
    def delete(self, project_name):
        try:
            if current_app.config.get('LOGIN_DISABLED'):
                data = json.loads(request.get_data(as_text=True))
                user = data['user']
                user_info = get_user_info_by_user_name(user)
                if user_info:
                    user_id = user_info.user_id
                else:
                    raise ValueError(f"用户 {user} 不存在！")
            else:
                user_id = current_user.user_id
            return delete_project(user_id, project_name)
        except Exception as e:
            traceback.print_exc()
            return {"code":0, "msg":str(e)}
