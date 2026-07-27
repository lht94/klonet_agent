import json
import traceback
import requests
from flask import request
from flask.views import MethodView
from ....tools.log_tools import FLASK_LOGGER
from ....tools.tools import get_host_ip, netmask_cidr
from ....tools.context import redis_context, check_table_existence
from flask_login import login_required
from ....Function_layer import master_config_promethueus, pro_monitor_query
from ....Implement_layer import ContainerManager as container_manager
from ....vemu_config.config import PROJ_CONFIG
from ....Service_layer.redisAPI import WorkerRedis
from ....Service_layer.ssh_worker_manager import get_worker_ip
import grequests


# Master修改Prometheus文件
class PlatMonitorFileAPI(MethodView):
    '''
    POST    /master/platmonitor_file/ 添加来自worker监控组件的port_list信息
    DELETE  /master/platmonitor_file/ 修改来自worker监控组件的port_list信息

    使用来自Worker反馈的端口列表,添加/修改prometheus配置信息
    '''
  
    def post(self):
        # 添加port_list
        port_list = request.form.getlist("port_list")
        FLASK_LOGGER.debug(f"Prometheus文件添加worker监控组件端口：{port_list}")
        try:
            master_config_promethueus.change_pro_file(
                port_list, file_path= PROJ_CONFIG.prometheus_file_path,
    file_name=PROJ_CONFIG.prometheus_file_name)
            # 在Master上接收来自Worker的port_list，修改并重启Prometheus
            restart_result = container_manager.run_shell("docker restart prometheus").decode()
            FLASK_LOGGER.debug(restart_result)
            if "prometheus" in container_manager.run_shell("docker ps").decode():
                return {'code': 1, 'msg': '修改Prometheus配置文件成功'}
            else:
                return {'code': 0, 'msg': '修改Prometheus配置文件失败,容器未启动'}
        except Exception as e:
            return {'code': 0, 'msg': '修改Prometheus配置文件失败，由于:' + str(e)}

    def delete(self):
        # 删除port_list
        port_list = request.form.getlist("port_list")
        FLASK_LOGGER.debug(f"Prometheus文件删除worker监控组件端口：{port_list}")
        try:
            master_config_promethueus.change_pro_file(
                port_list, file_path=PROJ_CONFIG.prometheus_file_path,
                file_name=PROJ_CONFIG.prometheus_file_name, choice="delete")
            return {'code': 1, 'msg': '监控容器删除成功'}
        except Exception as e:
            return {'code': 0, 'msg': '监控容器删除失败，由于:' + str(e)}


class PlatMonitorAPI(MethodView):
    '''
    POST    /master/platmonitor/ 创建监控组件
    DELETE  /master/platmonitor/ 删除监控组件

    向worker发送监控创建/创建请求
    '''

    def post(self):
        try:
            # 添加
            worker_redis = WorkerRedis()
            worker_list = worker_redis.get_all_workers()
            worker_redis.close()

            # 做若干操作，使得master所在宿主机上的worker_ip需要在worker_list的第一位
            # 这里潜在的坑是：要求master所在的宿主机上必须有worker进程

            # 本请求一定是在master上执行，因此获取到的是master所在宿主机上的worker_ip
            worker_ip_of_master_server = get_host_ip() 
            try:
                worker_list.remove(worker_ip_of_master_server)
            except ValueError:
                raise ValueError(f"master所在宿主机上的worker_ip: "
                    f"{worker_ip_of_master_server}需在worker_list中")
            worker_list.append(PROJ_CONFIG.master_ip)
            worker_list.reverse()

            # 此处目前仅能使用同步操作，若异步则会有并发冲突
            for worker_ip in worker_list:
                worker_port = PROJ_CONFIG.worker_port
                worker_url = f"http://{worker_ip}:{worker_port}/worker/platmonitor/"
                FLASK_LOGGER.debug(f"worker_url:{worker_url}")
                resp = requests.post(url=worker_url)
                resp_code = resp.json()['code']
                if not resp_code:
                    return {'code': 0, 'msg': '监控组件创建失败'}
            return {'code': 1, 'msg': '监控组件创建成功'}
        except Exception as e:
            traceback.print_exc()
            return {'code': 0, 'msg': f'监控组件创建失败：{e}'}

 
    def delete(self):
        try:
            # 向worker发信息删除监控组件
            worker_redis = WorkerRedis()
            worker_list = worker_redis.get_all_workers()
            worker_redis.close()
            for worker_ip in worker_list:
                worker_port = PROJ_CONFIG.worker_port
                worker_url = f"http://{worker_ip}:{worker_port}/worker/platmonitor/"
                FLASK_LOGGER.debug(f"worker_url:{worker_url}")
                resp = requests.delete(url=worker_url)
                resp_code = resp.json()['code']
                if not resp_code:
                    return {'code': 0, 'msg': '监控组件创建失败'}
            return {'code': 1, 'msg': '监控组件删除成功'}
        except Exception as e:
            traceback.print_exc()
            return {'code': 0, 'msg': f'监控组件创建失败：{e}'}

class PlatMonitorNeQueryAPI(MethodView):
    '''
    POST    /master/platmonitor_ne_query/ 

    查询Prometheus各个节点的监控信息
    '''
    

    def post(self):
        query_dict = json.loads(request.get_data(as_text=True))
        user, topo, metric_list = query_dict["user"], query_dict["topo"], query_dict["metric"]
        ne_name = query_dict.get("ne_name", "")
        time_args = query_dict.get("time_args", {})
        try:
            if ne_name != "":
                result = pro_monitor_query.query_ne_info(user, topo, ne_name, metric_list, **time_args)
            else:
                result = pro_monitor_query.query_topo_info_metric(user, topo, metric_list)
            # result = pro_monitor_query.query_ne_info(user, topo, ne_name, metric_list, **time_args)

            # 增加网卡信息、端口映射、所在worker的ip等信息的显示
            # 0、上面获得的result可能为空字典，需要限定
            if result == {}:
                result = {ne_name: {'cpu': '0', 'mem': '0'}}
            
            if ne_name != "":
                with redis_context(user) as user_db_cli:
                    # 1、网卡对应
                    table = f'{topo}_{ne_name}'
                    result[ne_name]['nics'] = {}
                    for key in user_db_cli.get_all_keys(table):
                        if key[:5] == 'link_':
                            link_info = user_db_cli.get_value(table, key)
                            result[ne_name]['nics'][link_info['name']] = {
                                'nic_name':link_info['nic'], \
                                'nic_ip': f"{link_info['ip']}" \
                                    f"{netmask_cidr(link_info['mask'])}"
                            }

                    # 2、端口映射
                    table = f'{topo}_port_mapping'
                    if check_table_existence(user, table) and \
                    user_db_cli.check_exist(table, ne_name):
                        result[ne_name]['port'] = user_db_cli.get_value(table, ne_name)
                    else:
                        result[ne_name]['port'] = {}

                    # 3、节点所在worker的ip
                    result[ne_name]['ip'] = get_worker_ip(user, topo, ne_name)
            FLASK_LOGGER.debug(result)

        except Exception as e:
            return {"msg": f"查询拓扑/节点资源用量信息失败, 由于{e.args[0]}"}
        return result


class PlatMonitorHostQueryAPI(MethodView):
    '''
    POST /master/platmonitor_host_query/

    查询宿主机的平台监控信息
    '''


    def post(self):
        query_dict = json.loads(request.get_data(as_text=True))
        metric = query_dict["metric"]
        time_args = query_dict.get("time_args", {})
        try:
            result = pro_monitor_query.query_host_info(metric, **time_args)
        except Exception as e:
            return {"msg": f"查询宿主机资源用量信息失败, 由于{e.args[0]}"}
        return result

