
import logging
import json
from datetime import datetime

from functools import wraps
import traceback
from  flask import request
from ..Service_layer.redisAPI import UserMapRedis
from colorama import Fore, Style
import os
import sys
from concurrent_log_handler import ConcurrentRotatingFileHandler
from ..vemu_config.config import PROJ_CONFIG
from ..Service_layer.mysql_api.user_logging import *


class ManagerLogger(object):
    """管理日志记录器类

    用于向文件、控制台输出日志信息
    """
    def __init__(self, logger = 'flask'):
        '''获取一个管理日志记录器，默认为flask，若无则创建记录器，输出管理日志

        管理日志器可在Config中查询，可用方法包括debug、info、warning、error和critical
        例如:

        logger = ManagerLogger()
        logger.error('这是一条error信息')
        logger.info('这是一条info信息')

        Attributes:
            logger: 定义对应的程序模块名name
        '''
        self.enable = PROJ_CONFIG.manager_logger_enable
        if not self.enable:
            return
        # 获取控制台logger
        self.logger_console = logging.getLogger('console')
        # 颜色 map 映射
        self.formatter_color_map = {
            'debug': Fore.LIGHTGREEN_EX,
            'info': Fore.CYAN,
            'warning': Fore.LIGHTYELLOW_EX,
            'error': Fore.LIGHTRED_EX,
            'critical': Fore.LIGHTMAGENTA_EX,
        }
        # 获取一个文件logger，如果没有则创建
        self.logger_file = logging.getLogger(name = logger)
        self.logger_file.setLevel(logging.DEBUG)
        self.logger_file.propagate = 0
        if not self.logger_file.handlers:
            # 创建一组handler，用于写入日志文件
            formatter = logging.Formatter(
                "%(asctime)s - %(message)s")
            # rq = time.strftime("%Y-%m-%d", time.localtime(time.time()))
            # access文件
            log_path = PROJ_CONFIG.loggging_access_filepath
            fh = ConcurrentRotatingFileHandler(log_path, 'a', \
                    PROJ_CONFIG.file_maxBytes, PROJ_CONFIG.file_backupCount)
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(formatter)
            self.logger_file.addHandler(fh)
            # error文件
            log_path = PROJ_CONFIG.loggging_error_filepath
            fh = ConcurrentRotatingFileHandler(log_path, 'a', \
                    PROJ_CONFIG.file_maxBytes, PROJ_CONFIG.file_backupCount)
            fh.setLevel(logging.WARNING)
            fh.setFormatter(formatter)
            self.logger_file.addHandler(fh)

    def _findCaller(self):
        """定位调用管理日志的入口
        
        利用栈回溯找到调用入口，返回函数名，文件名，行数等信息从调用函数到实际的_log，
        如果期间有对函数的新增封装，需要修改_getframe()中的参数以确定回溯位置正确。
        """
        # stack traceback 
        # 下面函数调用的过程有变化时需要修改栈回溯的值
        # ManagerLogging.debug() -> ManagerLogging._log() -> logging.debug()
        f = sys._getframe(3)

        rv = "(unknown file)", 0, "(unknown function)", 
        if hasattr(f, "f_code"):
            co = f.f_code
            # filename = os.path.normcase(co.co_filename)
            mf = os.path.split(co.co_filename)
            # index = (co.co_filename.index('/vemu_uestc'))
            # rv = (co.co_filename[index:], f.f_lineno, co.co_name)
            rv = (mf[1], f.f_lineno, co.co_name)
        return rv 
    
    def _log(self, method:str, msg:str):
        '''自定义格式处理，输出到控制台和文件

        利用Fore和Style将文件输出与控制台输出的格式进行了分别处理，因为在普通的txt文件
        中无法识别字体颜色的编码，造成乱码，所以直接输出文字
        '''
        fn, lno, func = self._findCaller()
        # 控制台
        log = getattr(self.logger_console, method)
        log(Fore.WHITE + f"{fn}[line:{lno}]:{func} - " +\
                self.formatter_color_map[method] + f"{method.upper()} - " +\
                f"{str(os.getpid())} - " + str(msg) + Style.RESET_ALL)
        # 文件
        log = getattr(self.logger_file, method)
        log(f"{fn}[line:{lno}]:{func} - {method.upper()} - {str(os.getpid())} - "\
                 + str(msg))

    def debug(self, msg):
        """定义控制台输出debug级日志

        Args:
            msg: 输出的日志消息

        """
        if not self.enable or not PROJ_CONFIG.debug_enable:
            return
        self._log('debug', msg)
    
    def info(self, msg):
        if not self.enable or not PROJ_CONFIG.info_enable:
            return
        self._log('info', msg)
 
    def warning(self, msg):
        if not self.enable or not PROJ_CONFIG.warning_enable:
            return
        self._log('warning', msg)

    def error(self, msg):
        if not self.enable or not PROJ_CONFIG.error_enable:
            return
        self._log('error', msg)

    def critical(self, msg):
        if not self.enable or not PROJ_CONFIG.critical_enable:
            return
        self._log('critical', msg)

class UserLogger(object):
    '''用户日志记录器，输出用户日志。

    用以初始化一个用户日志记录器，mysql数据库中读、写、查询日志，目前保留了redis的接口，
    实际已经没有再使用。日志级别为First时，不需要指定topo，其余需要指定具体topo，例如: 

        logger = UserLogger('xxx', UserLogLevel.First)
        logger.log_to_mysql('创建项目xx')
        
        logger = UserLogger('xxx', UserLogLevel.Second, 'test')
        logger.log_to_mysql('创建项目test')

    Attributes:
        user: 用户名。
        topo: 项目名，默认为None。
        level: 用户日志等级，First不需要指定topo，其余需要指定topo。
        redis_table_name: redis表名（已弃用）
        redis_events_count: redis数据表条目数(已弃用)
        enbale: 用户日志使能标志，若为False，则不会向数据库记录任何信息
    '''
    def __init__(self, user, level:UserLogLevel, topo = 'None'):
        self.user = user
        self.level = level
        # 需要保证 level 和 topo 的对应关系是准确的
        if self.level == UserLogLevel.First:
            self.topo = 'None'
        else:
            self.topo = topo
        self.redis_table_name = None
        self.redis_events_count = None
        self.enable = PROJ_CONFIG.user_logger_enable
    
    def _determine_mysql_table(self):
        '''确定与mysql数据库相关的信息

        Raises:
            ValueError: 用户日志级别或项目名出错
        '''
        if self.level == UserLogLevel.First:
            self.mysql_events_count = PROJ_CONFIG.mysql_events_count_First
        elif self.level == UserLogLevel.Second:
            self.mysql_events_count = PROJ_CONFIG.mysql_events_count_Second
            if not self.topo:
                raise ValueError('Invalid value of topo')
            self.redis_table_name = f'{self.user}_{self.topo}_log'
        else:
            raise ValueError('Invalid value of level')

    def _determine_redis_table(self):
        '''确定与redis数据库相关的信息

        Raises: 
            ValueError
        '''
        if self.level == UserLogLevel.First:
            self.redis_events_count = PROJ_CONFIG.redis_events_count_First
            self.redis_table_name = f'{self.user}_log'
        elif self.level == UserLogLevel.Second:
            self.redis_events_count = PROJ_CONFIG.redis_events_count_Second
            if not self.topo:
                raise ValueError('Invalid value of topo')
            self.redis_table_name = f'{self.user}_{self.topo}_log'
        else:
            raise ValueError('Invalid value of level')
        
    def log_to_redis(self, msg:str):
        '''向redis数据库输出一条用户日志信息

        Args:
            msg: 日志消息字符串

        Raises: 
            ValueError
            RuntimeError
        '''
        if not self.enable:
            return 
        #检查是否存在表，不存在为首次操作，直接插入，存在则读取表内容，修改表内容
        info_dict = {}
        now_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        msg_modified = {'user':self.user, 'topo':self.topo ,'msg':msg , 'time':now_time}
        try:
            self._determine_redis_table()
            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(self.user)
            if not user_db_cli.check_exist(self.redis_table_name, 1):
                user_db_cli.set_value(self.redis_table_name, 1, msg_modified)
            else:
                #更新日志表内容
                log_dict = user_db_cli.get_all_values(self.redis_table_name)
                user_db_cli.del_all_values(self.redis_table_name)
                #往次记录逐个后挪
                for key,value in log_dict.items():
                    if int(key) <= self.redis_events_count - 1:
                        info_dict[str(int(key) + 1)] = value
                #本次记录
                info_dict[1] = msg_modified        
                user_db_cli.set_all_values(self.redis_table_name, info_dict)
        except ValueError as e:
            raise e
        except:
            raise RuntimeError('Unknown error of redis')
        finally:
            user_map_redis.close()
            user_db_cli.close()
        
    def delete_from_redis(self):
        '''删除redis用户日志信息

        Returns:
            bool: 删除成功为1，失败为0

        Raises:
            ValueError
            RuntimeError
        '''
        if not self.enable:
            return True
        try:
            self._determine_redis_table()
            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(self.user)
            print(self.redis_table_name)
            if not user_db_cli.check_exist(self.redis_table_name, 1):
                return False
            else:
                user_db_cli.del_table(self.redis_table_name)
                return True
        except: 
            raise RuntimeError('Unknown error of redis')
        finally:
            user_map_redis.close()
            user_db_cli.close()
        
    def get_from_redis(self):
        '''查看redis数据库中用户日志信息

        Returns: 
            dict: 日志信息字典

        Raises: 
            ValueError
            RuntimeError
        '''
        info_dict = {'msg':'Log information not fuound'}
        if not self.enable:
            return info_dict
        try:
            self._determine_redis_table()
            user_map_redis = UserMapRedis()
            user_db_cli = user_map_redis.get_user_db(self.user)
            if not user_db_cli.check_exist(self.redis_table_name, 1):
                return info_dict
            else:
                info_dict = user_db_cli.get_all_values(self.redis_table_name)
        except: 
            raise RuntimeError('Unknown error of redis')
        finally:
            user_map_redis.close()
            user_db_cli.close()
            return info_dict

    def log_to_mysql(self, msg:str):
        """记录一条用户日志

        除了记录一条日志外，还在此处判断用户日志条目是否达到上限，达到上限则删除最
        旧的日志信息，日志条目数在Config中给出

        """
        if not self.enable:
            return
        try:
            self._determine_mysql_table()
        # 只打印不抛出
        except ValueError:
            traceback.print_exc()

        if count_user_logs(self.user, self.level, self.topo) < self.mysql_events_count:
            add_user_log(self.user, self.topo, self.level, msg)
        # 先删除后添加
        else:
            id = get_user_oldest_log_id(self.user, self.level, self.topo)
            if not delete_user_log(id):
                print('delete failed,unknown error of mysql')
            add_user_log(self.user, self.topo, self.level, msg)

    def delete_from_mysql(self):
        """目前默认全删除

        Returns:
            删除成功返回1，失败0
        """
        if not self.enable:
            return True
        return delete_all_user_logs(self.user, self.level, self.topo)
        

    def get_from_mysql(self):
        """返回一个可以根据key排序的日志字典，记录了用户最近的操作
        """
        log_list = []
        temp_dict = {'msg':'Log information not fuound'}
        if not self.enable:
            return temp_dict
        if self.level == UserLogLevel.First:
            log_list = get_user_logs(self.user, self.level)
        else:
            log_list = get_user_logs(self.user, self.level, topo = self.topo)
        info_dict = {}
        cunt = 0
        # 反向遍历，将更新的操作，置于更前端
        for log in reversed(log_list):
            cunt += 1
            info_dict[f'{cunt}'] = log.to_dict()
        if not cunt:
            # 日志为空
            return temp_dict
        else: 
            return info_dict
        
FLASK_LOGGER = ManagerLogger()