import json

from flask import request
from flask.views import MethodView

from ....tools.context import redis_context
from ....tools import get_host_ip
from ....Service_layer.NEManager import QuaggaRunner
from ....Service_layer.TopoManager import ServiceManager
from ....tools.log_tools import FLASK_LOGGER

class OspfAreaStartApi(MethodView):
    """
    分域启动OSPF进程
    """

    def post(self):
        data = json.loads(request.get_data(as_text=True))
        try:
            user, topo, ospf_area, subtopo = data['user'], data['topo'], data['ospf_area'], data['subtopo']
            # worker需要得到所有的根据域的信息查到所有的在该worker上的该域的节点
            # 然后进行服务的启动
            with redis_context(user) as user_db_cli:
                pipe = user_db_cli._db_conn.pipeline()
                pipe.hget('topo_ospf_area', topo)
                pipe.hget('subtopo_service', subtopo)
                result = pipe.execute()
                area_ne, subtopo_ne = set(result[0][ospf_area]), set(result[1]['routers'])
                # 需要起服务的节点为交集
                routers = list(set(area_ne) & set(subtopo_ne))
                service_manager = ServiceManager(user, topo, subtopo)
                service_manager.__setattr__('routers', routers)
                result = service_manager._start_l3_service()
                if not result['code']:
                    return {'code': 0, 'msg': f'start {ospf_area} router service failed'}
                return {'code': 1, 'msg': f'start {ospf_area} router service successfully'}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': f'start {ospf_area} router service failed'}
