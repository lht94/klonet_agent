from ..Service_layer.mysql_models import KVMImage, UserInfo
from ..Service_layer.mysql_api.user_login import get_user_id_by_user_name
from ..Service_layer.mysql_api.user_info import get_user_info_by_user_name
from ..Service_layer.mysql_api.kvm_image import check_public_kvm_image, check_privite_kvm_image
from ..tools.file_tool import in_directory, clear_empty_directory
from .mysql_manager import get_row, check_row_exists
import os


def create_kvm_image_object(file_name, **image_args):
    '''
    创建kvm镜像的ORM对象
    
    Args:
        file_name: "", # 镜像名称
        image_args: # 镜像参数
            {
                "user": "", # 用户名
                "type": "", # 镜像类型
                "cpu": "", # 镜像CPU资源需求
                "mem": "", # 镜像内存资源需求
                "path": "", # 镜像存储路径（可选）
            }
    
    Returns:
        kvm_image: kvm镜像的ORM对象
        
    Rasies:
        ValueError: 查询数据库镜像名称重复时触发
    '''
    # 允许不同用户之间重名命名
    user_id = get_user_id_by_user_name(image_args["user"])
    if check_privite_kvm_image(user_id, file_name):
        raise ValueError("私有镜像名称重复，请修改镜像名后再重新上传")
    kvm_image = KVMImage()
    kvm_image.user_id = get_user_id_by_user_name(image_args["user"])
    kvm_image.image_name = file_name
    kvm_image.type = image_args["type"]
    kvm_image.cpu = image_args["cpu"]
    kvm_image.memory_requirements = image_args["mem"]
    kvm_image.path = image_args["path"]
    
    return kvm_image

def del_kvm_image(user_image_file_path, registry_user_path):
    '''
    删除master上镜像仓库的文件

    Args:
        user_image_file_path: "", # 镜像文件的完整路径
    
    '''
    try:
        if in_directory(user_image_file_path, registry_user_path):
            os.remove(user_image_file_path)
            clear_empty_directory(registry_user_path)
        else:
            raise ValueError(f"试图删除{user_image_file_path},"
                f"该文件夹是{registry_user_path}以外的文件夹")
    except FileNotFoundError:
        pass
    
def check_user_delete_image(user_name, file_name):
    '''
    删除镜像时，根据user-file_name优先检查数据库中是否有该镜像
    存在则再检查用户删除镜像的操作是否正确
    
    PS: 暂时放弃了公仓管理，意义不大
    
    Args:
        user_name: "", 用户名称
        file_name: "", 镜像文件名
        
    Returns:
        正确返回True，否则False; 同时返回实例信息
    '''
    user_id = get_user_info_by_user_name(user_name).user_id
    model_info = get_row(KVMImage, image_name=file_name, user_id=user_id)
    if not model_info:
        return False, model_info
    else:
        return True, model_info
        # user_id = get_user_info_by_user_name(user_name).user_id
        # # 如果是自己的镜像随便删除
        # if model_info.user_id == user_id:
        #     return True, model_info
        # else:
        #     if get_user_role_by_name(user_name) == 3:   # 超级管理员
        #         return True, model_info
        #     else:
        #         return False, model_info
        
def check_overlap_with_user_image(user_name, image_name):
    '''
    基于用户名-镜像名的组合检查是否重名
    
    Args:
        user_name: "", 用户名称
        image_name: "", 镜像名称
        
    Returns:
        重复返回False，不重复返回True
    '''
    user_id = get_user_info_by_user_name(user_name).user_id
    if check_row_exists(KVMImage, user_id=user_id, image_name=image_name):
        return False
    else:
        return True
    
def get_default_kvm_image_cpu_and_mem(image_name):
    '''
    根据镜像名称从kvm_image_list.json中获取默认镜像的资源信息
    
    Args:
        image_name: "", 镜像名称
    
    Returns:
        返回镜像的cpu和mem信息
    '''
    with open("../webserver/api/image/kvm_image_list.json") as f:
        info = f.read()
        for type_images in info.values():
            for image in type_images:
                if image["image_name"] == image_name:
                    return image["cpu"], image["mem"]
                
def get_KVM_image_cpu_and_mem(user, image_name: str):
    '''
    通过用户名-镜像名在mysql中查找单个镜像的资源需求信息
    
    Args:
        user: 用户名
        image_name: 镜像名
    Returns:
        镜像的cpu（核心个数）和mem需求
    '''
    user_id = get_row(UserInfo, name=user).user_id
    data = get_row(KVMImage, user_id=user_id, image_name=image_name)
    return data.cpu, data.memory_requirements