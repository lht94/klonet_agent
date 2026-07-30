from flask.views import MethodView
from ....vemu_config.config import PROJ_CONFIG
from ....Service_layer.kvm_image_sync import init_rsync_daemon
from ....Service_layer.LinkManager import shell_execute
from ....Service_layer.mysql_api.kvm_image import get_all_self_image
from ....Service_layer.AsyncTopoManager import KVMImageSyncTasks
from ....tools.log_tools import FLASK_LOGGER
from ....tools.file_tool import check_directory_exits
from flask import request
import json
import traceback
from threading import Thread

DEFAULT_MOD = "default"
WEB_MOD = "web_image"

class ImageSyncAPI(MethodView):
    def post(self):
        '''
        POST /master/sync_kvm_image/    master向worker同步镜像
        
        参数为：
        "worker_ip": "", 同步的worker对象
        "image_paths": "", 同步用户自上传镜像的路径（可选）
        '''
        try:
            print("开始进行镜像同步")
            worker_ip = json.loads(request.get_data(as_text=True))["worker_ip"]
            # 初始化rsync --daemon服务
            # 当worker与master不在同一宿主机上的时候才初始化rsync，避免复写conf文件报错
            if worker_ip != PROJ_CONFIG.master_ip:
                init_rsync_daemon()
            # 开始分类处理镜像文件
            # 同步平台默认镜像
            if PROJ_CONFIG.only_default_image_sync:
                path = f"{PROJ_CONFIG.kvm_image_registry_dir}/kvm_default/"
                if worker_ip != PROJ_CONFIG.master_ip:
                    shell_execute(f"rsync -a {path} vemu@{worker_ip}::{DEFAULT_MOD}")
                else:
                    # 当worker与master在同一宿主机上时使用rsync需要输入密码，很奇怪
                    # 目前的解决办法是default镜像不同步，其他镜像直接cp
                    pass
            # 同步web端上传镜像（不区分用户）
            if PROJ_CONFIG.only_web_kvm_image_sync:
                path = f"{PROJ_CONFIG.kvm_image_registry_dir}/kvm_image_registry/"
                if worker_ip != PROJ_CONFIG.master_ip:
                    if check_directory_exits(path):
                        shell_execute(f"rsync -a {path} vemu@{worker_ip}::{WEB_MOD}")
                    else:
                        pass
                else:
                    # master和worker同主机时采用cp
                    worker_path = f"{PROJ_CONFIG.kvm_image_registry_dir}/"
                    shell_execute(f"cp -r {path}. {worker_path}")
            # 同步非web端上传镜像（不区分用户）
            if PROJ_CONFIG.only_self_image_sync:
                if worker_ip != PROJ_CONFIG.master_ip:
                    # 不采用从数据库直接请求是为了避免前后数据库不一致问题
                    image_paths = json.loads(request.get_data(as_text=True))["image_paths"]
                    index = 0
                    # 多线程执行
                    # 复用平台队列多线程模型失败
                    # image_sync_tasks = []
                    # for path in image_paths:
                    #     args = (path, worker_ip, index)
                    #     print(f"=========同步镜像{index}")
                    #     image_sync_tasks.append((_sync_image, args))
                    #     index += 1
                    # print(index)
                    # image_sync_cli = KVMImageSyncTasks(image_sync_tasks)
                    # image_sync_cli.wait_task_done()
                    
                    # 多线程朴素版
                    # 没有采用队列线程模型不是很好限制线程数
                    # TODO（wudx）：感觉目前的需求量应该不足以在某一时刻把核心吃满
                    image_sync_tasks = []
                    for path in image_paths:
                        t = Thread(target=_sync_image, args=(path, worker_ip, index))
                        index += 1
                        t.start()
                        image_sync_tasks.append(t)
                    for t in image_sync_tasks:
                        t.join()
                else:
                    # 当master和worker位于同一主机上时不需要同步
                    # 因为在上传镜像时就要求master的同一路径下也必须保有镜像
                    pass
            FLASK_LOGGER.debug('================向新worker镜像同步完毕===============')
            return {"code": 1, "msg": "完成镜像同步"}
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}

class SelfImageInfoAPI(MethodView):
    def get(self):
        '''
        GET /master/get_self_image_info/    获取用户自上传镜像路径信息
        '''
        try:
            print("获取self镜像信息")
            image_info = get_all_self_image()
            paths = []
            for image_item in image_info:
                paths.append(image_item.path)
            return {"code":1, "image_paths": paths}
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}
            
        
def _sync_image(path, worker_ip, index):
    """
    同步镜像承载函数
    
    Args:
        path: "", # 同步镜像路径
        worker_ip: "", # 同步对象IP
        index: "", # rsync同步所使用的mod的index
        
    Returns:
        执行结果
    """
    return shell_execute(f"rsync -a {path} vemu@{worker_ip}::self_image_{index}")
    