from flask.views import MethodView
from flask import request
from ....Service_layer.permission_manager import get_public_image
from flask_login import login_required
from ....tools.user_tool import get_user_name
from ....Service_layer.permission_manager import get_user_role_by_name


class PublicStore(MethodView):
    """
    /master/perm/publicstore/
    """
 
    def get(self):
        """
        返回共有镜像仓库列表
        /master/perm/publicstore/?user=tbb
        """
        data = request.args.get("user")
        name = get_user_name(data, "GET")
        if not (get_user_role_by_name(name) in [2, 3]):
            return {"code": 0, "msg": "Current user does not have permission!"}
        ret = get_public_image()
        if ret != None:
            return ret
        return {
            "code": 0,
            "msg": "failed!"
        }