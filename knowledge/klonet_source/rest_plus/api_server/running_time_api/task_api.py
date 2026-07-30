from flask_restplus import Resource, fields, Namespace


task_ns = Namespace('runtime-task')

task_model = task_ns.model('task_resp', {
    'code': fields.Integer(default=1, description='操作状态码'),
    'task_status': fields.String(default='SUCCESS', description='当前执行状态'),
    'task_id': fields.String(description='任务的唯一标识码'),
    'result': fields.String(description='任务的执行结果， 在状态为SUCCESS的时候此字段有值'),
    'msg': fields.String(description='描述信息')
})


@task_ns.route('/task/<string:task_id>')
@task_ns.param('task_id', '任务标识')
class RuntimeTask(Resource):

    @task_ns.doc('得到任务')
    @task_ns.marshal_with(task_model)
    def get(self, task_id):
        pass


# 6e79653262e4   progrium/consul   "/bin/start -server …"   3 seconds ago    Up 3 seconds     0.0.0.0:8400->8400/tcp, 0.0.0.0:8600->53/udp,
# 72ea19789569   progrium/consul   "/bin/start -server …"   4 weeks ago      Up 3 weeks      53/tcp, 53/udp, 8300-8302/tcp, 8400/tcp, 8301-8302/udp, 0.0.0.0:8008->8500/tcp   consul
# cc938807520e   progrium/consul   "/bin/start -server …"   9 minutes ago    Up 9 minutes    53/tcp, 0.0.0.0:8400->8400/tcp, 8300-8302/tcp, 8301-8302/udp, 0.0.0.0:8500->8500/tcp, 0.0.0.0:8600->53/udp   consul