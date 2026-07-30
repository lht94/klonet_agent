from flask.views import MethodView
from flask import request
import json
from ....Service_layer.LoadManager_console import UploadFile
from ....Implement_layer.LinkManager.link_operate import shell_execute
from gevent import subprocess
from os import path
from werkzeug.utils import secure_filename
from ....tools.log_tools import FLASK_LOGGER
from ....vemu_config.config import PROJ_CONFIG

class UploadFileAPI(MethodView):
    """
    worker的文件上传相关操作
    """

    def post(self):
        """
        worker接收post请求

        Return:
            dict: {
                'code': 0失败，1成功
                'msg': 提示消息
            }
        """

        # 接收请求中的文件
        file_from_master_request = request.files['file']

        # 接收请求中的json
        user = request.form.get('user')
        topo = request.form.get('topo')
        ne_name = request.form.get('ne_name')
        ne_id = request.form.get('ne_id')
        file_path = request.form.get('file_path')
        file_from_master_request.filename = request.form.get('file_name')
        NEservice = request.form.get('NEservice')
        NEtype = request.form.get('NEtype')


        #static_folder_name = f'{PROJ_CONFIG.up_end_dir}/{user}_{topo}_{ne_name}'
        static_folder_name = f'{PROJ_CONFIG.up_temp_dir}/{user}_{topo}_{ne_name}'


        try:
            shell_execute(f'sudo mkdir -p {static_folder_name}')
        except subprocess.CalledProcessError as e:
            pass

        safe_names = secure_filename(file_from_master_request.filename)

        upload_path = path.join(
            static_folder_name,
            safe_names)

        try:
            file_from_master_request.save(upload_path)
        except:
            FLASK_LOGGER.error('Can\'t save file to worker host!')
            return {
                'code': 0
            }

        upload_manager = UploadFile(
            container_id=ne_id,
            file_path=upload_path,
            NEservice=NEservice,
            NEtype=NEtype)

        try:
            result = upload_manager.cp_file(container_store_path=file_path)
        except:
            return {'code': 0}

        try:
            # print(upload_path)
            shell_execute(f'sudo rm {upload_path}')
        except:
            FLASK_LOGGER.debug('Upload success BUT Delete worker local failed!')

        return result
