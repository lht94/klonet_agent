from flask import Flask
from flask_restplus import reqparse, Resource, fields, Namespace
from flask_restplus.utils import default_id

expr_monitor_ns = Namespace('expr_monitor', descriptions='expr monitor api')

'''
monitor_deplpy
'''
monitor_ne_info = expr_monitor_ns.model('monitor_ne_info', {
    'ne_name': fields.String(required=True, description='节点名'),
    'nic_ip': fields.String(required=True, description='节点ip地址'),
    'port': fields.String(required=False, default="\"\"", 
        description='流量的端口号'),
})

monitor_event_params = expr_monitor_ns.model('monitor_event_params', {
    'src': fields.Nested(monitor_ne_info, required=True, 
        description='源节点信息'),
    'dst': fields.Nested(monitor_ne_info, required=True,
        description='目的节点信息'),
    'proto_type': fields.String(required=True, 
        description='流量协议类型（当前可选项为tcp/udp）'),
})

# 不用的performance对应不同的params，这个逻辑怎么写？主要是为了以后的扩展
monitor_event = expr_monitor_ns.model('monitor_event', {
    'performance': fields.String(required=True, 
        description='要监控的性能指标（当前可选项为throughput/loss/delay）'),
    'params': fields.Nested(monitor_event_params, required=True, 
        description='监控参数'),
})

monitor_deplpy = expr_monitor_ns.model('monitor_deplpy', {
    'user': fields.String(required=True, description='用户名'),
    'expr': fields.String(required=True, description='监控服务名'),
    'topo': fields.String(required=True, description='拓扑名'),
    'events_to_monitor': fields.List(fields.Nested(monitor_event), 
        required=True, description='监控子事件')
})

monitor_deplpy_response = expr_monitor_ns.model('monitor_deplpy_response', {
    'code': fields.Integer(default=1, description='0为操作失败。1为操作成功'),
    'msg': fields.String(default='success',
        description='操作的信息，报错时则为详细的报错信息'),
})

'''
monitor_terminate
'''
monitor_terminate = expr_monitor_ns.model('monitor_terminate', {
    'user': fields.String(required=True, description='用户名'),
    'expr': fields.String(required=True, description='监控服务名'),
    'topo': fields.String(required=True, description='拓扑名'),
})
monitor_terminate_response = expr_monitor_ns.model(
    'monitor_terminate_response', {
        'code': fields.Integer(default=1, 
            description='0为操作失败。1为操作成功'),
        'msg': fields.String(default='success', 
            description='操作的信息，报错时则为详细的报错信息'),
})

'''
monitor_result
'''
monitor_result = expr_monitor_ns.model('monitor_result', {
    'user': fields.String(required=True, description='用户名'),
    'expr': fields.String(required=True, description='监控服务名'),
    'topo': fields.String(required=True, description='拓扑名'),
    'data_type': fields.String(required=True, 
        description='数据类型，perf/raw 二选一'),
    'event_seq': fields.String(default="",
        description='监控子事件序号'),
})

monitor_result_resp = expr_monitor_ns.model('monitor_result_resp', {
    'code': fields.Integer(default=1, description='0为获取失败。1为获取成功'),
    'files': fields.List(fields.String,
        default=[], description='监控结果文件列表'),
    'msg': fields.String(default='success', 
        description='操作的信息，报错时则为详细的报错信息')  
})

@expr_monitor_ns.route('/')
class ExprMonitor(Resource):

    @expr_monitor_ns.doc('deploy monitor')
    @expr_monitor_ns.expect(monitor_deplpy)
    @expr_monitor_ns.marshal_with(monitor_deplpy_response)
    def post(self):
        '''
        启动网络实验监控
        '''
        parser = reqparse.RequestParser()
        parser.add_argument('user', type=str, required=True)
        parser.add_argument('expr', type=str, required=True)
        parser.add_argument('topo', type=str, required=True)
        parser.add_argument('events_to_monitor', type=list, required=True)
        try:
            args = parser.parse_args()
            return 'success', 200
        except:
            return 'error', 400

    @expr_monitor_ns.doc('terminate monitor')
    @expr_monitor_ns.expect(monitor_terminate)
    @expr_monitor_ns.marshal_with(monitor_terminate_response)
    # delete 里面如何去检查post的ID呢
    def delete(self):
        '''
        终止网络实验监控
        '''
        parser = reqparse.RequestParser()
        parser.add_argument('user', type=str, required=True)
        parser.add_argument('expr', type=str, required=True)
        parser.add_argument('topo', type=str, required=True)
        try:
            args = parser.parse_args()
            return 'success', 200
        except:
            return 'error', 400

@expr_monitor_ns.route('/result/')
class ExprMonitorResult(Resource):
    @expr_monitor_ns.doc('expr monitor result')
    @expr_monitor_ns.expect(monitor_result)
    @expr_monitor_ns.marshal_with(monitor_result_resp)
    def post(self):
        '''
        获取网络实验监控结果
        '''
        parser = reqparse.RequestParser()
        parser.add_argument('user', type=str, required=True)
        parser.add_argument('expr', type=str, required=True)
        parser.add_argument('topo', type=str, required=True)
        parser.add_argument('data_type', type=str, required=True)
        parser.add_argument('event_seq', type=str, required=True)
        try:
            args = parser.parse_args()
            return 'success', 200
        except:
            return 'error', 400

