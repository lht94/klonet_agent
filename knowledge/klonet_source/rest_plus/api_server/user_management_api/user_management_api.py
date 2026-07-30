from flask import Flask
from flask_restplus import reqparse, Resource, fields, Namespace

register_ns = Namespace('register', descriptions='user register api')
login_ns = Namespace('login', descriptions='user login api')
logout_ns = Namespace('logout', descriptions='user logout api')

'''
UserRegisterAPI
'''
user_register = register_ns.model('user_register', {
    'name': fields.String(required=True, 
        description='用户名（最大长度为50个字符）'),
    'password': fields.String(required=True,
        description='密码（最大长度为40个字符）'),
    'phone': fields.Integer(required=True,
        description='手机号'),
    'email': fields.String(required=True, 
        description='邮箱（最大长度为50个字符）'),
    'role': fields.Integer(required=True, 
        description=('用户权限，可选值为0/1/2 '  
                     '用户权限：0-普通用户 1-普通管理员 2-超级管理员'))
})

user_register_response = register_ns.model('user_register_response', {
    'code': fields.Integer(default=1, description='0为注册失败。1为注册成功'),
    'msg': fields.String(default='success', 
        description='若注册成功则为success，若注册失败则为错误信息')
})

'''
UserLoginAPI
'''
user_login = login_ns.model('user_login', {
    'name': fields.String(required=True, 
        description='用户名'),
    'password': fields.String(required=True,
        description='密码'),
})

user_login_response = login_ns.model('user_login_response', {
    'code': fields.Integer(default=1, description='0为登录失败。1为登录成功'),
    'msg': fields.String(default='success', 
        description='若登录成功则为success，若登录失败则为错误信息')
})

'''
UserLogoutAPI
'''
user_logout_response = logout_ns.model('user_logout_response', {
    'code': fields.Integer(default=1, description='0为登出失败。1为登出成功'),
    'msg': fields.String(default='success', 
        description='若登出成功则为success，若登出失败则为错误信息')
})

# TODO: 忘记密码/管理员界面相关/个人中心相关等

@register_ns.route('/')
class UserRegisterAPI(Resource):

    @register_ns.doc('user register api')
    @register_ns.expect(user_register)
    @register_ns.marshal_with(user_register_response)
    def post(self):
        '''
        用户注册
        '''
        parser = reqparse.RequestParser()
        parser.add_argument('name', type=str, required=True)
        parser.add_argument('password', type=int, required=True)
        parser.add_argument('phone', type=int, required=True)
        parser.add_argument('email', type=str, required=True)
        parser.add_argument('role', type=int, required=True)
        try:
            return 'success', 200
        except:
            return 'error', 400

@login_ns.route('/')
class UserLoginAPI(Resource):

    @login_ns.doc('user login api')
    @login_ns.expect(user_login)
    @login_ns.marshal_with(user_login_response)
    def post(self):
        '''
        用户登录
        '''
        parser = reqparse.RequestParser()
        parser.add_argument('name', type=str, required=True)
        parser.add_argument('password', type=int, required=True)
        try:
            return 'success', 200
        except:
            return 'error', 400

@logout_ns.route('/')
class UserLogoutAPI(Resource):

    @logout_ns.doc('user logout api')
    @logout_ns.marshal_with(user_logout_response)
    def delete(self):
        '''
        用户登出
        '''
        try:
            return 'success', 200
        except:
            return 'error', 400