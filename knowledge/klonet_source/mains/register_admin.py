import requests
from vemu_uestc.vemu_config.config import PROJ_CONFIG


req_url = f'http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/user_register/'
data = {
            "name":"admin",
            "password":"[REDACTED]",
            "phone":0,
            "email":"0",
            "role": "super_admin",
        }
req_ret = requests.post(req_url, json=data)
print(req_ret.text)