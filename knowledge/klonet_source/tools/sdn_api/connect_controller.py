import requests
import copy
from . import config

def _put(self, url_suffix, json=None, data=None):
        return requests.put(url=f"{self.url}{url_suffix}", 
            json=json, data=data)

url = f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/modification/container/"

class Switches(object):

    def __init__(self, user_name, project_name,
                backend_ip=None, backend_port=None):
        super().__init__(backend_ip, backend_port)
        self.user = user_name
        self.project = project_name
        info={}
        payload = {"user": self.user, "topo": self.project, 
            "info": src_node.dictform()}
        resp = self._put("/modification/container/", json=payload)

from ...Service_layer.NEManager import (DynamicNeCreator, DefaultNEDeleter, HostEditor,
                                         SwitchEditor, QuaggaEditor,
                                         OvsEditor, UbuntuEditor, OvsCreator)
from ...Service_layer import NEManager
from ...Service_layer.redisAPI import UserMapRedis

sw=Switches(user_name="sw",project_name="sdn",)

def modify():
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
            print('没有该节点对应的Editor类')
            return {'code': 0, 'msg': '编辑节点失败'}
    _, ne_editor_cls = cls_lst[0]
    ne_editor = ne_editor_cls(topo, name, changed, info, re_cli)
    ne_editor.modify()
    return {'code': 1, "msg": "编辑节点成功"}