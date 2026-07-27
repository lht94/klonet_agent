from flask.views import MethodView
from ....Service_layer.permission_manager import check_permission, get_all_user
from flask_login import login_required
from ....tools.user_tool import get_user_name
from flask import request


class PermissionCheck(MethodView):
    """
    /master/perm/check/
    """

    def get(self):
        """
        GET 用于用户管理
        /master/perm/check/?user=tbb
        """
        data = request.args.get("user")
        name = get_user_name(data, "GET")
        if check_permission(name, "SuperDelete.post"):  
            ret_list = get_all_user()
            user_list = []
            for item in ret_list:
                tmp_dict = {}
                tmp_dict["ifadmin"] = item[2]
                tmp_dict["name"] = item[1]
                tmp_dict["user_id"] = item[0]
                user_list.append(tmp_dict)
            return {
                "code": 1,
                "msg": "Permissions checking pass!",
                "user_list": user_list  # 返回所有注册的用户user_id和name
            }
        return {
            "code": 0,
            "msg": "Current user doesn't have permissions to do this!"
        }