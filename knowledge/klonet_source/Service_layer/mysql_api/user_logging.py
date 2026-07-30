import enum
from ...webserver import mysql
from ..mysql_models import UserLogs,VemuLogs
from ..mysql_manager import count, get_all_row, get_row, delete
"""
有几个问题：
第一，需不需要排序，mysql现在似乎是有序的，不需要额外手段
第二，如何控制单个用户日志的总量限定，我觉得可以用一个表来映射用户已有的日志数量，避免反
复去查数据库，然后计数
第三，所有日志整合在一起，还是分开？上十万条数据的查询也就1s
先用一个表吧，毕竟这个实时性要求不高，后面有需要，在分表存储
"""


# 自定义日志分级，first日志于首页显示，second日志于项目页面显示
class UserLogLevel(enum.Enum):
    First = 1
    Second = 2


def add_user_log(user, topo, userloglevel, msg, commit=True):
    '''添加一行用户日志

    Args:
        topo: 项目名
        user: 用户名
        msg: 日志消息
        commit: 是否在函数内进行commit，默认为True
        userloglevel: 用户日志等级
    '''
    try:
        userlog = UserLogs()
        userlog.user_name = user
        userlog.project_name = topo
        userlog.user_log_level = userloglevel
        userlog.log_msg = msg
        mysql.session.add(userlog)

        vemulog = VemuLogs()
        vemulog.user_name = user
        vemulog.project_name = topo
        vemulog.user_log_level = userloglevel
        vemulog.log_msg = msg
        mysql.session.add(vemulog)
        if commit:
            mysql.session.commit()
    except Exception as e:
        mysql.session.rollback()
        raise e

def delete_user_log(id):
    """根据ID，删除一条日志
    """
    return delete(UserLogs, log_id = id)
    

def delete_all_user_logs(user, userloglevel, topo):
    """删除对应所有日志

    由于delete在表项个数为零时会报错，但实际删除逻辑是符合的，所以计数为零时直接返回True
    可以修改delete函数，但delete函数过于底层，所以在这里实现对零个表项的识别。
    """
    if not count(UserLogs, user_name = user, 
                 user_log_level = userloglevel, project_name = topo):
        return True
    return delete(UserLogs, user_name = user, 
                  user_log_level = userloglevel, project_name = topo)


def get_user_oldest_log_id(user, userloglevel, topo):
    '''通过user_name和userloglevel获取UserLogs表中最旧的一行日志

    Args:
        user: 用户名
        userloglevel: 用户日志等级
        topo: 项目名

    Return:
        int: 日志id
    '''

    log = get_row(UserLogs, user_name = user, 
                            user_log_level = userloglevel, project_name = topo)
    return log.log_id


def get_user_logs(user, userloglevel, **kwargs):
    '''通过user_name，topo和userloglevel获取UserLogs表的所有行

    Args:
        user: 用户名
        userloglevel: 用户日志等级

    Return:
        list: 日志model示例列表
    '''
    user_logs_model_list = []
    if userloglevel == UserLogLevel.First:
        user_logs_model_list = get_all_row(UserLogs, user_name = user, 
                                           user_log_level = userloglevel)
    else:
        topo = kwargs['topo']
        user_logs_model_list = get_all_row(UserLogs, user_name = user, 
                                           project_name = topo,
                                           user_log_level = userloglevel)
    return user_logs_model_list

def count_user_logs(user, userloglevel, topo):
    '''通过user_name、userloglevel和topo对日志进行计数

    用户日志级别为First时，不需要指定topo，其余需要指定topo
    Args:
        user: 用户名
        userloglevel: 用户日志等级
        topo: 项目名

    Return:
        int: 计数
    '''

    sum = count(UserLogs, user_name = user, 
                user_log_level = userloglevel, project_name = topo)
    return sum
