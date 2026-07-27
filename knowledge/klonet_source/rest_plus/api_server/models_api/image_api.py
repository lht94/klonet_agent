from flask_restplus import Resource, fields, Namespace
from werkzeug.datastructures import FileStorage

image_ns = Namespace('model-images')


# 应该是可以设置只用展示哪些属性的
image_config = image_ns.model('image_config', {})

image_field = image_ns.model('image', {
    '_id': fields.Integer(),
    'user': fields.String(required=True),
    'if_public': fields.Boolean(required=True, default=False),
    'type': fields.String(required=True),
    'name': fields.String(required=True),
    'config': fields.Nested(image_config),
    'icon': fields.Url(absolute=True),
    'docker_file': fields.String
})

images = image_ns.model('images', {
    'public': fields.List(fields.Nested(image_field)),
    'private': fields.List(fields.Nested(image_field))
})

image_resp = image_ns.model('images_resp', {
    'code': fields.Integer(required=True),
    'msg': fields.String(required=True),
    'images': fields.Nested(images)
})


@image_ns.route('/image/')
class ImageList(Resource):

    @image_ns.marshal_with(image_resp)
    @image_ns.doc('得到镜像列表，包括私仓和共仓')
    def get(self):
        pass

    @image_ns.doc('创建一个镜像')
    @image_ns.marshal_with(image_field)
    def post(self):
        # 这里应该自定义parser
        image_parser = image_field.parser()
        pass


# 应该设计成restful的样子，
# 只不过在访问和删除的时候是需要做权限检查的
@image_ns.route('/image/<int:image_id>/')
class Image(Resource):

    @image_ns.doc('得到镜像')
    @image_ns.marshal_with(image_field)
    def get(self, image_id):
        pass

    @image_ns.doc('修改某个镜像信息')
    @image_ns.marshal_with(image_field)
    def put(self, image_id):
        pass

    @image_ns.doc('删除某个镜像')
    def delete(self, image_id):
        return {
            'code': 0,
            'msg': 'return msg'
        }
