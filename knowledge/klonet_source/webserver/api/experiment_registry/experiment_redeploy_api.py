from flask.views import MethodView
from flask import request
import json
import traceback
import requests

from ....vemu_config.config import PROJ_CONFIG
from ....Service_layer.mysql_models import Experiment
from ....Service_layer.mysql_manager import check_row_exists, get_row


class ExperimentRedeployAPI(MethodView):
    """
    POST /master/experiment/redeploy/ 从实验仓库中重新部署某个实验
    
    参数为：
    "experiment_name": "", 实验仓库中的目标实验名
    "user": "", 用户名
    "topo": "", 用户即将部署的拓扑名
    
    """
    
    def post(self):
        print("开始复现实验")
        try:
            data = json.loads(request.get_data(as_text=True))
            if data['user'] and data['topo'] and ['experiment_name']:
                # 检查该实验是否存在
                if not check_row_exists(Experiment, 
                                    experiment_name = data['experiment_name']):
                    return {"code": 0, "msg": "实验仓库不存在该实验"}
                
                # 从数据库获取拓扑信息
                topo_info = get_row(Experiment, experiment_name =
                        data['experiment_name']).topo_json.decode()
                # 进行拓扑部署
                info = {
                    "user": data['user'],
                    "topo": data['topo'],
                    "networks": json.loads(topo_info)
                }
                print(info)
                req_url = (f"http://{PROJ_CONFIG.master_ip}:"
                           f"{PROJ_CONFIG.master_port}/master/topo/")
                res = requests.post(req_url, json=info)
                print(res)
                if not res.json()['code']:
                    return {"code": 0, "msg": "实验重新部署拓扑失败"}
                return {"code": 1, "msg": "实验正在重新部署拓扑"}
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": "实验重新部署拓扑失败"}