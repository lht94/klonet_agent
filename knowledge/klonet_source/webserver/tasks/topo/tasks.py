from time import time, sleep
import grequests,requests
import traceback
import random

from ....webserver import celery
from ....Service_layer.redis_error import (DbCreateFailedError, \
    NoFreeDbForUserError, DbAlreadyExistError)
from ....Service_layer.redisAPI import UserDB, UserMapRedis, WorkerRedis
from ....vemu_config.config import PROJ_CONFIG, SplitOption
from ....Function_layer.resource_manager import ResourceManager, \
    ResourceNotEnoughError, CompareRES_GetWorkerResponseError
from ....tools import get_host_ip
from ....tools.context import check_table_existence, redis_context
from ....tools.log_tools import UserLogLevel, UserLogger
from ....Function_layer.topo_preprocess import Topo_process
from ....Function_layer.deployed_proj_manager import delete_all_traffic, \
    delete_monitor_event
from ....Function_layer.deploy_process_bar import ProcessBarDelete
from ....satellite.satool import sat_topo_config
from ....satellite.master_evt_generate import sat_evt_generate

user_db_map = UserMapRedis()

def compat_json(user_topo_info):
    '''
    虚机-卫星-容器代码合并时的json兼容处理函数
    Args:
        user_topo_info: 包含用户和拓扑网络的json信息
    
    Returns：
        res: 适配后的json
    '''
    # wudx
    # 老版本json兼容增加key
    # 为了适配老版本json中的容器节点没有service字段，单独为每一个容器节点增加service字段
    for k,v in user_topo_info['networks'].items():
        if k == "hosts" or k == "switches" or k == "routers" or k == "controllers" or k == "dpdks":
            for node, node_info in v.items():
                # 没有service字段证明是老版本json
                if "service" not in node_info.keys():
                    node_info["service"] = "docker"
                    node_info["portname"] = None    # 目前仅为无效填充字段
                
                # 还要为配置有interface字段的节点加入平行边的后缀_1，否则该字段后续不能写入数据库
                if "interfaces" in node_info.keys():
                    for link_ip_config in node_info["interfaces"]:
                        # 以下代码为了兼容性略显丑陋
                        # 为应对json的调整
                        # 某些情况存在有service字段，但interface错误，依靠字符串分割多层判断逻辑来确定是否要追加后缀
                        check_str = link_ip_config["name"][len(node_info["name"]):] # 去掉节点开头的名称，然后尝试匹配后面的对端节点
                        for n, m in user_topo_info['networks'].items():     # 可能存在跨节点类别的链路连接，所以需要重新循环
                            if n == "hosts" or n == "switches" or n == "routers" or n == "controllers" or n == "dpdks":
                                for ops_node_name, _ in m.items():
                                    if check_str == ops_node_name:  # 匹配到证明该字符串就是某节点名称，是老前端，缺少_1后缀
                                        link_ip_config["name"] = link_ip_config["name"] + "_1"
                                    else:   # 未匹配到就说明已经是新前端，存在后缀了
                                        pass
        # 对于老版本links中不考虑平行边，所以缺少count字段，默认为缺少该字段的link赋值为1
        # 如果以后前端支持平行边了，那么得将以下写死的1改为传参！
        if k == "links":
            for link, link_info in v.items():
                if "count" not in link_info.keys():
                    link_info["count"] = 1
    return user_topo_info

def deploy_topo(user_topo_info):
    '''
    拓扑创建包装函数
    Args:
        user_topo_info: 从 post 请求里获得的用户名和拓扑名信息  
    Returns:
        result: celery 任务的 id ，用于查询任务是否完成与返回
    '''
    result = master_deploy_topo.delay(user_topo_info)
    print(f"task id: {result.id}")
    return {'task_id': result.id}


def delete_topo(user_topo_info):
    '''
    拓扑创建包装函数
    Args:
        user_topo_info: 从 post 请求里获得的用户名和拓扑名信息  
    Returns:
        result: celery 任务的 id ，用于查询任务是否完成与返回
    '''
    result = master_delete_topo.delay(user_topo_info)
    print(f"task id: {result.id}")
    return {'task_id': result.id}


@celery.task(track_started=True)
def master_deploy_topo(user_topo_info):
    """
    拓扑创建函数
    
    Args:
        user_topo_info (dict): 包含有user，topo的完整的拓扑字典信息
    """
    def _check_topo(user: str, topo: str, db_cli):
        """检查项目名是否为空或者重复。
        
        在检查项目是否重复时，额外考虑了creator字段的信息，这是为了将私有项目与多人共享
        项目的检查规则统一。引入多人共享项目后，存在不同用户创建同一名称项目的可能，因此，
        需要新的检查规则。在新规则下，一个用户创建的项目不允许重复；但在多人共享项目中，
        允许来自不同创建者的相同名项目。前端需要以合理的方式来标记多人共享项目的创建者，
        以便用户区分。
        
        Args:
            topo (str): 拓扑名
            db_cli (UserDB): 用户数据库连接
            
        Rasies:
            ValueError: 项目名重复或为空时触发
        """
        if not topo.strip():
            raise ValueError("项目名不能为空")
        
        deploying = f'{topo}_{user}'
        # 检查拓扑是否重名
        # deployed = db_cli.get_all_values('plane_topo_list').keys()
        
        deployed = []
        # 私有项目
        for k in db_cli.get_all_values('plane_topo_list').keys():
            deployed.append(f'{k}_{user}')
        #多人共享项目
        deployed.extend( db_cli.get_all_values('shared_topo_list').keys()) 
        #print(deployed)
        
        if deploying in deployed:
            raise ValueError("项目名不能重复")

    def _topo_split(data, user_db_cli: UserDB, worker_list, hardware):
        """
        NO_SPLIT切分模式下，将拓扑信息及各个节点及链路信息写入redis
        
        Args:
            data          (dict):  创建拓扑json描述文件
            user_db_cli (UserDB):  用户Redis数据库连接
            worker_list   (list):  worker 列表
        """
        topo_processed = Topo_process(data, hardware, worker_list, option=1)
        split_result = topo_processed()
        # pprint(split_result)
        # 写入topo_list
        topo = data['topo']
        user_db_cli.set_value('topo_list', topo, data)
        direct_write_tables = ['plane_topo_list', 'topo_service', 'topo2subtopo', 'subtopo2worker',
                               'plane_subtopo_list', 'subtopo_service', 'shared_topo_list']
        parse_write_tables = ['ne_table_dict', 'link_table_dict', 'vxlanlink_table_dict']
        for table in direct_write_tables:
            # print(table)
            if table == 'plane_topo_list':
                for k, v in split_result[table].items():
                    user_db_cli.set_value(table, k, v)
                    # TODO: 握手接口，待完善

                    # for another_user in v['invited_user_group'][1:]:
                    #     another_db_cli = user_db_map.get_user_db(another_user)
                    #     another_db_cli.set_value(table, k, v)
                    #     another_db_cli.close()
            else:
                for k, v in split_result[table].items():
                    user_db_cli.set_value(table, k, v)

        for key in parse_write_tables:
            tables = split_result[key]
            for k, v in tables.items():
                user_db_cli.set_all_values(k, v)

    try:
        # 拓扑信息的前置处理，检测是否有硬件设备
        if 'net1' in user_topo_info.get('networks', {}):
            net = user_topo_info.get('networks')['net1']
        else:
            net = user_topo_info.get('networks')
        current = time()
        user, topo = user_topo_info['user'], user_topo_info['topo']

        # 0、检查拓扑是否已创建
        try:
            user_db_cli = user_db_map.set_user_db(user)
        except DbAlreadyExistError:
            user_db_cli = user_db_map.get_user_db(user)
        except NoFreeDbForUserError:
            return {'code': 0, 'msg': '数据库用户数目已达上限'}
        except DbCreateFailedError:
            return {'code': 0, 'msg': '用户数据库创建失败'} 
        try:
            _check_topo(user, topo, user_db_cli)
        except ValueError as e:
            return {'code': 0, 'msg': e.args[0]}
        worker_redis = WorkerRedis()
        worker_redis.close()

        # 1、卫星部署
        # 启用卫星功能，且拓扑json包含satellite字段，才进行卫星部署
        sat_flag = PROJ_CONFIG.sat_enable and \
            'satellite' in user_topo_info['networks']
        if sat_flag:
           sat_ret = sat_topo_config(user_topo_info, user_db_cli, topo)
           if sat_ret['code'] == 0:
               return sat_ret
           else:
               user_topo_info = sat_ret['json']
               # wudx
               # master_topo中的json处理对卫星不生效
               # 卫星需要在此处单独兼容处理一次json
               print("+++++++++++++++++++++++++++++++++++++++++++++")
               user_topo_info = compat_json(user_topo_info)
        print(user_topo_info)
        # wudx
        # 1.5、 配置以CPU_SET方式使得容器资源隔离时，检查单个拓扑是否超过用户配额
        if PROJ_CONFIG.node_iso_resource_limit_CpuSet or PROJ_CONFIG.topo_iso_resource_limit_CpuSet:
            try:
                res_manager = ResourceManager(user_topo_info)
                need_cores = res_manager.check_cpu_need()
            except Exception as e:
                print(repr(e))
                print('采用CPU_SET方式进行资源限制时, 超过用户资源配额, 需要删除一些其他拓扑再进行部署')
                return {'code': 0, 'msg': '采用CPU_SET方式进行资源限制时, 超过用户资源配额, 需要删除一些其他拓扑再进行部署'}
            
        # gjh
        # 1.6、如果有hardware，在此处对hardware进行标识符判断
        hardware = False
        hosts = net.get('hosts', {})
        for v in hosts.values():
            if v['service'] == 'hardware':
                hardware = True
            else:
                pass
        # 2、拓扑切分
        # option：
        # 0表示不开启资源获取的切分
        # 1表示根据物理资源剩余量进行切分(不启动资源量化、仅根据前端传的资源需求量)
        # 2表示根据物理资源剩余量进行切分(开启资源量化——网络中可能存在的流量重新计算需求)
        option = PROJ_CONFIG.split_option
        print(f'拓扑切分方案: {option}')
        if option == SplitOption.SPLIT_WITH_RESOURCE or \
           option == SplitOption.SPLIT_WITH_QUANTIFICATION or \
           option == SplitOption.SPLIT_WITH_TOPO_RESOURCE:
            try:
                res_manager = ResourceManager(user_topo_info)
                # 资源计算与分配都可以在ResourceManager里做
                res_manager.get_split_scheme(option=option)
                res_manager.close()
            except ResourceNotEnoughError as e:
                return {'code': 0, 'msg': f'由于物理资源不足, topo创建失败, 其他信息{e.args}'}
            except CompareRES_GetWorkerResponseError as e:
                return {'code': 0, 'msg':'resource_manager:ResourceManager._resource_query:response.status_code!=200,\
                    master收不到worker的资源量通告'}
        # elif option == SplitOption.SPLIT_WITH_TOPO_RESOURCE:
        #     worker_list = worker_redis.get_all_workers()
        #     worker_resource_list = []
        #     scheme = {}
        #     for worker in worker_list:
        #         # 目前结合拓扑的切分只考虑了cpu这一个维度，没有考虑mem
        #         print(worker)
        #         cpu = resource_redis.get_resource(worker, 'cpu_time')
        #         print("cpu", cpu)
        #         worker_resource_list.append((worker, cpu['time_sum']))
        #     # worker_resource_list = [('192.168.1.124', 41), ('192.168.1.105', 21)]
        #     tp = tpn.topo_adapting_partition(data, worker_resource_list)
        #     tp()
        #     lists_of_Ne_and_weights = tp.topo_partition()
        #     if lists_of_Ne_and_weights == [0]:
        #         raise RuntimeError('硬件资源不足，请增加worker或减少创建节点所需的资源量')
        #     subtopo2nes = tp.sub_topos
        #     print("结合拓扑结构和资源切分结果：", subtopo2nes)

        #     scheme.update({'worker_list':worker_list})
        #     for i,subtopo in enumerate(subtopo2nes):
        #         resource_need = {'time_sum':lists_of_Ne_and_weights[i][1],'mem':0}
        #         tmp = {'ne_list':subtopo2nes[subtopo], 'resource_need':resource_need}
        #         scheme.update({subtopo:tmp.copy()})
        #     try:
        #         res_manager = ResourceManager(data, scheme)
        #         # 资源计算与分配都可以在ResourceManager里做
        #         res_manager.get_split_scheme(option=option)
        #         res_manager.close()
        #     except ResourceNotEnoughError as e:
        #         return {'code': 0, 'msg': f'由于物理资源不足, topo创建失败, 其他信息{e.args}'}
        else:
            worker_list = worker_redis.get_all_workers()
            _topo_split(user_topo_info, user_db_cli, worker_list, hardware)
        
        worker_list = []
        subtopo_list = user_db_cli.get_value('topo2subtopo', topo)
        for subtopo in subtopo_list:
            worker_hard = user_db_cli.get_value('subtopo2worker', subtopo)
            if worker_hard == 'hardware':
                pass
            else:
                worker_list.append(user_db_cli.get_value('subtopo2worker', subtopo))
        
        # wudx
        if PROJ_CONFIG.node_iso_resource_limit_CpuSet or PROJ_CONFIG.topo_iso_resource_limit_CpuSet:
        # 2.3.0、向redis写入topo所需要的核心数目信息
        # 放在此处虽然失去了表项处理的统一性，但是更为集中简单，且不用层层增加接口参数
            user_db_cli.set_value("topo_resource", topo, need_cores)
        # 2.3.1、计算全局各节点绑定cpu的信息
        # 如果后面部署节点时再计算此信息，会带来redis读写的数据一致性问题
        # 放在此处，可以达到一次读一次写，顺序计算全局的节点绑定情况
        # 引入预留给系统基本操作的核心参数，避免在节点对核心高占用时，导致系统基本命令无法保证
            try:
                res_manager.cal_cpuset_bind(PROJ_CONFIG.basic_os_cores)
            except Exception as e:
                print(e)
                

        # 2.5、进度条数据库里，为每个 worker 添加表项，初值为 int(0)
        with redis_context(user) as user_db_cli:
            for worker in worker_list:
                user_db_cli.set_value(PROJ_CONFIG.pb_table_name_prefix + '_' + \
                     user_topo_info['topo'] + '_' + 'deploy', worker, 0)

        # 3、worker 节点创建请求
        subtopo_list = user_db_cli.get_value('topo2subtopo', topo)
        # 添加异步请求的支持
        req_urls = []
        for subtopo in subtopo_list:
            worker_ip = user_db_cli.get_value('subtopo2worker', subtopo)
            if worker_ip == 'hardware':
                worker_ip = PROJ_CONFIG.hardware_worker
            info_dict = {'user': user, 'topo': topo, 'subtopo': subtopo}
            req_urls.append((f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/topo/', info_dict))
        print(f'req_url is: {req_urls}')
        rs = (grequests.post(url, json=req_paras) for url, req_paras in req_urls)

        # 4、所有 worker 均创建成功？
        resp_result = grequests.map(rs)
        print(resp_result)
        resp_status = [resp.json()['code'] for resp in resp_result]
        if not all(resp_status):
            return {'code': 0, 'msg': 'topo 创建失败'}

        # 5、worker 服务创建请求
        print('节点容器创建成功, 进行基本服务创建...')
        servcie_deploy_url = f'http://{get_host_ip()}:{PROJ_CONFIG.master_port}/master/service/'
        print(f'servcie_deploy_url: {servcie_deploy_url}')
        service_rs = [grequests.post(servcie_deploy_url, json={"user": user, "topo": topo}),]
        print('mapping service deploy req...')

        # 6、所有 worker 均创建成功？
        resp_result = grequests.map(service_rs)
        service_resp_code = [resp.json()['code'] for resp in resp_result]
        print(service_resp_code)
        if not all(service_resp_code):
            return {'code': 0, 'msg': 'topo服务创建失败'}
        print(f'创建耗时: {time() - current}')

        # 7、日志输出 wtx
        logger = UserLogger(user, UserLogLevel.First)
        logger.log_to_mysql(f'创建项目{topo}')

        # 7.5、卫星拓扑周期更新的任务发布
        if sat_flag:
            sat_evt_generate.delay(user, topo)

        # 8、返回创建成功
        master_delete_process_table.delay(user, topo, 'deploy')
        return {'code': 1, 'msg': '拓扑创建成功！'}

    except Exception as e:
        traceback.print_exc()
        master_delete_process_table.delay(user, topo, 'deploy')
        return {'code': 0, 'msg': str(e)}


@celery.task(track_started=True)
def master_delete_topo(user_topo_info):
    """
    拓扑删除函数
    
    Args:
        user_topo_info (dict): 仅包含user和topo的字典信息（与创建拓扑时不一样）
    """
    try:
        # 0、信息处理
        user, topo = user_topo_info['user'], user_topo_info['topo']
        # worker_redis = WorkerRedis()
        # worker_list = worker_redis.get_all_workers()
        # worker_redis.close()
            
        with redis_context(user) as user_db_cli:

            # 0.25、停止卫星拓扑刷新死循环
            table_name = f'{topo}{PROJ_CONFIG.sat_table_name}'
            if check_table_existence(user, table_name):
                user_db_cli.del_table(table_name)

            # 0.5、获取 worker 列表，并再进度条表中为每个 worker 设置表项，初值 int(0)
            # 获得包含每个 worker 的 ip 的列表
            worker_list = []
            # 找拓扑所在 subtopo，并通过 subtopo 的 list 找到相应 worker，并初始化进度条删除表的内容
            # 若 topo2subtopo 表不存在或 topo2subtopo 中的 topo 表项不存在，说明拓扑切分问题
            try:
                user_db_cli.check_table_exist('topo2subtopo')  # 表不存在，转到 expect，说明是拓扑切分问题
            except:
                return {'code': 1, 'msg': '项目删除成功'}
            if user_db_cli.check_exist('topo2subtopo', topo):  # 表项不存在，转到 else，说明是拓扑切分问题
                subtopo_list = user_db_cli.get_value('topo2subtopo', topo)
                for subtopo in subtopo_list:
                    worker_list.append(user_db_cli.get_value('subtopo2worker', subtopo))
                for worker in worker_list:
                    user_db_cli.set_value(PROJ_CONFIG.pb_table_name_prefix + '_' + \
                            user_topo_info['topo'] + '_' + 'delete', worker, 0)
            else:
                return {'code': 1, 'msg': '项目删除成功'}

            # 1、worker 节点删除请求
            req_urls = []
            for subtopo in subtopo_list:
                worker_ip = user_db_cli.get_value('subtopo2worker', subtopo)
                if worker_ip == 'hardware':
                    worker_ip = PROJ_CONFIG.hardware_worker

                if PROJ_CONFIG.heartbeat_enabled:
                    # （仅开启心跳时有此逻辑）不删除失效worker上的子拓扑，因为若尝试
                    # 失效worker上的子拓扑，会得不到响应，导致其它子拓扑无法正常删除。
                    # TODO：但是目前这样做会导致worker上的容器没有被清理
                    if worker_ip not in worker_list:
                        continue

                info_dict = {'user': user, 'topo': topo, 'subtopo': subtopo}
                req_urls.append((f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/topo/', info_dict))
            print(f'requ_url is: {req_urls}')
            rs = (grequests.delete(url, json=req_paras) for url, req_paras in req_urls)

            # 2、所有 worker 均创建成功？
            resp_result = grequests.map(rs)
            resp_status = [resp.json()['code'] for resp in resp_result]
            print(resp_status)
            if not all(resp_status):
                return {'code': 0, 'msg': '拓扑容器删除失败'}

            # 下面 3~6 步为对数据库内容的删除，归为进度条中的第四步骤
            # 3、删除拓扑调用
            if PROJ_CONFIG.split_option != SplitOption.NO_SPLIT:
                res_manager = ResourceManager(user_topo_info)
                res_manager.del_res_to_worker()
            # 4、监控控制删除
            if check_table_existence(user_topo_info['user'], f"{user_topo_info['topo']}_monitor"):
                del_result = delete_monitor_event(user_topo_info['user'], user_topo_info['topo'])
                if del_result["code"] == 0:
                    return {
                        'code': 0, 
                        'msg': f'拓扑容器删除成功，但是监控删除失败，错误信息：{del_result["msg"]}'
                    }
            if check_table_existence(user, f"{topo}_sflow"):
                #向http://{{master_ip}}:{{master_port}}/master/sflow/发送delete请求
                info_dict = {'user': user, 'topo': topo}
                req_url = f'http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/sflow/'
                response = requests.delete(req_url, json=info_dict)
                code = response.json()['code']
                if code :
                    print('sflow相关组件删除成功')
                else:
                    print('sflow相关组件删除有误')


            # 5、流量控制删除
            if check_table_existence(user_topo_info['user'], f"{user_topo_info['topo']}_traffic"):
                del_result = delete_all_traffic(user_topo_info["user"], user_topo_info['topo'])
                if del_result["code"] == 0:
                    return {
                        'code': 0, 
                        'msg': f'拓扑容器删除成功，但是流量删除失败，错误信息：{del_result["msg"]}'
                    }

            # 日志删除
            if check_table_existence(user_topo_info['user'],
                    f"{user_topo_info['user']}_{user_topo_info['topo']}_log"):
                user_db_cli.del_table(f"{user_topo_info['user']}_{user_topo_info['topo']}_log")
            
            logger = UserLogger(user, UserLogLevel.Second, topo)
            if not logger.delete_from_mysql():
                return {
                    'code': 0, 
                    'msg': '拓扑容器删除成功，但是日志信息失败'
                }
                
            # wudx
            # 5.5、CPU_SET模式下，查询相应表项，归还资源
            res_manager = ResourceManager(user_topo_info)
            # a.归还单个用户的资源配额
            res_manager.back_user_resource()
            # b.归还worker_resource中绑定的CPU资源
            res_manager.back_worker_cores()
            
            # 6、在这增加删除的topo总表项的接口
            user_db_cli.delete_topo_entry(topo)
            
            # 6.5、第四步骤完成后，更新进度条
            ProcessBarDelete(4, user_db_cli, topo)
            
            # 7、日志输出 wtx
            logger = UserLogger(user, UserLogLevel.First)
            logger.log_to_mysql(f'删除项目{topo}')

            # 8、返回创建成功
            master_delete_process_table.delay(user, topo, 'delete')
            return {'code': 1, 'msg': '项目删除成功'}

    except Exception as e:
        traceback.print_exc()
        master_delete_process_table.delay(user, topo, 'deploy')
        return {'code': 0, 'msg': str(e)}


@celery.task(track_started=True)
def master_delete_process_table(user, topo, usage):
    """
    删除redis中进度条表
    """
    # 进度条表名
    pb_table_name = PROJ_CONFIG.pb_table_name_prefix + '_' + topo + '_' + usage
    # 删除表前睡一会，避免前端请求进度时出现问题
    sleep(5)
    # 删除进度条表
    with redis_context(user) as user_db_cli:
        user_db_cli.del_table(pb_table_name)
