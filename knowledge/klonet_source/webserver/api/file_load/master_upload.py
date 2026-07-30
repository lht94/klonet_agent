import traceback

from flask.views import MethodView
from ....vemu_config.config import PROJ_CONFIG
from flask import request
from werkzeug.utils import secure_filename
from pypinyin import lazy_pinyin
from ....tools.context import redis_context
import requests
from ....tools.log_tools import UserLogger, UserLogLevel, FLASK_LOGGER
from flask_login import login_required

class UploadFileAPI(MethodView):
    """
    master上的文件上传相关操作
    """


    def post(self):
        """
        master请求对应worker，worker对url进行处理，将文件存到本地，再复制到容器内
        请求worker的是/worker/uload/

        Return:
            dict: {
                'code': 
                'msg': 
                'url': 
            }
        """

        try:
            # 接收请求中的json
            user = request.form.get('user')
            topo = request.form.get('topo')
            ne_name = request.form.get('ne_name')
            file_path = request.form.get('file_path')
        except Exception as e:
            traceback.print_exc()
            return {
                'code': 0,
                'msg': 'user or topo or ne_name or file_path error'
            }

        # 接收请求中的文件
        try:
            file_from_fend_request = request.files['file']
        except Exception as e:
            traceback.print_exc()
            return {
                'code': 0,
                'msg': 'file key error'
            }


        # file_name = secure_filename(file_from_fend_request.filename)
        file_name = secure_filename(''.join(lazy_pinyin(file_from_fend_request.filename)))

        table_name = f'{topo}_{ne_name}'
        worker_ip = ''
        with redis_context(user) as user_db_cli:
            ne_id = user_db_cli.get_value(table_name, 'NEid')
            subtopo = user_db_cli.get_value(table_name, 'NEloc')
            worker_ip = user_db_cli.get_value('subtopo2worker', subtopo)
            NEservice = user_db_cli.get_value(table_name, 'NEservice')
            NEtype = user_db_cli.get_value(table_name, 'NEtype')

        # 发送到worker的请求参数
        req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/uload/'
        dict_info = {
                'user': user,
                'topo': topo,
                'ne_name': ne_name,
                'ne_id': ne_id,
                'NEservice':NEservice,
                'NEtype':NEtype,
                'file_path': file_path,
                'file_name': file_name
            }
        files = {'file': file_from_fend_request}
        FLASK_LOGGER.debug(dict_info)
        # 请求worker
        try:
            result = requests.post(req_url, data=dict_info, files=files)
        except:
            return {
                    'code': 0,
                    'msg': f'{ne_name}\'s {file_name} upload failed!'
                }
        code = result.json()['code']

        if not code:
            return {
                    'code': 0,
                    'msg': f'{ne_name}\'s {file_name} upload failed!'
                }
        
        # 日志输出
        user = dict_info['user']
        logger = UserLogger(user, UserLogLevel.Second, topo)
        logger.log_to_mysql(f'上传文件{file_name}至节点{ne_name}:{file_path}')

        return {
                'code': 1,
                'msg': f'{ne_name}\'s {file_name} upload success!'
            }