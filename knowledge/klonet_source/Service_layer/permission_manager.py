from vemu_uestc.Service_layer.redisAPI import UserMapRedis
from .mysql_models import RoleAuthority, UserLogin, UserRole, Authorities, UserInfo, Image, Experiment
from ..webserver import mysql
from flask_login import current_user
from ..Function_layer.deployed_proj_manager import retrieve_topo_list
from ..vemu_config.config import PROJ_CONFIG
import requests
from ..tools.context import user_map_redis_context
from ..Service_layer.mysql_api.user_info import get_user_name_by_user_id


def check_user_exist(check_name):
    """
    检查name用户是否在数据库中存在

    Args:
        check_name: 要检查的用户名
    """
    return mysql.session.query(mysql.session.query(UserLogin).filter(UserLogin.name == check_name).exists()).scalar()
    

def check_permission(name, func_name):
    """
    检查name用户是否拥有func_name方法

    Args:
        name: 当前用户名
        func_name: 要检查的方法名称, 例如: UserLoginAPI.get

    Return:
        is_authority_exists: True表示有权限, False表示无权限
    """
    print(f'name={name} has permission of func={func_name}')
    try:
        # 按角色名和权限来查询角色是否有对应的权限，如果没有返回false，有就返回true
        user_id = mysql.session.query(UserLogin).filter(UserLogin.name == name).scalar().user_id
        # 当前所调用方法的authority_id
        func_auth_id = mysql.session.query(Authorities).filter(Authorities.authority_name == func_name).all()[0].authority_id
        # print("当前所调用方法的authority_id： ",func_auth_id)
    except Exception as e:
        mysql.session.rollback()
        raise e
    # 有user_id authority_id，判断权限是否存在
    is_authority_exists = False
    try:
        search_cmd = mysql.session.query(RoleAuthority).join(UserRole, UserRole.role_id == RoleAuthority.role_id).filter(UserRole.user_id == user_id, RoleAuthority.authority_id == func_auth_id)
        is_authority_exists = mysql.session.query(search_cmd.exists()).scalar()
        return is_authority_exists
    except Exception as e:
        mysql.session.rollback()
        raise e

def compare_role(user, name):
    """
    检查user用户的权限是否高于name用户

    Args:
        user: 用户1
        name: 用户2

    Return:
        is_user_higher: True表示更高, False平级或者更低
    """
    try:
        # 获取两个用户的user_id
        user_id = mysql.session.query(UserLogin).filter(UserLogin.name == user).scalar().user_id
        name_id = mysql.session.query(UserLogin).filter(UserLogin.name == name).scalar().user_id
    except Exception as e:
        mysql.session.rollback()
        raise e
    is_user_higher = False
    try:
        user_role = mysql.session.query(UserRole).filter(UserRole.user_id == user_id).scalar().role_id
        name_role = mysql.session.query(UserRole).filter(UserRole.user_id == name_id).scalar().role_id
        if int(user_role) > int(name_role):
            is_user_higher =True
        return is_user_higher
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_all_user():
    """
    返回数据库中现有的所有user_id和name和role_id

    Return:
        str形式, [(12, 'MT'), (23, 'yu'), (70, 'tbb')]
    """
    try:
        # 超级管理员不能删除超级管理员，所以这里不返回超级管理员
        return mysql.session.query(UserLogin.user_id, UserLogin.name, UserRole.role_id).join(UserRole, UserLogin.user_id == UserRole.user_id).filter(UserRole.role_id != 3).all()
    except Exception as e:
        mysql.session.rollback()
        raise e


def super_delete(name, delete_name):
    """
    通过用户名删除和用户有关的数据库表项

    Args:
        name: 当前用户名
        delete_name: 要删除的用户名

    Return:
        func_name: 要检查的方法名称, 例如: UserLoginAPI.get
    """
    # 先检查是否有这个user
    if not check_user_exist(delete_name):
        return {
            "code": 0,
            "msg": f'No user called {delete_name}!'
        }

    # 如果当前用户没有删除其他用户的权限，直接返回False
    if not check_permission(name, func_name="SuperDelete.post"):
        return {
            "code": 0,
            "msg": f'{name} don\'t have permission to delete user!'
        }

    # 删除用户的拓扑
    try:  
        topo_list = retrieve_topo_list(delete_name)['topo_list']
        for topo in topo_list:
            delete_url = f'http://127.0.0.1:{PROJ_CONFIG.master_port}/master/topo/'
            delete_json = {"user":delete_name, "topo": topo}
            requests.delete(delete_url, json=delete_json)
    except Exception as e:
        print(f'Delete topo error in super_delete()!')
        raise e
    
    # 删除delete_name用户的表象，包括UserRole、UserInfo、UserLogin
    try:  
        user_id = mysql.session.query(UserLogin).filter(UserLogin.name == delete_name).scalar().user_id  # tb：取消注释即真删除
        mysql.session.query(UserLogin).filter(UserLogin.name == delete_name).delete()
        mysql.session.query(UserInfo).filter(UserInfo.user_id == user_id).delete()
        mysql.session.query(UserRole).filter(UserRole.user_id == user_id).delete()
        mysql.session.commit()
        
    except Exception as e:
        mysql.session.rollback()
        return {
            'code': 0,
            'msg': 'Delete mysql error!'
        }
    
    # 将redis中关于本用户的东西 （1）DB0里的用户与DB映射；（2）对应DB里的数据flash掉，这两个用del_user_db就可以都做完了。
    with user_map_redis_context() as user_db_map:
        if not user_db_map.del_user_db(delete_name) == 1:
            return {
                'code': 0,
                'msg': 'Delete user db error!'
            }
    return {
        "code": 1,
        'msg': f'User={delete_name} has been deleted!'
    }
    

def get_user_role_by_name(user_name):
    """
    返回int类型的当前角色类型

    Args:
        user_name: 要查询角色的用户名

    Return: 
        1/2/3: 分别代表ordinary_user/admin/super_admin
    """
    name = user_name
    try:
        return mysql.session.query(UserRole.role_id).join(UserLogin, UserLogin.user_id == UserRole.user_id).filter(UserLogin.name == name).first()[0]
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_curr_user_role():
    """
    返回int类型的当前角色类型

    Return: 
        1/2/3: 分别代表ordinary_user/admin/super_admin
    """
    name = current_user.name
    try:
        return mysql.session.query(UserRole.role_id).join(UserLogin, UserLogin.user_id == UserRole.user_id).filter(UserLogin.name == name).first()[0]
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_all_images():
    """
    返回当前所有的镜像

    Return: 
        [(user_id, user_name, image_nam),(user_id, user_name, image_nam)……], 如[(5, 'msm123', 'duijie3'), (5, 'msm123', 'duijie')]
    """
    try:
        return mysql.session.query(Image.user_id, UserLogin.name, Image.image_name, Image.is_public, Image.tag).join(UserLogin, UserLogin.user_id == Image.user_id).filter(Image.user_id).all()
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_user_images(name):
    """
    返回当前用户拥有的镜像

    Return: 
        [(user_id, user_name, image_nam),(user_id, user_name, image_nam)……],如[(5, 'msm123', 'duijie3'), (5, 'msm123', 'duijie')]
    """
    try:
        user_id = mysql.session.query(UserInfo.user_id).filter(UserInfo.name == name)
        return mysql.session.query(Image.image_name, Image.tag, Image.type, Image.subtype, Image.size, Image.time, Image.is_public).join(UserLogin, UserLogin.user_id == Image.user_id).filter(Image.user_id == user_id).all()
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_current_image_list(name):
    """
    返回任意角色的镜像列表
    """
    role_id = get_user_role_by_name(name)
    if role_id == 3:
        return {"code": 1, "msg": "ok!", "image_list": get_all_images()}
    elif role_id == 2:
        # TODO 查到当前用户的镜像并返回
        return {"code": 1, "msg": "ok!", "image_list": get_user_images(name)}
    else: 
        return {"code": 0, "msg": "Permission denied!"}

def get_user_id_by_name(user_name):
    """
    根据用户名查找user_id

    Args:
        user_name: 用户名
    Return:
        role_id: int型id
    """
    name = user_name
    try:
        role_id = mysql.session.query(UserLogin.user_id).filter(UserLogin.name == name).first()[0]
        return role_id
    except Exception as e:
        mysql.session.rollback()
        raise e


def change_user_role(user_name, role_id):
    """
    改变用户的角色

    Args:
        user_name: 用户名
        role_id: 要变为的角色
    Return:
        True: 成功
        :失败则抛错
    """
    name = user_name
    change_id = role_id

    try:
        # try里修改了UserRole表的role_id
        user_id = get_user_id_by_name(name)
        user_role_item = mysql.session.query(UserRole).filter(UserRole.user_id == user_id).one()
        user_role_item.role_id = change_id
        mysql.session.commit()
        print("now role_id is =", mysql.session.query(UserRole).filter(UserRole.user_id == user_id).one().role_id)
        return True
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_private_image(name):
    """
    得到所有当前用户镜像
    """
    try:
        user_id = get_user_id_by_name(name)
        image_item = mysql.session.query(Image.image_name, Image.tag, Image.type, Image.subtype, Image.size, Image.time).filter(Image.user_id == user_id, Image.is_public == 0).all()
        ret = []
        for item in image_item:
            tmp = []
            item = tuple(item)
            for index in range(len(item)):
                if index == 5:
                    tmp.append(str(item[index]))
                else:
                    tmp.append(item[index])
            ret.append(tmp)        
        return {
            "code": 1,
            "msg": "ok!",
            "image_list": ret
        }
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_public_image():
    """
    得到所有共有仓库镜像
    """
    try:
        image_item = mysql.session.query(Image.image_name, Image.tag, Image.type, Image.subtype, Image.size, Image.time).filter(Image.is_public == 1).all()
        ret = []
        for item in image_item:
            tmp = []
            item = tuple(item)
            for index in range(len(item)):
                if index == 5:
                    tmp.append(str(item[index]))
                else:
                    tmp.append(item[index])
            ret.append(tmp)
        return {
            "code": 1,
            "msg": "ok!",
            "image_list": ret
        }
    except Exception as e:
        mysql.session.rollback()
        raise e
    
def get_experiments():
    """
    得到实验仓库所有实验
    """
    try:
        experi_item = mysql.session.query(Experiment.experiment_name, Experiment.user_id, Experiment.create_time, Experiment.have_scripts).all()
        ret = []
        print(experi_item)
        for item in experi_item:
            tmp = []
            item = tuple(item)
            for index in range(len(item)):
                if index == 1:
                    tmp.append(get_user_name_by_user_id(item[index]))
                elif index == 2:
                    tmp.append(str(item[index]))
                elif index == 3:
                    if str(item[index]) == "True":
                        tmp.append("是")
                    else:
                        tmp.append("否")
                else:
                    tmp.append(item[index])
            ret.append(tmp)
        print(ret)
        return {
            "code": 1,
            "msg": "ok!",
            "experi_list":ret
        }
    except Exception as e:
        mysql.session.rollback()
        raise e