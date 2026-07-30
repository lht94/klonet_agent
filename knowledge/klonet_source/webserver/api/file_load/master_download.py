import json
from re import U
from flask.views import MethodView
import grequests
from ....vemu_config.config import PROJ_CONFIG
from flask import request
from ....tools.context import redis_context
from ....tools.log_tools import UserLogger, UserLogLevel, FLASK_LOGGER
from flask_login import login_required


class DownloadFileAPI(MethodView):
    """
    /file/dload/
    POST master上的文件下载相关操作
    """

    def post(self):
        """
        master请求对应worker，得到url后返回给前端

        Return:
            dict: {
                'code': 0失败，1成功,
                'msg': 提示信息,
                'url': 完整的url
            }
        """
        dict_from_fend_request = json.loads(request.get_data(as_text=True))
        user, topo, ne_list = dict_from_fend_request['user'], dict_from_fend_request[
            'topo'], dict_from_fend_request['ne_list']
        FLASK_LOGGER.debug(dict_from_fend_request)
        url_list = []

        for ne_entity in ne_list:

            ne_name = ne_entity['ne_name']
            table_name = f'{topo}_{ne_name}'
            worker_ip = ''
            with redis_context(user) as user_db_cli:
                ne_id = user_db_cli.get_value(table_name, 'NEid')
                subtopo = user_db_cli.get_value(table_name, 'NEloc')
                worker_ip = user_db_cli.get_value('subtopo2worker', subtopo)
                NEservice = user_db_cli.get_value(table_name, 'NEservice')
                NEtype = user_db_cli.get_value(table_name, 'NEtype')

            filepath_list = ne_entity['file_list']

            for file_path in filepath_list:
                req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/dload/'
                dict_info = {
                    'user': user,
                    'topo': topo,
                    'ne_name': ne_name,
                    'NEtype': NEtype,
                    'ne_id': ne_id,
                    'NEservice': NEservice,
                    'file_path': file_path
                }
                FLASK_LOGGER.debug(topo)

                rs = (grequests.post(req_url, json=dict_info),)
                resp_result = grequests.map(rs)
                resp = [resp.json() for resp in resp_result]
                FLASK_LOGGER.debug(resp)
                if not resp[0]['code']:
                    return {
                        'code': 0,
                        'msg': f'{ne_name}的{file_path}下载失败'
                    }
                static_url = resp[0]['url']
                # TODO tb：测试改好的api能不能成功运行
                # url_list.append('http://'+ PROJ_CONFIG.public_ip + ':' + PROJ_CONFIG.public_port + static_url)
                url_list.append(f'http://{PROJ_CONFIG.public_ip}:{PROJ_CONFIG.public_port}{static_url}')

        if not url_list:
            return {
                'code': 0,
                'msg': '下载请求失败'
            }
        # 日志输出
        user = dict_info['user']
        logger = UserLogger(user, UserLogLevel.Second, topo)
        logger.log_to_mysql(f'下载节点{ne_name}文件{ filepath_list}')

        return {
            'code': 1,
            'msg': 'success',
            'url': url_list
        }
