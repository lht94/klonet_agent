import json
from pprint import pprint
import traceback
from flask.views import MethodView
from flask import request
from ....Service_layer.worker_batch_cmd_exec import batch_exec_cmd_in_ctns

class BatchExecCmdAPI(MethodView):
    '''
    /worker/batch_exec_cmd/
    POST 在本worker的容器中批量执行命令
    '''
    def post(self):
        '''
        在本worker的容器中批量执行命令

        输入：
            {
                'ctn_list':  ["要执行命令的容器列表"],
                'cmd': "要执行的命令，如ls"
            }
        '''
        try:
            
            data = eval(request.get_data(as_text=True))

            exec_results = batch_exec_cmd_in_ctns(data["user"], data["topo"],
                data["ctn_list"], data["cmd"], data["cmd_timeout_s"], data["block"])
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