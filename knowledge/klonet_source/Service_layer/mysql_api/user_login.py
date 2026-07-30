from ..mysql_manager import get_row_by_pk_id, get_row
from ..mysql_models import UserLogin

def get_user_login_by_user_id(user_id:int):
    '''
    通过user_id获取UserLogin表的一行

    Args:
        user_id: 用户id

    Return:
        UserLogin实例/None
    '''
    return get_row_by_pk_id(UserLogin, user_id)

def get_user_login_by_user_name(user_name:str):
    '''
    通过user_name获取UserLogin表中的一行

    Args:
        user_name: 用户名

    Return:
        UserLogin实例/None
    '''
    return get_row(UserLogin, name=user_name)

def get_user_name_by_user_id(user_id:int):
    '''
    通过user_id获取user_name

    Args:
        user_id: 用户id

    Return:
        user_name: 用户名

    Raises:
        ValueError: user_id的UserLogin表不存在
    '''
    user_login = get_row_by_pk_id(UserLogin, user_id)
    if user_login:
        return user_login.name
    else:
        raise ValueError(f"user_id[{user_id}]的UserLogin表不存在！")

def get_user_id_by_user_name(user_name) -> int:
    '''
    通过user_name获取user_id

    Args:
        user_name: 用户名

    Return:
        user_id: 用户id

    Raises:
        ValueError: f"用户 {user_name} 不存在！"
    '''
    user_login = get_row(UserLogin, name=user_name)
    if user_login:
        return user_login.user_id
    else:
        raise ValueError(f"用户 {user_name} 不存在！")