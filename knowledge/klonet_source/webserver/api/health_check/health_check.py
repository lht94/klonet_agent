from flask.views import MethodView
from flask_login import login_required

HEALTH_STATUS_RESP = {"code": 1, "msg": "server is running"}


class HealthCheckApi(MethodView):
    """
    server（服务器）健康状态检查
    
    如有响应返回, 说明服务没有被卡死
    如无响应返回，需要重启gunicorn
    
    GET  /health_status/
    """

    def get(self):
        return HEALTH_STATUS_RESP
