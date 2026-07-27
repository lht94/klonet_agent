import json
import traceback
from flask.views import MethodView
from flask import request
from flask_login import login_required
from ...tasks.monitor import tasks
from ....Service_layer.influxAPI import get_influx_data
from ....tools.log_tools import FLASK_LOGGER

class DataServerAPI(MethodView):
    '''
    POST 开始计算指标数据
    GET 获取实验监控数据
    '''

    def post(self):
        '''
        POST /data-server/expr/
        '''
        data = json.loads(request.get_data(as_text=True))
        info = {'user': data['user'], 'topo': data['topo'], 
                'expr': data['expr']}
        
        FLASK_LOGGER.debug(f"接收到开始计算信号，开始计算性能指标...")
        FLASK_LOGGER.debug(f"用户：{info['user']}，拓扑：{info['topo']}，"
              f"实验：{info['expr']}，")

        task_id_dict = tasks.data_server_start_calc(
            info['user'], info['topo'], info['expr'])

        resp = {'code': 1, 'msg': '成功接收到信号，开始计算性能指标...'}
        resp.update(task_id_dict)

        return resp

    def get(self):
        '''
        目前的
        GET /data-server/expr/

        感觉不是很符合RESTful API 的规范了
        感觉最开始是用户的信息不太好隐藏，如果显示编码在url里， 那用户数据是完全可以得到的
        按理说应该是用URL来完全标识一个资源实体的
        GET /topo/<toponame>/expr/<expr_name>/
        GET /topo/<toponame>/expr/<expr_name>/event/<event_seq>/
        这样才是比较符合规范的
        但是之前的json里面一开始， 拓扑创建的时候， 感觉就偏了。。。
        传入的参数包括 
        {
            'user': "用户名", 
            "topo": '拓扑名', 
            "data_type": "perf",      perf/raw 二选一
            'expr': "实验名",
            "event_seq": ""
        }
        '''
        data = json.loads(request.get_data(as_text=True))
        user, topo, data_type = data['user'], data['topo'], data['data_type']
        expr, event_seq = data['expr'], data['event_seq']
        try:
            file_info = get_influx_data(
                data_type, user, topo, expr, event_seq
            )
            result = {'code': 1, 'msg': '查询监控数据成功'}
            # [{'file_path': '/home/vemu4/vemu_dev/vemu_uestc/expr_monitor_user_data/aaa/expr1/', 
            # 'file_name': 'aaa_expr99_perf_data.csv'}]
            file_list = []
            # 按理说应该只需要返回文件名就可以了
            for file in file_info:
                relative_file_path = f'/static/{user}/{topo}/{file["file_name"]}'
                file_list.append(relative_file_path)
            result.update({'files': file_list})
            return result
        except Exception as e:
            traceback.print_exc()
            return {
                'code': 0,
                'msg': f'查询监控数据失败: {str(e)}'
            }