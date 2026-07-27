from flask.views import MethodView
from ....Service_layer.permission_manager import check_permission, get_all_images, get_user_role_by_name
from flask_login import login_required
from flask import request
from ....tools.user_tool import get_user_name
from ....tools.log_tools import FLASK_LOGGER

class ImageManage(MethodView):
    """
    /master/perm/image/
    """

    def get(self):
        """
        返回所有用户的私有镜像，用于管理员和超级管理员用户。
        /master/perm/image/?user=tbb
        """
        data = request.args.get("user")
        name = get_user_name(data, "GET")
        if check_permission(name, "ImageManage.get"):
            ret = {}
            for item in get_all_images():
                FLASK_LOGGER.debug(item)
                if (get_user_role_by_name(str(item[1])) == 1) and (int(item[3]) == 0):
                    if str(item[1]) not in ret:
                        ret[str(item[1])] = []
                    ret[str(item[1])].append([item[2], int(item[3])])
            return {
                "code": 1,
                "msg": "ok!",
                "imagelist": ret
            }
        return {
            "code": 0,
            "msg": "failed!"
        }