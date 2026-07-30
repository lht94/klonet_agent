import traceback
import json
from flask.views import MethodView
from flask import request
import docker
from ....tools.log_tools import FLASK_LOGGER

class ImagePullAPI(MethodView):
    '''
    POST /image/pull/ 拉取镜像

    参数为：
    "image": image_full_name, # 镜像全名

    '''

    def post(self):
        try:
            image = json.loads(request.get_data(as_text=True))
            FLASK_LOGGER.debug(image)
            #执行pull命令
            docker_client = docker.from_env()
            docker_client.api.pull(image["image_full_name"], tag=None)

            return {"code":1, "msg":"success"}
        except Exception as e:
            traceback.print_exc()
            return {"code":0, "msg":str(e)}
