from ..Service_layer.image_registry_upload import create_image_object
from ..Service_layer.image_registry_commit import hum_convert
from ..Service_layer.mysql_manager import delete, get_row
from ..Implement_layer.LinkManager import shell_execute
from ..vemu_config.config import PROJ_CONFIG
from ..Service_layer.mysql_models import Experiment
from gevent import subprocess

import os

def create_image_info_by_topo(user, experiment_name, container_name, topo_info:dict):
    """
    根据topo_info、容器名、项目（拓扑）名生成相应即将上传到实验仓库节点的镜像的ORM对象
    
    Args：
    container_name: "", # 容器名
    topo_info: # 拓扑信息
    {
        例如：
        "hosts": {
            "h1": {
                "gateway": "",
                "image_name": "host/ubuntu",
                "interfaces": [
                    {
                        "ip": "192.168.3.2",
                        "name": "h1s2",
                        "netmask": "255.255.255.0"
                    }
                ],
                "linestyle": "solid",
                "name": "h1",
                "resource_limit": {
                    "cpu": "8",
                    "mem": "20"
                },
                "subtype": "ubuntu",
                "type": "host",
                "x": 259.514,
                "y": 678.8985
            }
        }
        ...
    }
    
    Returns:
        image: 镜像的ORM对象
    """
    # 查询相应节点信息
    for i, j in topo_info.items():
        if j.get(container_name):
            container = j.get(container_name)
            type = container['type']
            subtype = container['subtype']
            cpu = container['resource_limit']['cpu']
            mem = container['resource_limit']['mem']
            
    # 此处采用小写字符串"true""flase"，应该是由于接口考虑json中均为小写字符串的原因
    info = {
        "image_name": f"image_{experiment_name}_{container_name}",
        "user": user,
        "tag": "latest",
        "type": type,
        "subtype": subtype,
        "is_public": "false",
        "config": '{}',
        "edit_config": '{}',
        "customize_icon": "true",
        "cpu": cpu,
        "memory_requirements": mem
    }
    image = create_image_object(**info)
    
    #获取镜像大小
    try:
        size_num=shell_execute("sudo docker inspect -f {{\".Size\"}} "+f"{image.image_full_name}")
        size=hum_convert(float(size_num))

    except subprocess.CalledProcessError as e:
        result={}
        result['error_msg'] = "GET IMAGE SIZE when execute command '" + e.cmd + \
        "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
        return result

    image.size = str(size)
        
    return image


def all_ne_images_rename(user, experiment_name, NEs_class_info, topo_info):
    """
    对拓扑文件中所有节点的镜像来源image_name还有subtype进行重命名
    (便于后面部署拓扑取镜像时沿用之前的接口)
    
    Args：
    user: "", # 用户名
    experiment_name: "", # 实验名
    NEs_class_info: {}, # 将节点按类分好的字典集合
    topo_info: {}, # 拓扑描述信息
    
    """
    
    def _ne_image_rename(container, container_info):
        # 特殊处理需要对image_name加tag，后续可能会随前端统一而修改
        # 由于采用自定义镜像，拓扑中对image_name使用image_full_name的命名方式
        # 以便调用接口
        tag = "latest"
        image_name = f"image_{experiment_name}_{container}"
        image_full_name = (f"{PROJ_CONFIG.image_registry_ip}:"
                      f"{PROJ_CONFIG.image_registry_port}/{user}/"
                      f"{image_name}:{tag}")
        subtype = f"{image_name}_{tag}"
        container_info['image_name'] = image_full_name
        container_info['subtype'] = subtype
        
    # hosts
    if NEs_class_info['hosts']:
        for host in NEs_class_info['hosts']:
            _ne_image_rename(host, topo_info['hosts'][host])
    # switches
    if NEs_class_info['switches']:
        for switch in NEs_class_info['switches']:
            _ne_image_rename(switch, topo_info['switches'][switch])
    # routers
    if NEs_class_info['routers']:
        for router in NEs_class_info['routers']:
            _ne_image_rename(router, topo_info['routers'][router])
    # controllers
    if NEs_class_info['controllers']:
        for controller in NEs_class_info['controllers']:
            _ne_image_rename(controller, topo_info['controllers'][controller])
    # dpdks
    if NEs_class_info['dpdks']:
        for dpdk in NEs_class_info['dpdks']:
            _ne_image_rename(dpdk, topo_info['dpdks'][dpdk])
            
            
def get_all_images(topo_info):
    """
    根据mysql实验仓库中的topo信息，不分类别获取所有节点的完整镜像名
    
    参数为：
    topo_info: # 拓扑信息
    {
        例如：
        "hosts": {
            "h1": {
                "image_name": "192.168.2.5:5024/experiment_admin/image_experiment1_h1:latest",
                "type": "host",
                "subtype": "image_experiment1_h1_latest",
                "x": 520,
                "y": 330,
                "name": "h1",
                "resource_limit": {
                    "cpu": "8",
                    "mem": "20"
                },
                "linestyle": "solid",
                "gateway": "",
                "interfaces": [
                    {
                        "name": "h1s1",
                        "ip": "10.0.0.1",
                        "netmask": "255.255.255.0"
                    }
                ]
            },
        ...
    }
    
    Returns:
    images_list: {
        "host": 
        ["192.168.2.5:5024/experiment_admin/image_experiment1_h1:latest",
        "192.168.2.5:5024/experiment_admin/image_experiment1_h2:latest"],
        "switches":
        ["192.168.2.5:5024/experiment_admin/image_experiment1_s1:latest"],
        "controllers":
        ...
    }
    """
    images_list=[]
    for i, j in topo_info.items():
        if i != "links":
            if j:
                for k, container in j.items():
                    images_list.append(container['image_name'])
    print(images_list)
    return images_list
                
                
def del_scripts(experiment_name):
    """
    删除实验脚本文件
    
    参数为：
    experiment_name: "", 实验名
    """
    if get_row(Experiment, experiment_name=experiment_name).have_scripts:
        try:
            scripts = f"{experiment_name}_scripts.tar"
            os.remove(PROJ_CONFIG.static_scripts_dir + "/" + scripts)
        except Exception as e:
            raise ValueError(f"删除脚本文件{scripts}出错")
    
def del_experi_mysql(experiment_name):
    """
    从mysql中的删除实验数据
    
    参数为：
    experiment_name: "", 实验名
    """
    try:
        delete(Experiment, experiment_name=experiment_name)
    except Exception as e:
        raise ValueError(f"从mysql中删除实验{experiment_name}出错")