from flask.views import MethodView
from ....Service_layer.permission_manager import get_user_role_by_name, change_user_role
from flask_login import current_user, login_required
import json
from flask import request
from ....tools.user_tool import get_user_name


class ChangeUser(MethodView):
    """
    /master/perm/changeuser/
    """

    def post(self):
        """
        改变用户和管理员用户的角色
        {
            "user":"tbb",
            "change_name":"tb22",
            "role_id":1或2
        }
        """
        data = json.loads(request.get_data(as_text=True))
        name = get_user_name(data, "POST")
        if not get_user_role_by_name(name) == 3:
            return {"code": 0, "msg": "Current user does not have permission!"}
        if get_user_role_by_name(data["change_name"]) == data["role_id"]:
            return {
                "code": 0,
                "msg": "This user is already a proper role!"
            }
        if change_user_role(data["change_name"], data["role_id"]):
            return {
                "code": 1,
                "msg": "ok!"
            }
        return {
            "code": 0,
            "msg": "failed!"
        }