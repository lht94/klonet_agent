from flask_restplus import Resource, fields, Namespace

# 需要举例子
link_ns = Namespace('runtime-link')


link = link_ns.model('link', {
    'link': fields.String(required=True, description='链路名'),
    'ne': fields.String(required=True, description='节点名'),
    'bw_kps': fields.String(required=True, description='带宽'),
    'queue_size_bytes': fields.String(required=True, description='队列大小'),
    'delaty_us': fields.String(required=True, description='时延'),
    'loss': fields.String(required=True, description='丢包'),
})


link_model = link_ns.model('link_model', {
    'user': fields.String(required=True, description='用户名'),
    'topo': fields.String(required=True, description='拓扑名'),
    'links': fields.List(fields.Nested(link))
})

link_resp = link_ns.model('link_resp', {
    'code': fields.Integer(default=1, description='0为操作失败。1为操作成功'),
    'msg': fields.String(default='success', description='操作的信息，报错时则为详细的报错信息'),
})


@link_ns.route('/link/')
class RuntimeLink(Resource):

    @link_ns.doc('添加链路配置')
    @link_ns.expect(link_model, validate=True)
    @link_ns.marshal_with(link_resp)
    def post(self):
        pass

    @link_ns.doc('修改链路配置')
    @link_ns.expect(link_model, validate=True)
    @link_ns.marshal_with(link_resp)
    def put(self):
        pass

    @link_ns.doc('删除链路配置')
    @link_ns.expect(link_model, validate=True)
    @link_ns.marshal_with(link_resp)
    def delete(self):
        pass
