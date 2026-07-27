from atexit import unregister
import json
from flask_login import login_required
from flask import request
from flask.views import MethodView

from ....Service_layer.redisAPI import WorkerRedis


class RegisterWorkerAPI(MethodView):
    '''
    POST /master/worker/<worker_ip>/
    DELETE /master/worker/<worker_ip>/
    '''

    def post(self, worker_ip):
        print(f'register worker {worker_ip}...')
        worker_redis = WorkerRedis()
        try:
            worker_redis.set_worker(worker_ip)
            return {'code': 1, 'msg': '注册成功'}
        except:
            return {'code': 0, 'msg': '注册失败'}
        finally:
            worker_redis.close()
    
 
    def delete(self, worker_ip):
        print(f'unregister worker {worker_ip}...')
        worker_redis = WorkerRedis()
        try:
            worker_redis.del_worker(worker_ip)
            return {'code': 1, 'msg': '注销成功'}
        except:
            return {'code': 0, 'msg': '注销失败'}
        finally:
            worker_redis.close()
