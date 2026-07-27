import json
from flask.views import MethodView
from flask import request
from flask_login import login_required
from ....Service_layer.permission_manager import check_user_exist, compare_role
from ...web_back.user_manager import UserManager

class SetPwdAPI(MethodView):
    '''
    /master/setpwd/
    POST  高权限用户重置低权限用户密码
    '''

    def post(self):
        '''
        data = {
            "user":高权限用户名,
            "set_user":被重置密码用户的用户名,
            "password":充值后的密码
        }
        '''
        data = json.loads(request.get_data(as_text=True))
        password = data["password"]
        user = data["user"]
        set_user=data["set_user"]
        if not check_user_exist(set_user):
            return {"code": 0, "msg": '您要重置的用户不存在！'}
        if not compare_role(user, set_user):
            return {"code": 0, "msg": '您的权限过低，无法重置该用户密码！'}
        user_manager = UserManager()

        modify_result = user_manager.super_setpwd(set_user, 
            password)
        if modify_result['code'] == 1:
            resp = {'code': 1, 'msg': '重置密码成功！'}
        elif modify_result['code'] == 0:
            resp = {"code": 0, "msg": f"重置密码失败！{modify_result['msg']}"}
        else:
            resp = {"code": 0, "msg": f"登录失败！"} 
        return resp