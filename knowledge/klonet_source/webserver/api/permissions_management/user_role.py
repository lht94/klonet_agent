from flask.views import MethodView
from ....Service_layer.permission_manager import get_user_role_by_name
from flask_login import login_required
import json
from flask import request
from ....tools.user_tool import get_user_name


class UserRole(MethodView):
    """
    /master/perm/role/
    """

    def get(self):
        """
        得到当前的用户角色
        /master/perm/role/?user=tbb
        """
        data = request.args.get("user")
        name = get_user_name(data, "GET")
        ret = get_user_role_by_name(name)
        if ret != None:
            return {"code": 1, "role": ret, "msg":"ok!"}
        return {
            "code": 0,
            "msg": "failed!"
        }