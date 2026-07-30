from ...webserver import mysql
from ..mysql_models import RoleAuthority, Roles, UserInfo, UserRole, Authorities
from ..mysql_manager import count
from ...webserver.web_back.authority_management.initial_authority import initial_role_authority, initial_roles, initial_authorities

def check_authority_by_user_id(user_id:int, authority_id:int):
    '''
    通过用户id查询指定权限是否存在

    Args:
        user_id: 用户id
        authority_name: 权限名

    Returns:
        如果指定权限存在则返回True，否则返回False
    '''
    # UserRole表 用户id -> 角色id   
    #                           }--> RoleAuthority表 是否存在角色id-权限id这条表项
    #                     权限id
    try:
        q = mysql.session.query(RoleAuthority).join(
                UserRole, UserRole.role_id==RoleAuthority.role_id
                ).filter(UserRole.user_id==user_id,
                    RoleAuthority.authority_id==authority_id)
        is_authority_exists = mysql.session.query(q.exists()).scalar()

        return True if is_authority_exists else False
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_role_id_by_role_name(role_name:str) -> int:
    '''
    通过角色名获取角色id

    Args:
        role_name: 角色名

    Returns:
        role_id/None
    '''
    try:
        return mysql.session.query(Roles.role_id).filter_by(
            role_name=role_name).scalar()
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_authority_id_by_authority_name(authority_name:str) -> int:
    '''
    通过权限名获取权限id

    Args:
        role_name: 角色名

    Returns:
        role_id/None
    '''
    try:
        return mysql.session.query(Authorities.authority_id).filter_by(
            authority_name=authority_name).scalar()
    except Exception as e:
        mysql.session.rollback()
        raise e

def init_tables():
    '''
    初始化角色及权限相关表项
    查询角色表、权限表、角色_权限表是否为空，若为空则进行对表项进行初始化
    '''
    if (count(Roles) == 0 and count(Authorities) == 0 and
        count(RoleAuthority) == 0):
        try:
            print("init authority tables...")
            add_role(initial_roles, commit=False)
            add_authority(initial_authorities, commit=False)
            for role_name, authorities in initial_role_authority.items():
                bind_authorities_to_role(role_name, authorities, commit=False)
            mysql.session.commit()
            print("init done!")
        except:
            mysql.session.rollback()
            raise RuntimeError("权限相关表项初始化失败")

def add_role(roles:list, commit=True):
    '''
    添加角色

    Args:
        roles: 列表，其元素为角色名
        commit: 是否在函数内进行commit，默认为True
    '''
    try:
        for role_name in roles:
            role_model = Roles()
            role_model.role_name = role_name
            mysql.session.add(role_model)
        if commit:
            mysql.session.commit()
    except Exception as e:
        mysql.session.rollback()
        raise e

def add_authority(authorities:list, commit=True):
    '''
    添加权限

    Args:
        authorities: 列表，其元素为权限名
        commit: 是否在函数内进行commit，默认为True
    '''
    try:
        for authority_name in authorities:
            authority_model = Authorities()
            authority_model.authority_name = authority_name
            mysql.session.add(authority_model)

        if commit:
            mysql.session.commit()
    except Exception as e:
        mysql.session.rollback()
        raise e

def bind_user_to_roles(user_id:int, roles:str, commit=True):
    '''
    绑定用户至数个角色

    Args:
        user_name: 用户名
        roles: 列表，其元素为角色名
        commit: 是否在函数内进行commit，默认为True
    '''
    try:
        for role_name in roles:
            role_id = get_role_id_by_role_name(role_name)
            user_role_model = UserRole()
            user_role_model.user_id = user_id
            user_role_model.role_id = role_id
            mysql.session.add(user_role_model)

        if commit:
            mysql.session.commit()
    except Exception as e:
        mysql.session.rollback()
        raise e

def bind_authorities_to_role(role_name:str, authorities:list,commit=True):
    '''
    绑定数个权限至角色

    Args:
        role_name: 角色名
        authorities: 列表，其元素为权限名
        commit: 是否在函数内进行commit，默认为True
    '''
    try:
        role_id = get_role_id_by_role_name(role_name)
        for authority in authorities:
            role_authority_model = RoleAuthority()
            authority_id = get_authority_id_by_authority_name(authority)
            role_authority_model.role_id = role_id
            role_authority_model.authority_id = authority_id
            mysql.session.add(role_authority_model)

        if commit:
            mysql.session.commit()
    except Exception as e:
        mysql.session.rollback()
        raise e