import json
import inspect

from flask import request
from flask.views import MethodView

from ....Service_layer.NEManager import (DynamicNeCreator, DefaultNEDeleter, HostEditor,
                                         SwitchEditor, QuaggaEditor,
                                         OvsEditor, UbuntuEditor, OvsCreator)
from ....Service_layer import NEManager
from ....Service_layer.redisAPI import UserMapRedis
from ....Service_layer.redis_error import TableNotExistError
from ....tools.log_tools import FLASK_LOGGER

def get_ne_editor_by_subtype(ne_subtype=None):
    """
    根据节点子类型动态检查相对应的动态操作的功能代理类
    返回顺序 优先子类， 再父类， 父类应该在子类的
    Args:
        ne_subtype (str): 节点的子类型

    Returns:
        cls  (class): 返回对应的类
    """
    def validate(value):
        return inspect.isclass(value) and (value.__name__ == f'{ne_subtype.capitalize()}Editor')

    cls = inspect.getmembers(NEManager, validate)
    return cls


def get_ne_editor_by_type(ne_type):
    """
    根据节点主类型动态检查相对应的动态操作的功能代理类
    返回顺序 优先子类， 再父类， 父类应该在子类的
    Args:
        ne_type (str): 节点的主类型

    Returns:
        cls  (class): 返回对应的类
    """
    def validate(value):
        return inspect.isclass(value) and (value.__name__ == f'{ne_type.capitalize()}Editor')

    cls = inspect.getmembers(NEManager, validate)
    return cls


class DynamicKvmAPI(MethodView):
    """
    动态创建节点相关接口
    """
    def post(self):
        """
        动态创建节点容器API
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, name = data['user'], data['topo'], data['name']
        user_db_map = UserMapRedis()
        re_cli = user_db_map.get_user_db(user)
        user_db_map.close()
        ne_creator = DynamicNeCreator(user, topo, name, re_cli)
        ne_creator.create_and_run()
        ne_creator.close()
        return {'code': 1, 'msg': '创建节点成功'}

    def delete(self):
        """
        动态删除节点容器API
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, name = data['user'], data['topo'], data['name']
        user_db_map = UserMapRedis()
        re_cli = user_db_map.get_user_db(user)
        user_db_map.close()
        table = f'{topo}_{name}'
        info = re_cli.get_all_values(table)
        # 删除节点实体
        info.update({'user': user})
        info.update({'topo': topo})
        info.update({'ne_name': name})
        ne_deleter = DefaultNEDeleter(info)
        ne_deleter.stop_and_delete_dynamic(topo, name, re_cli)
        # 删除数据库中的端口映射表
        try:
            re_cli.check_table_exist(f'{topo}_port_mapping')
            re_cli.del_value(f'{topo}_port_mapping', name)
        except TableNotExistError:
            pass
        re_cli.close()
        return {'code': 1, 'msg': '删除节点成功'}

    def put(self):
        """
        动态修改节点属性API
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, name, changed = data['user'], data['topo'], data['name'], data['changed']
        # 检查changed中有改变的实例， 调用更底层的接口处理
        user_db_map = UserMapRedis()
        re_cli = user_db_map.get_user_db(user)
        user_db_map.close()
        table = f'{topo}_{name}'
        info = re_cli.get_all_values(table)
        cls_lst = get_ne_editor_by_subtype(info['NEsubtype'])
        # 这里需要返回主type类的代码
        if not cls_lst:
            cls_lst = get_ne_editor_by_type(info['NEtype'])
            if not cls_lst:
                FLASK_LOGGER.error('没有该节点对应的Editor类')
                return {'code': 0, 'msg': '编辑节点失败'}
        _, ne_editor_cls = cls_lst[0]
        ne_editor = ne_editor_cls(topo, name, changed, info, re_cli)
        ne_editor.modify()
        return {'code': 1, "msg": "编辑节点成功"}
