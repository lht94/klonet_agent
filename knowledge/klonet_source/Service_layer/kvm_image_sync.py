from ..vemu_config.config import PROJ_CONFIG
from ..tools.tools import get_host_ip, print_with_timestamp
from ..Implement_layer.LinkManager.link_operate import shell_execute
from ..Service_layer.mysql_api.kvm_image import get_all_self_image
from ..tools.file_tool import check_directory
from ..tools.tools import register_worker
from ..tools.context import Db0Context
from time import sleep
import requests
import traceback
import multiprocessing
import os
import json

DEFAULT_MOD = "default"
WEB_MOD = "web_image"

def start_kvm_image_sync():
    # 如果当前worker已经在worker_list其中了，则不再同步，以应对worker反复启停的情况
    with Db0Context() as db0_cli:
        worker_list = db0_cli.get_elements_in_set(PROJ_CONFIG.worker_list)
    worker_ip = get_host_ip()
    if worker_ip in worker_list:
        return
    sync_cli = WorkerImageSync()
    p = multiprocessing.Process(target=sync_cli.start_image_sync)
    p.start()
    register_worker()
    
    
class WorkerImageSync():
    def __init__(self):
        self.worker_ip = get_host_ip()
    
    def start_image_sync(self):
        '''
        请求master开始进行同步
        '''
        try:
            sleep(10)    # 先sleep几秒保证worker已经正常启动起来
            print_with_timestamp(f"Start sync kvm image...")
            # worker上rsync初始化
            init_rsync_daemon()
            # 分类处理worker上的同步mod信息
            info = {
                "worker_ip": self.worker_ip,
            }
            # 默认镜像mod
            if PROJ_CONFIG.only_default_image_sync:
                path = f"{PROJ_CONFIG.kvm_image_registry_dir}/kvm_default/"
                check_directory(path)
                add_rsync_mod(DEFAULT_MOD, path)
            # web镜像mod
            if PROJ_CONFIG.only_web_kvm_image_sync:
                path = f"{PROJ_CONFIG.kvm_image_registry_dir}/"
                check_directory(path)
                add_rsync_mod(WEB_MOD, path)
            # 用户自上传镜像mod
            if PROJ_CONFIG.only_self_image_sync:
                info["image_paths"] = []
                info_url = (f"http://{PROJ_CONFIG.master_ip}:"
                    f"{PROJ_CONFIG.master_port}/master/get_self_image_info/")
                paths = requests.get(info_url).json()["image_paths"]
                index = 0
                for path in paths:
                    dir = os.path.dirname(path) + "/"
                    check_directory(dir)   # 在worker上提前创建响应文件夹，因为这一步，导致同步用户自上传镜像是一个比较危险的行为
                    add_rsync_mod(f"self_image_{index}", dir)
                    info["image_paths"].append(path)
                    index += 1
            url = (f"http://{PROJ_CONFIG.master_ip}:"
                    f"{PROJ_CONFIG.master_port}/master/sync_kvm_image/")
            resp = requests.post(url, json=info)
            if resp.json()["code"]:
                return {"code": 1, "msg": "开始进行KVM镜像同步，此过程比较耗时"}
            else:
                return {"code": 0, "msg": "请求进行镜像同步失败"}
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}
            
def init_rsync_daemon():
    '''
    初始化rsync同步工具服务
    '''
    current_folder = os.path.dirname(os.path.abspath(__file__))
    os.environ['RSYNC_PASSWORD'] = PROJ_CONFIG.rsync_password   # 避免遗漏再次添加环境变量
    res = shell_execute(f"sudo bash {current_folder}/rsync_init.sh {PROJ_CONFIG.rsync_user} {PROJ_CONFIG.rsync_password}")
    print(res)

def add_rsync_mod(mod_name, path):
    '''
    rsync追加mod
    
    Args:
        mod_name: "", # mod名称
        path: "", # 目标同步路径
    '''
    current_folder = os.path.dirname(os.path.abspath(__file__))
    res = shell_execute(f"sudo bash {current_folder}/rsync_set.sh {PROJ_CONFIG.rsync_user} {mod_name} {path}")