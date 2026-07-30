from flask.views import MethodView
from ....vemu_config.config import PROJ_CONFIG
from vemu_uestc.Service_layer import platform_monitor_deploy

master_ip = PROJ_CONFIG.master_ip
master_port = PROJ_CONFIG.master_port


class PlatMonitorAPI(MethodView):
    '''
    POST    /worker/platmonitor/ 创建监控组件
    DELETE  /worker/platmonitor/ 删除监控组件

    根据vemu_config/settings的端口配置和Service_layer/platform_monitor_deploy添加监控组件
    '''
    
    def post(self):
        # 创建监控组件
        plat_mon = platform_monitor_deploy.PlatMonitorDeployManager(master_ip, master_port)
        return plat_mon.run_monitor()

    def delete(self):
        plat_mon = platform_monitor_deploy.PlatMonitorDeployManager(master_ip, master_port)
        return plat_mon.del_monitor()
