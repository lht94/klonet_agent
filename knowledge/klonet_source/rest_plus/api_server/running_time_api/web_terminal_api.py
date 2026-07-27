from flask import Flask
from flask_restplus import reqparse, Resource, fields, Namespace
from flask_restplus.utils import default_id

web_terminal_ns = Namespace('web_terminal', descriptions='web terminal api')

web_terminal_response = web_terminal_ns.model('monitor_result_response', {
    'code': fields.Integer(default=1, description='0为获取失败。1为获取成功'),
    'msg': fields.String(default='success', 
        description='操作的信息，报错时则为详细的报错信息'),
})

@web_terminal_ns.route('/<project_name>/<ne_name>/')
class WebTerminal(Resource):
    @web_terminal_ns.doc('expr monitor result')
    @web_terminal_ns.marshal_with(web_terminal_response)
    def get(self):
        try:
            return 'success', 200
        except:
            return 'error', 400