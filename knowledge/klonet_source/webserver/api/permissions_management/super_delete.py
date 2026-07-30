import json
from flask.views import MethodView
from flask import request
from ....Service_layer.permission_manager import super_delete
from flask_login import login_required
from ....tools.user_tool import get_user_name



class SuperDelete(MethodView):
    """
    /master/perm/superdelete/
    """

    def delete(self):
        """"
        用于超级管理员删除用户
        {"user":"tbb", "delete_name": 'tb22'}
        """
        data = json.loads(request.get_data(as_text=True))
        name = get_user_name(data, "POST")
        return super_delete(name, data["delete_name"])