from flask.views import MethodView
from flask import request
import requests
from ....tools.context import redis_context, judge_user_exist, check_table_key
from ....vemu_config.config import PROJ_CONFIG
import json
import traceback

class OvsCmdAPI(MethodView):
    '''
    /master/ovs_cmd/
    POST 指定ovs容器执行命令
    
    '''
    def post(self):
        '''
        向user->topo下的某一个ovs容器发送配置命令
        容器范围：仅限于ovs容器，有检查机制
        
        输入：
            {
                'user': "用户名",
                'topo': "项目名（拓扑名）",
                'node_name', "节点名称"
                'option': "执行的命令类型ovs-vsctl/ovs-ofctl",
                'action': "具体动作",
                'content': "动作具体内容"
            }
        
        返回：
            执行情况，如：
            {
                'code': 1,
                'exec_results': {
                    's1': {
                        '0_ovs-ofctl dump-flows init-br0': {
                            'exit_code': 0,
                            'output': 'NXST_FLOW reply (xid=0x4):\n cookie=0x0, duration=11305.282s, table=0, n_packets=34, n_bytes=1988, idle_age=11262, priority=0 actions=NORMAL\n'
                            }
                        }
                    },
                'msg': 'success'
            }
        '''
        data = json.loads(request.get_data(as_text=True))
        user = data['user']
        topo = data['topo']
        node_name = data['node_name']
        topo_list_table = "topo_list"
        # 基本检查
        if not judge_user_exist(user):
            msg = {'code': 0, 'msg': f'用户{user}不存在'}
            return msg
        if not check_table_key(user, topo_list_table, topo):
            msg = {'code': 0, 'msg': f'拓扑{topo}不存在'}
            return msg
        # 检查节点是否为ovs
        with redis_context(user)as user_db_cli:
            switch_list = user_db_cli.get_value(topo_list_table, topo)['networks']['switches']
            ovs_list = []
            for key, value in switch_list.items():
                if value['image_name'] == 'switch/ovs':     # 仅支持平台默认的ovs镜像
                    ovs_list.append(key)
            if not node_name in ovs_list:
                return {'code': 0, 'msg': f'ovs节点{node_name}不存在，ovs的节点有{ovs_list}'}
        # 执行配置命令
        # 调用容器执行命令接口，拼接payload
        try:
            content = data['content'][0] if len(data['content']) == 1 else ''
            cmd = (f"{data['option']} {data['action']} {content}")    # 尽力执行，懒得检查了
            pay_load = {
                'user': user,
                'topo': topo,
                'node_and_cmd':{
                    node_name: [cmd],  
                },
                'cmd_timeout_s': "1",
                'block': 'true'    # 阻塞执行
            }
            req_url = (f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/node_exec_cmd/")
            resp = requests.post(req_url, json=pay_load)
            print(resp.json())
        except Exception:
            traceback.print_exc()
        return resp.json()