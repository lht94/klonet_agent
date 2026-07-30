import json
import traceback
from flask_login import login_required
from flask import request
from flask.views import MethodView
from scapy.utils import PeriodicSenderThread
from ....Function_layer.link_health_master import LinkCheckerMaster
from ....tools.log_tools import FLASK_LOGGER

class LinkCheckerAPI(MethodView):
    '''
    /master/checklink/
    '''

    def post(self):
        '''
        启动所有worker上的l2ping_replyer，再启动链路检查进程
        '''
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            user, project_name = data["user"], data["project_name"]
            # （可选）是否只检查一轮（若不存在该值，则返回None）
            # 为True/False（使用assert进行字段检查）
            is_check_once = data.get("is_check_once")
            if is_check_once != None:
                assert is_check_once == True or is_check_once == False
            # （可选）每轮检查间隔时间，单位秒（若不存在该值，则返回None）
            # 为float或int类型（使用assert进行字段检查）
            check_interval_s = data.get("check_round_interval_s")
            if check_interval_s != None:
                assert (isinstance(check_interval_s, float) or
                        isinstance(check_interval_s, int))

            # 向worker发送开始检查或创建l2ping_replyer的信号
            # 向各worker发送的请求是异步的
            link_checker = LinkCheckerMaster(user, project_name)
            link_checker.send_start_signal("l2ping_replyer",
                                           is_check_once,
                                           check_interval_s)
            # l2ping_replyer创建完成后，启动检查进程checklink
            broken_vxlans = link_checker.send_start_signal("checklink",
                                                           is_check_once,
                                                           check_interval_s)
            return {"code": 1,
                    "msg": "检查成功！",
                    "broken_vxlans": broken_vxlans}
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e), "broken_vxlans": {}}
    
    def delete(self):
        '''
        停止worker上的链路检查进程
        '''
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            user = data["user"]
            project_name = data["project_name"]
            # 向各worker发送停止检查的信号
            link_checker = LinkCheckerMaster(user, project_name)
            link_checker.stop_check()
            # 返回
            return {"code": 1, "msg": "停止worker上的链路检查进程成功！"}

        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}


class RecordLinkHealthAPI(MethodView):
    '''
    /master/link_report/
    '''

    def post(self):
        '''
        记录链路检查结果
        '''
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            FLASK_LOGGER.debug(data)
            user, project_name, subtopo, broken_vxlan_list = \
                data["user"], data["project_name"], \
                data["subtopo"], data["broken_vxlan_list"]
            # 记录检查报告，将损坏链路列表写入redis
            link_checker = LinkCheckerMaster(user, project_name)
            link_checker.record_check_report(subtopo, broken_vxlan_list)
            # 返回
            return {"code": 1, "msg": "记录链路检查结果成功！"}
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}
