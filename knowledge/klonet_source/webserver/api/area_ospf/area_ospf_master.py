import json

from flask import request
from flask.views import MethodView
import grequests

from ....tools.log_tools import FLASK_LOGGER
from ....tools.context import redis_context
from ....vemu_config.config import PROJ_CONFIG


class OspfAreaStartApi(MethodView):
    """
    分域启动OSPF进程
    """

    def post(self):
        data = json.loads(request.get_data(as_text=True))
        try:
            user, topo, ospf_area = data['user'], data['topo'], data['ospf_area']
            with redis_context(user) as user_db_cli:
                area_nes = user_db_cli.get_value('topo_ne_areas', topo)['topo']
                pipe = user_db_cli._db_conn.pipeline()
                for ne in area_nes:
                    pipe.hget(f'{topo}_{ne}', 'NEloc')
                subtopos = list(set(pipe.execute()))
                # 得到subtopo 所在的IP地址
                for subtopo in subtopos:
                    pipe.hget('subtopo2worker', subtopo)
                worker_ips = pipe.execute()
            req_urls = []
            for subtopo, worker_ip in zip(subtopos, worker_ips):
                worker_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/area_ospf/'
                info = {'user': user, 'topo': topo, 'area': ospf_area, 'subtopo': subtopo}
                FLASK_LOGGER.debug(info)
                req_urls.append((worker_url, info))
            rs = (grequests.post(url, json=info) for url, info in req_urls)
            resps = grequests.map(rs)
            resp_status = [resp.json()['code'] for resp in resps]
            if not all(resp_status):
                return {'code': 0, 'msg': 'ospf服务分区启动失败'}
            return {'code': 1, 'msg': 'ospf服务分区启动成功'}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': 'ospf服务分区启动失败'}
