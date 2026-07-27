import time
import requests
import multiprocessing
from ..vemu_config.config import PROJ_CONFIG
from ..tools.tools import get_host_ip, register_worker
from ..tools.log_tools import FLASK_LOGGER

class HealthBeatSender():
    def __init__(self, send_interval_s=20):
        self.send_interval_s = send_interval_s

    def send(self):
        '''
        向master发送心跳包，间隔为send_interval_s秒
        '''
        try:
            while True:
                # 与worker注册时保持一致
                worker_info = {"worker_ip": get_host_ip()}

                FLASK_LOGGER.info(f"Send heatbeat to master")
                master_url = (f"http://{PROJ_CONFIG.master_ip}:"
                    f"{PROJ_CONFIG.master_port}/master/heartbeat/")
                try:
                    resp = requests.post(master_url, json=worker_info)
                except requests.exceptions.ConnectionError:
                    FLASK_LOGGER.error("Heartbeat is not acked.")
                FLASK_LOGGER.info(f"Response: {resp}")
                
                time.sleep(self.send_interval_s)
        except KeyboardInterrupt:
            print("Exit heatbeat process.")



def start_heatbeat_process():
    # 在心跳机制中，要求worker启动后会自动注册worker
    register_worker()

    heat_beat_sender = HealthBeatSender(20)
    p = multiprocessing.Process(target=heat_beat_sender.send)
    p.start()
    FLASK_LOGGER.info(f"Start heatbeat process! Pid={p.pid}")