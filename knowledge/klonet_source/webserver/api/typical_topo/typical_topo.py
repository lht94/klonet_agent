import json
from flask import request
from flask.views import MethodView
from flask_login import login_required
from . import topology_gen


class TypicalTopoAPI(MethodView):
    """
    典型拓扑生成API
    """
    def post(self):
        """
        Args:

        Returns:
            resp (dict): 响应的典型拓扑数据
        """
        para_dict = json.loads(request.get_data(as_text=True))
        typical_topo = topology_gen.Typical_topo(**para_dict)
        resp = typical_topo()
        return resp
