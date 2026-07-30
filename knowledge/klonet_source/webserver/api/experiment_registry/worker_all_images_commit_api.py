from flask import request
import traceback
import json
import threading
import docker
from flask.views import MethodView

from ....Service_layer.image_registry_upload import push_image
from ....tools.context import redis_context
from ....vemu_config.config import PROJ_CONFIG

class WorkerAllImagesCommitAPI(MethodView):
    """
    POST worker/all_images/commit   上传实验某一worker上所有节点镜像至镜像仓库
    
    （此函数主要为image_commit_api.py的封装和重写，缓解请求中再请求的问题）
    
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

    def commit_and_push(self, docker_client, container_id, image_full_name):
        # 获得某个节点容器id并commit为镜像
        container = docker_client.containers.get(container_id)
        container.commit(image_full_name)
        # push 
        push_image(docker_client, image_full_name)
        print(image_full_name)
        

    def post(self):        
        print("进入worker上的镜像提交")
        try:
            docker_client = docker.from_env()
            
            args = json.loads(request.get_data(as_text=True))
            
            # 获取用户对象
            redis_c = redis_context(args['user'])
            contariner_id_on_worker = []
            image_full_name_on_worker = []
            with redis_c as user_db_cli:
                for container_name in args['NEs_on_worker']:
                    table = f"{args['project_name']}_{container_name}"
                    print(table)
                    container_id = user_db_cli.get_value(table, 'NEid')
                    contariner_id_on_worker.append(container_id)
                    
                    # 为worker上每个节点指定完整镜像名
                    # tag待商榷，但latest目前看来最合理
                    # 镜像所属用户为experiment_admin
                    image_name = (f"image_{args['experiment_name']}_"
                                  f"{container_name}")
                    tag = "latest"
                    image_full_name = (f"{PROJ_CONFIG.image_registry_ip}:"
                                        f"{PROJ_CONFIG.image_registry_port}/"
                                        f"experiment_admin/{image_name}:{tag}")
                    image_full_name_on_worker.append(image_full_name)
            print(contariner_id_on_worker)
            print(image_full_name_on_worker)
            # 多线程并发commit and push镜像
            # 尚不清楚是否需要对线程数进行限制？
            T = []
            for container_id, image_full_name in zip(contariner_id_on_worker,
                                                     image_full_name_on_worker):
                t = threading.Thread(target=self.commit_and_push,
                                    args=(docker_client, container_id,
                                        image_full_name))
                T.append(t)
                t.start()
            for t in T:
                t.join()
                
            return {"code": 1, "msg": (f"experiment on worker "
                    f"{args['worker_ip']} commit success")}
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}
    