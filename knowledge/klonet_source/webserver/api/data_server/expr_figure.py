import json
import traceback
from flask.views import MethodView
from flask import request
from ....Service_layer.data_server_manager import DataServerManager

class ExprFigureAPI(MethodView):
    '''
    GET 获取实验绘图数据
    '''
    def get(self):
        data = json.loads(request.get_data(as_text=True))
        user = data['user']
        project_name = data['project_name']
        expr = data['expr']
        sub_event_seqs = data['event_seqs'].split(',')
        perf = data['perf']
        figure_type = data['figure_type']

        try:
            data_server_manager = DataServerManager()
            expr_figure_data = data_server_manager.get_expr_figure_data(
                user, project_name, expr, sub_event_seqs, perf, figure_type)
            return {
                'code': 1,
                'msg': "success",
                'data': expr_figure_data
            }
        except Exception as e:
            traceback.print_exc()
            return {
                'code': 0,
                'msg': str(e),
                'data': None
            }

        