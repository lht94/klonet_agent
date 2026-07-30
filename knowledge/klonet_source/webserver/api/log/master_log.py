import json
from flask_login import login_required
from  flask import request
from flask.views import MethodView
from ....tools.log_tools import *

class LoginfoQueryAPI(MethodView):
    '''用户日志接口

    用户日志即用户操作历史，完整记录位于mysql数据库当中，系统通过用户、拓扑、日志级别对
    日志进行了逻辑分类（此处的日志级别不同于debug、info等级别的含义，而是系统自定义的一
    个属性，分为一级、二级，一级日志用于在主界面显示，二级日志用于在实验界面显示），系统
    提供了查询和删除连个接口，日志的记录在代码中由系统实现。
    '''
    def post(self):
        """查询用户日志信息

        POST /master/loginfoquery
        
        Returns:
            dict: 包含用户历史操作信息的字典，返回前端用以渲染
        """
        # 统一有topo字段比较好
        data = json.loads(request.get_data(as_text=True))
        user, level = data["user"], data['level']
        loginfodict = {}
        try:
            if level == 'First':
                logger = UserLogger(user, UserLogLevel.First)
            elif level == 'Second':
                topo = data['topo']
                logger = UserLogger(user, UserLogLevel.Second, topo)
            loginfodict = logger.get_from_mysql()
        except:
            traceback.print_exc()
            return {'code':0, 'msg':'日志刷新失败'}
        else:
            return {'code':1, 'loginfodict':loginfodict}
    

    def delete(self):
        """删除用户日志信息

        删除该用户下的所有日志信息，DELETE /master/loginfoquery

        Returns:
            dict: 操作执行结果
        """
        data = json.loads(request.get_data(as_text=True))
        user, level = data['user'], data['level']
        try:
            if level == 'First':
                logger = UserLogger(user,UserLogLevel.First)
            elif level == 'Second':
                topo = data['topo']
                logger = UserLogger(user, UserLogLevel.Second, topo)
            res = logger.delete_from_mysql()
        except:
            return {'code':0, 'msg':'日志删除失败'}
        else:
            if res:
                return {'code':1, 'msg':'日志删除成功'}
            else:
                return {'code':0, 'msg':'日志删除失败'}


class LogtestAPI(MethodView):
    '''测试的接口，无实用'''
    def post(self):
        """
        POST /master/logtest/
        """
        FLASK_LOGGER.debug('测试')
        FLASK_LOGGER.info("测试")
        FLASK_LOGGER.error("测试")
        FLASK_LOGGER.critical('测试')
        return {'msg':1}