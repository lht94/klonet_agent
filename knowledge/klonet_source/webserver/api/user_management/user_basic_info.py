import json
from flask.views import MethodView
from flask import request
from ...web_back.user_manager import UserManager

# 后续看能不能和其它用户管理功能的类写在一块
class UserBasicInfoAPI(MethodView):
    def get(self):
        basic_info = UserManager.get_basic_info()
        return basic_info




