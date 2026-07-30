from flask import Blueprint, request
from flask.views import MethodView
import json


from ...tasks.monitor import tasks
from ....Service_layer.redisAPI import UserMapRedis
from ....Service_layer import worker_expr_monitor
from ....Service_layer.topo_deploy_errors import PcapDeployError
from ....tools import get_host_ip
from ....tools.log_tools import FLASK_LOGGER

local_ip = get_host_ip()


class MonitorAPI(MethodView):
    """
    在worker上创建事件监控器
    POST /monitor/ 
    DELETE /monitor/

    请求体中包含了
    {
        'user': user,
        'topo': topo,
        'expr': expr
    }
    """
    def post(self):
        """
        创建监控服务
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, expr = data['user'], data['topo'], data['expr']
        pcap_process_list = worker_expr_monitor.deploy_monitor(user, topo, expr)
        user_map_redis = UserMapRedis()
        # 对于下面这一部分， 暂时使用数据表进行数据的存储
        # 表为<topo>_<expr>_pcap_process
        # key: worker_ip      value: list
        user_db_cli = user_map_redis.get_user_db(user)
        try:
            table_name = f'{topo}_{expr}_pcap_process'
            user_db_cli.set_value(table_name, local_ip, pcap_process_list)     
            return {'code': 1, 'msg': '创建pcap监控程序成功'}   
        except:
            raise
        finally:
            user_db_cli.close()
            user_map_redis.close()

    def delete(self):
        """
        删除并停止监控服务
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, expr = data['user'], data['topo'], data['expr']
        user_map_redis = UserMapRedis()
        user_db_cli = user_map_redis.get_user_db(user)
        table_name = f'{topo}_{expr}_pcap_process'
        pcap_processing_list = user_db_cli.get_value(table_name, local_ip)
        # {'task_id': res.id, 'parent_id': res.parent.id}
        result = tasks.terminator_pcap_monitor(user, topo, expr, pcap_processing_list)
        FLASK_LOGGER.debug(result)
        result.update({'code': 1, 'msg': '终止成功,开始计算性能指标...'})
        # 将数据库里的信息删除
        FLASK_LOGGER.debug(f"del {table_name}->{local_ip}")
        user_db_cli.del_value(table_name, local_ip)
        FLASK_LOGGER.debug(result)
        return result
        # except:
        #     print('停止pcap失败')
        #     return {'code': 0, 'msg': '停止pcap失败'}# 将数据存储到数据库的代码
        # finally:
        #     user_db_cli.close()
        #     user_map_redis.close()

    def get(self):
        """
        不支持get方法
        """
        return {'msg': 'this url can be routed'}

class MonitorTcQueueAPI(MethodView):
    '''
    POST /worker/monitor/tc/queue/
    DELETE /worker/monitor/tc/queue/
    监控端口tc队列长度
    {
        "user": "wtx",  
	    "topo": "628",  
	    "interfaces": [
                {
                    "source_ne": "h1",
                    "target_ne": "s1"       
                }
        ]
    }

    '''
    def post(self):
        """
        启动TC队列监控程序
        """
        data = json.loads(request.get_data(as_text=True))
        # print(data)
        user, topo, interfaces = data['user'], data['topo'], data['interfaces']
        try:
            # 对target_ne的检查交给了deploy_tc_queue_monitor
            pcap_process_list = worker_expr_monitor.deploy_tc_queue_monitor(user, topo, interfaces)
        except ValueError as e:
            return {'code': 0, 'msg': e.args[0]}
        except RuntimeError as e:
            return {'code': 0, 'msg': e.args[0]}
        except:
            return {'code': 0, 'msg': '端口监控服务启动失败'}
        user_map_redis = UserMapRedis()
        user_db_cli = user_map_redis.get_user_db(user)
        try:
            table_name = f'{topo}_tc_queue_monitor_process'
            # 保存已存在的进程信息，防止覆盖
            if user_db_cli.check_exist(table_name, local_ip):
                temp_process_list = user_db_cli.get_value(table_name, local_ip)
                pcap_process_list = temp_process_list + pcap_process_list
            user_db_cli.set_value(table_name, local_ip, pcap_process_list)     
            return {'code': 1, 'msg': '端口监控服务启动成功'}   
        except:
            return {'code': 0, 'msg': '数据库读写失败'}  
        finally:
            user_db_cli.close()
            user_map_redis.close()
            
    def delete(self):
        """
        停止TC队列监控程序
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, interfaces = data['user'], data['topo'], data['interfaces']
        # print(data)
        try:
            stop_process_flag = worker_expr_monitor.stop_tc_queue_monitor(user, topo, interfaces)
            if stop_process_flag:
                return {'code': 1, 'msg': '端口监控服务停止成功'}
        except ValueError as e:
            return {'code': 0, 'msg': e.args[0]}
        except RuntimeError as e:
            return {'code': 0, 'msg': e.args[0]}
        except:
            return {'code': 0, 'msg':'端口监控服务停止失败'}