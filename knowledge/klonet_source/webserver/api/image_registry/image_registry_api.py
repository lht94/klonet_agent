import traceback
from flask.views import MethodView
from flask import request
from flask_login import login_required
from ....Service_layer.image_registry_upload import upload_image
from ....Service_layer.image_registry_commit import commit_image
import json
from ....vemu_config.config import PROJ_CONFIG
from ....Implement_layer.LinkManager import shell_execute
from gevent import subprocess
from ....Service_layer.mysql_api.image import check_image_name_and_tag,check_image_if_public,get_public_image_user
from ....Service_layer.image_registry_delete import check_worker_use_image,del_local_image,del_worker_image,del_registry,del_folder,del_mysql
from ....tools.log_tools import FLASK_LOGGER

IMAGE_REGISTRY_DIR = PROJ_CONFIG.image_registry_dir


class ImageUploadAPI(MethodView):
    '''
    POST /image/upload/ 上传镜像

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
        try:
            # 可通过传的参数中有无dockerfile判断上传方式
            # 不管前端表格里填啥，image_args中的value全是字符串形式
            image_args = request.form.to_dict()

            FLASK_LOGGER.debug(image_args)
            FLASK_LOGGER.debug(request.files)

            # 没有对应key则返回None
            image_tar = request.files.get("image_tar")
            dockerfile = request.files.get("dockerfile")
            attachment = request.files.get("attachment")
            icon = request.files.get("icon")

            if image_args['upload_type']=="dockerfile" or image_args['upload_type']=="image_tar":
                upload_image(image_tar, dockerfile, icon, **image_args)
            elif image_args['upload_type']=="commit":
                commit_image(**image_args)
            return {"code":1, "msg":"镜像上传成功"}
        except Exception as e:
            traceback.print_exc()
            msg=str(e)[0:6]
            FLASK_LOGGER.error(msg)
            if  msg =="镜像重复提示":
                return {"code":2, "msg":str(e)}
            if  msg =="镜像同步失败":
                return {"code":2, "msg":str(e)}
            else:
                return {"code":0, "msg":str(e)}


    def delete(self):
        
        #需删除：dockerhub里的镜像、存储的相关文件、mysql数据、本地和worker删除：docker rmi 包含本地和worker
        #mysql最后删，顺序为：相关文件、dockerhub、本地和worker、mysql数据
        '''
        Delete /image/upload/ 删除上传的镜像

        参数为image_args：
        "user":"", # 用户名
        "image_name":"", # 镜像名
        "tag":"", # 标签
        '''

        image_args = json.loads(request.get_data(as_text=True))


        for image_name,tag in zip(image_args["image_name"],image_args["tag"]):
            

            #检查该镜像是否存在/已删除？
            if not check_image_name_and_tag(image_name, tag):
                raise ValueError("删除mysql中不存在的镜像："+f"{image_name}:{tag}")


            #公有镜像的删除较特殊：用户不一定是镜像的上传者，需特殊处理包装一下：
            if check_image_if_public(image_name,tag):
                #不能用image_args['user']，用通过mysql表找到真正的user，重新组合成image_full_name
                user=get_public_image_user(image_name,tag)
                image_full_name = (f"{PROJ_CONFIG.image_registry_ip}:"
                                f"{PROJ_CONFIG.image_registry_port}/{user}/"
                                f"{image_name}:{tag}")
                image_full_name_no_tag = (f"{PROJ_CONFIG.image_registry_ip}:"
                                f"{PROJ_CONFIG.image_registry_port}/{user}/"
                                f"{image_name}")
                FLASK_LOGGER.debug(image_full_name)
            else:
                image_full_name = (f"{PROJ_CONFIG.image_registry_ip}:"
                                f"{PROJ_CONFIG.image_registry_port}/{image_args['user']}/"
                                f"{image_name}:{tag}")
                image_full_name_no_tag = (f"{PROJ_CONFIG.image_registry_ip}:"
                                f"{PROJ_CONFIG.image_registry_port}/{image_args['user']}/"
                                f"{image_name}")
 

            #先检查master和worker上是否有容器正在使用该镜像
            result = {}
            try:
                image_used_info=shell_execute(f"sudo docker ps -a| grep -w {image_full_name_no_tag}")
                #如果master使用该镜像
                if image_used_info :
                    FLASK_LOGGER.info("有项目创建的容器正在使用该镜像，暂不能删除")   #前端页面
                    FLASK_LOGGER.debug("使用的服务器为:本机")
                    return {"code": 0, "msg": "有项目创建的容器正在使用该镜像，暂不能删除"}

            except subprocess.CalledProcessError as e:
                #本机master未使用该镜像时：
                if e.stderr.rstrip()=="":  #本机master未使用时

                    #检查worker是否正在使用该镜像:
                    worker_use_info=check_worker_use_image(image_full_name_no_tag)

                    if worker_use_info["code"] == 1:
                        FLASK_LOGGER.debug("没有服务器使用该镜像,开始删除")

                        #删除本地和worker的镜像
                        del_local_image(image_full_name) 
                        del_worker_image(image_full_name)  
                        #调用del_registry函数，删除registry里的镜像
                        del_registry(image_name,tag,image_args['user'])
                        #调用del_folder函数，删除储存文件
                        del_folder(image_name,tag,image_args['user'])
                        #调用del_myaql函数，删除Mysql里的数据
                        del_mysql(image_name,tag,image_args['user'])

                    else:
                        return {"code": 0, "msg": "有项目创建的容器正在使用该镜像，暂不能删除"}


                else:
                    result['error_msg'] = "CHECK IMAGE IF USED ERROR when execute command '" + e.cmd + \
                        "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
                    return result
        

        FLASK_LOGGER.debug("镜像删除成功")
        return {"code": 1, "msg": "镜像删除成功"}  #前端页面




        
       







        

    
               

            


            


            