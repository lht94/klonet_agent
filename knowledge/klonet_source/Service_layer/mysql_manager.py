import traceback
from ..webserver import mysql

# 更多待补充

def count(model, *args, **kwargs) -> int:
    '''
    获取查询结果的行数
    使用方法：
        用法1：
        count(UserInfo, UserInfo.user_id==2)
        用法2：
        count(UserInfo, user_id=2)
        用法3：
        count(UserInfo, UserInfo.name=="test", user_id=2)
        用法4：
        count(UserInfo)

    Args:
        model: model类
        args: 查询条件，格式：model类.列名==值
        kwargs: 查询条件，格式：列名=值

    Return:
        0/行数(int)
    ''' 
    try:
        count_result = (mysql.session.query(model).filter(*args).
               filter_by(**kwargs).count())
        mysql.session.commit()
        return count_result
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_row(model, *args, **kwargs):
    '''
    获取指定表中的指定行
    使用方法：
        用法1：
        getrow(UserInfo, UserInfo.user_id==2)
        用法2：
        get_row(UserInfo, user_id=2)
        用法3：
        get_row(UserInfo, UserInfo.name=="test", user_id=2)

    Args:
        model: model类
        args: 查询条件，格式：model类.列名==值
        kwargs: 查询条件，格式：列名=值

    Return:
        model实例/None
    ''' 
    try:
        row = (mysql.session.query(model).filter(*args).
               filter_by(**kwargs).first())
        mysql.session.commit()
        return row
    except Exception as e:
        traceback.print_exc()
        mysql.session.rollback()
        
        raise e

def get_all_row(model, *args, **kwargs):
    '''
    获取指定表中的搜索的所有行
    使用方法：
        用法1：
        getrow(UserInfo, UserInfo.user_id==2)
        用法2：
        get_row(UserInfo, user_id=2)
        用法3：
        get_row(UserInfo, UserInfo.name=="test", user_id=2)

    Args:
        model: model类
        args: 查询条件，格式：model类.列名==值
        kwargs: 查询条件，格式：列名=值

    Return:
        model实例/None
    '''
    try:
        row = (mysql.session.query(model).filter(*args).filter_by(**kwargs).all())
        mysql.session.commit()
        return row #返回的是一个列表，即所有行的首地址，不能直接print,利用循环和下标索引得到数据
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_row_by_pk_id(model, pk_id:int):
    '''
    获取指定表中的指定行

    Args:
        model: model类
        pk_id: 主键id

    Return:
        model实例/None
    ''' 
    try:
        row = mysql.session.get(model, pk_id)
        mysql.session.commit()
        return row
    except Exception as e:
        mysql.session.rollback()
        raise e

def delete(model, *args, **kwargs):
    '''
    获取指定表中满足条件的行

    行个数为零时，会出错，需要额外处理为零的情况

    Args:
        model: model类
        pk_id: 主键id

    Return:
        删除成功返回
        {
            "code": 1,
            "msg": success
        }
        删除失败返回
        {
            "code": 1,
            "msg": success
        }
    ''' 
    try:
        model_obj = mysql.session.query(model).filter(*args).filter_by(**kwargs)
        result = model_obj.delete()
        mysql.session.commit()
        return result
    except Exception as e:
        mysql.session.rollback()
        raise e

def check_row_exists(model, *args, **kwargs):
    '''
    检查指定行是否存在
    使用方法：
        用法1：
        check_row_exists(UserInfo, UserInfo.user_id==2)
        用法2：
        check_row_exists(UserInfo, user_id=2)
        用法3：
        check_row_exists(UserInfo, UserInfo.name=="test", user_id=2)

    Args:
        model: model类
        args: 查询条件，格式：model类.列名==值
        kwargs: 查询条件，格式：列名=值

    Returns:
        如果指定行存在则返回True，否则返回False
    '''
    try:
        q = mysql.session.query(model).filter(*args).filter_by(**kwargs)
        is_exists = mysql.session.query(q.exists()).scalar()

        return True if is_exists else False
    except Exception as e:
        mysql.session.rollback()
        raise e