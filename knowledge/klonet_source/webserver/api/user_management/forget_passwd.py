import json, time
from random import randint, choice
from flask.views import MethodView
from flask import request

from ....tools.context import redis_context
from ....Service_layer.send_mail import send_forget_passwd_mail_to
from ....Service_layer.mysql_api.user_info import get_user_info_by_user_name
from ....Service_layer.redis_error import TableNotExistError
from ....vemu_config.config import PROJ_CONFIG


class ForgetPasswdAPI(MethodView):
    '''
    /master/forget_password/
    PUT  忘记密码, 发送邮件
    POST 检测验证码
    '''

    def put(self):
        '''
        data = {
            "name":用户名,
        }
        '''
        data = json.loads(request.get_data(as_text=True))
        user = data["name"]

        # 若邮箱功能使能，才进行邮件发送
        if not PROJ_CONFIG.mail_enable:
            return {"code": 0, "msg": "邮箱功能未使能!"}

        # 判断用户是否已经存在
        if not get_user_info_by_user_name(user):
            return {"code": 0, "msg": f"用户[{user}]不存在!"}
        
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
        if send_forget_passwd_mail_to(user, code):
            return {'code': 1, 'msg': '邮件成功发送!'}
        else:
            return {'code': 0, 'msg': '邮件发送失败!'}

    def post(self):
        '''
        data = {
            "name": 用户名,
            "code": 用户输入的验证码,
        }
        '''
        data = json.loads(request.get_data(as_text=True))
        user = data["name"]

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

        # 检测时间是否超时
        time_passed = time.time() - time_send_mail
        if time_passed < 0 or time_passed > PROJ_CONFIG.code_max_waiting_time:
            return {'code': 0, 'msg': '验证超时!'}

        # 检测验证码
        if data["code"] == real_code:
            return {'code': 1, 'msg': '验证成功!'}
        else:
            return {'code': 0, 'msg': '验证码错误!'}
        