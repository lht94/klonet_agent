import json
import traceback
from flask import request
from flask.views import MethodView
from ....Service_layer.ne_health_worker import NeCheckWorker

class NeCheckAPI(MethodView):
    """
    POST /worker/ne_health/
    """

    def post(self):
        try:
            # 从请求中的 json 获得用户数据
            data = json.loads(request.get_data(as_text=True))
            user, topo, subtopo = data['user'], data['topo'], data['subtopo']
            # 进行节点健康状况检查
            ne_checker = NeCheckWorker(user, topo, subtopo)
            error_nes = ne_checker.check()
            # 返回
            return {'code': 1,
                    'error_nes': error_nes,
                    'msg': '获取节点健康状态成功'}
        except Exception as e:
            traceback.print_exc()
            return {'code': 0, 
                    'error_nes': [], 
                    'msg': f'worker获取节点健康状态部分失败, 由于{e.args[0]}'}
        