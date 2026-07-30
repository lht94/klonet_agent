import json
import traceback

from flask import request
from flask.views import MethodView
from ....Service_layer.link_health_worker import LinkCheckerWorker
from ....tools.log_tools import FLASK_LOGGER

class LinkCheckerAPI(MethodView):
    '''
    /worker/checklink/
    '''
    def post(self):
        '''
        启动链路检查
        '''
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            user, project_name, subtopo, is_check_once, check_interval_s = \
                data["user"], data["project_name"], data["subtopo"], \
                data["is_check_once"], data["check_round_interval_s"]
            # 启动链路检查
            link_checker = LinkCheckerWorker(user, project_name, subtopo)
            has_vxlan, broken_vxlans = link_checker.start_check_process(
                is_check_once, check_interval_s)
            # 返回
            return {
                "code": 1,
                "msg": "This topo does not have vxlan!" \
                    if not has_vxlan else "check sucess",
                "broken_vxlans": broken_vxlans
            }
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}

    def delete(self):
        '''
        停止链路检查
        '''
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            FLASK_LOGGER.debug(data)
            user, project_name, subtopo = \
                data["user"], data["project_name"], data["subtopo"]
            # 停止链路检查
            link_checker = LinkCheckerWorker(user, project_name, subtopo)
            link_checker.stop_processes("checklink")
            link_checker.stop_processes("l2ping_replyer")
            # 返回
            return {"code": 1, "msg": "停止链路检查成功！"}
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}
        

class L2PingReplyerAPI(MethodView):
    '''
    /worker/l2ping_replyer/
    '''
    def post(self):
        '''
        启动l2ping_replyer
        '''
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            user = data["user"]
            project_name = data["project_name"]
            subtopo = data["subtopo"]
            is_check_once = data.get("is_check_once")
            # 启动l2ping_replyer
            link_checker = LinkCheckerWorker(user, project_name, subtopo)
            link_checker.start_replyer_processes(is_check_once)
            # 返回
            return {"code": 1, "msg": "启动l2ping_replyer成功！"}
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}

    def delete(self):
        '''
        停止l2ping_replyer
        '''
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            FLASK_LOGGER.debug(data)
            user = data["user"]
            project_name = data["project_name"]
            subtopo = data["subtopo"]
            # 停止l2ping_replyer
            link_checker = LinkCheckerWorker(user, project_name, subtopo)
            link_checker.stop_processes("l2ping_replyer")
            # 返回
            return {"code": 1, "msg": "停止l2ping_replyer成功！"}
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}
