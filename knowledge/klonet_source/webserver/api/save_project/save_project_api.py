import json
import traceback
from flask.views import MethodView
from flask import request, current_app
from flask_login import login_required, current_user
from ...web_back.static_project import project_save_as
from ....Service_layer.mysql_api.user_info import get_user_info_by_user_name

class SaveProjectAPI(MethodView):
    '''
    POST /my/project/
        将项目包含的拓扑信息，流量信息，监控信息做持久化存储。
    '''
    # redis要调这个API的话就再写个视图函数
    # 因为需要本视图函数的认证功能，用户只读取自己名下的项目内容

    def post(self):
        data = json.loads(request.get_data(as_text=True))
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
            return(project_save_as(user_id, 
                data["static_project_name"], data["deployed_project_name"]))
        except Exception as e:
            traceback.print_exc()
            return {"code":0, "msg":str(e)}