from flask.views import MethodView
from ....Service_layer.permission_manager import get_all_user, get_user_role_by_name
from flask_login import login_required
from ....tools.user_tool import get_user_name
from flask import request
import json


class ImageGetAllUser(MethodView):
    """
    /master/perm/imageuser/
    """

    def get(self):
        """
        GET 得到平台所有普通用户和管理员列表，用户管理员和超级管理员
        /master/perm/imageuser/?user=tbb

        Return: {
            "code": 1, 
            "msg": "Permissions checking pass!", 
            "user_list": [{"ifadmin": 1, "name": "nyx", "user_id": 8}, ]
            }
        """
        data = request.args.get("user")
        name = get_user_name(data, "GET")
        # 如果是管理员或超级管理员
        if get_user_role_by_name(name) in [2, 3]:  
            list_user = get_all_user()
            tmp = []
            for item in list_user:
                tmp.append({"user_id": item[0], "name": item[1], "ifadmin": item[2]})
            return {
                "code": 1,
                "msg": "Permissions checking pass!",
                "user_list": tmp  # 返回所有注册的用户user_id和name
            }
        return {
            "code": 0,
            "msg": "Current user doesn't have permissions to do this!"
        }