import json
from flask.views import MethodView
from flask import request, jsonify
from ....Function_layer.master_node_cmd_exec import node_exec_cmd_in_workers

class SendRDMACmdAPI(MethodView):
    '''
    /master/send_rdma_cmd/
    POST：向指定topo下的某些节点，发送预设的命令。

    输入：
    {
        "user": "用户名",
        "topo": "项目名",
        "nodes": ["h1","h2",...]
    }

    '''
    def post(self):
        try:
            data = request.get_json() # 从前端 POST 请求中提取 JSON 数据 , data是个字典
            user = data["user"] # 获取用户名
            topo = data["topo"] # 获取拓扑名
            nodes = data["nodes"]  # 获取要下发命令的节点列表,['h1', 'h2', ...]

            # 构建 node_and_cmd 字典，每个节点都下发相同的固定命令
            node_and_cmd = {}
            for node in nodes:
                node_and_cmd[node] = [
                    "echo 123 | sudo -S modprobe rdma_rxe",
                    "echo 123 | sudo -S rdma link add rxe_0 type rxe netdev ens3",
                    "echo 123 | sudo -S rdma link"
                ]

            # 构造调用 node_exec_cmd_api 所需的 payload 数据
            request_dict = {
                "user": user,
                "topo": topo,
                "node_and_cmd": node_and_cmd,
                "cmd_timeout_s": 10, # 命令执行超时时间，配置为300s
                "block": "true"
            }

            # 调用发送指令函数
            responses = node_exec_cmd_in_workers(request_dict)

            # 整理返回结果
            simplified_result = {}
            for worker_ip, worker_data in responses.items():
                for node_name, result in worker_data["worker_exec_results"].items():
                    simplified_result[node_name] = result

            return jsonify({
                "code": 1,
                "msg": "success",
                "exec_results": simplified_result
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({
                "code": 0,
                "msg": f"Failed: {repr(e)}",
                "exec_results": {}
            })

