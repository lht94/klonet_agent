import bcrypt, re, datetime
from flask import current_app
from flask_login import login_user, current_user
from flask_login.utils import logout_user
from flask import session
from ...Service_layer.mysql_models import UserLogin, UserInfo
from ...Service_layer.mysql_api.user_info import get_user_info_by_user_name, \
    get_user_info_by_user_id, check_passwd_complexity
from ...Service_layer.mysql_api.user_login import get_user_login_by_user_id, get_user_login_by_user_name
from ...Service_layer.send_mail import send_welcome_mail_to, send_forget_passwd_mail_to
from ...vemu_config.config import PROJ_CONFIG
from ...webserver import mysql


class UserManager():
    def register(self, name, password, phone, email, role):
        if get_user_info_by_user_name(name):
            resp = {"code": 0, "msg": f"注册失败！用户[{name}]已存在。"}
            return resp
        
        # 对注册用户的密码复杂度的限制
        passwd_complexity = check_passwd_complexity(password)
        if passwd_complexity['code'] == 0:
            resp = {"code": 0, "msg": f"注册失败！密码不合规范：{passwd_complexity['msg']}"}
            return resp
        else:
            passwd_level = passwd_complexity['level']
            print(f'密码等级为{passwd_level}')

        # 检测邮箱是否规范, 并尝试给改注册邮箱发送邮件
        re_mail = '^\w+([-+.]\w+)*@\w+([-.]\w+)*\.\w+([-.]\w+)*$'
        if not re.fullmatch(re_mail, email):
            resp = {"code": 0, "msg": "注册失败！邮箱不合规范！"}
            return resp
        # 若邮箱功能使能，才进行邮件发送
        if PROJ_CONFIG.mail_enable:
            if not send_welcome_mail_to(email):
                resp = {"code": 0, "msg": "注册失败！无法向该邮箱发送信息！"}
                return resp

        self.create_user(name, password, phone, email, role)

        print(f"user {name} register successfully.")

        resp = {'code': 1, 'msg': '注册成功！'}
        return resp

    def login(self, name, password):
        '''
        登录

        Args:
            name: 用户名
            password: 密码

        Returns:
            login_user函数异常导致登录失败返回-1
            账号或密码错误导致登录失败返回0，
            登录成功返回1，
        '''
        # 返回用户名或密码错误可增强安全性
        user_info = get_user_info_by_user_name(name)
        if user_info is None:
            print(f"{name} not exists.")
            return 0

        user_login = get_user_login_by_user_id(user_info.user_id)
        if not self.check_password(user_login, password):
            print(f"{name} password is wrong.")
            return 0
        
        if user_login.generated_id is None:
            two_id=0
            self.set_generated_id(user_login,two_id)
        
        one_id=user_login.generated_id%10
        if not PROJ_CONFIG.multi_login_allowed:
            one_id = one_id
        elif one_id == 9:
            one_id=0
        else:
            one_id=one_id+1
        self.modify_generated_id(user_login,one_id)
        
        is_login_success = login_user(user_login, remember=True)

        if not is_login_success:
            print(f"user [{name}] login failed.")
            return 2
        else:
            session.permanent = PROJ_CONFIG.login_expired
            print(f"user [{name}] login successfully.")
            return 1

    def logout(self):
        '''
        登出

        Args:
            None

        Returns:
            登出成功返回1
            登出失败返回0
        '''
        try:
            user_id = current_user.user_id
            user_info = get_user_info_by_user_id(user_id)
            logout_user()
            print(f"user [{user_info.name}] logout successfully!")
            return 1
        except Exception as e:
            print(e)
            return 0

    def create_user(self, name, password, phone, email, role):
        one_id=0
        try:
            user_login = UserLogin()
            self.set_password(user_login, password)
            # TODO: 入参检查
            user_login.name = name
            mysql.session.add(user_login)
            mysql.session.flush() # 暂存，以获取user_id
            user_login.generated_id=user_login.user_id*10+one_id
            
            user_info = UserInfo()
            user_info.user_id = user_login.user_id
            self.set_name(user_info, name)
            self.set_phone(user_info, phone)
            self.set_email(user_info, email)
            
            from ...Service_layer.mysql_api.auth import bind_user_to_roles
            bind_user_to_roles(user_login.user_id, [role])
            
            mysql.session.add(user_info)
            mysql.session.commit()
        except Exception as e:
            mysql.session.rollback()
            raise e

    def set_password(self, user_login, password:str):
        '''
        设置密码并加密存储

        Args:
            password: 要设置的密码的明文(明文的最大长度为40个字符)
        Returns:
            None
        '''
        # TODO(mt): 明文密码最大长度的检查
        user_login.password_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt())

    def check_password(self, user_login, password_to_check:str):
        '''
        密码校验

        Args:
            password_to_check: 要校验的密码
        Returns:
            True: 密码正确
            False: 密码错误
        '''
        return bcrypt.checkpw(password_to_check.encode("utf-8"), 
                              user_login.password_hash.encode("utf-8"))
    
    def set_name(self, user_info, name:str):
        '''
        设置用户名
        '''
        # TODO(mt): 用户名最大长度的检查
        # 入参类型检查应该放在哪里？
        user_info.name = name        

    def set_email(self, user_info, email:str):
        # TODO(mt): email最大长度的检查
        user_info.email = email

    def set_phone(self, user_info, phone:int):
        # TODO(mt): 手机号最大长度的检查
        user_info.phone = phone

    def set_generated_id(self, user_login, generated_id):
        '''
        设置sessionID
        '''
        user_login.generated_id = generated_id

    def modify_password(self, name, old_password:str, new_password:str):
        '''
        修改密码。验证旧密码后，进行新密码的修改

        Args:
            name: 用户名
            password: 要设置的密码的明文(明文的最大长度为40个字符)

        Returns:
            字典，包含code和msg两个字段。
            code: 修改成功返回1，修改失败返回0
            msg:  描述信息
        '''
        try:
            user_login = get_user_login_by_user_name(name)

            if not user_login:
                print(f"user {name} try to modify password, but user {name} not "
                    "exists.")
                return {'code': 0, 'msg': '用户不存在！'}

            if not self.check_password(user_login, old_password):
                print(f"{name} try to modify password, but old_password is wrong.")
                return {'code': 0, 'msg': '旧密码输入错误！'}

            # 对新密码的复杂度进行限制
            passwd_complexity = check_passwd_complexity(new_password)
            if passwd_complexity['code'] == 0:
                print(f"修改密码失败！密码不合规范：{passwd_complexity['msg']}")
                return {'code': 0, \
                    'msg': f"新密码不合规范：{passwd_complexity['msg']}"}
            else:
                passwd_level = passwd_complexity['level']
                print(f'密码等级为{passwd_level}')

            self.set_password(user_login, new_password)
            
            mysql.session.add(user_login)
            mysql.session.commit()
            return {'code': 1, 'msg': '修改密码成功！'}
        except Exception as e:
            mysql.session.rollback()
            raise e

    def super_setpwd(self, name, new_password:str):
        '''
        管理员重置密码
        '''
        try:
            user_login = get_user_login_by_user_name(name)

            if not user_login:
                print(f"user {name} try to modify password, but user {name} not "
                    "exists.")
                return {'code': 0, 'msg': '用户不存在！'}

            # 对新密码的复杂度进行限制
            passwd_complexity = check_passwd_complexity(new_password)
            if passwd_complexity['code'] == 0:
                print(f"修改密码失败！密码不合规范：{passwd_complexity['msg']}")
                return {'code': 0, \
                    'msg': f"新密码不合规范：{passwd_complexity['msg']}"}
            else:
                passwd_level = passwd_complexity['level']
                print(f'密码等级为{passwd_level}')

            self.set_password(user_login, new_password)
            
            mysql.session.add(user_login)
            mysql.session.commit()
            return {'code': 1, 'msg': '修改密码成功！'}
        except Exception as e:
            mysql.session.rollback()
            raise e

    def modify_generated_id(self, user_login, one_id):
        '''
        修改sessionID，保证多地登录时session的不同

        Args:
            user_login: 用户模型
            one_id: 要设置的sessionID的个位数
        最终的sessionID为不变的user_id作为整十数,加上变化的one_id组成
        '''
        try:

            if not user_login:
                print(f"user {user_login.name} not exists.")
                return {'code': 0, 'msg': '用户不存在！'}
            
            generated_id=one_id+user_login.user_id*10


            self.set_generated_id(user_login, generated_id)
            
            mysql.session.add(user_login)
            mysql.session.commit()
        except Exception as e:
            mysql.session.rollback()
            raise e
        
    @staticmethod
    def get_user_id():
        '''
        获取当前用户id
        '''
        return current_user.user_id

    @staticmethod
    def get_basic_info():
        '''
        获取用户基本信息，包括用户id和用户名
        
        Args:
            None

        Returns:
            若用户已登录，返回
            {
                "user_id": 用户id,
                "user_name": 用户名
            }
            若用户未登录，返回
            {
                "user_id": None,
                "user_name": None
            }
        '''
        if current_user.is_anonymous:
            basic_info = {
                "user_id": None,
                "user_name": None
            }
        else:
            basic_info = {
                "user_id": current_user.user_id,
                "user_name": current_user.name
            }
        
        return basic_info