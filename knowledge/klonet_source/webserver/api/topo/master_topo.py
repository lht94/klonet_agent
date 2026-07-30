from pprint import pprint
import traceback
from ....Service_layer.mysql_api.user_info import check_user_exist_by_user_name
from flask_login import login_required
import grequests
from flask.views import MethodView

from ....Function_layer.topo_preprocess import Topo_process
from ....Function_layer.resource_manager import ResourceManager, ResourceNotEnoughError
from ....tools import get_host_ip
from ....tools.context import check_table_existence, redis_context
from ....Service_layer.redisAPI import UserDB, UserMapRedis, WorkerRedis, ResourceRedis, UserCPUResourceRedis
from ....Service_layer.redis_error import (DbCreateFailedError, 
        KeyNotExistError, NoFreeDbForUserError, DbAlreadyExistError)

from ....Function_layer import topo_partition as tpn
from ....Function_layer.deployed_proj_manager import retrieve_topo, delete_all_traffic, delete_monitor_event
from ....vemu_config.config import PROJ_CONFIG, SplitOption
from ....tools.log_tools import *
from ...tasks.topo.tasks import deploy_topo, delete_topo, compat_json
from ....Service_layer.DockerSwarm import SwarmMaster
from ....Service_layer.redisAPI import WorkerRedis
import os
from ....tools.tools import get_host_ip
import time

user_db_map = UserMapRedis()
resource_redis = ResourceRedis('worker_resource')


class TopoDeployAPI(MethodView):
    """
    拓扑创建API
    """
    def post(self):
        """
        处理创建拓扑的HTTP请求
        """
        try:
            # 从请求里获取用户名和拓扑名
            user_topo_info = json.loads(request.get_data(as_text=True))
            # 从这开始是为了dockerswarm而加的，后期可能会优化，也可能不会
            if user_topo_info['networks']['controllers']:  
                worker_redis = WorkerRedis()
                worker_list = worker_redis.get_all_workers()
                master_local_IP = get_host_ip()
                swarm_master = SwarmMaster()
                swarm_master.docker_swarm_init()
                file_path = f'{os.getcwd()}/vemu_uestc/static_resources/docker_swarm_token'
                try:
                    with open(file_path, 'r') as f:
                        worker_token = f.read()
                except:
                    FLASK_LOGGER.debug(f'"{file_path}" not exist')
                    return {'code': 0, 'msg': f'"{file_path}" not exist'}
                data2worker = {'worker_token': worker_token, 'master_local_ip': master_local_IP}

                for worker_ip in worker_list:
                    if worker_ip == master_local_IP:
                        continue
                    req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/swarm/'
                    rs = (grequests.post(req_url, json=data2worker),)
                    resp_result = grequests.map(rs)
                    resp = [resp.json() for resp in resp_result]  # 到这swarm结束

            # 检查用户合法性
            if not check_user_exist_by_user_name(user_topo_info['user']):
                raise ValueError('用户不存在!')
            pb_table_name = PROJ_CONFIG.pb_table_name_prefix + '_' + user_topo_info['topo'] + '_' + 'deploy'
            
            #检查容器名的命名：
            for k,v in user_topo_info['networks'].items():
                if k == "hosts" or k == "switches" or k == "routers" or k == "controllers" or k == "dpdks":
                    for node in v:
                        #检查源和目的容器名是否为数字字母组合，且是否超过13位
                        if len(node) > 13 or len(node) >13:
                            return {'code': 0,'msg': f"容器名长度超过13位，请重新命名！"}
                        if node.isalnum() == False or node.isalnum() == False:
                            return {'code': 0,'msg': f"容器名中包含除字母或数字以外的符号，请重新命名！"}
            
            
            # wudx
            # 互斥检查
            if PROJ_CONFIG.resource_limit_enable:
                if PROJ_CONFIG.node_iso_resource_limit_CpuSet or PROJ_CONFIG.topo_iso_resource_limit_CpuSet:
                    raise ValueError("Quota和Cpuset资源限制模式冲突，请在配置文件中确认仅选择其中一种资源限制模式！")
            else:
                if PROJ_CONFIG.node_iso_resource_limit_CpuSet and PROJ_CONFIG.topo_iso_resource_limit_CpuSet:
                    raise ValueError("采用CPUSET进行资源限制时，请保持仅选择一种模式！")
                # 拓扑切分模式检查
                if PROJ_CONFIG.node_iso_resource_limit_CpuSet or PROJ_CONFIG.topo_iso_resource_limit_CpuSet:
                    if PROJ_CONFIG.split_option != SplitOption.SPLIT_WITH_TOPO_RESOURCE:
                        raise ValueError("CPUSET进行资源限制时，必须使用\"结合广度优先遍历和资源进行切分\"的拓扑切分模式！")
            # 为CPU_SET初始化一个用户配额表（不存在时才初始化）
            if PROJ_CONFIG.node_iso_resource_limit_CpuSet or PROJ_CONFIG.topo_iso_resource_limit_CpuSet:
                user_manager = UserMapRedis()
                user_list = user_manager.get_user_list()
                print(user_list)
                cpu_res_manager = UserCPUResourceRedis()
                cpu_res_manager.set_default_resource(user_list)
            
            # 虚机-卫星-容器代码版本合并所需要的兼容函数
            # 用于处理json信息
            if 'satellite' not in user_topo_info['networks']:
                user_topo_info = compat_json(user_topo_info)
            
            # 创建拓扑任务，返回 task ID
            task_id = deploy_topo(user_topo_info)['task_id']



            # redis中新增topo_info表，记录topo创建时间
            topo_info = {}
            topo_creat_time = topo_info.setdefault("topo_creat_time", time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
            with redis_context(user_topo_info['user']) as user_db_cli:
                user_db_cli.set_value("topo_info", user_topo_info['topo'], topo_info)


            with redis_context(user_topo_info['user']) as user_db_cli:
                user_db_cli.del_table(pb_table_name)  # process_bar_table 表删除
                user_db_cli.set_value(pb_table_name, 'task_id', task_id)
                
                worker_redis = WorkerRedis()
                worker_list = worker_redis.get_all_workers()
                for worker in worker_list:
                    user_db_cli.set_value(pb_table_name, worker, 0)
            
        except Exception as e:
            traceback.print_exc()
            return {'code': 0, 'msg': str(e)}

        return {'code': 1, 'msg': '拓扑创建任务已分发'}

   
    def delete(self):
        """
        处理删除拓扑的HTTP请求
        """
        try:
            # 从请求里获取用户名和拓扑名
            user_topo_info = json.loads(request.get_data(as_text=True))
            pb_table_name = PROJ_CONFIG.pb_table_name_prefix + '_' + user_topo_info['topo'] + '_' + 'delete'
            # 创建拓扑任务，返回 task ID
            task_id = delete_topo(user_topo_info)['task_id']

            with redis_context(user_topo_info['user']) as user_db_cli:
                user_db_cli.del_table(pb_table_name)  # process_bar_table 表删除
                user_db_cli.set_value(pb_table_name, 'task_id', task_id)

                worker_redis = WorkerRedis()
                worker_list = worker_redis.get_all_workers()
                for worker in worker_list:
                    user_db_cli.set_value(pb_table_name, worker, 0)

        except Exception as e:
            traceback.print_exc()
            return {'code': 0, 'msg': str(e)}
        
        return {'code': 1, 'msg': '拓扑删除任务已分发'}
    
 
    def get(self):
        """
        处理 GET HTTP请求
        """
        data = request.args
        user, topo = data['user'], data['topo']
        result = retrieve_topo(user, topo)
        if result['code']:
            return {'code': 1, 'stat': 1, 'msg': '拓扑正在运行'}
        else:
            return {'code': 1, 'stat': 0, 'msg': '拓扑未运行'}


class TopoServiceAPI(MethodView):
    """
    拓扑启动节点服务 API
    """

    def post(self):
        """
        处理启动服务的HTTP请求
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo = data['user'], data['topo']
        user_db_cli = user_db_map.get_user_db(user)
        try:
            subtopo_list = user_db_cli.get_value('topo2subtopo', topo)
            req_urls = []
            for subtopo in subtopo_list:
                worker_ip = user_db_cli.get_value('subtopo2worker', subtopo)
                if worker_ip == 'hardware':
                    worker_ip = PROJ_CONFIG.hardware_worker
                info_dict = {'user': user, 'topo': topo, 'subtopo': subtopo}
                req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/service/'
                FLASK_LOGGER.debug(f'service deploy... req_url: {req_url}')
                req_urls.append((req_url, info_dict))
            FLASK_LOGGER.debug(f'requ_url is: {req_urls}')
            rs = (grequests.post(url, json=req_paras) for url, req_paras in req_urls)
            resp_result = grequests.map(rs)
            resp_status = [resp.json()['code'] for resp in resp_result]
            if not all(resp_status):
                return {'code': 0, 'msg': '服务创建失败'}
            return {'code': 1, 'msg': '服务创建成功'}
        except:
            return {'code': 0, 'msg': '服务创建失败'}
        finally:
            user_db_cli.close()

  
    def delete(self):
        """
        处理删除服务的HTTP请求
        """
        return {'code': 0, 'msg': 'method not allowed', 'status': 405}

    def get(self):
        """
        处理GET服务的HTTP请求
        """
        return {'msg': 'this url can be routed'}
