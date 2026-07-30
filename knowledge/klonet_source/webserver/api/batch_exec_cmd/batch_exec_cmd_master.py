import json
import traceback
from flask.views import MethodView
from flask import request
from ...tasks.monitor import tasks
from ....Function_layer.master_batch_cmd_exec import exec_cmd_in_workers
from flask_login import login_required
from ....tools.log_tools import FLASK_LOGGER

class BatchExecCmdAPI(MethodView):
    '''
    /master/batch_exec_cmd/
    POST 统一执行命令
    '''
 
    def post(self):
        '''
        向user->topo下的容器发送统一命令。
        容器范围：全部容器/指定类型列表下的所有容器/指定容器列表

        输入：
            {
                'user': "用户名",
                'topo': "项目名（拓扑名）",
                'ctns': {
                    "list_type": "all/specified_ctn_type/specified_ctn_list",
                    "list": [] # all时为空，
                            # specified_ctn_type时为类型列表，如["hosts", "controllers"]
                            # specified_ctn_list时为容器列表，如["h1", "h2", "s1"]
                },
                'cmd': "要执行的命令，如ls"
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

            responses = exec_cmd_in_workers(data)

            return {
                'code': 1,
                'msg': f'success',
                'exec_results': responses
            }
        except Exception as e:
            traceback.print_exc()
            return {
                'code': 0,
                'msg': f'Failed: {repr(e)}',
                'exec_results': {}
            }