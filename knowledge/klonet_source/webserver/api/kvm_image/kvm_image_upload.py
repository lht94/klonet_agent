from flask.views import MethodView
from flask import request
from ....vemu_config.config import PROJ_CONFIG
from werkzeug.utils import secure_filename
from ....tools.context import Db0Context
import grequests
import traceback
import json
import os

from ....webserver import mysql
from ....Service_layer.kvm_image_upload import create_kvm_image_object, del_kvm_image
from ....tools.file_tool import check_file_exits, check_directory, clear_empty_directory
from ....Service_layer.kvm_image_upload import check_user_delete_image, check_overlap_with_user_image
from ....Service_layer.mysql_api.kvm_image import delete_kvm_image_mysql_row


IMAGE_REGISTRY_DIR = PROJ_CONFIG.kvm_image_registry_dir
WORKER_LIST = PROJ_CONFIG.worker_list

class KVMIamgeUploadAPI(MethodView):
    '''
    POST /master/kvm_image/upload/  上传KVM虚拟机镜像
    
    参数为:
    "user": "", 用户名
    "is_web_image": "", 是否采用web方式上传镜像
    "kvm_image": file or none,需要上传的虚拟机镜像文件(.qcow2文件)
    "type": "", # 镜像类型
    "cpu": "", # 镜像CPU资源需求
    "mem": "", # 镜像内存资源需求
    "path": "", # 镜像存储路径（可选）
    
    '''
    def post(self):
        image_args = request.form.to_dict()
        print(image_args)
        user = request.form.get('user')
        is_web_image = request.form.get('is_web_image')
        
        if is_web_image == "true":  # 参数需要与前端协调！
            print("开始从WEB端上传虚拟机镜像，这将是一个耗时且漫长的过程")
            image_file = request.files['kvm_image']
            file_name = image_file.filename     # 采用镜像文件的命名
        
            # 集中存储在master的以下路径，便于后续进行镜像同步
            image_path = f"{IMAGE_REGISTRY_DIR}/kvm_image_registry/{user}/"
            try:
                # 考虑web上传效率较低，用户可能仅会上传极少个数的镜像，因此采用分门别类单个上传
                if user and image_file:
                    # 进行简单的镜像文件后缀检查，需要用户严格使用非中文命名(安全性检查！)
                    # 后续考虑在前端进行检查，节省时间
                    print(file_name)
                    self._check_image_name(file_name)
                    
                    # 基于用户名-镜像名的数据库重名检查
                    if not check_overlap_with_user_image(user, file_name):
                        return {"code": 0, "msg": "镜像名重复，请修改检查后重新上传"}
                    
                    # 创建kvm镜像的ORM对象，并进行一些查重检查
                    kvm_image = create_kvm_image_object(file_name, **image_args)
                    
                    # 采用web上传镜像时，记为default
                    kvm_image.path = "default"
                    
                    # 镜像存储
                    check_directory(image_path)
                    image_file.save(image_path + file_name)
                    # save_file(image_path, file_name, image_file)
                    
                    # 镜像分发worker的逻辑
                    with Db0Context() as db0_cli:
                        worker_list = db0_cli.get_elements_in_set(WORKER_LIST)   ###
                        print("worker_list: ", worker_list)
                    # 生成url请求字典
                    info = {
                        "is_web_image": is_web_image,
                        "user": user,
                        "file_name": file_name
                    }
                    files = {"file": image_file}
                    req_urls = []
                    for worker_ip in worker_list:
                        req_url = (f"http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/kvm_image/upload/")
                        req_urls.append(req_url)
                    
                    tmp = (grequests.post(url, data=info, files=files) for url in req_urls)
                    resp_result = grequests.map(tmp)
                    resp_status = [resp.json()["code"] for resp in resp_result]
                    if not all(resp_status):
                        return {"code": 0, "msg": "KVM实验镜像向worker分发时失败"}
                    
                    # 记录数据库
                    mysql.session.add(kvm_image)
                    mysql.session.commit()
                return {"code": 1, "msg": "KVM镜像上传成功"}
            except:
                traceback.print_exc()
                # 异常则删除文件并回滚数据库
                del_kvm_image(image_path + file_name, image_path)
                mysql.session.rollback()
                return {"code": 0, "msg": "KVM镜像上传失败"}
            
        # 非web端上传镜像只用考虑存入数据库，主要涉及到路径信息
        # 需要向用户强调全局路径统一！
        else:
            try:
                path = request.form.get("path")
                file_name = path.split("/")[-1]     # 从文件路径中截取文件名
                print(file_name)
                self._check_image_name(file_name)
                # 基于用户名-镜像名的数据库重名检查
                if not check_overlap_with_user_image(user, file_name):
                    return {"code": 0, "msg": "镜像名重复，请修改检查后重新上传"}
                
                # 要求用户在master上也必须有此镜像，便于后续同步
                if not check_file_exits(path):
                    return {"code": 0, "msg": "请确保用户手动上传的镜像在master和worker的相同路径下均存在"}
                
                # 对用户提供的全局image_path进行检查（worker）
                # 在宿主机过多时很有必要
                with Db0Context() as db0_cli:
                    worker_list = db0_cli.get_elements_in_set(WORKER_LIST)   ###
                    print(worker_list)
                info = {
                    "is_web_image": is_web_image,
                    "path": path
                }
                req_urls = []
                for worker_ip in worker_list:
                    req_url = (f"http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/kvm_image/upload/")
                    req_urls.append(req_url)
                
                tmp = (grequests.post(url, data=info) for url in req_urls)
                resp_result = grequests.map(tmp)
                resp_status = [resp.json()["code"] for resp in resp_result]
                # TODO(wudx): 没有考虑反馈给用户哪些服务器上没有该镜像
                if not all(resp_status):
                    return {"code": 0, "msg": "用户提供的镜像路径并非全局唯一，请检查后再提交"}
                
                # 创建kvm镜像的ORM对象，并进行一些查重检查
                kvm_image = create_kvm_image_object(file_name, **image_args)
                # 记录数据库
                mysql.session.add(kvm_image)
                mysql.session.commit()
                return {"code": 1, "msg": "用户自提供路径镜像的相关数据记录成功"}
            except:
                traceback.print_exc()
                mysql.session.rollback()
                return {"code": 0, "msg": "用户自提供路径镜像数据记录失败，考虑其他方式提交"}
            
        
        
    # 检查文件名后缀是否规范
    def _check_image_name(self, filename):
        # if (not filename.endswith(".iso")) and (not filename.endswith(".qcow2")):
        if not filename.endswith(".qcow2"):
            raise ValueError("非.qcow2文件格式，请重新检查后上传")
        # 英文命名检查
        check_name = secure_filename(filename)
        print(check_name)
        if check_name != filename:
            raise ValueError("请确定文件命名仅包含英文和下划线等规范字符，不要包含中文以及空格！")
        
    # # 检查文件名是否重名
    # def _check_overlap_filename(self, image_name, dir):
    #     file_list = []
    #     for filename in os.listdir(dir):
    #         filepath = os.path.join(dir, filename)
    #         if os.path.isfile(filepath):
    #             file_list.append(filename)
        
    #     print(file_list)
    #     if image_name in file_list:
    #         raise ValueError("名称重复，请更改名称后上传")
        
    def delete(self):
        """
        Delete /master/kvm_image/upload/ 删除用户上传的一个或多个KVM镜像
        
        参数为：
        "user": "", 用户名
        "files": [], 需要删除的KVM镜像文件名列表
        
        """
        try:
            user = json.loads(request.get_data(as_text=True))["user"]
            files = json.loads(request.get_data(as_text=True))["files"]
            if user and files:
                print(f"开始进行用户{user}KVM镜像{files}的删除")
                path_list = []  # 按照镜像顺序一一对应的路径表
                image_id_list = []  # 按照镜像顺序
                # 查表，master上只查一次表效率更高
                for file in files:
                    if_delete, image_info = check_user_delete_image(user, file)
                    if not if_delete:
                        return {"code": 0, "msg": f"{file}镜像不支持删除，请确认镜像存在于仓库"}
                    path_list.append(image_info.path)
                    image_id_list.append(image_info.image_id)
                with Db0Context() as db0_cli:
                    worker_list = db0_cli.get_elements_in_set(WORKER_LIST)   ###
                    print(worker_list)
                info = {
                "user": user,
                "files": files,
                "path_list": path_list
                }
                req_urls = []
                for worker_ip in worker_list:
                    req_url = (f"http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/kvm_image/upload/")
                    req_urls.append(req_url)
                tmp = (grequests.delete(url, json=info) for url in req_urls)
                resp_result = grequests.map(tmp)
                resp_status = [resp.json()["code"] for resp in resp_result]
                if not all(resp_status):
                    return {"code": 0, "msg": "KVM实验镜像删除出现异常"}
                
                # worker上镜像全部删除完毕
                # 处理保留在master上的原始镜像备份
                dir = ""
                for file, path in zip(files, path_list):
                    if path == "default":
                        dir = f"{IMAGE_REGISTRY_DIR}/kvm_image_registry/{user}/"
                        file_path = f"{IMAGE_REGISTRY_DIR}/kvm_image_registry/{user}/{file}"
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            clear_empty_directory(dir)
                        else:
                            pass
                    else:
                        pass    # 考虑到多用户复用镜像，自定义路径镜像只对数据库处理
                # 全部删除完毕后进行数据库信息删除
                # 粗暴删除，删除操作是否正确在一开始已经判断
                for image_id in image_id_list:
                    delete_kvm_image_mysql_row(image_id)    # 底层自带回滚
                
                
            return {"code": 1, "msg": "KVM镜像删除成功"}
        except:
            traceback.print_exc()
            return {"code": 0, "msg": "KVM镜像删除失败或未完全删除"}