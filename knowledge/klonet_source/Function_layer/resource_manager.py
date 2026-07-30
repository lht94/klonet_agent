import json
import math
import requests
import re
from pprint import pprint
from ..tools import get_host_ip
from ..tools.log_tools import FLASK_LOGGER
from ..Service_layer.redisAPI import WorkerResourceRedis
from ..Function_layer.topo_preprocess import Topo_process
from ..Function_layer import topo_partition as tpn
from ..Service_layer.redisAPI import (UserDB, UserMapRedis, WorkerRedis, 
                                      UserCPUResourceRedis, ResourceRedis)
from ..Service_layer.redis_error import (DbCreateFailedError, 
        KeyNotExistError, NoFreeDbForUserError, DbAlreadyExistError)
from ..Service_layer.mysql_api.image import get_image_cpu_and_memory
from ..Service_layer.kvm_image_upload import get_default_kvm_image_cpu_and_mem, get_KVM_image_cpu_and_mem
from ..vemu_config.config import PROJ_CONFIG, SplitOption
regex_registry = re.compile(f"{PROJ_CONFIG.image_registry_ip}:{PROJ_CONFIG.image_registry_port}")

class CompareRES_GetWorkerResponseError(RuntimeError):
    """在进行实际资源与redis的worker_resource表数据对比时，没有获取到worker
        传过来的资源量，response.code不为200引发该错误"""

class ResourceQueryError(RuntimeError):
    """获取worker剩余资源信息失败，引发该异常"""

class ResourceNotEnoughError(RuntimeError):
    """worker资源不足导致切分方案失败，引发该异常"""

class ResourceSplitError(RuntimeError):
    """分配资源时，最后一个worker资源不足导致无法创建剩余拓扑，引发该异常，
    捕获异常后继续运行
    """

class Worker:
    """
    用于给worker资源进行排序
    """
    def __init__(self, ip, remain_cpu_core, remain_mem, remain_time_sum):
        self.ip = ip
        self.cpu_core = remain_cpu_core
        self.mem = remain_mem
        self.time_sum = remain_time_sum
    
    def __lt__(self, other):
        '''
        以cpu总的运行时间time_sum优先,然后是内存
        '''
        if self.time_sum == other.time_sum:
            return self.mem < other.mem
        return self.time_sum < other.time_sum

'''
测试数据
'''
worker_list = [Worker(1, 2, 100, 100), Worker(2, 4, 80, 100), Worker(3, 10, 100, 200), Worker(4, 2, 5000, 300),
               Worker(5, 2, 4800, 200)]

class DefaultImageResource:
    '''
    vemu_uestc/webserver/api/image/image_list.json文件中
    平台默认镜像的资源开销
    '''
    def __init__(self) -> None:
    # 默认镜像从文件中读取
        self.img2Resource = {}
        with open("vemu_uestc/webserver/api/image/image_list.json",'r') as f:
            default_imgInfo = json.load(f)
        for default_imgType in default_imgInfo:
            for default_img in default_imgInfo[default_imgType]:
                self.img2Resource.update({default_img['image_name']:default_img['resource_limit']})

class ResourceManager(DefaultImageResource):
    '''资源需求计算与管理、资源的获取与计算

    Attributes:
        topo_info: 拓扑json信息
        user: 拓扑user名
        topo: 拓扑topo名
        worker_resource: worker资源获取实例
        ne2res: 记录每个节点的需求情况
    '''
    # resource_type = ['cpu', 'mem']
    default_need = {
        'host': {
            'cpu': '100%',
            'mem': '100'
        },
        'switch': {
            'cpu': '100%',
            'mem': '100'
        },
        'router': {
            'cpu': '100%',
            'mem': '100'
        },
        'controller': {
            'cpu': '200%',
            'mem': '200'
        }
    }

    url = f"http://{get_host_ip()}:{PROJ_CONFIG.master_port}/master/resource/"

    def __init__(self, topo_info, scheme={}):
        super(ResourceManager, self).__init__()
        self.topo_info = topo_info
        self.user = self.topo_info['user']
        self.topo = topo_info['topo']
        user_db_map = UserMapRedis()
        self.worker_resource = WorkerResourceRedis()
        try:
            self.user_db_cli = user_db_map.set_user_db(self.user)
        except DbAlreadyExistError:
            self.user_db_cli = user_db_map.get_user_db(self.user)
        except NoFreeDbForUserError:
            return {'code': 0, 'msg': '数据库用户数目已达上限'}
        except DbCreateFailedError:
            return {'code': 0, 'msg': '用户数据库创建失败'}
        self.worker_list = []
        self.ne2res = {}
        self.scheme = scheme
        
    
    # wudx
    # 检查拓扑部署所需CPU个数是否超过单个用户的CPU配额
    def check_cpu_need(self):
        '''
        返回该拓扑所需要的CPU核心数目
        '''
        # 重新设计了高保真要求下，有关cpu核心数资源计算的方式
        self._calc_resource_need_cpuset()
        user_cpu_resource_manager = UserCPUResourceRedis()
        available_cpu = int(user_cpu_resource_manager.get_resource(self.user))
        if self.needs['core_num'] > available_cpu:
            print(self.needs['core_num'])
            raise ValueError("拓扑所需地CPU核心数超过用户配额，请减少节点数目，或删除其余无用拓扑")
        
        # 更新可用资源
        new_available_cpu = available_cpu - self.needs['core_num']
        user_cpu_resource_manager.set_resource(self.user, new_available_cpu)
        return self.needs['core_num']
        
    
    def get_split_scheme(self, option=1):
        '''
        创建拓扑时使用：
        计算拓扑所需资源、获取worker的剩余资源、得到切分方案
        '''
        # TODO（wudx)：目前下面仅第三种SPLIT_WITH_TOPO_RESOURCE切分方式能正常使用
        if option == SplitOption.SPLIT_WITH_RESOURCE:
            self._calc_resource_need()
        elif option == SplitOption.SPLIT_WITH_QUANTIFICATION:
            rsrc_quanlif = ResourceQuantification(self.topo_info)
            self.ne2res, self.ne_list, self.needs = rsrc_quanlif.calc_resource_need()
        elif option == SplitOption.SPLIT_WITH_TOPO_RESOURCE:
            worker_redis = WorkerRedis()
            remain_res = self._compare_res()
            # print("remain_res:", remain_res)
            self._calc_resource_need()
            worker_list = worker_redis.get_all_workers()
            FLASK_LOGGER.debug(f'workerlist:{worker_list}')
            worker_resource_list = []
            for worker in worker_list:
                # 目前结合拓扑的切分只考虑了cpu这一个维度，没有考虑mem
                FLASK_LOGGER.debug(worker)
                cpu = remain_res[worker]['cpu_time']
                FLASK_LOGGER.debug(f"cpu{cpu}")
                worker_resource_list.append((worker, cpu['time_sum']))
            # worker_resource_list = [('192.168.1.124', 41), ('192.168.1.105', 21)]
            tp = tpn.topo_adapting_partition(self.topo_info, worker_resource_list)
            tp()
            lists_of_Ne_and_weights = tp.topo_partition_new()
            if lists_of_Ne_and_weights == [0]:
                raise RuntimeError('硬件资源不足，请增加worker或减少创建节点所需的资源量')
            subtopo2nes = tp.sub_topos
            FLASK_LOGGER.debug(f"结合拓扑结构和资源切分结果：{subtopo2nes}")
            FLASK_LOGGER.debug(f"切分结果中的woker_list:{subtopo2nes.keys()}")

            nodes_info = self.topo_info['networks'].copy()
            del nodes_info['links']
            # 获取节点指定的workers
            node2worker_specified, specified_nodes = self.get_specified_node2worker(nodes_info)
            FLASK_LOGGER.debug(f'lll: {subtopo2nes} {node2worker_specified} { specified_nodes}')
            subtopo2nes_update = self.update_nodes2workers(subtopo2nes, node2worker_specified, specified_nodes)
            FLASK_LOGGER.debug(f"根据指定worker信息更新subtopo2nes：{subtopo2nes_update}")

            # 获取节点对应的资源量，{"h1":{"mem":,"cpu":}}
            node2resource = {}
            for ne_type in nodes_info:
                for node in nodes_info[ne_type]:
                    if nodes_info[ne_type][node]["service"] == "docker":
                        node2resource.update({node:nodes_info[ne_type][node]["resource_limit"]})
                    elif nodes_info[ne_type][node]["service"] == "kvm":
                        temp_cpu = str(int(nodes_info[ne_type][node]["resource_limit"]["cpu"]) * PROJ_CONFIG.ratio)
                        node2resource.update({node:{
                            "cpu": temp_cpu,
                            "mem": nodes_info[ne_type][node]["resource_limit"]["mem"]
                        }})
                    elif nodes_info[ne_type][node]["service"] == "hardware":
                        node2resource.update({node:{"cpu": "0", "mem": "0"}})    # 真实设备暂时写死为0，不计入平台资源

            self.scheme['worker_list'] = []
            for i,subtopo in enumerate(subtopo2nes_update):
                time_sum = 0
                for node in subtopo2nes_update[subtopo]:
                    time_sum += int(node2resource[node]['cpu'])
                # resource_need = {'time_sum':lists_of_Ne_and_weights[i][1],'mem':0}
                resource_need = {'time_sum':time_sum, 'mem':0}
                tmp = {'ne_list':subtopo2nes_update[subtopo], 'resource_need':resource_need}
                self.scheme.update({subtopo:tmp.copy()})
                self.scheme['worker_list'].append(subtopo)
        remain_res = self._compare_res()
        scheme = self.scheme
        FLASK_LOGGER.debug(f"scheme: {scheme}")
        # print("remain_res1:")
        # pprint(remain_res)
        # print("remain_res2:")
        # pprint(remain_res)
        # 切分后存数据库
        self._topo_split(self.topo_info, scheme)
        self.add_res_to_worker(scheme, remain_res)
        # return self.needs, remain_res

    def update_nodes2workers(self, subtopo2nes, specified_scheme, specified_nodes):
        """
        The update_nodes2workers function takes in a scheme and specified_scheme, which are both dictionaries.
        The scheme is the current state of the cluster, while specified_scheme is what we want to change it to.
        It then returns a new dictionary called scheme_update that contains all of the nodes from specified_nodes 
        that were not already present in scheme.
        
        Args:
            self: Access the class attributes
            scheme: Store the current scheme
            specified_scheme: Specify the nodes that are assigned to a particular worker
            specified_nodes: Specify the nodes that are assigned to a worker
        
        Returns:
            The scheme with the specified nodes removed from the workers
        
        Doc Author:
            Trelent
        """
        subtopo2nes_update = {}
        # subtopo2nes['hardware']
        for worker in subtopo2nes:
            tmp = list(set(subtopo2nes[worker]).difference(set(specified_nodes)))
            subtopo2nes_update.update({worker:tmp})
        for worker in specified_scheme:
            if worker in subtopo2nes_update:
                subtopo2nes_update[worker].extend(specified_scheme[worker])
            else:
                subtopo2nes_update[worker] = specified_scheme[worker]
        return subtopo2nes_update

    def get_specified_node2worker(self, nodes_info):
        """
        The get_specified_node2worker function returns a dictionary of the specified nodes and their corresponding worker.
        The function takes in the topology information as an argument, and returns a dictionary of specified nodes with their 
        corresponding workers.
        
        Args:
            self: Access the class attributes
        
        Returns:
            A dictionary that maps the specified node to its worker and a list of specified nodes
        
        Doc Author:
            Trelent
        """
        node2worker_specified = {}
        specified_nodes = []
        for node_type in nodes_info:
            for node in nodes_info[node_type]:
                if nodes_info[node_type][node].get('config') != None:
                    worker_specified = nodes_info[node_type][node].get('config').get('worker_specified')
                    if worker_specified != None:
                        if worker_specified not in node2worker_specified:
                            node2worker_specified[worker_specified] = [node]
                        else:
                            node2worker_specified[worker_specified].append(node)
                        specified_nodes.append(node)

        return node2worker_specified, specified_nodes

    def _compare_res(self):
        '''
        查询现有资源信息
        对比数据库中的剩余资源与当前新获取的剩余资源，
        返回较小的那个作为拓扑创建依据
        并添加worker的资源信息到self.workers中，用于计算方案
        
        Returns:
            remain_res:min(数据库中的剩余资源, 新获取的剩余资源)
        
        '''
        now_remain_res = self._resource_query() # 资源查询
        redis_remain_res = self.worker_resource.get_all_resources() # 数据查询
        # print("redis_remain_res:")
        # pprint(redis_remain_res)
        final_res = {}
        for ip, res_info in now_remain_res.items():
            worker_res = final_res.setdefault(ip, {})
            if not redis_remain_res.get(ip, {}):  # 数据库里没信息，以新获取的为准
                for key in res_info:
                    if key != "worker_ip" and key != "cpu_core":
                        worker_res[key] = res_info[key]
                    elif key == "cpu_core": # 没有信息，则初始化
                        worker_res[key] = {}
                        worker_res[key]['core_num'] = res_info[key] # 初始化还没有绑定核心
                        worker_res[key]['each_cpu'] = {}
                        for i in range(res_info[key]):
                            worker_res[key]['each_cpu'][str(i)] = 0 # 0则未被绑定核心
            else:  # 数据库里有信息，取较小值
                # cpu剩余时间
                # print("redis_remain_res[ip]['cpu_time']:", 
                #       redis_remain_res[ip]['cpu_time'], 
                #       type(redis_remain_res[ip]['cpu_time']))
                # print("redis_remain_res[ip]['cpu_time']['each_cpu']:", 
                #       redis_remain_res[ip]['cpu_time']['each_cpu'])
                cpu_time_info = worker_res.setdefault('cpu_time', {})
                # 1、总CPU剩余时间
                cpu_time_info['time_sum'] = min(
                    redis_remain_res[ip]['cpu_time']['time_sum'],
                    res_info['cpu_time']['time_sum'])
                # 2、每个CPU的剩余时间
                cpu_time_info['each_cpu'] = {}
                for cpu_core, remain_percent in res_info['cpu_time']['each_cpu'].items():
                    cpu_time_info['each_cpu'][cpu_core] = min(
                        remain_percent, 
                        redis_remain_res[ip]['cpu_time']['each_cpu'][cpu_core])
                # 3、内存
                worker_res['mem'] = min(res_info['mem'], 
                                        redis_remain_res[ip]['mem'])
                # 4、cpu核心个数，绑定的修改在判断之后做，cpu核数不会变，绑定核心需要数据库维护
                worker_res['cpu_core'] = redis_remain_res[ip]['cpu_core'] 
            self.worker_list.append(Worker(ip, worker_res['cpu_core']['core_num'], 
                                    worker_res['mem'], worker_res['cpu_time']['time_sum']))
        self.worker_list = sorted(self.worker_list)
        return final_res


    def _calc_resource_need(self):
        '''
        计算拓扑所需资源

        Returns:
            needs:拓扑累计所需的CPU(核数、cpu时间)、内存 dict
        '''
        self.needs = {
            'time_sum': '', # 需要的总cpu时间
            'mem': '', # 需要的总内存需求
            'core_num': '' # 需要的核心数量
        }
        network = self.topo_info["networks"]
        cpu_time = 0 # 需要的cpu时间需求
        mem_all = 0 # 内存需求
        ne_num = 0 # 需要的core_num
        for ne_type in network:
            if ne_type != "links": # 链路不在节点计算范围内
                for info in network[ne_type].values(): # 每个节点的信息
                    if info["service"] == "docker":
                        ne_num += 1
                        resource_limit = info.get('resource_limit', {})
                        if re.search(regex_registry, info['image_name']):
                            image_full_name = info['image_name']
                        else:
                            image_full_name = (f"{PROJ_CONFIG.image_registry_ip}:"
                                f"{PROJ_CONFIG.image_registry_port}/{self.user}/"
                                f"{info['image_name']}")
                        # 若为默认镜像，则不查询MySQL数据库，使用默认值
                        # 否则将查询数据库，上传私有镜像时会要求填写资源占用量
                        if info['image_name'] in self.img2Resource:
                            # print('镜像为基础镜像，使用image_list中的默认值')
                            cpu, mem = self.img2Resource[info['image_name']]['cpu'], \
                                self.img2Resource[info['image_name']]['mem']
                        else:
                            # print("从MySQL数据库中读取cpu和mem信息")
                            cpu, mem = get_image_cpu_and_memory(image_full_name)
                        # print("image_name:", image_full_name, "mem:", mem, "cpu:", cpu)
                        if resource_limit:
                            # print("使用用户提供的cpu和mem参数")
                            if resource_limit['cpu']:
                                ne_cpu = int(resource_limit['cpu'])
                            else:
                                # 默认值从mysql数据库中读取
                                ne_cpu = int(cpu)
                                resource_limit['cpu'] = cpu
                            if resource_limit['mem']:
                                ne_mem = int(resource_limit['mem'])
                            else:
                                ne_mem = int(mem)
                                resource_limit['mem'] = mem
                            cpu_time += ne_cpu
                            mem_all += ne_mem
                            self.ne2res[info['name']] = {
                                'cpu': ne_cpu,
                                'mem': ne_mem,
                                'ne_type': ne_type,
                                'image_name': info['image_name']
                            }
                        else:
                            ne_cpu = int(cpu)
                            ne_mem = int(mem)
                            cpu_time += ne_cpu
                            mem_all += ne_mem
                            self.ne2res[info['name']] = {
                                'cpu': ne_cpu,
                                'mem': ne_mem,
                                'ne_type': ne_type,
                                'image_name': info['image_name']
                            }
                    elif info["service"] == "kvm":
                        resource_limit = info.get('resource_limit', {})
                        if resource_limit:
                            ne_num += int(resource_limit['cpu'])    # VM的核心消耗也计入ne_num，虽然目前可能不会用到这个
                            ne_cpu = int(resource_limit['cpu']) * PROJ_CONFIG.ratio  # VM cpu的简单转换
                            ne_mem = int(resource_limit['mem'])
                        else:
                            # 一般传入的json都是已经查询获得resource_limit的数据的，下面的分支可能一般不会用到
                            # 从公用默认或者mysql中获取
                            # 公用默认镜像default_image
                            if info["vm_config"]["kvm_image"]["image_path"] == "default_image":
                                # if info["vm_config"]["type"] == "host":
                                #     image_name = PROJ_CONFIG.default_host_image
                                # elif info["vm_config"]["type"] == "router":
                                #     image_name = PROJ_CONFIG.default_router_image
                                # elif info["vm_config"]["type"] == "switch":
                                #     image_name = PROJ_CONFIG.default_switch_image
                                # elif info["vm_config"]["type"] == "controller":
                                #     image_name = PROJ_CONFIG.default_controller_image
                                image_name = info["vm_config"]["image_name"]    # 新增了默认镜像名称的字段
                                cpu, mem = get_default_kvm_image_cpu_and_mem(image_name)
                            # mysql获取镜像资源信息
                            else:
                                if info["vm_config"]["kvm_image"]["image_path"].startswith('self_upload_image:'):
                                    # web上传的镜像
                                    image_name = info["vm_config"]["kvm_image"]["image_path"].split(":")[-1]
                                else:
                                    # 非web端传入的镜像
                                    image_name = info["vm_config"]["kvm_image"]["image_path"].split("/")[-1]
                                cpu, mem = get_KVM_image_cpu_and_mem(self.user, image_name)
                            ne_cpu = int(cpu) * PROJ_CONFIG.ratio
                            ne_mem = int(mem)
                        # 累加合计cpu和mem
                        cpu_time += ne_cpu
                        mem_all += ne_mem
                        self.ne2res[info['name']] = {
                                'cpu': ne_cpu,
                                'mem': ne_mem,
                                'ne_type': ne_type,
                                'image_name': info['image_name']
                            }
                    elif info["service"] == "hardware": # 真实设备字段，需要校对名称
                        pass
                    else:
                        raise ValueError("设备类型异常，请确认json格式是否正确")
            else:
                #TODO(sw):计算每个节点的链路
                pass
            
        print("ne_num:", ne_num)
        self.ne_list = list(self.ne2res.keys())
        self.needs['time_sum'] = cpu_time
        self.needs['mem'] = mem_all
        self.needs['core_num'] = ne_num
    
    def _calc_resource_need_cpuset(self):
        '''wudx
        计算拓扑所需资源
        重写_calc_resource_need()函数
        根据CPU_SET方式重新设计关于core_num的计算，不再是粗暴的一个容器计算一个cpu核心

        Returns:
            needs:拓扑累计所需的CPU(核数、cpu时间)、内存 dict
        '''
        self.needs = {
            'time_sum': '', # 需要的总cpu时间
            'mem': '', # 需要的总内存需求
            'core_num': '' # 需要的核心数量
        }
        network = self.topo_info["networks"]
        cpu_time = 0    # 需要的cpu时间需求
        mem_all = 0     # 内存需求
        ne_num = 0      # 需要的core_num
        ne_cpu_count = 0.00 # 基本模式下有关核心数计算的临时存储值
        for ne_type in network:
            if ne_type != "links": # 链路不在节点计算范围内
                for info in network[ne_type].values(): # 每个节点的信息
                    # ne_num += 1
                    resource_limit = info.get('resource_limit', {})
                    if re.search(regex_registry, info['image_name']):
                        image_full_name = info['image_name']
                    else:
                        image_full_name = (f"{PROJ_CONFIG.image_registry_ip}:"
                            f"{PROJ_CONFIG.image_registry_port}/{self.user}/"
                            f"{info['image_name']}")
                    # 若为默认镜像，则不查询MySQL数据库，使用默认值
                    # 否则将查询数据库，上传私有镜像时会要求填写资源占用量
                    if info['image_name'] in self.img2Resource:
                        # print('镜像为基础镜像，使用image_list中的默认值')
                        cpu, mem = self.img2Resource[info['image_name']]['cpu'], \
                            self.img2Resource[info['image_name']]['mem']
                    else:
                        # print("从MySQL数据库中读取cpu和mem信息")
                        cpu, mem = get_image_cpu_and_memory(image_full_name)
                    # print("image_name:", image_full_name, "mem:", mem, "cpu:", cpu)
                    if resource_limit:
                        # print("使用用户提供的cpu和mem参数")
                        if resource_limit['cpu']:
                            ne_cpu = int(resource_limit['cpu'])
                        else:
                            # 默认值从mysql数据库中读取
                            ne_cpu = int(cpu)
                            resource_limit['cpu'] = cpu
                        if resource_limit['mem']:
                            ne_mem = int(resource_limit['mem'])
                        else:
                            ne_mem = int(mem)
                            resource_limit['mem'] = mem
                        cpu_time += ne_cpu
                        mem_all += ne_mem
                        self.ne2res[info['name']] = {
                            'cpu': ne_cpu,
                            'mem': ne_mem,
                            'ne_type': ne_type,
                            'image_name': info['image_name']
                        }
                    else:
                        ne_cpu = int(cpu)
                        ne_mem = int(mem)
                        cpu_time += ne_cpu
                        mem_all += ne_mem
                        self.ne2res[info['name']] = {
                            'cpu': ne_cpu,
                            'mem': ne_mem,
                            'ne_type': ne_type,
                            'image_name': info['image_name']
                        }
                    # 重新设计有关核心数的计算
                    # 1.按照高保真应用，以节点为单位进行资源隔离
                    if PROJ_CONFIG.node_iso_resource_limit_CpuSet:
                        ne_num += math.ceil(ne_cpu * 0.01)
                    # （TODO）wudx 
                    # 2.以拓扑为单位进行隔离
                    # 2.1 允许节点跨核心绑定
                    if PROJ_CONFIG.topo_iso_resource_limit_CpuSet:
                        ne_cpu_count += ne_cpu * 0.01
                    
                    # # 2.2 由于拓扑需要的核心数目，与拓扑切分后的子拓扑分布具有强耦合
                    # # 所以此处的总核心数目与真实核心数存在一定出入（本质上是单机计算）
                    # # 最坏情况为: 真实核心数目 - (worker_num - 1) = 计算核心数
                    # # 考虑worker数目一般远少于节点数目，因此认为计算值近似反映真实核心数目
                    # if PROJ_CONFIG.topo_iso_resource_limit_CpuSet:
                    #     ne_cpu_count += ne_cpu * 0.01
                    #     if ne_cpu_count >= 1.00:
                    #         ne_num += 1
                    #         if ne_cpu_count == 1.00:
                    #             ne_cpu_count = 0
                    #         else:
                    #             # 超过1个核心时，计算入下一个核心
                    #             # 全局来看并不精确，准确来说应该是一个背包问题
                    #             ne_cpu_count = ne_cpu
            else:
                #TODO(sw):计算每个节点的链路
                pass
        # 单独check，向上取整
        if PROJ_CONFIG.topo_iso_resource_limit_CpuSet:
            ne_num = math.ceil(ne_cpu_count)
        print("ne_num:", ne_num)
        self.ne_list = list(self.ne2res.keys())
        self.needs['time_sum'] = cpu_time
        self.needs['mem'] = mem_all
        self.needs['core_num'] = ne_num
        
    def cal_cpuset_bind(self, os_cores):
        '''wudx
        计算全局各个节点需要绑定的cpu信息，并在redis中更新worker_resource表
        1. 高性能模式，即一个容器可以独占一个核心或多个核心
        2. 基本模式，即一个拓扑为单位划分核心
        
        Args:
        os_cores (int): 留存给系统基本操作命令的核心数目 
        
        '''
        user_manager = UserMapRedis()
        user_cli = user_manager.get_user_db(self.user)
        # 剩余可用CPU资源
        res_manager = ResourceRedis("worker_resource")
        res_dict = res_manager.get_all_resources()    # 包含有所有worker的完整资源信息
        # print(res_dict)
        # print(self.topo_info)
        network = self.topo_info["networks"]
        
        # 1.高性能模式，以节点为单位划分核心
        if PROJ_CONFIG.node_iso_resource_limit_CpuSet:
            for ne_type in network:
                if ne_type != "links": # 链路不在节点计算范围内
                    for ne in network[ne_type].keys(): # 每个节点的名称
                        table_name = f"{self.topo}_{ne}"
                        resource_info = user_cli.get_value(table_name, "NEresource")
                        if int(resource_info["cpu"]) <= 0:
                            raise ValueError("在开启资源限制时，当前节点资源没有进行资源限制或资源限制错误")
                        fraction = int(resource_info["cpu"]) * 0.01
                        # 高性能需求下，令节点绑定cpu核心数严格向上取整
                        cpu_need_num = math.ceil(fraction)
                        
                        worker_ip = user_cli.get_worker_ip_by_ne_name(self.topo, ne)
                        res_copy = json.loads(res_dict[worker_ip])  # 仅仅是一份资源信息的拷贝，对原始数据res_dict不会有影响
                        # print("部署前：", res_copy["cpu_core"]["each_cpu"])
                        
                        # 依次序选取核心绑定到容器上
                        # 当其他节点核心占用过高时，会导致同一个拓扑的不同节点绑定到同一个核心上
                        
                        lowest = 0  # 核心数上期望的绑定节点数，依次增大，尽可能把当前节点绑定在占用少的核心上
                        count = 0   # 已经选取到的核心数
                        indices = []    # 目标核心index
                        while count != cpu_need_num:
                            basic_count = 0 # 预留给系统基本的核心计数，此处初始化保证每次都能跳过前面几个核心
                            for index, value in res_copy["cpu_core"]["each_cpu"].items():
                                basic_count += 1
                                if basic_count <= os_cores: # 跳过预留给系统的核心数目
                                    continue
                                if value == lowest and index not in indices:    # 避免重复选取相同核心
                                    indices.append(index)
                                    count += 1
                                if count == cpu_need_num:
                                    break
                            if count != cpu_need_num:
                                lowest += 1
                        for index in indices:
                            res_copy["cpu_core"]["each_cpu"][index] += 1
                        # 需要将每个worker的资源拷贝回去, 才会影响原始数据res_dict
                        res = json.dumps(res_copy)
                        res_dict.update({worker_ip: res})
                        
                        # 节点绑定核心记录在每个节点表内
                        core_dict = {f"{worker_ip}": indices}
                        user_cli.set_value(f"{self.topo}_{ne}", 'NEcpuset', core_dict)
                        
            # 最终一起将本拓扑的节点绑定变化回写进redis中
            # print("部署后：", res_dict)
            for worker_ip, res_info in res_dict.items():
                # 此时操作的res_info是个字符串，这样写其实不好，类型一直在转换
                # 可以在一开始的时候用一个循环将所有value的str类型转为dict，但是懒得改了
                res_manager.set_resource(worker_ip, json.loads(res_info))
        
        # 2.基本模式，以拓扑为单位划分核心
        if PROJ_CONFIG.topo_iso_resource_limit_CpuSet:
            # 初始化
            worker2cpu_num = {}     # 各worker上拓扑需要的核心数目
            worker2cpu_count = {}   # 各worker的CPU资源用量的临时计数
            ne_set2worker = {}      # 各worker上需要绑定在同一核心的节点集合，形式{worker_ip: [[n1, n2], [n3]]...}
            worker_manager = WorkerRedis()
            worker_list = worker_manager.get_all_workers()
            for worker_ip in worker_list:
                worker2cpu_num.update({worker_ip: 0})
                worker2cpu_count.update({worker_ip: 0.00})
                ne_set2worker.update({worker_ip:[]})
                
            res2worker_copy = {}
            # 类型转换
            for worker_ip, resource in res_dict.items():
                res2worker_copy.update({worker_ip:json.loads(resource)})
            # print(res2worker_copy)
            
            # 计算各worker上所需核心数目和同一核心上的节点集合
            NE_list = []
            for ne_type in network:
                if ne_type != "links": # 链路不在节点计算范围内
                    for ne in network[ne_type].keys(): # 每个节点的名称
                        NE_list.append(ne)
                        table_name = f"{self.topo}_{ne}"
                        resource_info = user_cli.get_value(table_name, "NEresource")
                        if int(resource_info["cpu"]) <= 0:
                            raise ValueError("在开启资源限制时，当前节点资源没有进行资源限制或资源限制错误")
                        fraction = int(resource_info["cpu"]) * 0.01
                        worker_ip = user_cli.get_worker_ip_by_ne_name(self.topo, ne)
                        standard = math.ceil(worker2cpu_count[worker_ip]) # 比较标准
                        pre_count = worker2cpu_count[worker_ip] # 初始值
                        worker2cpu_count[worker_ip] += fraction
                        # 当前核心有余量
                        if pre_count < standard:
                            if worker2cpu_count[worker_ip] > standard:
                                # 超过1个核心的资源量，跨核心
                                ne_set2worker[worker_ip][-1].append(ne)
                                # 建立后续的节点集合（考虑到一个节点需要2个及以上核心的情况）
                                rest = worker2cpu_count[worker_ip] - standard
                                for _ in range(math.ceil(rest)):
                                    ne_set2worker[worker_ip].append([ne])
                            else:
                                # 未超过1个核心的资源量，将节点放入上一个集合
                                ne_set2worker[worker_ip][-1].append(ne)
                        # 当前核心没有余量
                        elif pre_count == standard:
                            rest = worker2cpu_count[worker_ip] - standard
                            for _ in range(math.ceil(rest)):
                                ne_set2worker[worker_ip].append([ne])
                        else:
                            raise ValueError("CPU核心数目需求计算错误")
                        # print(ne_set2worker)
                            
                        # if worker2cpu_count[worker_ip] >= 1.00:
                        #     # 达到1时就新建立一个节点集合
                        #     ne_set2worker[worker_ip].append(set(ne))
                        #     worker2cpu_num[worker_ip] += 1
                        # else:
                        #     # 初始状态没有元素时？？？？
                        #     # 没达到1就将节点放入上个集合
                        #     ne_set2worker[worker_ip][-1].add(ne)
                        # if worker2cpu_count[worker_ip] == 1.00:
                        #     worker2cpu_count[worker_ip] = 0.00
                        # elif worker2cpu_count[worker_ip] > 1.00:
                        #     # 超过1个核心时，计算入下一个核心
                        #     # 全局来看并不精确，准确来说应该是一个背包问题
                        #     worker2cpu_count[worker_ip] = fraction
                        # else:
                        #     raise ValueError("CPU核心数目计算错误")
                        
            # 对每个worker计算最终需要的核心数量
            for worker_ip, count in worker2cpu_count.items():
                worker2cpu_num[worker_ip] = math.ceil(count)
            # print(worker2cpu_count)
            # print(worker2cpu_num)
            # print(ne_set2worker)
                        
            # 依次序选取核心绑定到容器上
            # 当其他节点核心占用过高时，会导致同一个拓扑的不同节点绑定到同一个核心上
            
            # 初始化绑定信息
            ne_bind_info = {}
            for ne in NE_list:
                attach_worker_ip = user_cli.get_worker_ip_by_ne_name(self.topo, ne)
                ne_bind_info.update({ne: {attach_worker_ip: []}})
            
            for worker_ip in worker_list:
                lowest = 0      # 核心数上期望的绑定节点数，依次增大，尽可能把当前节点绑定在占用少的核心上
                count = 0       # 已经选取到的核心数
                indices = []    # 目标核心index
                while count != worker2cpu_num[worker_ip]:
                    basic_count = 0 # 预留给系统基本的核心计数，此处初始化保证每次都能跳过前面几个核心
                    for index, value in res2worker_copy[worker_ip]["cpu_core"]["each_cpu"].items():
                        basic_count += 1
                        if basic_count <= os_cores: # 跳过预留给系统的核心数目
                            continue
                        if value == lowest and index not in indices:    # 避免重复选取相同核心
                            indices.append(index)
                            res2worker_copy[worker_ip]["cpu_core"]["each_cpu"][index] += len(ne_set2worker[worker_ip][count])  # 顺便更新核心使用情况
                            count += 1
                        if count == worker2cpu_num[worker_ip]:
                            break
                    if count != worker2cpu_num[worker_ip]:
                        lowest += 1
                # 对应每个节点的绑定信息
                ne_indx = 0 
                for index in indices:
                    NE_set = ne_set2worker[worker_ip][ne_indx]
                    ne_indx += 1
                    for ne in NE_set:
                        ne_bind_info[ne][worker_ip].append(index)
            
            # 将本拓扑的节点绑定变化信息回写进redis中
            for worker_ip, res_info in res2worker_copy.items():
                res_manager.set_resource(worker_ip, res_info)
                
            # 向每个节点数据表写入绑定信息
            for ne in NE_list:
                user_cli.set_value(f"{self.topo}_{ne}", 'NEcpuset', ne_bind_info[ne])
                    
            
        
    def back_user_resource(self):
        '''wudx
        删除拓扑时归还用户的cpu配额
        '''
        try:
            user_manager = UserMapRedis()
            user_db_cli = user_manager.get_user_db(self.user)
            used_cores = user_db_cli.get_value("topo_resource", self.topo)
            user_cpu_resource_manager = UserCPUResourceRedis()
            now_cores = int(user_cpu_resource_manager.get_resource(self.user))
            user_cpu_resource_manager.set_resource(self.user, used_cores + now_cores)
        except Exception as e:
            # 考虑到CPUSET模式会经常性的开关，可能有时候并不能获得信息，直接pass
            pass
    
    def back_worker_cores(self):
        '''wudx
        删除拓扑时归还每个worker的核心资源
        '''
        try:
            res_manager = ResourceRedis("worker_resource")
            res_dict = res_manager.get_all_resources()    # 包含有所有worker的完整资源信息
            res_copy = {}
            for worker_ip, res2worker in res_dict.items():
                res_copy[worker_ip] = json.loads(res2worker)   # 将字符串拷贝转换成嵌套字典的形式
            # print(res_copy)
            user_manager = UserMapRedis()
            user_db_cli = user_manager.get_user_db(self.user)
            # 不能使用此处的self.topo_info，跟部署拓扑时不一样，此处只传入了user和topo
            # print(self.topo_info)
            # network = self.topo_info["networks"]
            topo_info = user_db_cli.get_value("topo_list", self.topo)
            network = topo_info["networks"]
            for ne_type in network:
                if ne_type != "links":
                    for ne in network[ne_type].keys():  # 每个节点的名称
                        table_name = f"{self.topo}_{ne}"
                        bind_info = user_db_cli.get_value(table_name, "NEcpuset")   # 获取节点的绑定信息
                        for worker_ip, core_list in bind_info.items():  # 其实本循环只执行一次
                            for core in core_list:
                                res_copy[worker_ip]["cpu_core"]["each_cpu"][core] -= 1
            # 将数据回写到数据库中
            # 但无法避免多用户操作，始终会存在数据的一致性问题
            # print(res_copy)
            for worker_ip, res2worker in res_copy.items():
                print(res2worker)
                res_manager.set_resource(worker_ip, res2worker)
            
        except Exception as e:
            pass
        
    def _resource_query(self):
        '''
        查询目前所有worker的剩余资源情况
        
        Returns:
            now_remain_res:worker的剩余CPU、MEM情况
        now_remain_res = {
            worker_ip: {
                'cpu_time': {     // 每个cpu空余时间
                    'time_sum': 'xx',
                    'each_cpu': {
                        '0': 'xx',    
                        '1': 'xx',
                        ...
                    }
                },
                'cpu_core': 'xx', // cpu个数
                'mem': 'xx'       // 剩余内存
            }
            worker_ip2: {}
        }
        '''
        now_remain_res = {}
        # try:
        response = requests.get(self.url)
        if response.status_code == 200:
            if (response.json()['code'] == 1):
                print("code:", response.json()['code'])
                now_remain_res = response.json()['info']
            else:
                print("code:", response.json()['code'])
                raise ResourceQueryError()
        else:
            raise CompareRES_GetWorkerResponseError()
        # except Exception as e:
        #     print(e)
        
        return now_remain_res
    
    def _det_scheme(self):
        '''
        切分方案
        之后可能变动较大，还是先写成函数
        目前方案:1、采用前缀和,选择若干worker,基本资源是递增的,但方案不全
        scheme = {
            'worker_list': [worker_ip1, worker_ip2], # 有哪些worker
            'worker_ip1': {
                'ne_list': [ne1, ne2,],
                'resource_need': {
                    'time_sum': 'xx',
                    'mem': 'yy'
                }
            },
            'worker_ip2': {
                'ne_list': [ne3, ne4, ...],
                'resource_need': {
                    'time_sum': 'xx',
                    'mem': 'yy'
                }
            }
        }
        '''
        scheme = {}
        for worker in self.worker_list:
            if worker.time_sum >= self.needs['time_sum'] and \
               worker.mem >= self.needs['mem']:
                # 选择这个worker，最小化碎片，并更新final_res
                # 设置切分方案的格式
                res_need = {
                    'time_sum': self.needs['time_sum'],
                    'mem': self.needs['mem']
                }
                # 所有ne都给这个worker
                scheme = self._gen_scheme_format(
                    scheme, worker.ip, self.ne_list, res_need)
                break
        if not scheme:
            chosen_workers = []
            worker_num = len(self.worker_list)
            print("worker_num:", worker_num)
            prefix_time_sum = [0] * (worker_num + 1)
            prefix_mem = [0] * (worker_num + 1)
            for i in range(1, worker_num + 1):
                # print("i:", i)
                # print("prefix_time_sum[i]:", prefix_time_sum[i])
                # print("self.worker_list[i - 1]:", self.worker_list[i - 1])
                # print("prefix_time_sum[i - 1]:", prefix_time_sum[i - 1])
                prefix_time_sum[i] = self.worker_list[i - 1].time_sum + prefix_time_sum[i - 1]
                prefix_mem[i] = self.worker_list[i - 1].mem + prefix_mem[i - 1]
            print("prefix_time_sum:", prefix_time_sum, "prefix_mem:", prefix_mem)
            print("self.needs['time_sum']:", self.needs['time_sum'], 
                         "self.needs['mem']:", self.needs['mem'])
            chosen_worker_num = 2 # 两个开始，及分到2~worker_num个worker上
            while (not scheme) and chosen_worker_num <= worker_num: # i从2 ~ worker_num
                print("chosen_worker_num:", chosen_worker_num)
                for j in range(len(self.worker_list) - chosen_worker_num + 1):
                    workers = self.worker_list[j: j + chosen_worker_num]
                    total_time_sum = prefix_time_sum[j + chosen_worker_num] - prefix_time_sum[j] # 取 第i+1~j+i个worker
                    total_mem = prefix_mem[j + chosen_worker_num] - prefix_mem[j]
                    print("total_time_sum:", total_time_sum, "total_mem:", total_mem)
                    if (total_time_sum >= self.needs['time_sum'] and 
                        total_mem >= self.needs['mem']): # 找到合适的workers
                        for worker in workers:
                            chosen_workers.append(worker)
                        # 具体切分策略
                        try:
                            scheme = self._split_ne_to_worker(chosen_workers)
                            print("scheme:", scheme)
                            break
                        except ResourceSplitError as e:
                            print("该方案切分失败，报错信息：", e.args)
                            print("#" * 50, "继续计算切分策略", "#" * 50)
                            scheme = {}
                            continue
                chosen_worker_num += 1
            if not scheme:
                raise ResourceNotEnoughError(
                    f"所有Worker资源不足以放置topo:{self.topo_info['topo']}")
            else:
                return scheme
        else:
            return scheme
        
    def _split_ne_to_worker(self, workers):
        '''
        根据给出的worker规则和节点列表进行顺序放置,
        即把节点分到不同worker上,返回特定的切分格式
        scheme = {
            'worker_list': [worker_ip1, worker_ip2], # 有哪些worker
            'worker_ip1': {
                'ne_list': [ne1, ne2,],
                'resource_need': {
                    'time_sum': 'xx',
                    'mem': 'yy'
                }
            }
            'worker_ip2': {
                'ne_list': [ne3, ne4, ...],
                'resource_need': {
                    'time_sum': 'xx',
                    'mem': 'yy'
                }
            }
        }
        '''
        # worker_list
        scheme = {}
        pre = 0
        cur = 0
        workers_num = len(workers)
        ne_num = len(self.ne_list)
        print("workers_num:", workers_num)
        print("ne_list:", self.ne_list)
        for i in range(workers_num):
            time_sum = workers[i].time_sum
            mem = workers[i].mem
            ne_total_time_sum = 0 # 统计目前为止节点需要的资源用量
            ne_total_mem = 0
            if i == workers_num - 1:
                for ne_id in range(cur, ne_num):
                    ne_total_time_sum += self.ne2res[self.ne_list[ne_id]]['cpu']
                    ne_total_mem += self.ne2res[self.ne_list[ne_id]]['mem']
                if ne_total_time_sum <= time_sum and ne_total_mem <= mem:
                    res_need = {
                        'time_sum': ne_total_time_sum,
                        'mem': ne_total_mem
                    }
                    scheme = self._gen_scheme_format(
                        scheme, workers[i].ip, self.ne_list[cur:],
                        res_need
                    )
                else: # 因为有碎片内存、CPU等原因，最后一个worker可能放不下剩余容器
                    error_msg = (f'worker ip:{workers[i].ip},无法容纳剩余拓扑,'
                                f'剩余拓扑CPU需求:{ne_total_time_sum},'
                                f'剩余拓扑MEM需求:{ne_total_mem},'
                                f'worker CPU剩余:{time_sum},'
                                f'worker MEM剩余:{mem}')
                    raise ResourceSplitError(error_msg)
            else: # 也有可能碰到放完的情况
                while ne_total_time_sum + self.ne2res[self.ne_list[cur]]['cpu'] <= time_sum \
                      and ne_total_mem + self.ne2res[self.ne_list[cur]]['mem'] <= mem:
                    ne_total_mem += self.ne2res[self.ne_list[cur]]['mem']
                    ne_total_time_sum += self.ne2res[self.ne_list[cur]]['cpu']
                    cur += 1
                    res_need = {
                        'time_sum': ne_total_time_sum,
                        'mem': ne_total_mem
                    }
                    if cur > ne_num: # 在前几台worker已经创建完，就跳出循环
                        scheme = self._gen_scheme_format(
                            scheme, workers[i].ip, self.ne_list[pre: cur - 1],
                            res_need)
                        break
                    # print("cur:", cur)
                # print("pre:", pre, "cur:", cur)
                res_need = {
                    'time_sum': ne_total_time_sum,
                    'mem': ne_total_mem
                }
                scheme = self._gen_scheme_format(
                    scheme, workers[i].ip, self.ne_list[pre: cur], res_need)
                pre = cur
        return scheme
    
    def _gen_scheme_format(self, scheme, ip, ne_list: list, res_need: dict):
        '''
        生成特定格式的scheme
        '''
        worker_list = scheme.setdefault("worker_list", [])
        worker_list.append(ip)
        allo_detail = scheme.setdefault(ip, {})
        allo_detail["ne_list"] = ne_list # 所有节点都交给worker
        needs = allo_detail.setdefault("resource_need", {})
        needs['time_sum'] = res_need['time_sum']
        needs['mem'] = res_need['mem']
        return scheme
    
    def add_res_to_worker(self, scheme, remain_res):
        '''
        创建拓扑时调用，计算扣除拓扑所需资源后所剩余资源，
        并将剩余资源和切分策略存到数据库
        '''
        for worker_ip in scheme['worker_list']:
            if worker_ip == 'hardware':
                pass
            else:
                remain_res[worker_ip]['cpu_time']['time_sum'] -= scheme[worker_ip]['resource_need']['time_sum']
                remain_res[worker_ip]['mem'] -= scheme[worker_ip]['resource_need']['mem']        
        self._save_worker_res_to_db(remain_res)
        self._save_topo_scheme_to_db(scheme)

    def del_res_to_worker(self):
        '''
        删除拓扑时调用，查询数据库中的剩余资源，返还拓扑被分配的资源
        同时删除该拓扑对应的切分策略
        '''
        # 此时可以不用比大小，创建拓扑的时候会比较数据库与实际获取的差距
        remain_res = self.worker_resource.get_all_resources()
        scheme = self.user_db_cli.get_value("topo_split_scheme", self.topo)
        for worker_ip in scheme['worker_list']:
            if worker_ip == 'hardware':
                pass
            else:
                remain_res[worker_ip]['cpu_time']['time_sum'] += scheme[worker_ip]['resource_need']['time_sum']
                remain_res[worker_ip]['mem'] += scheme[worker_ip]['resource_need']['mem']
        self._save_worker_res_to_db(remain_res)
        self._del_topo_scheme_to_db() 


    def _save_worker_res_to_db(self, remain_res):
        '''
        存储worker的剩余资源到worker_resource表
        '''
        # print("remain_res:", remain_res)
        self.worker_resource.set_all_resource(remain_res)
        
    
    def _save_topo_scheme_to_db(self, scheme):
        '''
        存储topo分配的切分策略到用户的topo_split_scheme表
        '''
        self.user_db_cli.set_value("topo_split_scheme", self.topo, scheme)
    
    def _del_topo_scheme_to_db(self):
        '''
        删除该拓扑的切分策略表
        '''
        self.user_db_cli.del_value("topo_split_scheme", self.topo)

    def _topo_split(self, data, scheme:dict):
        '''
        拓扑切分：
        将信息写入plane_topo_list拓扑信息表，并通过ne_table_dict等完成对各个节点、链路表项的信息写入
        '''
        # split_result = get_plane_topo(data, worker_list, option=0)

        # 更新最新资源配置情况
        hardware = False
        self.update_redis_res()
        # 测试用切分
        topo_processed1 = Topo_process(data, hardware, scheme['worker_list'], scheme)
        split_result = topo_processed1()
        # pprint(split_result)

        # 正式切分
        # worker_redis = WorkerRedis()
        # worker_list = worker_redis.get_all_workers()
        # worker_redis.close()
        # topo_processed = Topo_process(data, worker_list, option=0)
        # split_result = topo_processed()
        # pprint(split_result)

        # 写入topo_list
        topo = data['topo']
        self.user_db_cli.set_value('topo_list', topo, data)

        direct_write_tables = ['plane_topo_list', 'topo_service', 'topo2subtopo',
         'subtopo2worker', 'plane_subtopo_list', 'subtopo_service']
        parse_write_tables = ['ne_table_dict', 'link_table_dict', 'vxlanlink_table_dict']
        for table in direct_write_tables:
            print(table)
            for k, v in split_result[table].items():
                self.user_db_cli.set_value(table, k, v)

        for key in parse_write_tables:
            tables = split_result[key]
            for k, v in tables.items():
                self.user_db_cli.set_all_values(k, v)
    
    def update_redis_res(self):
        '''
        更新redis中拓扑字典中各类型节点的cpu资源限制信息
        '''
        network = self.topo_info["networks"]
        for ne, res in self.ne2res.items():
            ne_type = res['ne_type']
            if network[ne_type][ne]['service'] == 'docker':
                # wudx
                # 初步理解此处要重新向json中写入cpu信息是因为可能查询镜像资源那块导致的资源数值变化
                # print(f"原topo_info{network[ne_type][ne]['resource_limit']['cpu']}")
                network[ne_type][ne]['resource_limit']['cpu'] = str(int(res['cpu']))
                # print(f"修改后topo_info{network[ne_type][ne]['resource_limit']['cpu']}")
            elif network[ne_type][ne]['service'] == 'kvm':
                # 虚机做额外处理，在self.ne2res中其数值被乘上了ratio以表示cpu运行时间
                # 但我们仍然希望在redis中存储的是cpu的核心数而不是运行时间
                network[ne_type][ne]['resource_limit']['cpu'] = str(int(int(res['cpu']) // PROJ_CONFIG.ratio)) # 取整
            elif network[ne_type][ne]['service'] == 'hardware':
                pass
            else:
                pass


    def close(self):
        self.worker_resource.close()
        self.user_db_cli.close()


class DynamicResourceManager(ResourceManager):
    """
    动态资源管理类
    
    Attributes:
        data (dict): 修改的数据
        name (str): 节点名称
        topo (str): 拓扑名称
        user (str): 用户名称
        ne_info (dict): 修改节点的信息
        worker2subtopo (dict): 所有worker_ip所对应的子拓扑信息
        worker_resource (WorkerResourceRedis): worker的redis资源管理类
        worker_list (list): worker列表
        user_db_cli (UserDB): Redis的用户数据连接
        
    """
    def __init__(self, data, worker2subtopo):
        """
        Attributes:
            data (dict): 修改的数据
            worker2subtopo (dict): 所有worker_ip所对应的子拓扑信息
            
        Returns:
            dict: 执行结果字典
        """
        self.data = data
        self.name = data['info']['name']
        self.topo = data["topo"]
        self.user = data["user"]
        self.ne_info = data["info"]
        self.worker2subtopo = worker2subtopo
        self.worker_resource = WorkerResourceRedis()
        self.worker_list = []
        user_db_map = UserMapRedis()
        try:
            self.user_db_cli = user_db_map.set_user_db(self.user)
        except DbAlreadyExistError:
            self.user_db_cli = user_db_map.get_user_db(self.user)
        except NoFreeDbForUserError:
            return {'code': 0, 'msg': '数据库用户数目已达上限'}
        except DbCreateFailedError:
            return {'code': 0, 'msg': '用户数据库创建失败'}
    
    def get_add_ne_worker(self):
        '''
        获取节点应当放置的worker位置
        '''
        # wudx 保持逻辑连贯一致，hardware不在此处处理，还是始终用worker_specified字段
        # 考虑在API传参时直接查表获取worker_specified再往后传入
        # 此函数也会被复用，但其实对hardware来说没什么大用
        
        # 获取剩余资源信息compare_res  
        self.remain_res = self._compare_res()     # 该函数初始化self.worker_list并按资源大小顺序排列
        resource_info = self.ne_info['resource_limit']
        print("resource_info:", resource_info)
        other_worker_list = []
        subtopo_worker_list = list(self.worker2subtopo.keys()) # 所有subtopo的worker
        # worker_list是按照CPU资源从小到大排序的
        # 遍历所有子拓扑的worker，看哪个worker放的下就可以了
        res = {}
        for worker in self.worker_list: 
            if worker.ip in subtopo_worker_list:
                if worker.time_sum >= int(resource_info['cpu']) and \
                worker.mem >= int(resource_info['mem']):
                    res['worker_ip'] = worker.ip
                    res['subtopo'] = self.worker2subtopo[worker.ip]
            elif worker.ip not in subtopo_worker_list:
                other_worker_list.append(worker) # 这样加入也是有序的
        # 都放不下再从其他worker下手
        # 如果是这个情况的话，plane_subtopo_list等需要新加subtopo
        # 注意回滚
        if not res:
            for worker in other_worker_list:
                if worker.time_sum >= int(resource_info['cpu']) and \
                    worker.mem >= int(resource_info['mem']):
                        res['worker_ip'] = worker.ip
                        subtopo = self.topo + "_sub" + str(len(
                            subtopo_worker_list) + 1)
                        res['subtopo'] = subtopo
                        print("new subtopo:", subtopo)
        if not res:
            raise ResourceNotEnoughError(
                    f"所有Worker资源不足以放置topo:{self.ne_info['name']}")
        else:
            return res

    def update_worker_resource(self, worker_ip, choice="add"):
        '''
        更新redis中的worker资源信息
        Args:   
            worker_ip: 动态创建所在的workerIP
            choice: add or delete , str, 是添加/删除节点
        '''
        resource_info = self.ne_info['resource_limit']
        time_sum = int(resource_info['cpu'])    # 虚机在此处仍然继承了转换后的cpu运行时间，而不是核心数
        mem = int(resource_info['mem'])
        if choice == "add":
            print("原资源：", self.remain_res)
            self.remain_res[worker_ip]['cpu_time']['time_sum'] -= time_sum
            self.remain_res[worker_ip]['mem'] -= mem
            print("现资源：", self.remain_res)
            # 切分策略更新
            scheme = self.user_db_cli.get_value("topo_split_scheme", self.topo)
            # 新worker
            if worker_ip in scheme['worker_list']:
                scheme[worker_ip]['ne_list'].append(self.name)
                scheme[worker_ip]['resource_need']['time_sum'] += time_sum
                scheme[worker_ip]['resource_need']['mem'] += mem
            else:
                # hardware对应的跳板交换机ip也会在此处被加入scheme
                scheme['worker_list'].append(worker_ip)
                worker_info = scheme.setdefault(worker_ip, {})
                worker_info['ne_list'] = [self.name,]
                res_need = worker_info.setdefault('resource_need', {})
                res_need['time_sum'] = time_sum
                res_need['mem'] = mem

        elif choice == "delete":
            # hardware被删除后，需要跳过对worker_resource表的处理
            if self.ne_info['service'] != 'hardware':
                self.remain_res[worker_ip]['cpu_time']['time_sum'] += int(
                    resource_info['cpu'])
                self.remain_res[worker_ip]['mem'] += int(resource_info['mem'])
            # 切分策略更新,删除了的节点一定在scheme的worker_list中了
            scheme = self.user_db_cli.get_value("topo_split_scheme", self.topo)
            scheme[worker_ip]['ne_list'].remove(self.name)
            scheme[worker_ip]['resource_need']['time_sum'] -= time_sum
            scheme[worker_ip]['resource_need']['mem'] -= mem
        # worker资源数据库
        self._save_worker_res_to_db(self.remain_res)
        self._save_topo_scheme_to_db(scheme)
    
    def del_ne_worker(self):
        '''
        删除节点时更新worker的资源信息
        '''
        # 删除节点的时候，只传入创建的worker, 需要传入ne的资源配置信息
        worker_ip = list(self.worker2subtopo.keys())[0]
        self.remain_res = self.worker_resource.get_all_resources()
        self.update_worker_resource(worker_ip, "delete")



class ResourceQuantification:
    '''资源量化: 得到节点的资源需求
    PPS(Packet Per Second)---->CPU(%)
    拟合公式:CPU = 73.501ln(PPS) - 769.18

    Attributes:
        topo_info: 拓扑描述信息
        pkt_len: 用户给出数据流的包平均长度
    '''
    def __init__(self, topo_info, pkt_len="1500") -> None:
        self.topo_info = topo_info
        # 包平均长度：Bytes
        self.pkt_len = int(pkt_len) 
        self.ne2link_resource = {}
        self.ne2res = {}
        self.ne_list = []

    def calc_resource_need(self):
        self.needs = {
            'time_sum': '', # 需要的总cpu时间
            'mem': '', # 需要的总内存需求
            'core_num': '' # 需要的核心数量
        }
        network = self.topo_info["networks"]
        cpu_time = 0 # 需要的cpu时间需求
        mem = 0 # 内存需求
        ne_num = 0 # 需要的core_num
        for ne_type in network:
            if ne_type != "links": # 链路不在节点计算范围内
                for info in network[ne_type].values(): # 每个节点的信息
                    ne_num += 1
                    resource_limit = info.get('resource_limit', {})
                    if resource_limit:
                        ne_cpu = int(resource_limit['cpu'])
                        ne_mem = int(resource_limit['mem'])
                        cpu_time += ne_cpu
                        mem += ne_mem
                        self.ne2res[info['name']] = {
                            'cpu': ne_cpu,
                            'mem': ne_mem,
                            'ne_type': ne_type,
                            'image_name': info['image_name']
                        }
                    else:
                        #TODO(sw)：考虑镜像默认的CPU和内存
                        pass
            else:
                for link_config in network[ne_type].values():
                    self._cal_link_resource(link_config)
    
        # print("ne_num:", ne_num)
        print("res_need:", self.ne2link_resource)
        self.ne_list = list(self.ne2res.keys())
        for ne, value in self.ne2link_resource.items():
            print(f"{ne}原资源需求:", self.ne2res[ne])
            ne_add = 0
            for link_name, res_need in value.items():
                if res_need['time_sum'] > 0:
                    self.ne2res[ne]['cpu'] += res_need['time_sum']
                    ne_add += res_need['time_sum']
                print("ne:", ne, "link:", link_name)
            # TODO(sw):这里还需要把更新后的资源需求添加到数据库
            cpu_time += ne_add
            print(f"{ne}加上链路后资源需求：", self.ne2res[ne])

        # 总需求
        self.needs['time_sum'] = cpu_time
        self.needs['mem'] = mem
        self.needs['core_num'] = ne_num
        return self.ne2res, self.ne_list, self.needs
    
    def _cal_link_resource(self, link_config):
        '''
        计算链路所需资源
        策略：仅端主机(hosts)发流,统计所有与端主机相连的链路

        Args:
            link_config: 链路配置
        ne2link = {
            'h1': {
                l1: {
                    time_sum: xxx,
                    mem: xxx // 待使用
                },
                l2: {
                    ...
                }
            },
            'h2': {

            }
        }
        '''
        src, tgt = link_config['source'], link_config['target']
        link_name = link_config['name']
        if src.startswith('h') or tgt.startswith('h'):
            # 此处认为该链路两端的带宽跑满
            # src
            src_links = self.ne2link_resource.setdefault(src, {})
            src_resource = src_links.setdefault(link_name, {})
            src_resource['time_sum'] = self._link_resource_to_cpu(
                link_config['config']['source'])
            # target
            tgt_links = self.ne2link_resource.setdefault(tgt, {})
            tgt_resource = tgt_links.setdefault(link_name, {})
            tgt_resource['time_sum'] = self._link_resource_to_cpu(
                link_config['config']['target'])

        
    def _link_resource_to_cpu(self, tc_config):
        '''
        从带宽转换为PPS,并从PPS转换为资源需求(目前仅考虑CPU)
        '''
        bw_kbit = tc_config['bw_kbit']

        pps = (bw_kbit / 8) * 2 ** 10 / self.pkt_len
        # print("pps", pps)
        cpu_time_sum = self._cal_pps_to_cpu(pps)
        return cpu_time_sum
    
    def _cal_pps_to_cpu(self, pps):
        '''
        拟合得到的pps与cpu之间的关系
        pps-->cpu(%)
        '''
        return 73.501 * math.log(pps) - 769.18




if __name__ == "__main__":
    data = {
        "user": "sw",  
        "topo": "resource_test",  
        "networks": {            
            "controllers": {},    
            "hosts": {           
                "h1": {
                    "resource_limit": {
                        "cpu": "20%",  
                        "mem": "10"  
                    }
                },
                "h2": {
                    "resource_limit": {
                        "cpu": "20%",  
                        "mem": "100"  
                    }
                }
            },
            "routers": {
                "r1": {
                    "resource_limit": {
                        "cpu": "20%",  
                        "mem": "10"  
                    }
                }
            },
            "switches": {
                "s1": {
                    "resource_limit": {
                        "cpu": "20%",  
                        "mem": "4000"  
                    }
                }
            }
        }
    }
    res_manager = ResourceManager(data)
    res_manager.get_resource()
