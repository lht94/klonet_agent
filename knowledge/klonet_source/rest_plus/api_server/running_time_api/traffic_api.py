from flask_restplus import Resource, fields, Namespace

# 需要添加测试用例
traffic_ns = Namespace('runtime-traffic')


# pkt_gen2
pkt_length = traffic_ns.model('pkt_length', {
    '<value>': fields.String(required=True, default='1', description='value的权重'),
})

pkt_gen2 = traffic_ns.model('pkt_gen2', {
    'src': fields.String(required=True, description='源目的名'),
    'dst': fields.String(required=True, description='目的节点名'),
    'src_ip': fields.String(required=True, description='源ip'),
    'dst_ip': fields.String(required=True, description='目的ip'),
    'rate': fields.String(required=True, description='on状态平均速率'),
    'pkt_length': fields.Nested(pkt_length, required=True),
    'duration': fields.String(required=True, description='应用持续时间'),
    'on_k': fields.String(required=True, description='on状态pareto分布的特性参数'),
    'on_min': fields.String(required=True, description='on状态pareto分布的尺度参数'),
    'off_k': fields.String(required=True, description='off状态pareto分布的特性参数'),
    'off_min': fields.String(required=True, description='off状态pareto分布的尺度参数'),
})


# traffic gen
req_size_dst_fields = traffic_ns.model('req_size', {
    '<value>': fields.String(required=True, description='#请求大小的分布(CDF)', default='1')
})
dscp_fields = traffic_ns.model('dscp', {
    '<value>': fields.String(required=True, description='请求大小分布', default='1')
})
rate_fields = traffic_ns.model('rate', {
    '<value>': fields.String(required=True, description='发送速率及权重', default='1')
})
fanout_fields = traffic_ns.model('fanout', {
    '<value>': fields.String(required=True, description='扇出值及权重', default='1')
})
cli_para_fields = traffic_ns.model('cli_param', {
    'b': fields.String(required=True, description='期望平均RX带宽'),
    'n': fields.String(required=True, description='请求个数'),
    't': fields.String(required=True, description='请求时间，与请求个数不能同时存在'),
    's': fields.String(required=True, description='生成随机数的种子，可不填')
})
client_config = traffic_ns.model('client_conf', {
    'server_list': fields.List(fields.String(required=True, default='<server_name>:<port>',
                                             description='server的节点名以及端口')),
    'req_size_dst': fields.Nested(req_size_dst_fields),
    'dscp': fields.Nested(dscp_fields),
    'rate': fields.Nested(rate_fields),
    'fanout': fields.Nested(fanout_fields)
})
traffic_gen_client = traffic_ns.model('traffic_cli', {
    'client_name': fields.String(required=True, description='客户端节点名'),
    'client_config': fields.Nested(client_config)
})
traffic_gen = traffic_ns.model('traffic_gen', {
    'mode': fields.String(required=True, description='0，1代表client和incast_client两种模式'),
    'server_list': fields.List(fields.String(required=True, default='<server_name>:<port>',
                                             description='server的节点名以及端口')),
    'client': fields.Nested(traffic_gen_client),
    'cli_param': fields.Nested(cli_para_fields),
})


# pkt_gen1
cons_para = traffic_ns.model('cons_para', {
    'size': fields.String(required=True, default='', description='时间间隔常量值')
})
exp_para = traffic_ns.model('exp_para', {
    'beta': fields.String(required=True, default='', description='exp参数')
})
normal_para = traffic_ns.model('normal_para', {
    'mu': fields.String(required=True, default='', description='normal均值'),
    'sigma': fields.String(required=True, default='', description='normal方差')
})
pareto_para = traffic_ns.model('pareto_para', {
    'min': fields.String(required=True, default='', description='pareto尺度参数'),
    'k': fields.String(required=True, default='', description='pareto特性参数')
})
mode_para = traffic_ns.model('mode_para', {
    'size': fields.String(default='', description='mode=constant, 有此值，时间间隔常量值'),
    'beta': fields.String(default='', description='mode=exp, 有此值，exp参数'),
    'mu': fields.String(default='', description='mode=normal, 有此值，normal均值'),
    'sigma': fields.String(default='', description='mode=normal, 有此值，normal方差'),
    'min': fields.String(default='', description='mode=pareto, 有此值，pareto尺度参数'),
    'k': fields.String(default='', description='mode=pareto, 有此值，pareto特性参数')
})
pkt_interval = traffic_ns.model('pkt_interval', {
    'mode': fields.String(required=True, default='', description='constant/exp/normal/pareto中的一个'),
    'para': fields.Nested(mode_para)
})
pkt_gen1 = traffic_ns.model('pkt_gen1', {
    'src': fields.String(required=True, description='源目的名'),
    'dst': fields.String(required=True, description='目的节点名'),
    'src_ip': fields.String(required=True, description='源ip'),
    'dst_ip': fields.String(required=True, description='目的ip'),
    'pkt_num': fields.String(required=True, description='指定的发包个数'),
    # 这里如何表示多种中的一个呢?
    'pkt_interval': fields.Nested(pkt_interval, required=True, description='#有4种模式，根据具体选择的模式填入相应参数'),
})


traffic_model = traffic_ns.model('traffic_model', {
    'user': fields.String(required=True),
    'topo': fields.String(required=True),
    'app_name': fields.String(required=True, description='流量标识'),
    'pkt_gen1': fields.List(fields.Nested(pkt_gen1)),
    'pkt_gen2': fields.List(fields.Nested(pkt_gen2)),
    'traffic_gen': fields.List(fields.Nested(traffic_gen))
})

traffic_app_model = traffic_ns.model('traffic_app_model', {
    'app_name': fields.String(required=True, description='流量标识'),
    'pkt_gen1': fields.List(fields.Nested(pkt_gen1)),
    'pkt_gen2': fields.List(fields.Nested(pkt_gen2)),
    'traffic_gen': fields.List(fields.Nested(traffic_gen))
})


resp_model = traffic_ns.model('traffic_response', {
    'code': fields.Integer(default=1, description='0为操作失败。1为操作成功'),
    'msg': fields.String(default='success', description='操作的信息，报错时则为详细的报错信息'),
})

tra_del_fields = traffic_ns.model('topo_del_model', {
    'user': fields.String(required=True),
    'topo': fields.String(required=True),
    'app_name': fields.String(required=True, description='流量标识')
})


# 其实创建的时候是不需要发送user 和 topo 字段了
@traffic_ns.route('/runtime/traffic_app/')
class RuntimeTraffic(Resource):

    @traffic_ns.doc('创建流量')
    @traffic_ns.expect(traffic_model, validate=True)
    @traffic_ns.marshal_with(resp_model)
    def post(self):
        pass

    @traffic_ns.doc('停止流量创建')
    @traffic_ns.expect(tra_del_fields, validate=True)
    @traffic_ns.marshal_with(resp_model)
    def delete(self):
        pass


@traffic_ns.route('/project/<string:project_name>/traffic_app/')
class TrafficAppList(Resource):

    @traffic_ns.doc('得到运行时实验流量数据列表')
    @traffic_ns.marshal_list_with(traffic_app_model)
    def get(self):
        pass

    @traffic_ns.doc('新建运行时流量数据')
    @traffic_ns.expect(traffic_app_model, validate=True)
    @traffic_ns.marshal_with(traffic_app_model)
    def post(self):
        pass

    @traffic_ns.doc('删除运行时实验流量数据列表')
    def delete(self):
        return {}


@traffic_ns.route('/project/<string:project_name>/traffic_app/<string:app_name>/')
class TrafficApp(Resource):

    @traffic_ns.doc('得到运行时某个流量数据信息')
    @traffic_ns.marshal_with(traffic_app_model)
    def get(self):
        pass

    @traffic_ns.doc('删除运行时某个流量数据信息')
    @traffic_ns.expect(traffic_app_model, validate=True)
    def delete(self):
        pass

    @traffic_ns.doc('修改运行时某个流量数据信息')
    @traffic_ns.expect(traffic_app_model, validate=True)
    @traffic_ns.marshal_with(traffic_app_model)
    def put(self):
        pass
