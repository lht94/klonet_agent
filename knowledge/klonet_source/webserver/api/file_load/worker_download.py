from ....vemu_config.config import PROJ_CONFIG
from flask.views import MethodView
from flask import request
import json
from ....Service_layer.LoadManager_console import DownloadFile
from ....Implement_layer.LinkManager.link_operate import shell_execute
from gevent import subprocess
from ....tools.log_tools import FLASK_LOGGER

class DownlaodFileAPI(MethodView):
    """
    worker的文件下载相关操作
    """

    def post(self):
        """
        worker执行post操作

        Return:
            dict: {
                'code': 0失败，1成功
                'msg': 提示消息
                'url': 文件的url（成功时才有，url不包括workerip和port）
            }
        """

        dict_from_master_request = json.loads(request.get_data(as_text=True))
        user = dict_from_master_request['user']
        topo = dict_from_master_request['topo']
        ne_name = dict_from_master_request['ne_name']
        NEservice = dict_from_master_request['NEservice']
        NEtype = dict_from_master_request['NEtype']
        FLASK_LOGGER.debug(dict_from_master_request)

        # 储存文件的本地文件夹
        static_folder_name_r = f'{PROJ_CONFIG.down_end_dir}/{user}_{topo}_{ne_name}'
        static_folder_name = f'{PROJ_CONFIG.down_temp_dir}/{user}_{topo}_{ne_name}'
        # 创建文件夹，若已有文件夹则抛出
        try:
            shell_execute(f'sudo mkdir -p {static_folder_name}')
            shell_execute(f'sudo mkdir -p {static_folder_name_r}')
        except subprocess.CalledProcessError as e:
            FLASK_LOGGER.error(e)

        file_name = dict_from_master_request['file_path'].split('/')[-1]
        file_path = dict_from_master_request['file_path']
        download_manager = DownloadFile(
            container_id=dict_from_master_request['ne_id'],
            file_path=dict_from_master_request['file_path'],
            NEservice=NEservice,
            NEtype = NEtype)
        
        json_static_url = download_manager.cp_file(
            static_path=static_folder_name+'/')
        FLASK_LOGGER.debug(topo)

        if not json_static_url['code']:
            return {
                'code': 0,
                'msg': 'worker得到url失败'
            }
        static_url = json_static_url['msg']
        static_url = f'{static_url}&filename={user}_{topo}_{ne_name}/{file_name}'
        print(static_url)
        # wudx fix
        # 文件暂存的地方是以完整文件路径存储的(user/file_dir/file_name)，并不是user/file_name
        shell_execute(f'sudo cp {static_folder_name}{file_path} {static_folder_name_r}/{file_name}')
        shell_execute(f'sudo rm {static_folder_name}{file_path}')
        shell_execute(f'sudo chmod 777 {static_folder_name_r}/{file_name}')
        return {
            'code': 1,
            'msg': 'Worker get url successful!',
            'url': static_url
        }
