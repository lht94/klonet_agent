from secrets import choice
import sys
import requests
import argparse

from vemu_uestc.tools import get_host_ip
from vemu_uestc.Service_layer import platform_monitor_deploy
from vemu_uestc.vemu_config.config import PROJ_CONFIG

# python3 register_worker.py --master <master_ip> --master_port <port> --choice <choice>
parser = argparse.ArgumentParser()
parser.add_argument("--master_ip", type=str, default=PROJ_CONFIG.master_ip)
parser.add_argument("--master_port", type=str, default=PROJ_CONFIG.master_port)
parser.add_argument("--choice", type=str, default="register", 
                    help="注册或注销worker,注册则填'r',注销则填'u'")

args = parser.parse_args()
master_ip = args.master_ip
master_port = args.master_port
choice = args.choice
req_url = f'http://{master_ip}:{master_port}/master/worker/{get_host_ip()}/'

try:
    if choice == 'r':
        result = requests.post(req_url)
        if result.json()['code']:
            print('注册成功')
        plat_mon = platform_monitor_deploy.PlatMonitorDeployManager(master_ip, master_port)
        result = plat_mon.run_monitor()
        if result['code']:
            print("Worker创建监控容器成功")
        else:
            print("Worker创建监控容器失败")
    elif choice == 'u':
        result = requests.delete(req_url)
        if result.json()['code']:
            print('注销成功')
        plat_mon = platform_monitor_deploy.PlatMonitorDeployManager(master_ip, master_port)
        result = plat_mon.del_monitor()
        if result['code']:
            print("Worker删除监控容器成功")
        else:
            print("Worker删除监控容器失败")
except Exception as e:
    print(e.args)