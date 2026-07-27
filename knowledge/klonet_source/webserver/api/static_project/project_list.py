import traceback
from flask.views import MethodView
from flask import request, current_app
from flask_login import login_required, current_user
from ...web_back.user_manager import UserManager
from ....Service_layer.mysql_api.static_project_my_api import get_project_list
from ....Service_layer.mysql_api.user_info import get_user_info_by_user_name

class StaticProjectListAPI(MethodView):
    '''
    GET /my/project_list/ 获取用户的项目列表
    '''

    def get(self):
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

            result = get_project_list(user_id)
            result = {
                "code":1,
                "project_list": result,
                "msg": "success"
            }
            return result
        except Exception as e:
            traceback.print_exc()
            return {"code":0, "project_list":[], "msg":str(e)}