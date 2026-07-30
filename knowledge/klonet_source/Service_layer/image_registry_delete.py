from ..vemu_config.config import PROJ_CONFIG
from vemu_uestc.Service_layer.redisAPI import WorkerRedis
import grequests
import docker
from ..Service_layer.mysql_api.image import check_image_if_public,get_public_image_user,get_user_id_by_user_name,delete_image_row
from ..Implement_layer.LinkManager import shell_execute
from ..Service_layer.image_registry_upload import IMAGE_REGISTRY_DIR,_del_image_files

docker_client = docker.from_env()

#与worker通信：
WORKER_LIST_TABLE_NAME = PROJ_CONFIG.worker_list
WORKER_PORT = PROJ_CONFIG.worker_port
class MyWorkerRedis(WorkerRedis):
    def get_all_workers(self):
        return list(self._db_conn.smembers(WORKER_LIST_TABLE_NAME)) 

def err_handler(request,exception):
        print('发生异常，具体信息为：',exception)

worker_redis = MyWorkerRedis()
worker_list = worker_redis.get_all_workers()    


#看worker上镜像是否被使用 
def check_worker_use_image(image_full_name_no_tag):
    reqs = []
    for worker_ip in worker_list:
        worker_url = f"http://{worker_ip}:{WORKER_PORT}/image/delete/"
        print(worker_url)
        info_dict = {
                "worker_ip":worker_ip,
                "image_full_name_no_tag":image_full_name_no_tag
        }
        req = grequests.get(worker_url, json=info_dict)
        reqs.append(req)
    resps = grequests.map(reqs,exception_handler=err_handler)
    print(resps)
    for resp in resps:
        if resp.json()["image_used_info"] == 1: #其他worker使用了
            print("有项目创建的容器正在使用该镜像，暂不能删除")   #前端页面
            print("使用的worker_ip为:"+resp.json()["worker_ip"])
            return {"code": 0, "msg": "有项目创建的容器正在使用该镜像，暂不能删除"}
    return {"code": 1, "msg": "没有服务器使用该镜像,开始删除"}



#删除本地镜像
def del_local_image(image_full_name):
    try:
        #删除maset本机镜像
        docker_client.images.remove(image_full_name)
    except docker.errors.APIError as e:
        if e.status_code == 404:
            print("No such image,or image is deleted，continue")
        else:
            print(e)
            return str(e)


#删除worker镜像
def del_worker_image(image_full_name):
    reqs2 = []
    for worker_ip in worker_list:
        worker_url = f"http://{worker_ip}:{WORKER_PORT}/image/delete/"
        print(worker_url)
        info_dict = {
                "image_full_name":image_full_name
        }
        req = grequests.delete(worker_url, json=info_dict)
        reqs2.append(req)
    resps = grequests.map(reqs2,exception_handler=err_handler)
    print(resps)

    #bug:不管有没有容器使用，都能删除，根本不会报错
    for resp in resps:
        print(resp.json()["msg"])
        if resp.json()["delete_info"] != 1:
            print(f"删除失败")


#删除registry里的镜像,这一步暂时是简易删除法，暂时是写死的，不会引起报错，shell命令
def del_registry(image_name,tag,username):
    #命令：sudo docker -H 10.1.1.114:2375 exec registry(仓库容器的容器名) rm -rf /var/lib/registry/docker/registry/v2/repositories/msm123/tongbutest14(镜像名：user/Image_name)
    #如果镜像仓库就在本机，则不用10.1.1.114
    try:
        registry_container_name="registry"
        if check_image_if_public(image_name,tag):
            user=get_public_image_user(image_name,tag)
        else:
            user=username
        #删除镜像文件
        #如果镜像仓库创建在master主机上
        if PROJ_CONFIG.master_ip == PROJ_CONFIG.image_registry_ip:
            shell_execute(f"sudo docker exec {registry_container_name} "
                        f"rm -rf /var/lib/registry/docker/registry/v2/repositories/{user}/{image_name}")
        #镜像仓库和master不在同一本机
        else:
            shell_execute(f"sudo docker -H {PROJ_CONFIG.image_registry_ip}:"
                        f"{PROJ_CONFIG.remote_docker_daemon_port} "
                        f"exec {registry_container_name} rm -rf "
                        f"/var/lib/registry/docker/registry/v2/repositories/{user}/{image_name}")
        
        # TODO(wudx):  2024.10.9
        # bug发现：在tar上传镜像时，由于内容一样会导致每次image id一样，在私仓删除并进行GC后，再次push并从私仓拉取会报错
        # docker.errors.NotFound: 404 Client Error: Not Found ("manifest for 192.168.1.33:5024/sadmin/u6:latest not found: manifest unknown: manifest unknown")
        
        # 1.垃圾回收GC是一个比较危险的操作，该操作并不是事务性的，所以在进行 GC 的时候最好暂停 PUSH 镜像，以免把正在上传的镜像 layer 给 GC 掉
        # 2.C之后一定要重启，因为registry容器缓存了镜像layer的信息，当删除掉一个镜像A后边GC掉该镜像的layer之后，
        # 如果不重启 registry 容器，当重新PUSH镜像 A 的时候就会提示镜像 layer 已经存在，不会重新上传 layer 但实际上已经被 GC 掉了，
        # 最终会导致镜像 A 不完整无法 pull 到该镜像
        # ref: https://cloud.tencent.com/developer/article/2129675
        
        # 所以将垃圾回收改成config中的可选开关（默认关闭），定期手动开启进行GC，并在GC后重启registry
        if PROJ_CONFIG.GC_enable:
            #垃圾回收
            if PROJ_CONFIG.master_ip == PROJ_CONFIG.image_registry_ip:
                shell_execute(f"sudo docker exec {registry_container_name} "
                            f"bin/registry garbage-collect /etc/docker/registry/config.yml")

            else:
                shell_execute(f"sudo docker -H {PROJ_CONFIG.image_registry_ip}:"
                            f"{PROJ_CONFIG.remote_docker_daemon_port} "
                            f"exec {registry_container_name} bin/registry garbage-collect /etc/docker/registry/config.yml")
            # 重启registry容器
            shell_execute(f"sudo docker restart {registry_container_name}")
    except docker.errors.APIError as e:
        print(e)
        return str(e)


#删除文件。若删除失败，则需恢复前两步数据,若报错？？？

def del_folder(image_name,tag,username):
    #公有镜像删除需特殊处理
    if check_image_if_public(image_name,tag):
        user=get_public_image_user(image_name,tag)
        user_image_folder = (f"{IMAGE_REGISTRY_DIR}/{user}/"
                            f"{image_name}/{tag}")
    else:
        user_image_folder = (f"{IMAGE_REGISTRY_DIR}/{username}/"
                            f"{image_name}/{tag}")

    _del_image_files(user_image_folder)


#删除mysql数据，失败则会滚，并回复前三步数据
def del_mysql(image_name,tag,username):
    #公有镜像删除需特殊处理
    if check_image_if_public(image_name,tag):
        user=get_public_image_user(image_name,tag)
        user_id = get_user_id_by_user_name(user)
    else:
        user_id = get_user_id_by_user_name(username)
        
    delete_image_row(user_id, image_name, tag)
 
    
#（实验仓库专用）删除registry里的镜像，去掉了垃圾回收机制，暂时写死
# TODO(Wudx): 
# 从镜像仓库pull时遇到过bug，有时有些镜像无法成功pull
# 这样导致后续会报错，但重启容器registry后此问题得到解决
# 测试后发现可能是本地私仓删除镜像进行垃圾回收机制所导致
# 当删除私仓镜像时不采用垃圾回收机制就永远不会报错
# pull本地镜像仓库的bug，跟实验仓库代码无关，大多时候能正常运行
def del_registry_for_experi(image_name,tag,username):
    #命令：sudo docker -H 10.1.1.114:2375 exec registry(仓库容器的容器名) rm -rf /var/lib/registry/docker/registry/v2/repositories/msm123/tongbutest14(镜像名：user/Image_name)
    #如果镜像仓库就在本机，则不用10.1.1.114
    try:
        registry_container_name="registry"
        if check_image_if_public(image_name,tag):
            user=get_public_image_user(image_name,tag)
        else:
            user=username
        #删除镜像文件
        #如果镜像仓库创建在master主机上
        if PROJ_CONFIG.master_ip == PROJ_CONFIG.image_registry_ip:
            shell_execute(f"sudo docker exec {registry_container_name} "
                        f"rm -rf /var/lib/registry/docker/registry/v2/repositories/{user}/{image_name}")
        #镜像仓库和master不在同一本机
        else:
            shell_execute(f"sudo docker -H {PROJ_CONFIG.image_registry_ip}:"
                        f"{PROJ_CONFIG.remote_docker_daemon_port} "
                        f"exec {registry_container_name} rm -rf "
                        f"/var/lib/registry/docker/registry/v2/repositories/{user}/{image_name}")

    except docker.errors.APIError as e:
        print(e)
        return str(e)