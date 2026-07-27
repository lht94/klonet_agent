from ..Service_layer.redisAPI import UserMapRedis, DB0
from ..Service_layer.redis_error import *


class redis_context:
    """
    得到数据库连接
    """
    def __init__(self, user):
        """
        Args:
            user (str): 用户名
        """
        user_db_map = UserMapRedis()
        self.user_db_cli = user_db_map.get_user_db(user)
        user_db_map.close()

    def __enter__(self):
        """
        Returns:
            返回用户数据库连接
        """
        return self.user_db_cli

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.user_db_cli.close()


class user_map_redis_context:
    """
    得到用户数据库映射的管理对象
    with
    """
    def __init__(self):
        self.user_dp_map = UserMapRedis()
    
    def __enter__(self):
        """
        Returns:
            返回用户数据库映射的管理对象
        """
        return self.user_dp_map
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.user_dp_map.close()


class Db0Context(redis_context):
    def __init__(self):
        """
        Db0上下文管理器

        例子：
        with Db0Context() as db0_cli:
            try:
                db0_cli.check_table_exist("my_table_name")
            except TableNotExistError:
                print("Table not exist!")
        """
        self.user_db_cli = DB0()


def judge_user_exist(user):
    """
    判断用户是否存在
    Args:
        user (str): 用户名
    """
    user_dp_map = UserMapRedis()
    try:
        user_dp_map.get_user_db(user)
    except DbNotExistError:
        return False
    else:
        return True
    finally:
        user_dp_map.close()


def check_table_key(user, table_name, key):
    """
    检查指定表的键是否存在

    :param user:用户名
    :param table_name:表名
    :param key:
    :return: 若存在则返回True，不存在则返回False
    """
    with redis_context(user) as user_db_cli:
        try:
            user_db_cli.get_value(table_name, key)
        except KeyNotExistError:
            return False
        except TableNotExistError:
            return False
        else:
            return True


def check_table_existence(user, table_name):
    """
    检查指定表是否存在

    :param user:用户名
    :param table_name:表名
    :return: 若存在则返回True，不存在则返回False
    """
    with redis_context(user) as user_db_cli:
        try:
            user_db_cli.check_table_exist(table_name)
            return True
        except TableNotExistError:
            return False