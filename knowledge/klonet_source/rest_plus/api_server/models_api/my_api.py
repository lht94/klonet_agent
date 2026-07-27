from flask import Flask
from flask_restplus import reqparse, Resource, fields, Api, Namespace

my_ns = Namespace('mysql-models',
    descriptions='operation about model data')

'''
ProjectList
'''
project_info = my_ns.model('project_info', {
    'name': fields.String(required=True, description='项目名'),
    'create_time': fields.String(required=True, description='项目创建时间'),
    'modified_time': fields.String(required=True, description='项目修改时间'),
})

get_project_list_response = my_ns.model('get_project_list_response', {
    'code': fields.Integer(default=1, description='0为获取失败。1为获取成功'),
    'project_list': fields.List(fields.Nested(project_info),
        default=[], description='该用户的项目名称列表，未获取成功则为空列表[]'),
    'msg': fields.String(default='success', 
        description='若获取成功则为success，若获取失败则为错误信息')
})

@my_ns.route('/project_list/')
class ProjectList(Resource):

    @my_ns.doc('project_list')
    @my_ns.marshal_with(get_project_list_response)
    def get(self): # 应该就是不需要用户名？
        '''
        获取用户的项目列表
        '''
        try:
            return 'success', 200
        except:
            return 'error', 400

'''
TrafficAPPList
'''
load_traffic_list_response = my_ns.model('traffic_list_response', {
    'code': fields.Integer(default=1, description='0为获取失败。1为获取成功'),
    'traffic_list': fields.List(fields.String,
        default=[], description='该用户的流量服务名称列表'),
    'msg': fields.String(default='success',
        description='若获取成功则为success，若获取失败则为错误信息')
})

@my_ns.route('/project/<int:project_id>/traffic_app_list/')
class TrafficAPPList(Resource):

    @my_ns.doc('traffic_list')
    @my_ns.marshal_with(load_traffic_list_response)
    def get(self):
        '''
        获取流量服务列表
        '''
        try:
            return 'success', 200
        except:
            return 'error', 400

'''
TrafficAPP
'''
get_traffic_app_response = my_ns.model('traffic_app_response', {
    'code': fields.Integer(default=1, description='0为获取失败。1为获取成功'),
    # 应该是只传字符串就可以了吧？然后前端再将字符串作为json解析
    'traffic_app': fields.String(default="", 
        description='该流量服务的配置，未获取成功则为空字符串\"\"'), 
    'msg': fields.String(default='success', 
        description='若获取成功则为success，若获取失败则为错误信息')
})

del_traffic_app_response = my_ns.model('del_traffic_app_response', {
    'code': fields.Integer(default=1, description='0为删除失败。1为删除成功'),
    'msg': fields.String(default='success', 
        description='若删除成功则为success，若删除失败则为错误信息')
})

@my_ns.route('/project/<int:project_id>/traffic_app/<int:traffic_app_id>/')
class TrafficAPP(Resource):
    
    @my_ns.doc('get_traffic_app')
    @my_ns.marshal_with(get_traffic_app_response)
    def get(self):
        '''
        获取流量服务
        '''
        try:
            return 'success', 200
        except:
            return 'error', 400

    @my_ns.doc('del_traffic_app')
    @my_ns.marshal_with(del_traffic_app_response)
    def delete(self):
        '''
        删除流量服务
        '''
        try:
            return 'success', 200
        except:
            return 'error', 400

'''
MonitorEventList
'''
load_monitor_list_response = my_ns.model('monitor_list_response', {
    'code': fields.Integer(default=1, description='0为获取失败。1为获取成功'),
    'monitor_list': fields.List(fields.String,
        default=[], description='该用户的监控服务列表，未获取成功则为空列表[]'),
    'msg': fields.String(default='success',
        description='若获取成功则为success，若获取失败则为错误信息')
})

@my_ns.route('/project/<int:project_id>/monitor_event_list/')
class MonitorEventList(Resource):

    @my_ns.doc('monitor_list')
    @my_ns.marshal_with(load_monitor_list_response)
    def get(self):
        '''
        获取监控服务列表
        '''
        try:
            return 'success', 200
        except:
            return 'error', 400

'''
MonitorEvent
'''
get_monitor_event_response = my_ns.model('monitor_event_response', {
    'code': fields.Integer(default=1, description='0为获取失败。1为获取成功'),
    # 应该是只传字符串就可以了吧？然后前端再将字符串作为json解析
    'monitor_event': fields.String(default="", 
        description='该监控服务的配置，未获取成功则为空字符串\"\"'), 
    'msg': fields.String(default='success', 
        description='若获取成功则为success，若获取失败则为错误信息')
})

del_monitor_event_response = my_ns.model('del_monitor_event_response', {
    'code': fields.Integer(default=1, description='0为删除失败。1为删除成功'),
    'msg': fields.String(default='success', 
        description='若删除成功则为success，若删除失败则为错误信息')
})

@my_ns.route('/project/<int:project_id>/monitor_event/<int:monitor_event_id>/')
class MonitorEvent(Resource):

    @my_ns.doc('get_monitor_event')
    @my_ns.marshal_with(get_monitor_event_response)
    def get(self):
        '''
        获取监控服务
        '''
        try:
            return 'success', 200
        except:
            return 'error', 400

    @my_ns.doc('del_monitor_event')
    @my_ns.marshal_with(del_monitor_event_response)
    def delete(self):
        '''
        删除监控服务
        '''
        try:
            return 'success', 200
        except:
            return 'error', 400

'''
Project
'''
traffic_json = fields.String(required=True, 
    description='字符串化的流量服务配置json')
monitor_json = fields.String(required=True,
    description='字符串化的监控服务配置json')
project_model = my_ns.model('project', {
    'topo': fields.String(required=True, description='字符串化的拓扑json'),
    'traffics': fields.List(traffic_json, default=[], 
        description='流量服务json列表'),
    'monitors': fields.List(monitor_json, default=[], 
        description='监控服务json列表')
})

project_save_as_response = my_ns.model('save_project_response', {
    'code': fields.Integer(default=1, description='0为保存失败。1为保存成功'),
    'msg': fields.String(default='success', 
        description='若保存成功则为success，若保存失败则为错误信息'),
})

delete_project_response = my_ns.model('delete_project_response', {
    'code': fields.Integer(default=1, description='0为删除失败。1为删除成功'),
    'msg': fields.String(default='success', 
        description='若删除成功则为success，若删除失败则为错误信息'),
})

get_project_response = my_ns.model('get_project_response', {
    'code': fields.Integer(default=1, description='0为获取失败。1为获取成功'),
    'msg': fields.String(default='success', 
        description='若保存成功则为success，若保存失败则为错误信息'),
    'project': fields.Nested(project_model),
})

@my_ns.route('/project/<project_name>/')
class Project(Resource):
    @my_ns.doc('project_save_as')
    @my_ns.expect(project_model)
    @my_ns.marshal_with(project_save_as_response)
    def post(self):
        '''
        项目另存为
        '''
        parser = reqparse.RequestParser()
        parser.add_argument('traffics', type=list, required=True)
        parser.add_argument('monitors', type=list, required=True)
        try:
            return 'success', 200
        except:
            return 'error', 400

    @my_ns.doc('delete_project')
    @my_ns.marshal_with(delete_project_response)
    def delete(self):
        '''
        删除项目
        '''
        try:
            return 'success', 200
        except:
            return 'error', 400

    @my_ns.doc('get_project')
    @my_ns.marshal_with(get_project_response)
    def get(self):
        '''
        获取项目
        '''
        try:
            return 'success', 200
        except:
            return 'error', 400