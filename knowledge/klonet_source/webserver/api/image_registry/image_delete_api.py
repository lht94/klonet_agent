import traceback
import json
from flask.views import MethodView
from flask import request
import docker
from ....Implement_layer.LinkManager import shell_execute
from gevent import subprocess
from ....tools.log_tools import FLASK_LOGGER

class ImageDeleteAPI(MethodView):
    '''
    DELETE /image/delete/ 删除镜像

    参数为：
    "image": image_full_name, # 镜像全名

    '''

    def get(self):
        try:
            #检查本worker上是否有目标镜像被使用
            result={}
            image = json.loads(request.get_data(as_text=True))
            image_full_name_no_tag=image["image_full_name_no_tag"]
            image_used_info=shell_execute(f"sudo docker ps -a| grep -w {image_full_name_no_tag}")
            #如果有容器使用该镜像
            if image_used_info :
                    return {"image_used_info":1,"worker_ip":image["worker_ip"]}

        except subprocess.CalledProcessError as e:
                #如果没有容器使用该镜像
                if e.stderr.rstrip()=="":
                    return {"image_used_info":0}
                else:
                    result['error_msg'] = "CHECK IMAGE IF USED ERROR when execute command '" + e.cmd + \
            "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
                    return result

        except Exception as e:
            traceback.print_exc()
            return {"code":0, "msg":str(e)}

    def delete(self):
        #删除worker的本地镜像：
        try:
            image = json.loads(request.get_data(as_text=True))
            FLASK_LOGGER.debug(image)
            #执行remove命令
            docker_client = docker.from_env()
            docker_client.images.remove(image["image_full_name"])
            return {"delete_info":1, "msg":"success"}
        
        except docker.errors.APIError as e:
            if e.status_code == 404:
                FLASK_LOGGER.error("No such image,or image is deleted，continue")
                return {"delete_info":1, "msg":"No such image,or image is deleted，continue"}
            else:
                FLASK_LOGGER.error(e)
                return {"delete_info":0, "msg":str(e)}

        except Exception as e:
            traceback.print_exc()
            return {"delete_info":0, "msg":str(e)}
