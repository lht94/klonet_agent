from flask import request
from flask.views import MethodView
from ....Service_layer.worker_get_resource import WorkerResource
from ....tools.log_tools import FLASK_LOGGER


class ResourceAPI(MethodView):
    '''
    GET /worker/resource/
    '''
    def get(self):
        worker_resource = WorkerResource()
        try:
            res = worker_resource.get_resource()
            return {'code': 1, 'msg': '获取宿主机剩余资源', 'info': res}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': '获取宿主机剩余资源信息失败', 'info': {}}