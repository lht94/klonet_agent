import json
from pprint import pprint
import traceback
from flask.views import MethodView
from flask import request
from ....Service_layer.worker_node_exec_cmd import exec_cmd_in_node

class NodeExecCmdAPI(MethodView):
    '''
    /worker/batch_exec_cmd/
    POST 在本worker的容器中批量执行命令
    '''
    def post(self):
        '''
        在本worker的指定容器列表中执行该容器对应的命令

        输入：
            {
                "user":user, 
                "topo": topo, 
                "node_list": [], #该worker对应的容器列表 
                "node_and_cmd": {
                    "h1":["route add -host 192.168.1.2 dev eth0:0"," route add default gw 192.168.1.1"],    #指定要执行的多条命令
                    "h2":["route add –net 10.0.0.0/24 gw 192.168.1.129","route add default gw 192.168.1.1"]
                }}
                "cmd_timeout_s":"1"(命令执行超时时间，默认为1s，可配置(0,300]s)
                "block":"true/false"，是否阻塞执行命令
        '''
        try:
            data = eval(request.get_data(as_text=True))
            exec_results = exec_cmd_in_node(data["user"], data["topo"],
                data["node_list"], data["node_and_cmd"], data["cmd_timeout_s"],
                data["block"])
            # exec_results = "ls"
            return_dict = {
                    'code': 1,
                    'msg': 'success',
                    'worker_exec_results': exec_results
                }

            return return_dict
        except Exception as e:
            traceback.print_exc()
            return {
                'code': 0,
                'msg': f'Batch exec cmd in worker failed: {repr(e)}',
                'exec_results': {}
            }