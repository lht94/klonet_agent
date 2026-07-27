from flask.views import MethodView
from ....Service_layer.permission_manager import check_permission, get_user_images
from flask_login import login_required
from flask import request
from ....tools.user_tool import get_user_name


class OneManage(MethodView):
    """
    /master/perm/oneimage/
    """

    def get(self):
        """
        GET 返回checkname用户的私有镜像，用于管理员和超级管理员用户。
        /master/perm/oneimage/?user=tbb&checkname=msm128

        Return: {
            "code": 0, 
            "imagelist": [[11, "tb22", "tar", false, "latest", "2022-06-07 09:06:12"], ], 
            "msg": "ok!"
            }
        """
        try:
            check_name = request.args.get("checkname")
            data = request.args.get("user")
            name = get_user_name(data, "GET")
        except:
            return {"code": 0, "msg": "failed!"}

        if check_permission(name, "ImageManage.get"):
            # 根据传进来的参数，来得到该用户的所有镜像
            check_user_images = get_user_images(check_name)
            ret = []
            for item in check_user_images:
                tmp = []
                item = tuple(item)
                if item[6] == False:
                    for index in range(len(item)):
                        if index == 6:
                            continue
                        if index == 5:
                            tmp.append(str(item[index]))
                        else:
                            tmp.append(item[index])
                    ret.append(tmp)
            return {
                "code": 0,
                "msg": "ok!",
                "image_list": ret
            }
        return {"code": 0, "msg": "failed!"}