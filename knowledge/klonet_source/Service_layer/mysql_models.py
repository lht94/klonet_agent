from sqlalchemy.sql.sqltypes import Boolean
from ..webserver import mysql
from flask_login import UserMixin
from sqlalchemy.dialects.mysql import *
from sqlalchemy import text
from .mysql_manager import get_row
import time

class UserLogin(mysql.Model, UserMixin):
    # 建表时表名自动变为user_login，其它表同
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '用户登录表',
        'mysql_charset': 'utf8'
    }

    user_id = mysql.Column(BIGINT(unsigned=True), primary_key=True, 
        autoincrement=True, nullable=False, comment="用户ID")
    generated_id = mysql.Column(BIGINT(unsigned=True), nullable=True, 
        unique=True, comment="sessionID")
    # 加冗余，便于查询
    name = mysql.Column(VARCHAR(50), nullable=False, 
        unique=True, comment="用户名")
    password_hash = mysql.Column(CHAR(60), nullable=False, 
        comment="bcrypt加密的密码")
    # By default, TIMESTAMP columns are NOT NULL, cannot contain NULL values, 
    # and assigning NULL assigns the current timestamp.
    create_time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP'))
    update_time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
    
    def get_id(self):
        return self.generated_id

    @staticmethod
    def get(generated_id):
        try:
            user_obj = get_row(UserLogin, generated_id=generated_id)
            if user_obj:
                # print(f'in model get, type of user_obj={type(user_obj)}')
                # print(current_user.user_id)
                return user_obj
        except:
            mysql.session.rollback()
            return None
        return None


class UserInfo(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '用户信息表',
        'mysql_charset': 'utf8'
    }

    user_info_id = mysql.Column(BIGINT(unsigned=True), primary_key=True, 
        autoincrement=True, nullable=False, comment="自增主键ID")
    user_id = mysql.Column(BIGINT(unsigned=True), nullable=False, unique=True,  
        comment="用户ID")
    name = mysql.Column(VARCHAR(50), nullable=False, 
        unique=True, comment="用户名")
    phone = mysql.Column(BIGINT(unsigned=True), nullable=False, default=0,
        comment="手机号")
    email = mysql.Column(VARCHAR(50), nullable=True, comment="邮箱")
    update_time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))

class UserRole(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '用户-角色表',
        'mysql_charset': 'utf8'
    }
    
    # 用户-角色是多对多关系
    
    user_role_id = mysql.Column(BIGINT(unsigned=True), primary_key=True, 
        autoincrement=True, nullable=False, comment="自增主键ID")
    user_id = mysql.Column(BIGINT(unsigned=True), nullable=False,
        comment="用户ID")
    role_id = mysql.Column(BIGINT(unsigned=True), nullable=False,
        comment="角色ID")

class Roles(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '角色表',
        'mysql_charset': 'utf8'
    }

    role_id = mysql.Column(BIGINT(unsigned=True), primary_key=True, 
        autoincrement=True, nullable=False, comment="角色ID")
    role_name = mysql.Column(VARCHAR(20), nullable=False, unique=True, 
        default="ordinary_user", comment="角色名")

class RoleAuthority(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '角色-权限表',
        'mysql_charset': 'utf8'
    }

    role_authority_id =  mysql.Column(BIGINT(unsigned=True), primary_key=True, 
        autoincrement=True, nullable=False, comment="自增主键ID")
    role_id = mysql.Column(BIGINT(unsigned=True), nullable=False,
        comment="角色ID")
    authority_id = mysql.Column(BIGINT(unsigned=True), nullable=False,
        comment="权限ID")

class Authorities(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '权限表',
        'mysql_charset': 'utf8'
    }

    authority_id = mysql.Column(BIGINT(unsigned=True), primary_key=True, 
        autoincrement=True, nullable=False, comment="权限ID")
    authority_name = mysql.Column(VARCHAR(50), nullable=False, unique=True,
        comment="权限名")
    

class Projects(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '项目表',
        'mysql_charset': 'utf8'
    }

    project_id = mysql.Column(BIGINT(unsigned=True), primary_key=True, 
        autoincrement=True, nullable=False, comment="自增主键ID")
    user_id = mysql.Column(BIGINT(unsigned=True), nullable=False,
        comment="所属用户ID")
    project_name = mysql.Column(VARCHAR(50), nullable=False, comment="项目名")
    create_time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP'))
    update_time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))


class Topos(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '拓扑表',
        'mysql_charset': 'utf8'
    }

    # 只存储拓扑名，可根据拓扑名等信息在磁盘找json文件。
    # 链路表、监控服务表、流量服务表同。

    topo_id = mysql.Column(BIGINT(unsigned=True), primary_key=True, 
        autoincrement=True, nullable=False, comment="自增主键ID")
    project_id = mysql.Column(BIGINT(unsigned=True), nullable=False,
        comment="所属项目ID")
    topo_name = mysql.Column(VARCHAR(50), nullable=False, comment="拓扑名")
    create_time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP'))
    update_time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP')) 


class Links(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '链路表',
        'mysql_charset': 'utf8'
    }

    link_id = mysql.Column(BIGINT(unsigned=True), primary_key=True, 
        autoincrement=True, nullable=False, comment="自增主键ID")
    topo_id = mysql.Column(BIGINT(unsigned=True), nullable=False,
        comment="所属拓扑ID")
    link_name = mysql.Column(VARCHAR(50), nullable=False, comment="链路名")
    create_time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP'))
    update_time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP')) 


class MonitorEvents(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '监控服务表',
        'mysql_charset': 'utf8'
    }

    monitor_event_id = mysql.Column(BIGINT(unsigned=True), primary_key=True, 
        autoincrement=True, nullable=False, comment="自增主键ID")
    project_id = mysql.Column(BIGINT(unsigned=True), nullable=False, comment="所属项目ID")
    monitor_event_name = mysql.Column(VARCHAR(50), nullable=False, 
        comment="监控服务名")
    create_time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP'))
    update_time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP')) 


class TrafficApps(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '流量服务表',
        'mysql_charset': 'utf8'
    }

    traffic_app_id = mysql.Column(BIGINT(unsigned=True), primary_key=True, 
        autoincrement=True, nullable=False, comment="自增主键ID")
    project_id = mysql.Column(BIGINT(unsigned=True), nullable=False, comment="所属项目ID")
    traffic_app_name = mysql.Column(VARCHAR(50), nullable=False, 
        comment="流量服务名")
    create_time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP'))
    update_time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))


class Image(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '镜像表',
        'mysql_charset': 'utf8'
    }
    image_id = mysql.Column(BIGINT(unsigned=True), primary_key=True,
                            autoincrement=True, nullable=False, comment='镜像id(pk)')
    user_id = mysql.Column(BIGINT(unsigned=True), nullable=False, comment='所属用户ID')
    # 前面应该增加用户名和registry的信息，进行image名字的拼配
    # 类似于 image_name = <registry>/<username>/<service_type>-<name>
    image_name = mysql.Column(VARCHAR(100), nullable=False, comment='镜像名')
    tag = mysql.Column(VARCHAR(50), nullable=False, default='latest', comment='镜像标签')

    # switch router host p4等
    type = mysql.Column(VARCHAR(50), nullable=False, comment='类型')
    subtype = mysql.Column(VARCHAR(50), nullable=False, comment='子类型')
    is_public = mysql.Column(Boolean, nullable=False, comment='是否为公共镜像')
    config = mysql.Column(JSON, nullable=False, comment='配置')
    edit_config = mysql.Column(JSON, nullable=False, comment='编辑配置')
    customize_icon = mysql.Column(Boolean,  nullable=False, 
        comment='镜像图标')
    size = mysql.Column(VARCHAR(100), nullable=False, comment='镜像大小')
    time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
     #资源相关
    cpu = mysql.Column(VARCHAR(100), nullable=False, comment='CPU需求')
    memory_requirements = mysql.Column(VARCHAR(100), nullable=False, comment='内存需求大小')
    image_full_name = mysql.Column(VARCHAR(100), nullable=False, comment='镜像全名')
    
class KVMImage(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': 'kvm虚拟机镜像表',
        'mysql_charset': 'utf8'
    }
    image_id = mysql.Column(BIGINT(unsigned=True), primary_key=True,
                            autoincrement=True, nullable=False, comment='镜像id(pk)')
    user_id = mysql.Column(BIGINT(unsigned=True), nullable=False, comment='所属用户ID')
    image_name = mysql.Column(VARCHAR(100), nullable=False, comment='镜像名')
    type = mysql.Column(VARCHAR(50), nullable=False, comment='类型')
    
     #资源相关
    cpu = mysql.Column(VARCHAR(100), nullable=False, comment='CPU需求')
    memory_requirements = mysql.Column(VARCHAR(100), nullable=False, comment='内存需求大小')
    path = mysql.Column(VARCHAR(800), nullable=True, comment='镜像存储路径')
    time = mysql.Column(TIMESTAMP, 
        server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))

    # @property
    # def serialization(self):
    #     return {
    #         '_id': self.image_id,
    #         'name': self.image_name,
    #         'config': self.image_config,
    #         'service_type': self.service_type,
    #         'icon': self.icon
    #     }

    # def __str__(self):
    #     return self.serialization

    # def __repr__(self):
    #     return self.serialization

class UserLogs(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '日志表',
        'mysql_charset': 'utf8'
    }

    log_id = mysql.Column(BIGINT(unsigned=True), primary_key=True, 
        autoincrement=True, nullable=False, comment="自增主键ID")
    user_name = mysql.Column(VARCHAR(50), nullable=False, comment='所属用户名')
    project_name = mysql.Column(VARCHAR(50), nullable=False, comment="所属项目名")
    log_msg = mysql.Column(VARCHAR(100), nullable=False, comment='日志消息')
    #这是一个 datetime 对象
    log_time =mysql.Column(TIMESTAMP, server_default=text('CURRENT_TIMESTAMP'))
    user_log_level = mysql.Column(VARCHAR(20), nullable=False, comment='用户日志等级')

    def to_dict(self): 
        log_info_dict = {
            "msg":self.log_msg,
            "topo":self.project_name,
            "time":self.log_time.strftime("%Y-%m-%d %H:%M:%S"),
            "user":self.user_name
        }
        return log_info_dict

class VemuLogs(mysql.Model):
    # 此表用于后台记录运维信息，不可删除
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '日志表（永久）',
        'mysql_charset': 'utf8'
    }

    log_id = mysql.Column(BIGINT(unsigned=True), primary_key=True, 
        autoincrement=True, nullable=False, comment="自增主键ID")
    user_name = mysql.Column(VARCHAR(50), nullable=False, comment='所属用户名')
    project_name = mysql.Column(VARCHAR(50), nullable=False, comment="所属项目名")
    log_msg = mysql.Column(VARCHAR(100), nullable=False, comment='日志消息')
    #这是一个 datetime 对象
    log_time =mysql.Column(TIMESTAMP, server_default=text('CURRENT_TIMESTAMP'))
    user_log_level = mysql.Column(VARCHAR(20), nullable=False, comment='用户日志等级')

    def to_dict(self): 
        log_info_dict = {
            "msg":self.log_msg,
            "topo":self.project_name,
            "time":self.log_time.strftime("%Y-%m-%d %H:%M:%S"),
            "user":self.user_name
        }
        return log_info_dict

class Experiment(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '实验仓库表',
        'mysql_charset': 'utf8'
    }
    experiment_id = mysql.Column(BIGINT(unsigned=True), primary_key=True,
                 autoincrement=True, nullable=False, comment="自增主键ID")
    experiment_name = mysql.Column(VARCHAR(50), nullable=False, comment="实验名")
    user_id = mysql.Column(BIGINT(unsigned=True), nullable=False, comment="所属用户ID")
    # is_public = mysql.Column(Boolean, nullable=False, comment='是否为公开')
    create_time = mysql.Column(TIMESTAMP, server_default=text('CURRENT_TIMESTAMP'))
    topo_json = mysql.Column(LONGBLOB, nullable=False, comment="实验拓扑描述json文件")
    have_scripts = mysql.Column(Boolean, nullable=False, comment="是否有脚本文件")
    experiment_scripts_name = mysql.Column(VARCHAR(50), nullable=True, comment="实验脚本名")