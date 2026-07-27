import re
from ..mysql_models import UserInfo
from ..mysql_manager import get_row
from ..mysql_manager import check_row_exists
from ...webserver import mysql


def check_passwd_complexity(passwd:str):
    """
    检查密码的复杂度是否合格

    Args:
        passwd: 待检测的密码

    Return:
        字典，包含 'code', 'level', 'msg' 三个字段
        'code':  0或1，表示密码是否合格，0为不合格，1为合格
        'level': 密码等级，共包括0、1、2、3四个等级。0表示密码不合格，其余等级数字越大说明密码越安全
        'msg':   对密码问题或等级的说明
    """
    # 首先检测长度，长度不小于8，不大于20字符
    if len(passwd) < 8:
        return {'code': 0, 'level': 0, 'msg': '密码太短，至少8个字符'}
    if len(passwd) > 20:
        return {'code': 0, 'level': 0, 'msg': '密码太长，至多20个字符'}
    
    # 判定3级密码：8~20位数字、字母、特殊字符，三个缺一不可
    re_3 = '(?=.*[0-9])(?=.*[a-zA-Z])(?=.*[^a-zA-Z0-9]).{8,20}'
    if re.fullmatch(re_3, passwd):
        return {'code': 1, 'level': 3, 'msg': '密码等级为3'}

    # 判定2级密码：8~20位数字+字母；字母+特殊字符，特殊字符+数字
    re_2 = '^(?![\d]+$)(?![a-zA-Z]+$)(?![^\da-zA-Z]+$).{8,20}$'
    if re.fullmatch(re_2, passwd):
        return {'code': 1, 'level': 2, 'msg': '密码等级为2'}

    # 判定1级密码：8~20位纯数字或者纯小写字母或者纯大写字母
    re_1 = '^(\d{8,10})|([a-z]{8,10})|([A-Z]{8,10})$'
    if re.fullmatch(re_1, passwd):
        return {'code': 1, 'level': 1, 'msg': '密码等级为1'}
    
    return {'code': 0, 'level': 0, 'msg': '密码至少包含一个字母或数字'}

def get_user_name_by_user_id(user_id:int):
    '''
    通过user_id获取user_name

    Args:
        user_id: 用户id

    Return:
        None/name: 用户名
    '''
    try:
        return (mysql.session.query(UserInfo.name).filter_by(user_id=user_id)
                .scalar())
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_user_info_by_user_name(name:str):
    '''
    通过user_name获取UserInfo表的一行

    Args:
        name: 用户名

    Return:
        UserInfo实例/None
    '''
    return get_row(UserInfo, name=name)

def get_user_info_by_user_id(user_id:int):
    '''
    通过user_id获取UserInfo表的一行

    Args:
        name: 用户id

    Return:
        UserInfo实例/None
    '''
    return get_row(UserInfo, user_id=user_id)

def check_user_exist_by_user_name(name:str) -> bool:
    '''
    通过user_name检查UserInfo表是否存在该用户

    Args:
        name: 用户名

    Return:
        True/False
    '''
    return check_row_exists(UserInfo, name=name)