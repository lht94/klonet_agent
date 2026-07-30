from flask import request
import traceback
import json
from flask.views import MethodView
import grequests

import requests

from ....Service_layer.redisAPI import UserMapRedis
from ....Service_layer.experiment_image_manager import (create_image_info_by_topo,
                                                        all_ne_images_rename)
from ....tools.context import redis_context, Db0Context
from ....vemu_config.config import PROJ_CONFIG
from ....Function_layer.deployed_proj_manager import retrieve_topo
from ....webserver import mysql



class ReqExperimentCommitAPI(MethodView):
    """
    POST /master/experiment/commmit/ 上传拓扑所有镜像至镜像仓库
    
    参数为：
    "user": "", # 用户名
    "topo": "", # 拓扑名
    "experiment": "", # 实验名
    
    """
    
    def post(self):
        print("进入实验镜像提交")
        try:
            # 从前端传json文件请求master下发实验上传镜像命令
            info = json.loads(request.get_data(as_text=True))
            # 从post请求里的json文件中获取用户名和项目名
            if "user" in info and "topo" in info and "experiment" in info:
                user, topo, experiment= (info["user"], info["topo"], 
                                         info["experiment"])
                # 进入redis数据库，利用redisAPI的api查询user2DB表，定位用户相应DB
                redis_c = redis_context(user)
                with redis_c as user_db_cli:
                    # 对原始拓扑信息的image_name和subtype进行重命名
                    topo_info = retrieve_topo(user, topo)['networks']
                    NEs_class_info = user_db_cli.get_value('topo_service', topo)
                    all_ne_images_rename(user, experiment, NEs_class_info, 
                                         topo_info)   
                    # 通过项目在plane_topo_list中找到所有的容器节点
                    NEs = user_db_cli.get_value("plane_topo_list", topo)["NEs"]
                    # 通过项目名在topo2subtopo中找到相应的子拓扑
                    sub_topo_list = user_db_cli.get_value("topo2subtopo", topo)
                    # 然后在subtopo2worker下记录涉及到的worker的post请求                
                    commit_urls = []        
                    pull_urls = []
                    with Db0Context() as db0cli:
                        worker_list = db0cli.get_elements_in_set(PROJ_CONFIG.worker_list)
                        print(worker_list)        
                    for sub_topo in sub_topo_list:
                        subtopo2worker_ip = user_db_cli.get_value(
                            "subtopo2worker", sub_topo)
                        worker_list.remove(subtopo2worker_ip)
                        # 并在plane_subtopo_list找出分布在各子拓扑上的节点
                        NEs_links_info = user_db_cli.get_value(
                            "plane_subtopo_list", sub_topo)
                        NEs_on_worker = NEs_links_info["NEs"]
                        
                        # 生成每个worker相应的commit_url
                        commit_url = (f"http://{subtopo2worker_ip}:"
                        f"{PROJ_CONFIG.worker_port}/worker/all_images/commit/")
                        # 生成每个worker后续相应的pull_url
                        pull_url = (f"http://{subtopo2worker_ip}:"
                        f"{PROJ_CONFIG.worker_port}/worker/all_images/pull/")
                        # 为每个worker创建一个json
                        info_dict = {
                            "project_name": topo,
                            "experiment_name": experiment,
                            "user": user,
                            "worker_ip": subtopo2worker_ip,
                            "NEs_on_worker": NEs_on_worker,
                            "NEs": NEs
                            }
                        
                        # 存储对每个worker的url和相应json信息
                        commit_urls.append((commit_url, info_dict))
                        pull_urls.append((pull_url, info_dict))
                if worker_list:
                    rest_of_worker = worker_list
                    print(rest_of_worker)
                    for rest_worker in rest_of_worker:
                        url = (f"http://{rest_worker}:"
                        f"{PROJ_CONFIG.worker_port}/worker/all_images/pull/")
                        info_dict = {
                                "project_name": topo,
                                "experiment_name": experiment,
                                "user": user,
                                "worker_ip": rest_worker,
                                "NEs_on_worker": [],
                                "NEs": NEs
                                }
                        pull_urls.append((url, info_dict))
                print(commit_urls)
                print(pull_urls)
                # master向各worker上提交grequest请求，各worker开始将节点镜像上传
                # 测试时只有一个worker，相当于requests.post
                rs_commit = (grequests.post(
                    url, json=req_paras) for url, req_paras in commit_urls)
                resp_result = grequests.map(rs_commit)
                resp_status = [resp.json()["code"] for resp in resp_result]
                if not all(resp_status):
                    return {"code": 0, "msg": "实验镜像上传中断"}
                
                # TODO(Wudx): 
                # 从镜像仓库pull时遇到过bug，有时有些镜像无法成功pull
                # 这样导致后续会报错，但重启容器registry后此问题得到解决
                # 猜想可能是本地私仓删除镜像进行垃圾回收机制所导致
                # 当删除私仓镜像时不采用垃圾回收机制就永远不会报错
                # pull本地镜像仓库的bug，跟实验仓库代码无关，大多时候能正常运行

                # master向各worker提交grequest请求，各worker开始pull本地没有的镜像
                rs_pull = (grequests.post(
                    url, json=req_paras) for url, req_paras in pull_urls)
                resp_result = grequests.map(rs_pull)
                resp_status = [resp.json()["code"] for resp in resp_result]
                if not all(resp_status):
                    return {"code": 0, "msg": "实验镜像拉取中断"}
                
                
                # 镜像成功push并且pull后，将镜像信息上传至experiment_admin的数据库
                # 专有管理实验镜像，以便与其他普通用户的镜像相隔离
                for container_name in NEs:
                    image = create_image_info_by_topo("experiment_admin", 
                                        experiment, container_name, topo_info)
                    print(image.image_full_name)
                    mysql.session.add(image)
                    mysql.session.commit()
                return {"code": 1, "msg": "实验镜像上传成功"}
                
        # 异常处理
        except Exception as e:
            mysql.session.rollback()
            traceback.print_exc()
            return {"code": 0, "msg": "实验镜像上传失败"}
        
