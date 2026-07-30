import json
import traceback
from flask import request
from flask.views import MethodView
from ....Function_layer.server_health_master import WorkerHealthGuardian
from flask_login import login_required

class HeartbeatApi(MethodView):
    """
    master接收来自worker的心跳包
    POST /master/heartbeat/
    """

    def post(self):
        try:
            # 从请求中的 json 获得worker的信息
            worker_info = json.loads(request.get_data(as_text=True))
            # 得到worker的ip
            worker_ip = worker_info["worker_ip"]
            # master接收心跳包后，更新heartbeat表中worker最近一次心跳的时间戳
            guardian = WorkerHealthGuardian()
            guardian.on_recv_heartbeat(worker_ip)
            # 给worker返回确认信号
            return {"code": 1, "msg": "Ack!"}
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": f"{str(e)}"}


class QuerySingleProjectHealthApi(MethodView):
    """
    检查单项目在心跳机制下的健康情况
    GET /master/heartbeat_health/?user=<user_name>&project=<project_name>
    """

    def get(self):
        try:
            # 从请求中的 json 获得信息
            data = request.args
            # 参数检查
            # 应包含用户名和项目名
            if (not data.get("user")) or (not data.get("project")):
                return {"code": 0, "msg": "Query Params should be /master/"
                    "heartbeat_health/?user=<user_name>&project=<project_name>"
                    ", please check!"}
            # 获取当前项目状态
            guardian = WorkerHealthGuardian()
            broken_status = guardian.get_project_broken_status(
                data.get("user"), data.get("project"))
            # 返回项目状态
            return {
                "code": 1,
                "msg": "success",
                "is_broken": broken_status["is_broken"],
                "broken_nes": broken_status["broken_nes"]
            }
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}


class QueryUserAllProjectHealthApi(MethodView):
    """
    检查某用户的所有项目在心跳机制下的健康情况
    GET /master/heartbeat_health_all/?user=<user_name>
    """

    def get(self):
        try:
            # 从请求中的 json 获得用户数据
            data = request.args
            # 参数检查
            # 应包含用户名
            if not data.get("user"):
                return {"code": 0, "msg": "Query Params should be /master/"
                    "heartbeat_health_all/?user=<user_name>, please check!"}
            # 获取用户所有项目状态
            guardian = WorkerHealthGuardian()
            broken_status = guardian.get_user_all_broken_projects(
                data.get("user"))
            # 返回项目状态
            return {
                "code": 1,
                "msg": "success",
                "broken_status": broken_status
            }
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}
    