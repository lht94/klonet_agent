import json
import traceback
from flask.views import MethodView
from flask import request
from ....Function_layer.master_node_cmd_exec import node_exec_cmd_in_workers
from flask_login import login_required
from ....tools.log_tools import FLASK_LOGGER

class NodeExecCmdAPI(MethodView):
    '''
    /master/node_exec_cmd/
    POST 指定容器执行多条命令
    '''

    def post(self):
        '''
        向user->topo下的容器发送统一命令。
        容器范围：指定容器列表

        输入：
            {
                'user': "用户名",
                'topo': "项目名（拓扑名）",
                'node_and_cmd': {
                    "h1":["route add -host 192.168.1.2 dev eth0:0"," route add default gw 192.168.1.1"],
                    "h2":["route add –net 10.0.0.0/24 gw 192.168.1.129","route add default gw 192.168.1.1"]
                },
                'cmd_timeout_s':"1"(命令执行超时时间，默认为1s，可配置(0,300]s)
                "block":"true/false"，是否阻塞执行命令
            }
        
            Returns:
        
        返回：各worker上各容器命令的执行情况
         responses = {
            "worker_ip": {
                "code": 0/1, # 1成功，0失败
                "msg": "success"/"失败原因"
                "exec_results": {
                    "容器名": {
                        "exit_code": "容器命令执行返回码"
                        "output": "容器命令执行输出"
                    },
                    ...
                }
            },
            ...
        }
        '''

        try:
                data = json.loads(request.get_data(as_text=True))            
                FLASK_LOGGER.debug(data)

                for node, cmd in data["node_and_cmd"].items():
                    if not isinstance(cmd, list):
                        return {
                            'code': 0,
                            'msg': f'The node_and_cmd\'s value should be list, '
                                'please check!',
                            'exec_results': {}
                        }

                responses = node_exec_cmd_in_workers(data)

                #对respose进行简化,不显示worker_ip,直接显示各容器执行情况，方便查看使用
                responses2={}

                for worker_ip,_ in responses.items():
                    for node,_ in responses[worker_ip]["worker_exec_results"].items():
                        responses2[node]=responses[worker_ip]["worker_exec_results"][node]

                return {
                    'code': 1,
                    'msg': f'success',
                    'exec_results': responses2
                }
        except Exception as e:
                traceback.print_exc()
                return {
                    'code': 0,
                    'msg': f'Failed: {repr(e)}',
                    'exec_results': {}
                }