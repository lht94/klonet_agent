import requests
from ..webserver import mysql
from ..vemu_config.config import PROJ_CONFIG
from .redisAPI import UserMapRedis
from ..Service_layer.image_registry_upload import create_image_object
from ..Implement_layer.LinkManager import shell_execute
from gevent import subprocess


IMAGE_REGISTRY_DIR = PROJ_CONFIG.image_registry_dir
user_db_map = UserMapRedis()

def commit_image( **image_args):
    '''
    通过将本机的容器commit为镜像后上传至仓库

    Args:
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
                "upload _type":# 上传方式
                "project_name":# 项目名称
                "container_name":# 容器名
            }

    '''

    
    try:

        image = create_image_object(**image_args)

        # 构建/加载
        image_full_name = (f"{PROJ_CONFIG.image_registry_ip}:"
            f"{PROJ_CONFIG.image_registry_port}/{image_args['user']}/"
            f"{image_args['image_name']}:{image_args['tag']}")

 
        #定位到容器具体的worker，发送命令给该worker执行命令
        user_db_cli = user_db_map.get_user_db(image_args['user'])
        ne_loc = user_db_cli.get_value(f"{image_args['project_name']}_{image_args['container_name']}", 'NEloc')
        worker_ip = user_db_cli.get_value("subtopo2worker", ne_loc)
        print("该镜像对应的worker_IP为:",worker_ip)
        url = f"http://{worker_ip}:{PROJ_CONFIG.worker_port}/image/commit/"
        commit_info = {
                "image_full_name":image_full_name,
                "user":image_args['user'],
                "project_name":image_args['project_name'],
                "container_name":image_args['container_name']
                    }
        resp = requests.post(url, json=commit_info)
        if resp.json()["code"] != 1:
             print(f"commit容器失败")


        # #获取镜像大小
        # image_full_name_no_tag = (f"{PROJ_CONFIG.image_registry_ip}:"
        #     f"{PROJ_CONFIG.image_registry_port}/{image_args['user']}/"
        #     f"{image_args['image_name']}")
        # print(image_full_name_no_tag)
        # try:
        #     size=shell_execute(f"sudo docker images | grep -w {image_full_name_no_tag} "
        #                         f"|grep {image_args['tag']}| awk {{'print $7'}}")
        # except subprocess.CalledProcessError as e:
        #     result={}
        #     result['error_msg'] = "GET IMAGE SIZE when execute command '" + e.cmd + \
        #     "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
        #     return result
        # image.size = str(size)

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
        mysql.session.rollback()
        raise e
    finally:
        pass

def hum_convert(value):
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    #本来应是1024，但为了和docker images 命令出来的size大小保持一致，设为1000
    size = 1000.0
    for i in range(len(units)):
        if (value / size) < 1:
            return "%.2f%s" % (value, units[i])
        value = value / size