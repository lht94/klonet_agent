import json

from flask import request
from flask.views import MethodView

from ....Service_layer.influxAPI import get_influx_data


class ExprDataAPI(MethodView):

    """
    传入的参数包括
    得到监控数据
    """

    def post(self):
        """
        从influxdb得到监控数据并下载
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, data_type = data['user'], data['topo'], data['data_type']
        expr, event_seq = data['expr'], data['event_seq']
        try:
            file_info = get_influx_data(
                data_type, user, topo, expr, event_seq
            )
            result = {'code': 1, 'msg': '查询监控数据成功'}
            # [{'file_path': '/home/vemu4/vemu_dev/vemu_uestc/expr_monitor_user_data/aaa',
            # 'file_name': 'aaa_expr99_perf_data.csv'}]
            file_list = []
            for file in file_info:
                relative_file_path = f'/static/{user}/{file["file_name"]}'
                file_list.append(relative_file_path)
            result.update({'files': file_list})
            return result
        except:
            return {
                'code': 0,
                'msg': '查询监控数据失败'
            }
