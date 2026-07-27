from flask import request
from flask_login import login_required
from flask.views import MethodView
from ....Service_layer.redisAPI import WorkerRedis
from ....vemu_config.config import PROJ_CONFIG

import grequests

class ResourceAPI(MethodView):
    '''
    GET /master/resource/
    '''
    def get(self):
        worker_redis = WorkerRedis()
        worker_list = worker_redis.get_all_workers()
        worker_redis.close()
        req_urls = []
        for worker_ip in worker_list:
            req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/resource/'
            req_urls.append(req_url)
        req = (grequests.get(url) for url in req_urls)
        resp_result = grequests.map(req)
        resp_status_code = [resp.json()['code'] for resp in resp_result]
        if not all(resp_status_code):
            return {'code': 0, 'msg': '获取宿主机剩余资源信息失败', "info": {}}
        # TODO(sw):返回结果
        resource_info = {}
        for resp in resp_result:
            info = resource_info.setdefault(resp.json()['info']['worker_ip'], {})
            for key in resp.json()['info']:
                info[key] = resp.json()['info'][key]
        return {'code': 1, 'msg': '获取宿主机剩余资源信息成功', "info": resource_info}