import json
import requests
from flask.views import MethodView
from flask import request
from ....vemu_config.config import PROJ_CONFIG
from flask_login import login_required

class MasterExprFigureAPI(MethodView):
    '''
    GET 获取实验绘图数据
    '''

    def get(self):
        data = request.args

        req_url = f'http://{PROJ_CONFIG.data_server_ip}'\
                  f':{PROJ_CONFIG.data_server_port}/data-server/expr_figure'
        result = requests.get(req_url, json=data).json()
        return result
