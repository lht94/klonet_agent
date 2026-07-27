import json
from flask_login import login_required
from flask.views import MethodView
from flask import request
from ...web_back.user_manager import UserManager
from ....tools.log_tools import UserLogLevel, UserLogger
class ModifyPasswordAPI(MethodView):
    
    '''
    /master/modify_password/
    PUT  修改密码
    '''
    @login_required
    def put(self):
        '''
        data = {
            "name":用户名,
            "old_password": 旧密码,
            "new_password": 新密码
        }
        '''
        data = json.loads(request.get_data(as_text=True))

        user_manager = UserManager()

        modify_result = user_manager.modify_password(data["name"], 
            data["old_password"], data["new_password"])
        if modify_result['code'] == 1:
            resp = {'code': 1, 'msg': '修改密码成功！'}
            logger = UserLogger(data['name'], UserLogLevel.First)
            logger.log_to_mysql(f'用户修改密码')
        elif modify_result['code'] == 0:
            resp = {"code": 0, "msg": f"修改密码失败！{modify_result['msg']}"}
        else:
            resp = {"code": 0, "msg": f"登录失败！"} 

        return resp
