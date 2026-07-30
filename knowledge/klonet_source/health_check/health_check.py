import time
import subprocess
from collections import namedtuple

import schedule
import requests
from requests.exceptions import ReadTimeout

from ..tools.tools import get_host_ip

"""
先检查 进程是否卡死
若卡死 就重启相关形成
不用区分是master 还是worker

找到相对应的进程
粗暴一点，不管是不是还在处理其他的连接请求。

需要得到特定的端口和IP地址
最终的 IP 和 端口 是在gunicorn中得到的

"""
SERVER_IP = get_host_ip()
MASTER_SERVER_PORT = 5000
WORKER_SERVER_PORT = 5001
DATA_SERVER_PORT = 5555

master_health_check_url = f"{SERVER_IP}:{MASTER_SERVER_PORT}/server_health/"
worker_health_check_url = f"{SERVER_IP}:{WORKER_SERVER_PORT}/server_health/"
data_server_helath_check_url = f"{SERVER_IP}:{DATA_SERVER_PORT}/server_health/"


# 定义不同gunicorn_server 及其配置
GunicornServer = namedtuple("GunicornServer", ["health_check_url", "pid_file", "start_cmd"])

master_server = GunicornServer(health_check_url=master_health_check_url, pid_file='gun.py',
                               start_cmd="sudo /usr/local/python3/bin/gunicorn -c gun.py master_main:flask_app")
worker_server = GunicornServer(health_check_url=master_health_check_url, pid_file='worker_gun.py',
                               start_cmd="sudo /usr/local/python3/bin/gunicorn -c worker_gun.py worker_main:flask_app")


@schedule.repeat(schedule.every(5).seconds, worker_server)
@schedule.repeat(schedule.every(5).seconds, master_server)
def check_server_health(gunicorn_server: GunicornServer):
    try:
        requests.get(gunicorn_server.health_check_url, timeout=5)
    except ReadTimeout:
        # 如果超时，说明server卡住, 需要重启gunicorn
        with open(gunicorn_server.pid_file) as f:
            pid = f.readline()
        kill_cmd = f"kill {pid}"
        try:
            # 关闭 gunicorn
            subprocess.run(kill_cmd, check=True)
            # 重启gunicorn
            subprocess.run(gunicorn_server.start_cmd)
        except subprocess.CalledProcessError:
            print(f"重启{gunicorn_server.pid_file.split('.')[0]}失败, 稍后重新尝试...")


if __name__ == "__main__":
    while True:
        schedule.run_pending()
        time.sleep(1)
