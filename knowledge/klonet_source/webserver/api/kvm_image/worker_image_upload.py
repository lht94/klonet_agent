from flask.views import MethodView
from ....vemu_config.config import PROJ_CONFIG
from ....tools.file_tool import check_directory, clear_empty_directory, check_file_exits
from flask import request
import os
import traceback
import json

IMAGE_REGISTRY_DIR = PROJ_CONFIG.kvm_image_registry_dir

class WokerImageUploadAPI(MethodView):
    '''
    POST /worker/kvm_image/upload/ 将kvm镜像转发给各worker并保存
    
    Args:
    "is_web_image": "", 是否为web端上传镜像
    
    其余参数分为两种情况：
    "is_web_image" == "true":
    (
    "user": "", 用户名
    "file_name": "", 镜像文件名
    "file": file, 镜像文件
    )
    "is_web_image" == "false":
    (
    "path": "", 镜像文件的绝对路径
    )
    '''
    def post(self):
        is_web_image = request.form.get('is_web_image')
        if is_web_image == 'true':
            print("worker开始保存接收到的KVM镜像文件")
            try:
                user = request.form.get('user')
                file_name = request.form.get('file_name')
                image_file = request.files['file']
                if image_file:
                    # web端上传文件，默认路径写死
                    image_path = f"{IMAGE_REGISTRY_DIR}/{user}/"
                    check_directory(image_path)
                    image_file.save(image_path + file_name)
                    # save_file(image_path, file_name, image_file)    # 此处是无脑复写
                return {"code": 1, "msg": "当前worker保存KVM镜像成功"}
            except:
                traceback.print_exc()
                return {"code": 0, "msg": "当前worker保存KVM镜像失败"}
        else: 
            if check_file_exits(request.form.get("path")):
                return {"code": 1, "msg": "当前worker此路径上拥有该KVM镜像"}
            else:
                return {"code": 0, "msg": "当前worker此路径上没有该KVM镜像"}
        
    # 检查文件名是否重名
    def _check_overlap_filename(self, image_name, dir):
        file_list = []
        for filename in os.listdir(dir):
            filepath = os.path.join(dir, filename)
            if os.path.isfile(filepath):
                file_list.append(filename)
        
        print(file_list)
        if image_name in file_list:
            raise ValueError("名称重复，请更改名称后上传")
        
    def delete(self):
        """
        Delete /master/kvm_image/upload/ 删除用户上传的一个或多个KVM镜像
        
        参数为：
        "user": "", 用户名
        "files": [], 需要删除的KVM镜像文件名列表
        "path_list": [], 镜像文件对应的路径列表
        
        """
        try:
            user = json.loads(request.get_data(as_text=True))["user"]
            files = json.loads(request.get_data(as_text=True))["files"]
            path_list = json.loads(request.get_data(as_text=True))["path_list"]
            if user and files:
                print(f"开始进行KVM镜像{files}的删除")
                dir = ""
                for file, path in zip(files, path_list):
                    if path == "default":
                        dir = f"{IMAGE_REGISTRY_DIR}/{user}/"
                        file_path = f"{IMAGE_REGISTRY_DIR}/{user}/{file}"
                        print(file_path)
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            clear_empty_directory(dir)
                        else:
                            pass
                    else:
                        pass    # 考虑到可能多个用户复用同一个镜像，用户自定义路径镜像不做删除处理
            return {"code":1, "msg": "当前worker镜像删除成功"}
        except:
            traceback.print_exc()
            print("当前worker删除镜像失败或不完全删除")
            return {"code": 0, "msg": "当前worker删除镜像失败或不完全删除"}