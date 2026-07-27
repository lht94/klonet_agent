from flask.views import MethodView
from flask import request


from ....Service_layer.redisAPI import UserMapRedis

user_db_map = UserMapRedis()

class GetPerformanceAPI(MethodView):

    def get(self):
        """

            GET /master/ne_performance/
            查询某个用户的某个拓扑的某个ne的性能数据
            
        """
        user = request.args.get('user')
        topo = request.args.get('topo')
        ne = request.args.get('ne')
        db_cli = user_db_map.get_user_db(user)

        table = f'{topo}_{ne}'
        performance = db_cli.get_value(table, 'NEperformance')

        if performance == '':
            return {'code': 0, 'performance': 'No performance data'}
        else:
            return {'code': 1, 'performance': performance}
