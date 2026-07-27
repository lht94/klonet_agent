import json
from flask_login import login_required
from flask import request
from flask.views import MethodView
import requests

from ....vemu_config.config import PROJ_CONFIG


class ExprDataAPI(MethodView):
    """
    监控数据下载API
    传入的参数包括 
    {
        'user': "用户名", 
        "topo": '拓扑名', 
        "data_type": "perf",
        'expr': "实验名",
        "event_seq": ""
    }
    """
    # 这里需要直接请求data_server来下载数据
 
    def post(self):
        """
        请求worker得到性能数据
        """
        data = json.loads(request.get_data(as_text=True))
        req_url = f'http://{PROJ_CONFIG.data_server_ip}:'\
                    f'{PROJ_CONFIG.data_server_port}/data-server/expr'
        result = requests.get(req_url, json=data).json()
        return result
