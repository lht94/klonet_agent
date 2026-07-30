from flask import request
from flask.views import MethodView
from ....Service_layer.ne_health_master import NeCheckMaster
import json
import traceback
from flask_login import login_required

class NeCheckAPI(MethodView):
    """
    获取节点健康状态

    POST /master/ne_health/
    {
        "user": "xx",
        "topo": "yy"
    }
    """

    def post(self):
        try:
            # 从请求中的 json 获得用户数据
            data = json.loads(request.get_data(as_text=True))
            user, topo = data["user"], data["topo"]
            # 进行节点健康状况检查
            ne_checker = NeCheckMaster(user, topo)
            result = ne_checker.send_request()
            # 返回
            return result
        except Exception as e:
            traceback.print_exc()
            return {'code': 0, "msg": f"获取节点健康状态失败, 由于{e}"}
        