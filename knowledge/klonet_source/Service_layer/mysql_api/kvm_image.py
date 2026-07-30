from ..mysql_manager import check_row_exists, delete, get_all_row
from ..mysql_models import KVMImage

def check_public_kvm_image(file_name):
    '''
    检查公有kvm镜像是否重复
    
    Args:
        file_name: "", # 镜像文件名
        
    Returns:
        存在返回True，否则False
    '''
    return check_row_exists(KVMImage, image_name=file_name)

def check_privite_kvm_image(user_id, file_name):
    '''
    检查私有kvm镜像是否重复(用户id-镜像名组合）
    
    Args:
        user_id: (int), # 用户id
        file_name: "", # 镜像文件名
        
    Returns:
        存在返回True，否则False
    '''
    return check_row_exists(KVMImage, user_id=user_id, image_name=file_name)

def delete_kvm_image_mysql_row(image_id):
    '''
    根据镜像ID删除数据库中的相关信息
    
    Args:
        image_id: (int), # 镜像ID
        
    Returns:
        删除成功返回True，否则False
    '''
    return delete(KVMImage, image_id=image_id)

def get_all_kvm_image_by_user_id(user_id):
    '''
    根据用户ID获取数据库中用户名下所有可用KVM镜像
    
    Args:
        user_id: (int), # 用户ID
        
    Returns:
        由所有的镜像model实例构成的列表
    '''
    return get_all_row(KVMImage, user_id=user_id)

def get_all_web_image():
    '''
    不区分用户，获取数据库中所有web端上传镜像
    
    Returns:
        由所有的web端上传镜像model实例构成的列表
    '''
    return get_all_row(KVMImage, path="default")

def get_all_self_image():
    '''
    不区分用户，获取数据库中所有非web端上传的镜像
    
    Returns:
        由所有非web端上传的镜像model实例构成的列表
    '''
    return get_all_row(KVMImage, KVMImage.path!="default")