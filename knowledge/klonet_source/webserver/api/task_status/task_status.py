import json
from flask import request
from flask.views import MethodView
from celery.result import AsyncResult
from ....webserver import celery
from flask_login import login_required


class TaskStatusAPI(MethodView):
    """
    查询celery中异步任务的执行状态
    GET /master/task/<task_id>/
    """
  
    def get(self, task_id):
        """
        Args:
            task_id (str): 编码在url中的task_id

        Returns:
            resp (dict): 查询结构的响应
        """
        print(task_id)
        try:
            res = AsyncResult(task_id, app=celery)
            res_status = res.status
            resp = {
                'task_status': res_status,
                'task_id': task_id,
                'code': 1
            }
        except:
            return {'code': 0, 'msg': '查询任务失败'}
        if res_status == 'PENDING':
            resp['msg'] = '等待任务执行中...'
        elif res_status == 'STARTED':
            resp['msg'] = '任务已经开始...'
        elif res_status == 'FAILURE':
            resp['msg'] = '任务执行失败...'
            res.forget()
        else:
            resp['msg'] = '任务执行成功...'
            resp['result'] = res.get()
        return resp

    def post(self):
        return {'code': 0, 'msg': 'method not allowed', 'status': 405}

    def delete(self):
        return {'code': 0, 'msg': 'method not allowed', 'status': 405}


# class PcapStatusAPI(MethodView):
#     """
#     GET /pcap_task/ 
#     {'task_id': '', 'parent_id'}
#     task: start_raw_data_calc
#     parent: save_raw_data_to_db
#     """
#     def get(self):
#         data = json.loads(request.get_data(as_text=True))
#         task_id, parent_id = data['task_id'], data['parent_id']
#         pare_res = AsyncResult(parent_id)
#         res = AsyncResult(task_id)
#         pare_stat, res_stat = pare_res.status, res.status
#         resp = {
#             'task_status': res_stat,
#             'parent_status': pare_stat
#         }
#         # 进行状态检查和结果反馈
#         # 父任务状态是 挂起，说明还没有开始
#         # 需要返回code吗， 不然API不是统一的
#         if pare_stat == 'PENDING':
#             resp['msg'] = '等待任务执行中...'
#         elif pare_stat == 'STARTED' and res_stat == 'PENDING':
#             resp['msg'] = '任务开始执行...正在将原始数据写入数据库...'
#         elif pare_stat == 'FAILURE':
#             resp['msg'] = '写入数据库失败...'