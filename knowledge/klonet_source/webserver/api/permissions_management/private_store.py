from flask.views import MethodView
from ....Service_layer.permission_manager import get_private_image
from flask_login import login_required
import json
from flask import request
from ....tools.user_tool import get_user_name


class PrivateStore(MethodView):
    """
    /master/perm/privatestore/
    """

    def get(self):
        """
        返回私有镜像仓库列表
        /master/perm/privatestore/?user=msm123
        """
        data = request.args.get("user")
        name = get_user_name(data, "GET")
        ret = get_private_image(name)
        if ret != None:
            return ret
        return {
            "code": 0,
            "msg": "failed!"
        }