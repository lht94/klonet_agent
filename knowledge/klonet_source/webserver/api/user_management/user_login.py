import json
from locale import currency
from flask.views import MethodView
from flask import request
from flask_login import current_user
from ...web_back.user_manager import UserManager
from ...web_back.authority_management.authority_manager import permission_required
from ....Service_layer.mysql_api.auth import get_authority_id_by_authority_name
from ....Service_layer.permission_manager import get_user_role_by_name
from ....tools.log_tools import UserLogLevel,UserLogger
from ....Service_layer.redisAPI import UserMapRedis
from ....Service_layer.redis_error import (DbCreateFailedError, 
        KeyNotExistError, NoFreeDbForUserError, DbAlreadyExistError)

user_db_map = UserMapRedis()

class UserLoginAPI(MethodView):
    
    '''
    /master/user_login/
    POST  用户登录
    '''
    def post(self):
        '''
        data = {
            "name":用户名,
            "password":密码,
        }
        '''
        data = json.loads(request.get_data(as_text=True))

        user_manager = UserManager()

        login_result = user_manager.login(data["name"], data["password"])
        if login_result == 1:
            #日志输出
            user = data['name']
            #检查是否有数据库
            try:
                user_db_cli = user_db_map.set_user_db(user)
            except DbAlreadyExistError:
                user_db_cli = user_db_map.get_user_db(user)
            except NoFreeDbForUserError:
                return {'code': 0, 'msg': '数据库用户数目已达上限'}
            except DbCreateFailedError:
                return {'code': 0, 'msg': '用户数据库创建失败'}
            logger = UserLogger(user, UserLogLevel.First)
            logger.log_to_mysql(f'用户登录')
            resp = {'code': 1, 'msg': '登录成功！', 'role': get_user_role_by_name(data["name"])}  # tb:添加了当前用户的role返回值


        elif login_result == 0:
            resp = {"code": 0, "msg": f"登录失败！用户名或密码错误。"} 
        else:
            resp = {"code": 0, "msg": f"登录失败！"} 

        return resp

    # test
    def get(self):
        result = get_authority_id_by_authority_name("UserLoginAPI.get")
        print(result)
        return {"code": 0, "msg": f"请先登录！"}

    @permission_required
    def delete(self):
        print("in delete")
        return {"code": 0, "msg": f"请先登录！"}

    # @permission_required
    def put(self):
        print("in put")
        return {"code": 0, "msg": f"请先登录！"}