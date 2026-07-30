import docker
import shutil
import os
import requests
from sqlalchemy import true
from ..tools.file_tool import in_directory
from ..webserver import mysql
from ..Service_layer import mysql_models
from ..Service_layer.mysql_api.user_login import get_user_id_by_user_name
from ..webserver.app_factory import dockerfiles, icons, attachments
from ..vemu_config.config import PROJ_CONFIG
from ..tools.tools import str2bool, str2dict
from ..Service_layer.mysql_api.image import check_id_image_name_and_tag,check_public_image
from ..Service_layer.redisAPI import WorkerRedis
from ..Implement_layer.LinkManager import shell_execute
from gevent import subprocess

IMAGE_REGISTRY_DIR = PROJ_CONFIG.image_registry_dir

def upload_image(image_tar, dockerfile,icon, **image_args):
    '''
    通过dockerfil或镜像tar的方式上传镜像

    Args:
        # TODO: 镜像压缩文件的格式是不是不止tar?可能还有gzip?是的话这个变量都得改名
        image_tar: 由docker save命令生成的镜像tar压缩文件 
        dockerfile: dockerfile
        attachment: 附件（压缩文件）
        icon: 图标文件（图像文件）
        image_args: 镜像的参数，细节为
            {
                "user": "", # 用户名
                "image_name":"", # 镜像名
                "tag":"", # 镜像TAG，默认值：latest
                "build_args":"", # 构建参数
                "type":"", # 类型
                "subtype":"", # 子类型
                "is_public": True/False, # 是否为公共镜像
                "edit_config": {},  # bianji jiemian canshu
                "config": {}, # 镜像配置
                "customize_icon": True 
            }

    raises:
        ValueError: 需上传image_tar或dockerfile中的一个
    '''
    docker_client = docker.from_env()
 #  result = docker_client.images.get_registry_data("220.243.137.36:10024/mt/test_image:v0.1")
 #   reference = result.id
    


    if (not image_tar and not dockerfile) or (image_tar and dockerfile):
        raise ValueError("需上传image_tar或dockerfile中的一个！")
    
    # TODO(MaTie, 20210520): 用户上传的文件很危险，需进行安全性检查
    # 用户镜像文件夹：vemu_uestc/<用户名>/<镜像名>/<tag>/
    
    user_image_folder = (f"{IMAGE_REGISTRY_DIR}/{image_args['user']}/"
                        f"{image_args['image_name']}/{image_args['tag']}")
    
    try:

        image = create_image_object(**image_args)
        # save_image_files(user_image_folder, image_tar, dockerfile,
        #     icon)
        save_image_files(user_image_folder, image_tar, dockerfile)

        # 构建/加载
        image_full_name = (f"{PROJ_CONFIG.image_registry_ip}:"
            f"{PROJ_CONFIG.image_registry_port}/{image_args['user']}/"
            f"{image_args['image_name']}:{image_args['tag']}")
        docker_client = docker.from_env()
        if image_tar:
            print("upload_image_by_tar")
            # tar文件里其实是有镜像的名字和tag信息的
            # 但还是让用户输入镜像的名字和tag，否则保存文件时不好保存，还需额外处理
            load_image(docker_client, user_image_folder, image_args['user'],
                image_args['image_name'], image_args['tag'])
        elif dockerfile:
            print("upload_image_by_dockerfile")
            build_image(docker_client, user_image_folder, image_full_name, 
                image_args["build_args"])
        
        push_image(docker_client, image_full_name)
        pull_image(image_full_name)


    #获取镜像大小
        try:
            size_num=shell_execute("sudo docker inspect -f {{\".Size\"}} "+f"{image_full_name}")
            size=hum_convert(float(size_num))

        except subprocess.CalledProcessError as e:
            result={}
            result['error_msg'] = "GET IMAGE SIZE when execute command '" + e.cmd + \
            "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
            return result

        image.size = str(size)



        # 没有问题则提交，否则回滚
        mysql.session.add(image)
        mysql.session.commit()
    except Exception as e:
        # 失败则删除文件并回滚mysql事务
        _del_image_files(user_image_folder)
        mysql.session.rollback()
        raise e
    finally:
        # TODO: 构建成功后将attachment删除，dockerfile和icon予以保留
        pass

def hum_convert(value):
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    #本来应是1024，但为了和docker images 命令出来的size大小保持一致，设为1000
    size = 1000.0
    for i in range(len(units)):
        if (value / size) < 1:
            return "%.2f%s" % (value, units[i])
        value = value / size       
        
def save_image_files(user_image_folder, image_tar, dockerfile):
    # save_image_files(user_image_folder, image_tar, dockerfile, attachment,
    # icon):
    '''
    保存用户上传的镜像相关文件，包括合法性检查。
    TODO: 可接受的文件类型的判断和考虑。可否通过改upload插件里extension的方式实现？

    Args:
        user_image_folder: 镜像仓库文件夹名
        image_tar: 由docker save命令生成的镜像tar压缩文件
        dockerfile: dockerfile文件，werkzeug.FileStorage类型
        attachment: 附件文件，werkzeug.FileStorage类型
        icon: 图标文件，werkzeug.FileStorage类型

    Returns:
        None

    Raises:
        UploadNotAllowed: 上传的文件不被允许
    '''
    if image_tar:
        # TODO(mt, 20210525): 还差对tar的类型检测
        dockerfiles.save(image_tar, folder=user_image_folder, name="image.tar")
    # dockerfile和icon改名，便于统一管理；attahments不改名，因为构建时就删了
    if dockerfile:
        dockerfiles.save(dockerfile, folder=user_image_folder, 
            name="dockerfile")
    #attachment暂时不要
    # if attachment:
    #     attachments.save(attachment, folder=user_image_folder, name="")
    # 判断icon文件类型 
    # icon暂时不要  
    # if icon:
    #     icons.save(icon, folder=user_image_folder, name=icon.filename)
    #     # 先存后改名：便于使用save函数中的类型判断功能
    #     icon_type = os.path.splitext(icon.filename)[1]
    #     os.rename(f"{user_image_folder}/{icon.filename}",
    #             f"{user_image_folder}/icon{icon_type}")
    print("save success")


def _del_image_files(user_image_folder):
    '''
    删除指定镜像的文件夹（包含其目录下的所有文件及文件夹）。
    若文件夹不存在，不报错
    若用户文件夹为空，则删除这个空文件夹

    由于该API很危险，因此只是采用再写一遍的方式而不是调用API（保存已创建项目为已保存
    项目处也有使用）

    Args:
        user_image_folder: 镜像仓库文件夹名

    Returns:
        None
    '''
    try:
        # 该判断很重要！！！！请勿去掉！！！！
        if in_directory(user_image_folder, IMAGE_REGISTRY_DIR):
            # 删除user_image_folder文件夹及其所有内容
            shutil.rmtree(user_image_folder)

            # 若用户文件夹为空，则删除这个空文件夹
            user_folder = os.path.dirname(user_image_folder)
            if not os.listdir(user_folder):
                os.rmdir(user_folder) # 只会删除空文件夹，较为安全
        else:
            raise ValueError(f"试图删除{user_image_folder},"
                f"该文件夹是{IMAGE_REGISTRY_DIR}以外的文件夹")
    except FileNotFoundError:
        pass

def create_image_object(**image_args):
    '''
    创建镜像的ORM对象

    Args:
        image_args: 镜像的参数, 细节为
            {
                "user": "", # 用户名
                "image_name":"", # 镜像名
                "tag":"", # 镜像TAG，默认值：latest
                "build_args":"", # 构建参数
                "type":"", # 类型
                "subtype":"", # 子类型
                "is_public": True/False, # 是否为公共镜像
                "edit_config": {},  #
                "config": {}, # 镜像配置
                "customize_icon": True 
            }

    Returns:
        image: 镜像的ORM对象

    Raises:
        ValueError: 用户名-镜像-TAG的组合重复时触发
    '''

    if image_args["is_public"]=="true":
        if check_public_image(image_args["image_name"], image_args["tag"]) :
            raise ValueError(f"镜像重复提示：{image_args['image_name']}:{image_args['tag']}"
                "已存在，请修改镜像名或TAG标签后重新上传！")
    else:
        user_id = get_user_id_by_user_name(image_args["user"])
        if check_id_image_name_and_tag(user_id, image_args["image_name"], 
                                    image_args["tag"]):
            raise ValueError(f"镜像重复提示：{image_args['image_name']}:{image_args['tag']}"
                "已存在，请修改镜像名或TAG标签后重新上传！")
    image = mysql_models.Image()

    image.user_id = get_user_id_by_user_name(image_args["user"])
    image.image_name = image_args["image_name"]
    image.tag = image_args["tag"]
    image.type = image_args["type"]
    image.subtype = image_args["subtype"]
    image.is_public = str2bool(image_args["is_public"])
    image.edit_config = str2dict(image_args["edit_config"])
    image.config = str2dict(image_args["config"])
    image.customize_icon = str2bool(image_args["customize_icon"])
    image.cpu = image_args["cpu"]
    image.memory_requirements = image_args["memory_requirements"]

    user=image_args["user"]
    image.image_full_name = (f"{PROJ_CONFIG.image_registry_ip}:{PROJ_CONFIG.image_registry_port}"
                            f"/{user}/{image.image_name}:{image.tag}")

    return image

def build_image(docker_client, user_image_folder, image_full_name, 
    build_args=None):
    '''
    通过dockerfile来构建镜像，并上传至镜像仓库
    TODO(mt, 20210523): build_args功能未测试，注释

    Args:
        docker_client: DockerClient对象
        user_image_folder: 镜像仓库文件夹名
        image_full_name: 完整的镜像名，如192.168.1.44:30000/mt/myimage:v1.0
        build_args: 镜像构建参数
    '''

    if build_args == None:
        imageTuple = docker_client.images.build(path=user_image_folder, 
            tag=image_full_name, quiet=True, forcerm=True,timeout=30)
    else:
        dict_build_args=eval(build_args) # 将参数强制字典化，后续可用jsonschema来检查
        imageTuple = docker_client.images.build(path=user_image_folder, 
            tag=image_full_name, quiet=True, forcerm=True, buildargs=dict_build_args)

    print('imageTuple:', imageTuple)
    print("build success")

def load_image(docker_client, user_image_folder, user, image_name, tag):
    '''
    加载镜像tar压缩文件为镜像

    Args:
        docker_client: DockerClient对象
        user_image_folder: 镜像仓库文件夹名
        image_full_name: 完整的镜像名，如192.168.1.44:30000/mt/myimage:v1.0
    '''
    with open(f"{user_image_folder}/image.tar", "rb") as f:
        # TODO(mt, 20210525): 这个函数为什么有时会返回多个镜像？
        images = docker_client.images.load(f.read())
    # if len(images) > 1:
    #     raise RuntimeError("docker load返回的镜像大于一个！")
    # 按照用户填写的信息对镜像进行更名
    images[0].tag(f"{PROJ_CONFIG.image_registry_ip}:"
            f"{PROJ_CONFIG.image_registry_port}/{user}/{image_name}", tag=tag) # TODO: type/image_name

    print(images)
    print("load success")

WORKER_LIST_TABLE_NAME = PROJ_CONFIG.worker_list
WORKER_PORT = PROJ_CONFIG.worker_port
class MyWorkerRedis(WorkerRedis):
    def get_all_workers(self):
        return list(self._db_conn.smembers(WORKER_LIST_TABLE_NAME)) 
    
def push_image(docker_client, image_full_name):
    '''
    上传镜像至镜像仓库

    Args:
        docker_client: DockerClient对象
        image_full_name: 完整的镜像名，如192.168.1.44:30000/mt/myimage:v1.0
    
    Raises:
        docker.errors.APIError: If the server returns an error.
    '''
    try:
        for line in docker_client.api.push(image_full_name, stream=True, 
                                        decode=True):
            print(line)

        print("push success")
    except Exception as e:
        raise e

def pull_image(image_full_name):

    def err_handler(request,exception):
        print('发生异常，具体信息为：',exception)

    worker_redis = MyWorkerRedis()
    print(worker_redis)
    worker_list = worker_redis.get_all_workers()    
    print("worker list:", worker_list)
    resps = []
    for worker_ip in worker_list:
        worker_url = f"http://{worker_ip}:{WORKER_PORT}/image/pull/"
        print(worker_url)
        info_dict = {
            "image_full_name":image_full_name
        }
        resp = requests.post(worker_url, json=info_dict)
        resps.append(resp)
    # resps = grequests.map(reqs,exception_handler=err_handler)
    print(resps)
    for resp in resps:
        if resp.json()["code"] != 1:
            print(f"pull拉取失败")
            print(resp)
            raise ValueError(f"镜像同步失败：服务器同步拉取该镜像失败，该镜像上传失败，请更改检查文件后重新上传！")
            
    print("pull success")
    
def pull_image_by_noPost(docker_client, image_full_name):
    '''
    不通过post请求的方式从镜像仓库pull镜像
    （为缓解gunnicorn因嵌套请求可能导致阻塞卡住）

    Args:
        docker_client: DockerClient对象
        image_full_name: 完整的镜像名，如192.168.1.44:30000/mt/myimage:v1.0
    
    Raises:
        docker.errors.APIError: If the server returns an error.
    '''
    try:
        res = docker_client.api.pull(image_full_name, tag=None)
        print(res)
        print("pull success")
    except Exception as e:
        raise e
    
    