from flask import request, send_file
from flask.views import MethodView
import traceback
import json

from ....vemu_config.config import PROJ_CONFIG
from ....Service_layer.mysql_models import Experiment
from ....Service_layer.mysql_manager import get_row, check_row_exists

class ScriptsDownloadAPI(MethodView):
    """
    POST /master/scripts/download/ 下载某个实验的脚本压缩包
    
    参数为：
    "experiment_name": "", 实验名
    
    """
    def post(self):
        print("开始下载实验脚本")
        data = json.loads(request.get_data(as_text=True))
        # 检查该实验是否存在
        if not check_row_exists(Experiment, 
                            experiment_name = data['experiment_name']):
            return {"code": 0, "msg": "实验仓库不存在该实验"}
        # 判断是否用户有上传脚本
        have_scripts = get_row(Experiment, 
                    experiment_name=data['experiment_name']).have_scripts
        if not have_scripts:
            return {"code": 0, "msg": "该实验没有脚本"}
        try:
            file_path = (f"{PROJ_CONFIG.static_scripts_dir}/"
                         f"{data['experiment_name']}_scripts.tar")
            return send_file(file_path, as_attachment=True)
        except Exception as e:
            traceback.print_exc()
            return {"code": 0, "msg": "下载脚本失败"}
    