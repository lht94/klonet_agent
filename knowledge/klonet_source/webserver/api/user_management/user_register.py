import json, time
from flask.views import MethodView
from flask import request
from ...web_back.user_manager import UserManager
from ....Service_layer.redisAPI import UserMapRedis,UserDB
from random import randint, choice
from ....tools.context import redis_context
from ....Service_layer.send_mail import  send_mail_to
from ....Service_layer.mysql_api.user_info import get_user_info_by_user_name
from ....Service_layer.redis_error import TableNotExistError
from ....vemu_config.config import PROJ_CONFIG

class UserRegisterAPI(MethodView):

    '''
    /master/forget_password/
    PUT  填写信息, 接受验证码
    POST 检测验证码，向管理员发送注册信息
    '''

    def put(self):
        '''
        data = {
            "name":用户名,
            "email":邮箱,
        }
        '''

        data = json.loads(request.get_data(as_text=True))
        email = data["email"]
        user = data["name"]

        # 若邮箱功能使能，才进行邮件发送
        if not PROJ_CONFIG.mail_enable:
            return {"code": 0, "msg": "邮箱功能未使能!"}

        register_user_in_redis(data["name"])
       
        
        # 随机生成验证码
        code = ''
        for _ in range(6):
            n = randint(0, 9)
            b = chr(randint(65, 90))
            s = chr(randint(97, 122))
            code += str(choice([n, b, s]))

        # 在数据库中进行持久化
        with redis_context(user) as user_db_cli:
            user_db_cli.set_value('forget_passwd_code', str(time.time()), code)
        
        # 发送忘记密码的邮件
        if send_mail_to(email, "您好！您正在申请注册klonet平台，需要的验证码为：\n\n"+code+"\n\n验证码5分钟内有效，请及时处理，切勿泄露给他人，如非您本人操作，请忽略此邮件。","klonet平台注册验证码"):
            return {'code': 1, 'msg': '邮件成功发送!'}
        else:
            return {'code': 0, 'msg': '邮件发送失败!'}


    def post(self):
        '''
        data = {
            "name": 用户名,
            "password": 密码,
            "phone": 电话,
            "code": 用户输入的验证码,
            "role": 0/1/2,
            "email":邮箱
        }
        '''
        data = json.loads(request.get_data(as_text=True))
        user = data["name"]

        if PROJ_CONFIG.register_audit:
            # 若邮箱功能使能，才进行邮件发送
            if not PROJ_CONFIG.mail_enable:
                return {"code": 0, "msg": "邮箱功能未使能!"}
            
            # 从数据库里读取验证码
            with redis_context(user) as user_db_cli:
                try:
                    user_db_cli.check_table_exist('forget_passwd_code')
                except TableNotExistError:
                    return {'code': 0, 'msg': '缺少数据库信息!'}
                code_dict = user_db_cli.get_all_values('forget_passwd_code')
                user_db_cli.del_table('forget_passwd_code')

            # 字典信息提取
            for key, val in code_dict.items():
                time_send_mail = float(key)  # 时间戳, 检测操作是否超时
                real_code = val              # 真实密码, 与收到的进行匹配

            # 检测时间是否超时，错误
            time_passed = time.time() - time_send_mail
            if time_passed < 0 or time_passed > PROJ_CONFIG.code_max_waiting_time:
                return {'code': 0, 'msg': '验证超时!'}
            elif data["code"] == real_code:
                code1 = ''
                for _ in range(6):
                    n = randint(0, 9)
                    b = chr(randint(65, 90))
                    s = chr(randint(97, 122))
                    code1 += str(choice([n, b, s]))
                audit_dict={"audit_code":code1,"name":data["name"],"password":data["password"],"phone":data["phone"],"email":data["email"],"role":data["role"]}
                with redis_context(user) as user_db_cli:
                    user_db_cli.set_all_values('audit', audit_dict)
                if send_mail_to("2628984534@qq.com", "管理员您好！当前klonet平台用户注册，信息如下：\n\n用户名："+audit_dict["name"]+"\n密码已通过审核\n"+"手机号："+audit_dict["phone"]+"\n邮箱："+audit_dict["email"]+"\n注册角色："+audit_dict["role"]+"\n审核码为："+audit_dict["audit_code"]+",请您及时以管理员身份登录平台，输入用户名、审核码以及审核意见完成注册审核","用户注册审核"):
                    return {'code': 1, 'msg': '恭喜您已通过验证，注册信息已提交管理员，请等待审核通过'}
                else:
                    return {'code': 0, 'msg': '邮件发送失败!'}
            else:
                return {'code': 0, 'msg': '验证码错误!'}
        else:
            data = json.loads(request.get_data(as_text=True))
            register_user_in_redis(data["name"])
            user_manager = UserManager()
            resp = user_manager.register(data["name"], data["password"], data["phone"], data["email"], data["role"])
            return resp

def register_user_in_redis(user):
        try:
            user_re_map = UserMapRedis()
            user_re_map.set_user_db(user)
            user_re_map.close()
        except:
            return {'code': 0, 'msg': '用户redis数据库创建失败'}