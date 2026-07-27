from flask import request
import traceback
import json
from flask.views import MethodView
import docker
from ....Service_layer.redisAPI import UserMapRedis
from ....Service_layer.image_registry_upload import push_image,pull_image
from ....tools.log_tools import FLASK_LOGGER

class ImageCommitAPI(MethodView):
    '''
    POST /image/commit/ 提交容器为镜像

    参数为：
    "user": "", # 用户名
    "image_name":"", # 镜像名
    "tag":"", # 镜像TAG，默认值：latest
    "build_args":"", # 构建参数
    "type":"", # 类型
    "subtype":"", # 子类型
    "is_public": True/False, # 是否为公共镜像
    "edit_config": {},  #镜像可修改的配置，如端口修改
    "config": {}, # 镜像默认配置
    "customize_icon": True,
    "upload_type":# 上传方式
    "project_name":# 项目名称
    "container_name":# 容器名

    '''

    def post(self):
        FLASK_LOGGER.info("进入提交")
        try:
            FLASK_LOGGER.debug("进入提交")
            docker_client = docker.from_env()
            
            args = json.loads(request.get_data(as_text=True))

            #获得容器id
            user_map_redis = UserMapRedis()
            user_db_cli= user_map_redis.get_user_db(args['user'])
            FLASK_LOGGER.debug(user_db_cli)
            table = f"{args['project_name']}_{args['container_name']}"
            FLASK_LOGGER.debug(table)
            container_id = user_db_cli.get_value(table, 'NEid')
            user_map_redis.close()
            #commit镜像
            container=docker_client.containers.get(container_id)
            container.commit(args['image_full_name'])


            #push镜像
            push_image(docker_client, args['image_full_name'])
            pull_image(args['image_full_name'])



            return {"code":1, "msg":"success"}
        except Exception as e:
            traceback.print_exc()
            return {"code":0, "msg":str(e)}
