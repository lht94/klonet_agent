from functools import wraps
from flask_login import current_user
from flask import current_app
from ....webserver import mysql
from ....Service_layer.mysql_manager import count
from ....Service_layer.mysql_models import Authorities, Roles, RoleAuthority, UserRole
from ....Service_layer.mysql_api.auth import get_role_id_by_role_name, check_authority_by_user_id

def permission_required(func):
    '''
    权限验证装饰器
    注意：请确保被装饰器装饰的方法已包含在权限相关表中，否则查不到该方法相关的表项时将会
          拒绝调用该方法。
    注意：已包含@login_required功能，无需再加@login_required装饰器

    例：
    @permission_required
    def get(self):
        print("in get")
    '''
    @wraps(func)
    def decorated_view(*args, **kwargs):
        # 验证用户是否登录
        if not current_user.is_authenticated:
            return current_app.login_manager.unauthorized()
        
        authority_name = func.__qualname__

        auth = Authorities.query.filter_by(
            authority_name=authority_name).first()
        if auth:
            authority_id = auth.authority_id
        else:
            print(f"Authority [{authority_name}] don\'t in authority list.")
            return {
                "code":3,
                "msg":("Authentication failed. "
                      "Current authority don\'t in authority list.")}

        # 查询当前用户是否拥有此项权限
        if check_authority_by_user_id(
            current_user.user_id, authority_id):
            print(f"user: {current_user.user_id} action: {authority_name} pass")
            return func(*args, **kwargs)
        else:
            print(f"user: {current_user.user_id} action: {authority_name} denied")
            return {"code":2, "msg":"Authentication failed. No permission."}

    return decorated_view
    
    
            

            