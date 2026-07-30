from flask import request
from flask.views import MethodView
import traceback
import json
import requests
import os
from werkzeug.utils import secure_filename
from gevent import subprocess

from ....webserver import mysql
from ....Function_layer.deployed_proj_manager import retrieve_topo
from ....vemu_config.config import PROJ_CONFIG
from ....tools.context import redis_context
from ....tools.tools import str2bool
from ....Service_layer.experiment_image_manager import (all_ne_images_rename,
                                                        get_all_images,
                                                        del_scripts,
                                                        del_experi_mysql)
from ....Service_layer.mysql_models import Experiment
from ....Service_layer.mysql_api.user_info import (get_user_info_by_user_name,
                                                   get_user_name_by_user_id)
from ....Service_layer.mysql_manager import check_row_exists, get_row
from ....Service_layer.image_registry_delete import (check_worker_use_image, 
                                                     del_local_image, 
                                                     del_worker_image,
                                                     del_registry_for_experi,
                                                     del_mysql)
from ....Service_layer.permission_manager import (get_user_role_by_name,
                                                  check_user_exist)
from ....Implement_layer.LinkManager import shell_execute


def _register_experiment_admin():
    req_url = (f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}"
               f"/master/user_register/")
    data = {
            "name": "experiment_admin",
            "password": "[REDACTED]",
            "phone": 0,
            "email": "1879424157@qq.com",
            "role": "super_admin"
        }
    req_res = requests.post(req_url, json=data)
    res = json.loads(req_res.text)
    print(res["msg"])
    return res


class ExperimentUploadAPI(MethodView):
    """
    POST /master/experiment/upload/ 上传实验至实验仓库
    
    参数为：
    "experiment_name": "", 实验名
    "user": "", 用户名
    "topo": "", 拓扑名
    "have_scripts": "true/flase", 是否有脚本上传(注意为小写字符串以区分boolean类型)
    "experi_scripts": file, 需要上传的脚本文件压缩包
    
    """
    def post(self):
        print("开始将实验上传到实验仓库")
        # 初始化时注册experiment_admin
        if not check_user_exist("experiment_admin"):
            try:
                res = _register_experiment_admin()
                if not res["code"]:
                    return {"code": 0, "msg": "experiment_admin初始化注册失败"}
            except Exception as e:
                traceback.print_exc()
                return {"code": 0, "msg": "experiment_admin初始化注册失败"}
        try:
            experiment_name = request.form.get('experiment_name')
            # 检查实验名是否已经存在
            # 关于实验名是否合法待检查
            if check_row_exists(Experiment, experiment_name=experiment_name):
                return {"code": 0, "msg": "该实验已存在，请重新为实验名命名"}
            user = request.form.get('user')
            topo = request.form.get('topo')
            have_scripts = request.form.get('have_scripts')
            if have_scripts == "true":
                # 接收文件
                # 需要判断仅接收压缩文件，否则解压会出错（应该是在前端进行判断）
                try:
                    experi_scripts_files = request.files['experi_scripts']
                except Exception as e:
                    traceback.print_exc()
                    return {
                        "code": 0,
                        "msg": "file key error"
                    }

            if user and topo:
                # 检查目标topo是否存在
                with redis_context(user) as user_db_cli:
                    if not user_db_cli.check_exist('topo_list', topo):
                        return {"code": 0, "msg": "目标项目名不存在"}
                print(f"将{user}的{topo}作为"
                      f"{experiment_name}上传")
                # 将所有节点镜像上传
                req_url = (f"http://{PROJ_CONFIG.master_ip}:"
                        f"{PROJ_CONFIG.master_port}/master/experiment/commmit/")
                info = {
                    "user": user,
                    "topo": topo,
                    "experiment": experiment_name
                }
                res = requests.post(req_url, json=info)
                print(res)
                if not res.json()['code']:
                    return {"code": 0, "msg": "实验上传中断"}
                # 得到的retrieve_info只取"networks"字段
                retrieve_info = retrieve_topo(user, topo)
                topo_info = retrieve_info['networks']
                # 通过topo_service获得所有的容器节点的分类，并对各个image_name重命名
                # 尚不确定对controller、dpdks镜像进行重命名是否有问题？
                with redis_context(user) as user_db_cli:
                    NEs_class_info = user_db_cli.get_value('topo_service', topo)
                    all_ne_images_rename("experiment_admin", experiment_name, 
                                         NEs_class_info, topo_info)    
                print(topo_info)
                
                # 上传数据库
                experiment = Experiment()
                experiment.experiment_name = experiment_name
                experiment.user_id = get_user_info_by_user_name(user).user_id
                experiment.have_scripts = str2bool(have_scripts)
                
                if have_scripts == "true":
                    # 保存脚本压缩文件包
                    # 统一处理为根据实验名命名
                    if not os.path.exists(PROJ_CONFIG.static_scripts_dir):
                        os.makedirs(PROJ_CONFIG.static_scripts_dir)
                    safe_name = secure_filename(f"{experiment_name}_scripts.tar")
                    file_path = os.path.join(PROJ_CONFIG.static_scripts_dir, 
                                        safe_name)
                    experi_scripts_files.save(file_path)
                    experiment.experiment_scripts_name = (f"{experiment_name}_"
                                                        "scripts.tar")

                # 将python对象转换成json对象并编码
                experiment.topo_json = json.dumps(topo_info).encode()
                
                mysql.session.add(experiment)
                mysql.session.commit()
    
            return {"code": 1, "msg": "实验上传成功"}
        except Exception as e:
            mysql.session.rollback()
            traceback.print_exc()
            return {"code": 0, "msg": "实验上传失败"}
        
    def delete(self):
        """
        Delete /master/experiment/upload/ 删除实验仓库中的某个实验
        
        参数为：
        "experi_name": "", 实验名
        "user": "", 用户名
        
        """
        # 检查用户权限
        user = json.loads(request.get_data(as_text=True))["user"]
        experi_name = json.loads(request.get_data(as_text=True))["experi_name"]
        experi_user = get_user_name_by_user_id(get_row(
            Experiment, Experiment.experiment_name==experi_name).user_id)
        if experi_user != user:
            if get_user_role_by_name(user) != 3:
                return {"code":0, "msg": "当前用户权限不足，无法删除实验"}
        # 需要删除：master和worker的镜像，远程镜像仓库的镜像，脚本文件，mysql数据
        
        print("开始删除实验")
        
        if not check_row_exists(Experiment, experiment_name=experi_name):
            return {"code": 0, "msg": "实验仓库不存在该实验，删除失败"}
        
        # 检查镜像是否有人使用
        topo_info = json.loads(get_row(Experiment, experiment_name =
                        experi_name).topo_json.decode())
        images_list = get_all_images(topo_info)
        # 看似对每一个镜像进行检查，其实检查完第一个镜像没有人使用后就开始进行全部删除
        for image_full_name in images_list:
            image_full_name_no_tag = image_full_name.rsplit(":", 1)[0]
            print(image_full_name_no_tag)
            result = {}
            try:
                image_used_info=shell_execute(f"sudo docker ps -a| "
                                            f"grep -w {image_full_name_no_tag}")
                # 如果master使用该镜像
                if image_used_info :
                    print(f"有项目创建的容器正在使用实验镜像{image_full_name}，"
                            "暂不能删除")   #前端页面
                    print("使用的服务器为:本机")
                    return {"code": 0, "msg": "有项目创建的容器正在使用实验中的"
                            "镜像，暂不能删除"}
            except subprocess.CalledProcessError as e:
                #本机master未使用该镜像时：
                if e.stderr.rstrip()=="":
                    # 检查worker上是否有人使用镜像
                    worker_use_info = check_worker_use_image(
                                        image_full_name_no_tag)
                    if not worker_use_info["code"]:
                        return {"code": 0, "msg": "worker上有容器正在使用实验中的"
                                "镜像，暂不能删除"}
                else:
                    result['error_msg'] = "CHECK IMAGE IF USED ERROR when execute command '" + e.cmd + \
                        "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
                    return result
                
            # 开始删除镜像
            try: 
                # 删除本地和worker中的镜像
                del_local_image(image_full_name)
                del_worker_image(image_full_name)
                # 删除registry里的镜像
                image_name = image_full_name_no_tag.split("/")[-1]
                print(f"删除的镜像为{image_name}")
                del_registry_for_experi(image_name, 
                                        "latest", "experiment_admin")
                # 删除mysql的image中的镜像数据
                del_mysql(image_name, "latest", "experiment_admin")
            except Exception as e:
                traceback.print_exc()
                return {"code": 0, "msg": "删除实验镜像时异常"}
        # 删除脚本文件
        try:
            del_scripts(experi_name)
            del_experi_mysql(experi_name)
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": f"删除实验{experi_name}失败"}
    
        return {"code": 1, "msg": f"删除实验{experi_name}成功"}
        