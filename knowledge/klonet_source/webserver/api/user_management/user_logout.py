from flask.views import MethodView
from ...web_back.user_manager import UserManager
from flask import current_app

from flask_login import login_required
from flask_login import current_user
from ...web_back.authority_management.authority_manager import permission_required

class UserLogoutAPI(MethodView):
    '''
    master/user_logout/
    POST  用户登出
    '''

    def delete(self):
        '''
        无需参数
        '''
        if current_app.config.get('LOGIN_DISABLED'):
            return {'code': 1, 'msg': '登出成功！'}
        user_manager = UserManager()
        if user_manager.logout():
            resp = {'code': 1, 'msg': '登出成功！'}
        else:
            resp = {'code': 0, 'msg': '登出失败.'}
        return resp

    @permission_required
    def post(self):
        print("in post")
        return {"msg":"in post"}