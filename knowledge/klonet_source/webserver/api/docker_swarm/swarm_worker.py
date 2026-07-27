import json
from flask.views import MethodView
from flask import request
from ....tools.log_tools import *
from ....Service_layer.DockerSwarm import SwarmWorker

class DockerSwarmWorker(MethodView):
    """
    master上的docker swarm初始
    """

    def post(self):
        """
        /worker/swarm/

        Return:
            dict: {
                'code': 0失败，1成功,
                'msg': 提示信息,
                'url': 完整的url
            }
        """
        worker_token = json.loads(request.get_data(as_text=True))['worker_token']

        return SwarmWorker().docker_join(worker_token)

    def delete(self):
        """
        /worker/swarm/

        Return:
            dict: {
                'code': 0失败，1成功,
                'msg': 提示信息,
                'url': 完整的url
            }
        """
        
        return SwarmWorker.docker_leave()