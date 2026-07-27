from flask_restplus import Resource, fields, Namespace


# 需要补充例子
topo_ns = Namespace('runtime-topo')

# model define
# 接口实体
interface_fields = topo_ns.model('runtime_interface', {
    'name': fields.String(required=True),
    'ip': fields.String(required=True),
    'netmask': fields.String(required=True),
    'gateway': fields.String(required=True)})

# host
# 前端传来的会是有可能会有缺省的字段，应该如何匹配呢
host_fields = topo_ns.model('runtime_host', {
    'name': fields.String(required=True), 'id': fields.String(required=True),
    'image_name': fields.String(required=True), 'type': fields.String(required=True),
    'subtype': fields.String(required=True), 'virtualization': fields.String(required=True),
    'interfaces': fields.List(fields.Nested(interface_fields))})
hosts = topo_ns.model('hosts', {'<host_name>': fields.Nested(host_fields)})

# switch
switch_fields = topo_ns.model('runtime_switch', {
    'name': fields.String(required=True), 'id': fields.String(required=True),
    'image_name': fields.String(required=True), 'type': fields.String(required=True),
    'subtype': fields.String(required=True), 'stp': fields.Boolean(),
    'x': fields.String(required=True), 'y': fields.String(required=True),
    # 控制器的信息
    'controllers': fields.List(fields.String)})

switches = topo_ns.model('switches', {'<switch_name>': fields.Nested(switch_fields)})

# controllers
controller_fields = topo_ns.model('runtime_controller', {
    'name': fields.String(required=True), 'id': fields.String(required=True),
    'image_name': fields.String(required=True), 'type': fields.String(required=True),
    'subtype': fields.String(required=True)})

controllers = topo_ns.model('controllers', {'<controller_name>': fields.Nested(controller_fields)})

# router
# router config
net_conf_array = fields.List(fields.String)
rip = topo_ns.model('rip_conf', {
    'enable': fields.Integer(required=True, default=0, description='0启用、1不启用'),
    'networks': fields.List(fields.String),
    'neighbors': fields.List(fields.String),
    'version': fields.Integer(default=2)})

areas_id = topo_ns.model('<area_id>', {'<area_id>': fields.List(fields.String)})
ospf = topo_ns.model('ospf_conf', {
    'enable': fields.Integer(required=True, default=0, description='0启用、1不启用'),
    'router_id': fields.String(required=True),
    'networks': fields.List(fields.List(fields.String)),
    'areas': fields.Nested(areas_id)
})

bgp = topo_ns.model('bgp_conf', {
    'enable': fields.Integer(required=True, default=0, description='0启用、1不启用'),
    'asn': fields.String(required=True), 'router_id': fields.String(required=True),
    'networks': fields.List(fields.String),
    'neighbors': fields.List(fields.List(fields.String))})

router_conf_fields = topo_ns.model('router_conf', {
    'rip': fields.Nested(rip),
    'ospf': fields.Nested(ospf),
    'bgp': fields.Nested(bgp),
})

router_fields = topo_ns.model('runtime_router', {
    'name': fields.String(required=True), 'id': fields.String(required=True),
    'image_name': fields.String(required=True),
    'type': fields.String(required=True), 'subtype': fields.String(required=True),
    'config': fields.Nested(router_conf_fields),
    'interfaces': fields.List(fields.Nested(interface_fields))})
routers = topo_ns.model('routers', {'<router_name>': fields.Nested(router_fields)})


# link
link_field = topo_ns.model('runtime_link', {
    'name': fields.String(required=True),
    'delay': fields.String(required=True), 'jitter': fields.String(required=True),
    'loss': fields.String(required=True), 'max_bandwidth': fields.String(required=True), 'burst': fields.String(required=True),
    'latency': fields.String(required=True),
    'sourceid': fields.String(required=True), 'targetid': fields.String(required=True),
    'source': fields.String(required=True), 'sourceIP': fields.String(required=True), 'sourceType': fields.String(required=True),
    'target': fields.String(required=True), 'targetIP': fields.String(required=True), 'targetType': fields.String(required=True),
})
links = topo_ns.model('runtime_links', {'<link_name>': fields.Nested(link_field)})

# 这里使用的应该是fields
net = topo_ns.model('runtime_net', {
    'hosts': fields.Nested(hosts), 'switches': fields.Nested(switches), 'routers': fields.Nested(routers),
    'controllers': fields.Nested(controllers), 'links': fields.Nested(links)
})
networks = topo_ns.model('runtime_networks', {
    '<net_name>': fields.Nested(net)
})

topo_model = topo_ns.model('topo_model', {
    'user': fields.String(required=True), 'topo': fields.String(required=True),
    'networks': fields.Nested(networks)
})

topo_del_fields = topo_ns.model('topo_del_model', {'user': fields.String(required=True), 'topo': fields.String(required=True)})

response_model = topo_ns.model('topo_response', {
    'code': fields.Integer(default=1, description='0为操作失败。1为操作成功'),
    'msg': fields.String(default='success', description='操作的信息，报错时则为详细的报错信息'),
})

# 要不只做参数检查， 不做文档的展示
# 还要使用example value, 最好是使用example value
@topo_ns.route('/runtime/project/')
class RuntimeTopo(Resource):

    @topo_ns.doc('deploy topo')
    @topo_ns.expect(topo_model, validate=True)
    @topo_ns.marshal_with(response_model)
    def post(self):
        pass

    @topo_ns.doc('delete topo')
    @topo_ns.expect(topo_del_fields, validate=True)
    @topo_ns.marshal_with(response_model)
    def delete(self):
        pass


project_fields = topo_ns.model('project', {
    'name': fields.String(required=True, description='')
})


@topo_ns.route('/project/')
class Topo(Resource):

    @topo_ns.doc('得到已创建的项目列表')
    @topo_ns.marshal_list_with(project_fields)
    def get(self):
        pass
