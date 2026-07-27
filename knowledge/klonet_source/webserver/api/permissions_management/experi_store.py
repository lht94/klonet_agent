from flask.views import MethodView
from flask import request
from flask_login import login_required
from ....Service_layer.permission_manager import get_experiments

class ExperiStore(MethodView):
    """
    GET /master/perm/experi_store/ 
    
    返回实验仓库中的实验列表
    """
    @login_required
    def get(self):
        ret = get_experiments()
        if ret != None:
            return ret
        return {
            "code": 0,
            "msg": "failed!"
        }
    