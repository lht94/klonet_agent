from flask import request
import traceback
import json
import threading
import docker
from flask.views import MethodView

from ....Service_layer.image_registry_upload import pull_image_by_noPost
from ....vemu_config.config import PROJ_CONFIG

class WorkerAllImagesPullAPI(MethodView):
    """
    POST worker/all_images/pull  从镜像仓库拉取worker本地没有的镜像
    
    参数为：
        "project_name": "", 项目名（实验拓扑名）
        "experiment_name": "", 实验名
        "user": "", 用户名
        "worker_ip": "", 当前worker的ip
        "NEs_on_worker": [], 当前worker上所有的容器节点
        "NEs" :[], 此项目涉及到的所有容器节点
    
    Returns:
    {"code": 1, "msg": "" }
    
    """
        
    def pull(self, docker_client, image_full_name):
        # 拉取镜像仓库的镜像
        pull_image_by_noPost(docker_client, image_full_name)
        print(image_full_name)
        
        
    def post(self):        
        print("worker开始拉取本地没有的镜像")
        try:
            docker_client = docker.from_env()
            
            args = json.loads(request.get_data(as_text=True))
            
            other_NEs = list(set(args['NEs']) - set(args['NEs_on_worker']))
            # 非本worker上的节点镜像，然后从镜像仓库多线程pull
            other_image_full_name = []
            for container_name in other_NEs:
                image_name = f"image_{args['experiment_name']}_{container_name}"
                tag = "latest"
                image_full_name = (f"{PROJ_CONFIG.image_registry_ip}:"
                                    f"{PROJ_CONFIG.image_registry_port}/"
                                    f"experiment_admin/{image_name}:{tag}")
                other_image_full_name.append(image_full_name)
            print(f"非本地镜像有：{other_image_full_name}")
            
            T = []
            if other_image_full_name:
                for container_name in other_image_full_name:
                    t = threading.Thread(target=self.pull, args=(docker_client, 
                                                                container_name))
                    T.append(t)
                    t.start()
                for t in T:
                    t.join()
                
            print("非本地镜像拉取结束")
            return {"code": 1, "msg": "拉取镜像成功"}
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}