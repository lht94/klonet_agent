from flask import Flask
from flask_restplus import Api
from .running_time_api import *
from .models_api import *
from .user_management_api import *


def create_api_document_app():
    app = Flask(__name__)
    # 创建API 实例
    api = Api(title='VEMU API', description='VEMU API description')
    # 对namespace进行注册
    api.add_namespace(link_ns, path='/re')
    api.add_namespace(traffic_ns, path='/re')
    api.add_namespace(topo_ns, path='/re')
    api.add_namespace(task_ns, path='/re')
    api.add_namespace(expr_monitor_ns, path='/re')
    api.add_namespace(image_ns, path='/my')
    api.add_namespace(my_ns, path='/my')
    api.add_namespace(register_ns, path='/')
    api.add_namespace(login_ns, path='/')
    api.add_namespace(logout_ns, path='/')
    api.add_namespace(web_terminal_ns, path='/')
    api.init_app(app)
    return app
