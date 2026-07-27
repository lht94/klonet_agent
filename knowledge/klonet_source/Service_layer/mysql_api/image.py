from sqlalchemy import true
from ..mysql_manager import check_row_exists, delete
from ..mysql_models import Image
from ..mysql_manager import get_all_row,get_row
from ...Service_layer.mysql_api.user_login import get_user_id_by_user_name,get_user_name_by_user_id

def check_id_image_name_and_tag(user_id:int, image_name:str, tag:str):
    '''
    检查用户id-镜像名-tag的组合是否存在

    Args:
        user_id: 用户id
        image_name: 镜像名
        tag: 镜像tag

    Returns:
        如果指定组合存在则返回True，否则返回False
    '''
    return check_row_exists(Image, user_id=user_id, image_name=image_name, 
        tag=tag)

def check_image_name_and_tag(image_name:str, tag:str):
    '''
    检查镜像名-tag的组合是否存在

    Args:
        image_name: 镜像名
        tag: 镜像tag

    Returns:
        如果指定组合存在则返回True，否则返回False
    '''
    return check_row_exists(Image,image_name=image_name, 
        tag=tag)


def check_public_image(image_name:str, tag:str):
    '''
    检查共有仓库镜像名-tag的组合是否存在

    Args:
        image_name: 镜像名
        tag: 镜像tag

    Returns:
        如果指定组合存在则返回True，否则返回False
    '''
    public_image=get_image_by_is_public(is_public=1)
    for i in public_image:
        if i.image_name==image_name and i.tag==tag:
            return true

def check_image_if_public(image_name:str, tag:str):
    '''
    检查镜像是否为公有镜像

    Args:
        image_name: 镜像名
        tag: 镜像tag

    Returns:
        如果指定组合存在则返回True，否则返回False
    '''
    row=get_row(Image,image_name=image_name, tag=tag)

    return row.is_public

def get_public_image_user(image_name:str, tag:str):
    '''
    获取公有镜像的上传者用户名

    Args:
        image_name: 镜像名
        tag: 镜像tag

    Returns:
        如果指定组合存在则返回True，否则返回False
    '''

    row=get_row(Image,image_name=image_name, tag=tag)
    user=get_user_name_by_user_id(row.user_id)
    return user

def delete_image_row(user_id:int, image_name:str, tag:str):
    '''
    删除该用户的某镜像

    Args:
        user_id: 用户id
        image_name: 镜像名
        tag: 镜像tag

    '''
    return delete(Image, user_id=user_id, image_name=image_name, 
        tag=tag)

def get_image_by_user_id(user_id:int):
    '''
    通过user_id获取Image表的所有行

    Args:
        user_id: 用户id

    Return:
        Image实例/None
    '''
    return get_all_row(Image, user_id=user_id)


def get_image_by_is_public(is_public:int):
    '''
    通过is_public获取Image表的公有镜像行

    Args:
        is_public:公有镜像标志

    Return:
        Image实例/None
    '''
    return get_all_row(Image, is_public=is_public)


def get_image_cpu_and_memory(image_full_name:str):
    '''
    通过完整镜像名image_full_name获取镜像的资源需求信息
    
    Args:
        image_full_name: 完整镜像名称
    
    Returns:
        镜像的cpu需求和内存mem需求
    '''
    data=get_row(Image, image_full_name=image_full_name)

    return data.cpu,data.memory_requirements
