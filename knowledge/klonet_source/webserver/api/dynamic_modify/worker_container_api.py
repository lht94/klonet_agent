import json
import inspect
import subprocess

from flask import request
from flask.views import MethodView

from ....Service_layer.NEManager import (DynamicNeCreator, DefaultNEDeleter, HostEditor,
                                         SwitchEditor, QuaggaEditor,
                                         OvsEditor, UbuntuEditor, OvsCreator)
from ....Service_layer import NEManager
from ....Service_layer.redisAPI import UserMapRedis
from ....Service_layer.redis_error import TableNotExistError
from ....tools.log_tools import FLASK_LOGGER
from ....Function_layer.master_node_cmd_exec import node_exec_cmd_in_workers

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


class DynamicContainerAPI(MethodView):
    """
    动态创建节点相关接口
    """
    def post(self):
        """
        动态创建节点容器API
        
        POST /modification/container/
        
        Args:
            data (dict): 用户名-拓扑名-节点名信息字典
            
        Returns:
            dict: 执行结果字典
        
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
        
        DELETE /modification/container/
        
        Args:
            data (dict): 用户名-拓扑名-节点名的字典
            
        Returns:
            dict: 执行结果字典
        
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
        
        PUT /modification/container/
        
        Args:
            data (dict): 用户名-拓扑名-节点名-变更信息的字典
            
        Returns:
            dict: 执行结果字典
        
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

class ContainerStartAPI(MethodView):
    """
    启动指定容器ID(NAMES)的API
    POST /worker/container/start/
    入参: {"container_id": "xxx"}
    """
    def post(self):
        data = request.get_json()
        # data = json.loads(request.get_data(as_text=True))
        container_id = data.get("container_id")
        if not container_id:
            return {"code": 0, "msg": "缺少container_id"}
        try:
            subprocess.check_call(["docker", "start", container_id]) #docker start <container_id>
            return {"code": 1, "msg": "容器启动成功"}
        except Exception as e:
            return {"code": 0, "msg": f"容器启动失败: {str(e)}"}

class OvsStartAPI(MethodView):
    """
    启动指定OVS交换机的Open vSwitch服务
    POST /worker/ovs/start/
    入参: {"user":"xxx","topo":"xxx","ovs_name":"xxx"}
    """
    def post(self):
        data = request.get_json()
        # data = json.loads(request.get_data(as_text=True))
        user = data["user"] # 获取用户名
        topo = data["topo"] # 获取拓扑名
        ovs_name = data["ovs_name"]  # 获取要下发命令的OVS名称's1'
        request_dict = {
            "user": user,
            "topo": topo,
            "node_and_cmd": {
                    ovs_name:["service openvswitch-switch start"] # 启动Open vSwitch服务
            },
            "cmd_timeout_s": 10, # 命令执行超时时间，配置为300s
            "block": "true"
        }
        try:
            responses = node_exec_cmd_in_workers(request_dict) # 发送指令到指定OVS容器
            return {"code": 1, "msg": "OVS服务配置成功"}
        except Exception as e:
            return {"code": 0, "msg": f"OVS服务配置失败: {str(e)}"}