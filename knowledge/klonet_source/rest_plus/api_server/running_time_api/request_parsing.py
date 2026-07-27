from flask import jsonify

from flask_restplus import reqparse, Resource, fields
from ...api_server import api, ns


# model define
# 接口实体
interface_fields = {'name': fields.String(required=True), 'ip': fields.String(required=True),
                    'netmask': fields.String(required=True),
                    'gateway': fields.String(required=True)}

# host
# 前端传来的会是有可能会有缺省的字段，应该如何匹配呢
host_fields = {'name': fields.String(required=True), 'id': fields.String(required=True), 'image_name': fields.String(required=True),
               'type': fields.String(required=True), 'subtype': fields.String(required=True), 'virtualization': fields.String(required=True),
               'interfaces': fields.List(fields.Nested(interface_fields))}

# switch
switch_fields = {'name': fields.String(required=True), 'id': fields.String(required=True), 'image_name': fields.String(required=True),
                 'type': fields.String(required=True), 'subtype': fields.String(required=True), 'stp': fields.Boolean(),
                 'x': fields.String(required=True), 'y': fields.String(required=True),
                 # 控制器的信息
                 'controllers': fields.List(fields.String)}

# controllers
controller_fields = {'name': fields.String(required=True), 'id': fields.String(required=True), 'image_name': fields.String(required=True),
                     'type': fields.String(required=True), 'subtype': fields.String(required=True)}

# router
rip = {'networks': fields.List(fields.String), 'neighbors': fields.List(fields.String), 'version': fields.Integer}
ospf = {'router_id': fields.String(required=True), 'networks': fields.List(fields.List(fields.String)),
        'areas': {'area_id': fields.List(fields.String)}}
bgp = {'asn': fields.String(required=True), 'router_id': fields.String(required=True), 'networks': fields.List(fields.String),
       'neighbors': fields.List(fields.List(fields.String))}
router_fields = {'name': fields.String(required=True), 'id': fields.String(required=True), 'image_name': fields.String(required=True),
                 'type': fields.String(required=True), 'subtype': fields.String(required=True),
                 'config': {'rip': fields.Nested(rip), 'ospf': fields.Nested(ospf), 'bgp': fields.Nested(bgp)},
                 'interfaces': fields.List(fields.Nested(interface_fields))}


# link
link = {'name': fields.String(required=True), 'delay': fields.String(required=True), 'jitter': fields.String(required=True),
        'loss': fields.String(required=True), 'max_bandwidth': fields.String(required=True), 'burst': fields.String(required=True),
        'latency': fields.String(required=True), 'sourceid': fields.String(required=True), 'targetid': fields.String(required=True),
        'source': fields.String(required=True), 'sourceIP': fields.String(required=True), 'sourceType': fields.String(required=True),
        'target': fields.String(required=True), 'targetIP': fields.String(required=True), 'targetType': fields.String(required=True),
}

# 这种是真的有点难表示哦
# 为什么不使用列表呢？？？？？
net = {'hosts': {}, 'switches': {}, 'routers': {}, 'controllers': {}, 'links': {}}
topo_model = {'user': fields.String(required=True), 'topo': fields.String(required=True), 'networks': {
    'net1': fields.Nested(net)
}}

topo_del_fields = {'user': fields.String(required=True), 'topo': fields.String(required=True)}


@ns.route('/master/topo/')
@ns.response(500, 'deploy error')
class RuntimeTopo(Resource):

    @ns.doc('deploy topo')
    @ns.expect(topo_model)
    @ns.marshal(topo_model)
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument('user', type=str, required=True)
        parser.add_argument('topo', type=str, required=True)
        parser.add_argument('networks', type=str, required=True)
        parser.add_argument('user', type=str, required=True)
        try:
            args = parser.parse_args()
            return 'success', 200
        except:
            return 'error', 400

    @ns.doc('delete topo')
    @ns.expect(topo_del_fields)
    # delete 里面如何去检查post的ID呢
    def delete(self):
        return {'code': 1, 'msg': 'success'}
