import json
from flask.views import MethodView
from flask import request
from flask_login import login_required
from ....Service_layer.permission_manager import check_permission
from ...web_back.user_manager import UserManager
from ....Service_layer.redisAPI import UserMapRedis,UserDB
from ....tools.context import redis_context, user_map_redis_context
from ....Service_layer.send_mail import  send_mail_to
from ....Service_layer.mysql_api.user_info import get_user_info_by_user_name
from ....Service_layer.redis_error import TableNotExistError
from ....vemu_config.config import PROJ_CONFIG

class UserAuditAPI(MethodView):
    '''
    /master/user_audit/
    POST  管理员审核注册
    '''

    def post(self):
        '''
        data = {
            "name":管理员用户名,
            "auditcode":审核码,
            "register_user":被审核用户的用户名,
            "opinion":审核意见("Y"或者"N")
        }
        '''
        data = json.loads(request.get_data(as_text=True))
        auditcode = data["auditcode"]
        user = data["name"]
        register_user=data["register_user"]
        opinion=data["opinion"]
        if not check_permission(user, func_name="SuperDelete.post"):
            return {"code": 0, "msg": '您没有审核注册的权限！'}
        
        with redis_context(register_user) as user_db_cli:
            try:
                user_db_cli.check_table_exist('audit')
            except TableNotExistError:
                return {'code': 0, 'msg': '缺少数据库信息!'}
            audit_dict = user_db_cli.get_all_values('audit')
            
        if not auditcode==audit_dict["audit_code"]:
            return {"code": 0, "msg": '您输入的审核码不正确'}
        
        if opinion=="N":
            with user_map_redis_context() as user_db_map:
                fun=user_db_map.del_user_db(register_user)
            if not fun:
                return {"code": 0, "msg": "用户注册信息删除失败！"}
            elif not PROJ_CONFIG.mail_enable:
                return {"code": 0, "msg": "邮箱功能未使能!"}
            elif send_mail_to(audit_dict["email"], "很遗憾，您的注册信息未通过审核，请检查信息是否符合规定后重新注册","klonet平台注册审核结果"):
                return {'code': 1, 'msg': '您的审核意见为拒绝，已通知该注册用户'}
            else:
                return {'code': 0, 'msg': '邮件发送失败!'}
        elif opinion=="Y":
            if not PROJ_CONFIG.mail_enable:
                return {"code": 0, "msg": "邮箱功能未使能!"}
            user_manager = UserManager()
            resp = user_manager.register(audit_dict["name"], audit_dict["password"], audit_dict["phone"], audit_dict["email"], audit_dict["role"])
            if send_mail_to(audit_dict["email"], "您在klonet平台的注册信息已通过审核，平台注册结果为：\n\n"+resp["msg"],"klonet注册审核结果"):
                with redis_context(register_user) as user_db_cli:
                    user_db_cli.del_table('audit')
                return {'code': 1, 'msg': '您的审核意见为同意，已通知该注册用户'}
            else:
                return {'code': 0, 'msg': '邮件发送失败!'}
        else:
            return {'code': 0, 'msg': '无法识别您的审核意见，请检查输入格式是否正确，注意审核意见栏，若为同意则填写大写字母Y，拒绝则填写大写字母N'}
