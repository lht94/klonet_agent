from abc import ABCMeta, abstractmethod
import copy
import re
from ....Service_layer.redisAPI import WorkerRedis
from ....tools import get_vxlan_vni, get_vxlan_ovs_id, cidr_netmask, netmask2cidr
from ....tools.generate_ne_id import MySnow, SnowFlakekvm
from ....Function_layer.topo_preprocess import Ne_host, Ne_router, Ne_switch, Ne_controller
from ....Function_layer.resource_manager import DynamicResourceManager, DefaultImageResource
from ....Service_layer.redis_error import KeyNotExistError
from ....Service_layer.mysql_api.image import get_image_cpu_and_memory
from ....Service_layer.kvm_image_upload import get_default_kvm_image_cpu_and_mem, get_KVM_image_cpu_and_mem
from ....vemu_config.config import PROJ_CONFIG, SplitOption
from ....Implement_layer.LinkManager import link_operate
from ....tools.log_tools import FLASK_LOGGER

regex_registry = re.compile(f"{PROJ_CONFIG.image_registry_ip}:{PROJ_CONFIG.image_registry_port}")

snow = MySnow()
kvm_snow = SnowFlakekvm()

NE_TYPE_WITH_INTF = ['host', 'router', 'switch']


def get_ne_types(ne_type: str):
    """
    根据节点的type字段返回复数形式type的字段
    Args:
        ne_type (str): 容器类型

    Returns:
        ne_type(es) (str): 负数表示
    """
    if ne_type in ['host', 'router', 'floodlight', 'controller', 'dpdk']:
        return ne_type + 's'
    elif ne_type in ['switch',]:
        return ne_type + 'es'
    else:
        raise TypeError('wrong ne type')





class DataHandler(metaclass=ABCMeta):
    """
    数据处理的抽象基类定义 modify_db() rollback_db() 接口
    """
    @abstractmethod
    def modify_db(self):
        raise NotImplementedError

    @abstractmethod
    def rollback_db(self):
        raise NotImplementedError


class NECreateDataHandler(DataHandler,DefaultImageResource):
    """
    动态创建节点的数据处理类
    
    Arributes:
        data (dict): 修改的数据
        user (str): 用户名
        re_cli (UserDB): Redis数据连接
        info (dict): 修改节点的信息
        info_config (dict): 节点的配置信息
        name (str): 节点名称
        topo (str): 拓扑名称
        topo_subtopo2worker (dict): 子拓扑和worker_ip的字典对应信息
        worker2topo_subtopo (dict): 子拓扑和worker_ip的字典对应信息
        
    """
    def __init__(self, data: dict, re_cli):
        """
        Args:
            data (dict): 修改的数据
            re_cli (UserDB): Redis数据连接

        Returns:
            None

        """
        DefaultImageResource.__init__(self)
        self.data = data
        self.user = data['user']
        self.re_cli = re_cli
        self.info = data['info']
        self.info_config = data['info']['config']
        self.name = data['info']['name']
        self.topo = data['topo']
        self.topo_subtopo2worker = {}
        self.worker2topo_subtopo = {}
        
    def get_subtopo2worker(self) -> dict:
        '''
        获取某一拓扑下当前的子拓扑和worker_ip的字典
        '''
        subtopo2worker = self.re_cli.get_all_values('subtopo2worker')
        for subtopo in subtopo2worker:
            if '_'.join(subtopo.split('_')[:-1]) == self.topo:
                self.topo_subtopo2worker.update({subtopo:subtopo2worker[subtopo]})
                self.worker2topo_subtopo.update({subtopo2worker[subtopo]:subtopo})

    def modify_db(self):
        """
        修改Redis数据库
        """
        FLASK_LOGGER.debug('get subtopo info')
        self.get_subtopo2worker()
        FLASK_LOGGER.debug('set ne info...')
        subtopo, worker_ip = self._set_ne_info()
        FLASK_LOGGER.debug('set plane and service info...')
        self._set_plane_and_service_db(subtopo, worker_ip)
        FLASK_LOGGER.debug(f'worker ip is {worker_ip}')
        return [worker_ip, ]

    def _set_plane_and_service_db(self, subtopo, worker_ip):
        """
        修改 plane_topo_list, topo_service, topo2subtopo, plane_subtopo_list, subtopo_servcie 表
        """
        ori_info = {}
        # 写入 plane_topo_list, topo_service
        FLASK_LOGGER.debug('写入 plane_topo_list, topo_service...')
        for table in ['plane_topo_list', 'topo_service']:
            topo = self.topo
            info = self.re_cli.get_value(table, topo)
            ori_info[table] = info
            new_info = copy.deepcopy(info)
            ne_lst = new_info.get('NEs') if 'NEs' in new_info else \
                new_info.get(get_ne_types(self.info['type']))
            ne_lst.append(self.name)
            self.re_cli.set_value(table, topo, new_info)
        # 写入 topo2subtopo, plane_subtopo_list， subtopo_service
        FLASK_LOGGER.debug('写入 topo2subtopo, plane_subtopo_list， subtopo_service...')
         
        #TODO(sw):subtopo不存在则创建
        # 需要修改plane_subtopo_list，subtopo_service，topo2subtopo， subtopo2worker
        if subtopo not in self.subtopos:
            self._init_new_table(subtopo, ori_info, worker_ip)
        else:
            for table in ['plane_subtopo_list', 'subtopo_service']:
                info = self.re_cli.get_value(table, subtopo)
                ori_info[table] = info
                # 这里得用deepcopy来存储之前的值， 不然赋值为引用仍会修改之前的值
                new_info = copy.deepcopy(info)
                ne_lst = new_info.get('NEs') if 'NEs' in  new_info else \
                        new_info.get(get_ne_types(self.info['type']))
                ne_lst.append(self.name)
                self.re_cli.set_value(table, subtopo, new_info)
            setattr(self, 'ori_info', ori_info)
        if PROJ_CONFIG.split_option != SplitOption.NO_SPLIT:
            # 考虑资源
            FLASK_LOGGER.debug("update resource redis")
            self._update_res_db(worker_ip, "add")
    
    def _init_new_table(self, subtopo, ori_info, worker_ip):
        '''
        新节点创建在新的subtopo上，每个subtopo相关的表都需要更新
        '''
        for table in ['plane_subtopo_list', 
                      'subtopo_service', 
                      'topo2subtopo', 
                      'subtopo2worker']:
            if table == 'plane_subtopo_list':
                info = {'NEs': [], 'links': [], 'vxlanlinks': []}
                # 因为是新子拓扑，原来redis没有这个拓扑信息，用不上回滚ori_info
                ne_lst = info.get('NEs')
                ne_lst.append(self.name)
                self.re_cli.set_value(table, subtopo, info)
            elif table == 'subtopo_service':
                info = {'switches': [], 'hosts': [], 
                        'routers': [], 'dpdks': [], 'controllers': []}
                ne_lst = info.get(get_ne_types(self.info['type']))
                ne_lst.append(self.name)
                self.re_cli.set_value(table, subtopo, info)
            elif table == 'topo2subtopo':
                info = self.re_cli.get_value(table, self.topo)
                ori_info[table] = info
                new_info = copy.deepcopy(info)
                new_info.append(subtopo)
                self.re_cli.set_value(table, self.topo, new_info)
            elif table == 'subtopo2worker':
                info = worker_ip
                self.re_cli.set_value(table, subtopo, info)
            
        setattr(self, 'ori_info', ori_info)

    def _set_ne_info(self):
        """
        生成并写入动态添加的节点信息
        """
        topo, name = self.topo, self.name
        if PROJ_CONFIG.split_option == SplitOption.NO_SPLIT:
            sub1 = self.re_cli.get_value('topo2subtopo', topo)[0]
            FLASK_LOGGER.debug(f'sub1 is {sub1}')
            worker_ip = self.re_cli.get_value('subtopo2worker', sub1)
            self.subtopos = self.re_cli.get_value('topo2subtopo', topo)
            subtopo = sub1
        else:
            #TODO(sw):检查资源配置
            self._check_resource_info()
            worker2subtopo = {}
            self.subtopos = self.re_cli.get_value('topo2subtopo', topo)
            for subtopo in self.subtopos:
                worker_ip = self.re_cli.get_value('subtopo2worker', subtopo)
                worker2subtopo[worker_ip] = subtopo
            subtopo, worker_ip = self._get_dynamic_deploy_worker(worker2subtopo)

        ne_id = snow.get_id()
        info = {
            'NEservice': self.info['service'],
            'NEvmconfig': self.info['vm_config'] if self.info['service'] == 'kvm' else {},
            'NEnic': [kvm_snow.get_id() for i in range(self.info['vm_config']['port_num'])] if self.info['service'] == 'kvm' else [],
            'NEinterface': [], 
            'NEimage': self.info['image_name'],
            'NEtype': self.info['type'],
            'NEsubtype': self.info['subtype'],
            'NEx': self.info['x'],
            'NEy': self.info['y'],
            # 'NEresource': self.info['resource_limit'],
            'NEloc': subtopo,
            'NEid': ne_id,
            'NElinestyle': self.info['linestyle'],
            'NEnet': 0,
            'NEperformance': self.info.get('performance', "")
        }
        # info中记录资源信息重新处理
        # 虚机记录在redis里的cpu信息还是得回到以核心数量为单位
        # 但self.info中的依然是转换后的cpu运行时间，跟info不一样
        if PROJ_CONFIG.split_option == SplitOption.NO_SPLIT:    # 均分的时候没有对resource进行换算
            info['NEresource'] = self.info['resource_limit']
        else:
            info['NEresource'] = {
                'cpu': str(int(self.info['resource_limit']['cpu']) // PROJ_CONFIG.ratio) if self.info['service'] == 'kvm' else self.info['resource_limit']['cpu'],
                'mem': self.info['resource_limit']['mem']
            }
        info = self._get_ne_config_para(info)
        # 在_get_ne_config_para函数中对用户指定的worker进行判断，若指定了则覆盖默认方法的worker采用用户指定的worker
        subtopo= info['NEloc']
        if 'worker_specified' in self.info_config and self.info_config.get('worker_specified') != '':
            worker_ip = self.info_config.get('worker_specified')
        # 若为dpdk节点，需要添加dpdk_nums键值
        if info['NEtype'] == 'dpdk':
            # dpdk_ctn_num = len(self.re_cli.get_value('topo_service', topo)['dpdks'])
            dpdk_ctn_max = self.re_cli.get_value('topo_service', topo)['dpdks']
            if dpdk_ctn_max != []:
                dpdk_ctn_max_num = int(dpdk_ctn_max[-1][4:])
            else:
                dpdk_ctn_max_num = 0
            # 按道理，节点动态创建，数字是按顺序往上增的，也就是说dpdk1，dpdk2...，从小到大的顺序，那直接取最后一个的编号
            tap_num = dpdk_ctn_max_num * 2 # 假设已有x个dpdk复合节点，则下一个tap网卡编号为2x、2x+1
            info.update({'dpdk_nums':[str(link_operate.generate_uuid_len_10()), str(link_operate.generate_uuid_len_10()), tap_num]})
        # 这里NEconfig的格式需要再商议
        # 这里还需要针对不同节点对参数进行特异化的处理
        FLASK_LOGGER.debug(f'info is {info}')
        self.re_cli.set_all_values(f'{topo}_{name}', info)
        if self.info['service'] == 'kvm':
            nodetointerface_info = self._get_kvm_nodetointerface(info)
            interfacetoname_info = self._get_kvm_interfacetoname(info, self.info['portname'])
            self.re_cli.set_all_values(f'{topo}_{name}_nodetointerface', nodetointerface_info)
            self.re_cli.set_all_values(f'{topo}_{name}_interfacetoname', interfacetoname_info)
        elif self.info['service'] == 'docker':
            self.re_cli.set_all_values(f'{topo}_{name}_nodetoname', {})
        return subtopo, worker_ip
    
    def _check_resource_info(self):
        """
        确定节点镜像的资源限制信息
        """
        resource_info = self.info['resource_limit']
        # TODO(sw)如果为空就获取默认的
        if self.info['service'] == 'docker':
            if re.search(regex_registry, self.info['image_name']):
                image_full_name = self.info['image_name']
            else:
                image_full_name = (f"{PROJ_CONFIG.image_registry_ip}:"
                    f"{PROJ_CONFIG.image_registry_port}/{self.data['user']}/"
                    f"{self.info['image_name']}")
            if self.info.get("tag", "") and "latest" not in image_full_name:
                image_full_name = image_full_name + ":" + self.info.get("tag", "")
            elif not self.info.get("tag", "") and "latest" not in image_full_name:
                image_full_name += ":latest"
            FLASK_LOGGER.debug("image_full_name: "+ str(image_full_name))
            FLASK_LOGGER.debug(self.info['image_name'] + str(self.img2Resource))
            if self.info['image_name'] in self.img2Resource:
                FLASK_LOGGER.debug('镜像为基础镜像，使用image_list中的默认值')
                cpu, mem = self.img2Resource[self.info['image_name']]['cpu'], \
                    self.img2Resource[self.info['image_name']]['mem']
            else:
                FLASK_LOGGER.debug("从MySQL数据库中读取cpu和mem信息")
                cpu, mem = get_image_cpu_and_memory(image_full_name)
            FLASK_LOGGER.debug("cpu:"+ str(cpu) + " mem:" + str(mem))
            if not resource_info['cpu']:
                resource_info['cpu'] = "10"
            if not resource_info['mem']:
                resource_info['mem'] = "20"
        elif self.info['service'] == 'kvm':
            if resource_info:
                resource_info["cpu"] = str(int(resource_info["cpu"]) * PROJ_CONFIG.ratio)   # 转换为cpu_time
            else:
                # resource_info为空时才查询，一般传过来的应该都不为空的
                if self.info["vm_config"]["kvm_image"]["image_path"] == "default_image":
                    if self.info["vm_config"]["type"] == "host":
                        image_name = PROJ_CONFIG.default_host_image
                    elif self.info["vm_config"]["type"] == "router":
                        image_name = PROJ_CONFIG.default_router_image
                    elif self.info["vm_config"]["type"] == "switch":
                        image_name = PROJ_CONFIG.default_switch_image
                    elif self.info["vm_config"]["type"] == "controller":
                        image_name = PROJ_CONFIG.default_controller_image
                    cpu, mem = get_default_kvm_image_cpu_and_mem(image_name)
                else:
                    if self.info["vm_config"]["kvm_image"]["image_path"].startswith('self_upload_image:'):
                        # web上传的镜像
                        image_name = self.info["vm_config"]["kvm_image"]["image_path"].split(":")[-1]
                    else:
                        # 非web端传入的镜像
                        image_name = self.info["vm_config"]["kvm_image"]["image_path"].split("/")[-1]
                    cpu, mem = get_KVM_image_cpu_and_mem(self.user, image_name)
                    
                resource_info = {"cpu": str(int(cpu) * PROJ_CONFIG.ratio), "mem": mem} # 转换为cpu_time
        elif self.info['service'] == 'hardware':
            resource_info = {"cpu": "0", "mem": "0"}    # 占位，后续参与切分不报错
        else:
            pass
    
    def _get_dynamic_deploy_worker(self, worker2subtopo):
        """
        获取动态添加节点所在子拓扑subtopo和worker的ip
        """
        self.res_manager = DynamicResourceManager(self.data, worker2subtopo)
        res = self.res_manager.get_add_ne_worker()
        return res['subtopo'], res['worker_ip']
    
    def _update_res_db(self, worker_ip, choice="add"):
        """
        向redis中写入更新后的worker资源信息
        """
        self.res_manager.update_worker_resource(worker_ip, choice)
        self.res_manager.close()

    def rollback_db(self):
        pass

    def _get_ne_config_para(self, info: dict):
        """
        得到ne_config 中的内容
        Args:
            info (dict): 节点配置信息

        Returns:
            info (dict):基本类型节点的config信息

        """
        ne_type = self.info['type']
        # 这里的ne_config对应于 [NEconfig][config]中的内容
        ne_config = {}
        # worker_specified字段为空就默认worker，否则使用用户指定的worker
        if 'worker_specified' in self.info_config:
            if self.info_config.get('worker_specified') in self.worker2topo_subtopo:    
                # 若指定创建的worker上已经有sub_topo了，则直接将其NEloc指定为该sub_topo即可
                info['NEloc'] = self.worker2topo_subtopo[self.info_config.get('worker_specified')]
            else:
                # 若指定创建的worker上还没有sub_topo，则新建一个sub_topo
                info['NEloc'] = f'{self.topo}_sub{len(self.topo_subtopo2worker)+1}'
        if ne_type == 'switch':
            stp = self.info_config.get('stp') if self.info_config.get('stp') else True
            ne_config['stp'] = stp
            ne_config['controllers'] = self.info_config['controllers']
        elif ne_type == 'host':
            info.update({'NEgateway': self.info["gateway"]})
        elif ne_type == 'controller':
            ne_config['port'] = self.info_config['port']
        elif ne_type == 'router':
            info.update({'NEgateway': self.info["gateway"]})
            ne_config.update(self.info_config)
        else:
            pass
        info.update({'NEconfig': {'config': ne_config}})
        return info
   
    def _get_kvm_nodetointerface(self, info: dict):
        nodetointerface_info = {}
        if info['NEtype'] == 'host':
            for i in range(len(info['NEnic'])):
                nodetointerface_info.setdefault('eth' + str(i+1), info['NEnic'][i])
        elif info['NEtype'] == 'router':
            for i in range(len(info['NEnic'])):
                nodetointerface_info.setdefault("Ethernet1/0/" + str(i+1), info['NEnic'][i])
        else:
            pass
        return nodetointerface_info

    def _get_kvm_interfacetoname(self, info: dict, name: list):
        interfacetoname_info = {}
        for i in range(len(info['NEnic'])):
            interfacetoname_info.setdefault(info['NEnic'][i], name[i])
        return interfacetoname_info

    def _get_docker_nodetoname(self, info: dict):
        pass

class NEDeleteDataHandler(DataHandler):
    """
    删除节点时Redis信息维护代理类
    
    Attributes:
        re_cli (UserDB): Redis数据库连接
        name (str): 节点名称
        topo (str): 拓扑名称
        info (dict): 节点详细信息
        data (dict): 删除节点信息
    """

    def __init__(self, data, re_cli):
        """
        Args:
            data (dict): 删除节点的信息
            re_cli (UserDB): Redis数据库连接

        Returns:
            None
        """
        self.re_cli = re_cli
        self.name = data['info']['name']
        self.topo = data['topo']
        self.info = data['info']
        self.data = data

    def modify_db(self):
        """
        维护数据库信息 步骤如下:
        删除节点的时候，需要删除
        1. 节点容器
        2. 删除节点容器的表，
        3. 节点容器相关的链路表， 如有必要，
        4. 还有vxlan的表
            先查找link, 再查vxlanlink, 删除vxlanlink, 删除对端的信息
            删除对端表项的信息   这里只用来删除表项信息
            还需要删除对端节点信息中的链路信息
        """
        table = f'{self.topo}_{self.name}'
        info = self.re_cli.get_all_values(table)
        self.resource_limit = info.get("NEresource", {})
        FLASK_LOGGER.debug(f'get info {info}')
        for k, v in info.items():
            if k.startswith('link'):
                self._delete_link(k[5:])
        # 删除表项信息
        FLASK_LOGGER.debug('delete info in common table ...')
        self._del_plane_and_service_table(info['NEloc'])
        self.re_cli.del_all_values(table)
        if info['NEservice'] == 'kvm':
            self.re_cli.del_all_values(f'{table}_nodetointerface')
            self.re_cli.del_all_values(f'{table}_interfacetoname')
        elif info['NEservice'] == 'docker':
            try:
                self.re_cli.del_all_values(f'{table}_nodetoname')
            except:
                pass

    def _delete_link(self, link):
        """
        删除节点相连的链路数据库表
        """
        link_deleter = LinkDeleteHandler(self.topo, link, self.re_cli)
        link_deleter.modify_db()

    def _del_plane_and_service_table(self, subtopo):
        """
        将该节点的信息从与topo相关的表项中删除
        """
        ori_info = {}
        # 删除plane_topo_list, topo_service
        FLASK_LOGGER.debug(f'delete info in plane_topo_list, topo_service...')
        for table in ['plane_topo_list', 'topo_service']:
            info = self.re_cli.get_value(table, self.topo)
            FLASK_LOGGER.debug(f'info of {table}: {info}')
            ori_info[table] = info
            new_info = copy.deepcopy(info)
            ne_lst = new_info.get('NEs') if new_info.get('NEs') else \
                    new_info.get(get_ne_types(self.info['type']))
            FLASK_LOGGER.debug(f'delete info in {table}....')
            FLASK_LOGGER.debug(f'ne_lst before {ne_lst}')
            FLASK_LOGGER.debug(ne_lst)
            ne_lst.remove(self.name)
            FLASK_LOGGER.debug(f'ne_lst after {ne_lst}')
            self.re_cli.set_value(table, self.topo, new_info)
        # 删除topo2subtopo, plane_subtopo_list, subtopo_service中的相关数据
        FLASK_LOGGER.debug('delete info in plane_subtopo_list, subtopo_service...')
        for table in ['plane_subtopo_list', 'subtopo_service']:
            info = self.re_cli.get_value(table, subtopo)
            ori_info[table] = info
            new_info = copy.deepcopy(info)
            FLASK_LOGGER.debug(f'ne_lst before {ne_lst}')
            ne_lst = new_info.get('NEs') if new_info.get('NEs') else \
                new_info.get(get_ne_types(self.info['type']))
            ne_lst.remove(self.name)
            FLASK_LOGGER.debug(f'ne_lst after {ne_lst}')
            self.re_cli.set_value(table, subtopo, new_info)
        setattr(self, 'ori_info', ori_info)
        if PROJ_CONFIG.split_option != SplitOption.NO_SPLIT:
            worker_ip = self.re_cli.get_value('subtopo2worker', subtopo)
            worker2subtopo = {worker_ip: subtopo}
            FLASK_LOGGER.debug(self.info)
            if self.info['service'] == 'docker':
                res_limit = self.data['info'].setdefault('resource_limit', {})
                res_limit['cpu'] = self.resource_limit['cpu']
                res_limit['mem'] = self.resource_limit['mem']
            elif self.info['service'] == 'kvm':
                res_limit = self.data['info'].setdefault('resource_limit', {})
                res_limit['cpu'] = str(int(self.resource_limit['cpu']) * PROJ_CONFIG.ratio)  # 直接采用数据库中的信息
                res_limit['mem'] = self.resource_limit['mem']
            elif self.info['service'] == 'hardware':
                res_limit = self.data['info'].setdefault('resource_limit', {})
                res_limit = {'cpu':'0', 'mem':'0'}  # 强制为0
            else:
                pass
            res_manager = DynamicResourceManager(self.data, worker2subtopo)
            res_manager.del_ne_worker()
            res_manager.close()

    def rollback_db(self):
        pass


class LinkCreateHandler(DataHandler):
    """
    动态链路创建数据信息维护表
    
    Attributes:
        data (dict): 链路信息
        name (str): 链路名称
        topo (str): 拓扑名称
        re_cli (UserDB): 数据库连接
        src (Node): 源节点对象
        tgt (Node): 目的节点对象
        info (dict): 链路具体配置信息
    
    """
    def __init__(self, data, re_cli):
        """
        Args:
            data (dict): 链路信息
            re_cli (UserDB): 数据库连接

        Returns:
            None
        """
        self.data = data
        self.name = data['info']['name']
        self.topo = data['topo']
        self.re_cli = re_cli
        self.src = data['info']['source']
        self.tgt = data['info']['target']
        self.info = data['info']

    @property
    def _if_same_worker(self):
        """
        检查链路两端节点是否在同一宿主机上
        """
        src_worker_ip = self.re_cli.get_worker_ip_by_ne_name(self.topo, self.src)
        tgt_worker_ip = self.re_cli.get_worker_ip_by_ne_name(self.topo, self.tgt)
        return src_worker_ip == tgt_worker_ip

    def modify_db(self):
        """
        修改链路相关数据表
        Args:
            None

        Returns:
            worker_ip_list (list): worker_ip 列表
        """
        # 判断两端节点是否位于同一个worker
        # 还需要写入plane_topo_list 和 plane_subtopo_list
        link_info = self._get_veth_link_table_info()
        self._set_link_info_to_ne()
        self._set_plane_topo_list()
        self._set_plane_subtopo_list()
        if self._if_same_worker:
            # 若两端在同一宿主机上，写入普通的veth pair的信息，并且返回worker_ip
            # 并返回(worker_ip, name)的元组， 方便构建worker_link的请求体
            self.re_cli.set_all_values(f'{self.topo}_{self.name}', link_info)
            return [(self.re_cli.get_worker_ip_by_ne_name(self.topo, self.src), self.name), ]
        else:
            # 若两端在不同的宿主机上， 写入基本链路信息和vxlan的信息，并返回worker_ip
            link_info.update({'vxlan': [f'link_{self.name}_vxlan1', f'link_{self.name}_vxlan2']})
            self.re_cli.set_all_values(f'{self.topo}_{self.name}', link_info)
            vxlans, worker_ip_lst = self._get_vxlan_link_table_info()
            for vxlan, info in vxlans.items():
                self.re_cli.set_all_values(f'{self.topo}_{vxlan}', info)
            return worker_ip_lst

    def _set_plane_topo_list(self):
        """
        存储 plane_topo_list表
        """
        table = 'plane_topo_list'
        ori_info = self.re_cli.get_value(table, self.topo)
        ori_info['links'].append(self.name)
        self.re_cli.set_value(table, self.topo, ori_info)

    def _set_plane_subtopo_list(self):
        """
        存储 plane_subtopo_list表
        """
        table = 'plane_subtopo_list'
        # 如果不是vxlan, src 和 tgt 在一个subtopo中
        if self._if_same_worker:
            loc = self.re_cli.get_value(f'{self.topo}_{self.src}', 'NEloc')
            ori_info = self.re_cli.get_value(table, loc)
            ori_info['links'].append(self.name)
            self.re_cli.set_value(table, loc, ori_info)
        # 分别在不同的子拓扑中，则需要分别写信息
        else:
            # 默认设置： vxlan1是src的信息， vxlan2 是 tgt的信息
            for vxlan, ne in zip([f'link_{self.name}_vxlan1', f'link_{self.name}_vxlan2'],
                                 [self.src, self.tgt]):
                loc = self.re_cli.get_value(f'{self.topo}_{ne}', 'NEloc')
                ori_info = self.re_cli.get_value(table, loc)
                ori_info['vxlanlinks'].append(vxlan)
                self.re_cli.set_value(table, loc, ori_info)

    def _set_link_info_to_ne(self):
        """
        存储链路信息到节点表
        """
        link_key = f'link_{self.name}'
        # 这里创建新链路的时候，IP mask都是没有的，没连线根本就不能配置
        # 所以写入的时候， 就是为空的， 之后配置的时候，进行update就可以了
        # 这里需要加入端口的信息，对于 host 和 router来说
        # kvm需要额外修改NEvmconfig
        src_type, tgt_type = self.info['sourceType'], self.info['targetType']
        src_service = self.re_cli.get_value(f"{self.topo}_{self.src}", "NEservice")
        tgt_service = self.re_cli.get_value(f"{self.topo}_{self.tgt}", "NEservice")
        print(src_service, tgt_service)
        if src_service == 'kvm':
            src_vmport = self.info['VMsourcePort']
            config = self.re_cli.get_value(f"{self.topo}_{self.src}", "NEvmconfig")
            # 设置端口占用
            config['check_port'][src_vmport] = 1
            self.re_cli.set_value(f'{self.topo}_{self.src}', "NEvmconfig", config)
        if tgt_service == 'kvm':
            tgt_vmport = self.info['VMtargetPort']
            config = self.re_cli.get_value(f"{self.topo}_{self.tgt}", "NEvmconfig")
            # 设置端口占用
            config['check_port'][tgt_vmport] = 1
            self.re_cli.set_value(f'{self.topo}_{self.tgt}', "NEvmconfig", config)            
        common_info  = {"ip": "", "mask": ""}
        # set src 之后不能简单的update， 不然之后的信息会被覆盖掉
        src_value = {'name': f"{self.src}{self.tgt}_{self.info['count']}"} if src_type in NE_TYPE_WITH_INTF else common_info
        src_value.update(common_info)
        src_value.update({'nic':f"to{self.tgt}_{self.info['count']}"})
        print(src_value)
        self.re_cli.set_value(f'{self.topo}_{self.src}', link_key, src_value)
        # set tgt
        dst_value = {'name': f"{self.tgt}{self.src}_{self.info['count']}"} if tgt_type in NE_TYPE_WITH_INTF else common_info
        dst_value.update(common_info)
        dst_value.update({'nic':f"to{self.src}_{self.info['count']}"})
        print(dst_value)
        self.re_cli.set_value(f'{self.topo}_{self.tgt}', link_key, dst_value)
        print(self.re_cli.get_value(f'{self.topo}_{self.tgt}', link_key))

    def rollback_db(self):
        pass

    def _get_vxlan_link_table_info(self):
        """
        得到vxlan链路的相关信息
        Args:
            None

        Returns:
            vxlans, [(src_worker_ip, vxlan1), (tgt_worker_ip, vxlan2)] (tuple): vxlan相关信息
        """
        # 这里目前只是在写入和存储相关的数据信息，不承担实体的操作
        vxlan1, vxlan2 = f'link_{self.name}_vxlan1', f'link_{self.name}_vxlan2'
        vni = get_vxlan_vni()
        src_worker_ip = self.re_cli.get_worker_ip_by_ne_name(self.topo, self.src)
        tgt_worker_ip = self.re_cli.get_worker_ip_by_ne_name(self.topo, self.tgt)
        fmveth = self.re_cli.get_value(f'{self.topo}_{self.name}', 'sourceveth')
        toveth = self.re_cli.get_value(f'{self.topo}_{self.name}', 'targetveth')
        vxlans = {
            # 这里只要进行拆分并确保数据的存储正确就行
            # 此处存储src的信息, 存储基本的信息就可
            vxlan1: {
                "VNI": vni,
                "remoteIP": tgt_worker_ip,
                "source": self.src,
                # 这里target ovs的名称也是随机生成的嘛
                "target": get_vxlan_ovs_id(),
                "sourcePort": "",
                "partof": self.name,
                "sourceIP": self.info['sourceIP'],
                'sourceveth': str(fmveth) + '0', 
                'targetveth': str(fmveth) + '1',
            },
            # 此处存储tgt的信息, 存储基本的信息就可
            vxlan2: {
                "VNI": vni,
                "remoteIP": src_worker_ip,
                "source": self.tgt,
                "target": get_vxlan_ovs_id(),
                "sourcePort": "",
                "partof": self.name,
                "sourceIP": self.info['targetIP'],
                'sourceveth': str(toveth) + '0', 
                'targetveth': str(toveth) + '1'
            }
        }
        return vxlans, [(src_worker_ip, vxlan1), (tgt_worker_ip, vxlan2)]

    def _get_veth_link_table_info(self):
        """
        Args:
            None

        Returns:
            info (dict): veth pair 链路信息

        """
        # 这里还没有写sourceID 和 targetID
        src_id = self.re_cli.get_value(f'{self.topo}_{self.src}', 'NEid')
        tgt_id = self.re_cli.get_value(f'{self.topo}_{self.tgt}', 'NEid')
        self.info['count'] = self.re_cli.get_parallel_by_nes(self.topo, self.src, self.tgt)+1
        print(self.info['count'])
        info = {
            'sourceNE': self.src,
            'sourceType': self.info['sourceType'],
            'sourcePort': "",
            'sourceID': src_id,
            'sourceIP': self.info["sourceIP"],
            'targetNE': self.tgt,
            'targetType': self.info['targetType'],
            'targetPort': "",
            'targetID': tgt_id,
            'targetIP': self.info['targetIP'],
            'sourceservice': self.re_cli.get_value(f'{self.topo}_{self.src}', 'NEservice'),
            'targetservice': self.re_cli.get_value(f'{self.topo}_{self.tgt}', 'NEservice'),
            'sourceveth':kvm_snow.get_id(),
            'targetveth':kvm_snow.get_id(),
            'parallel':self.info['count'],
            'VMsourcePort': self.info.get('VMsourcePort', ""),
            'VMtargetPort': self.info.get('VMtargetPort', ""),
        }
        return info
    



class LinkDeleteHandler(DataHandler):
    """
    链路删除时Redis数据维护代理类
    
    Attributes:
        topo (str): 拓扑名称
        name (str): 链路名称
        re_cli (UserDB): Redis数据库连接
        ori_info (dict): 原始拓扑平面(plane_topo_list或plane_subtopo_list)的条目信息
    
    """

    def __init__(self, topo, name, re_cli):
        """
        Args:
            topo (str):     拓扑名称
            name (str):     链路名称
            re_cli (UserDB):Redis数据库连接
        """
        self.name = name
        self.topo = topo
        self.re_cli = re_cli
        self.ori_info = {}

    def modify_db(self):
        """
        修改相关数据库的表项
        先进行实体的删除操作
        再进行数据库的修改
        """
        # 删除表项的相关操作
        table = f"{self.topo}_{self.name}"
        info = self.re_cli.get_all_values(table)
        # 删除存在的和这条链路关联的vxlan的表
        vxlans = info.get('vxlan')
        if vxlans:
            self._del_vxlan_info(vxlans)
        # 删除两端节点表中关于该链路的信息
        src, tgt = info['sourceNE'], info['targetNE']
        self._del_link_info_in_ne(src, tgt)
        # 删除plane_topo_list和plane_subtopo_list中的信息
        self._del_plane_topo_list()
        self._del_plane_subtopo_list(src, tgt)
        self.re_cli.del_all_values(table)

    def _del_vxlan_info(self, vxlans):
        """
        删除vxlan表的信息
        """
        for vxlan in vxlans:
            self.re_cli.del_all_values(f"{self.topo}_{vxlan}")

    def _del_link_info_in_ne(self, src, tgt):
        """
        删除与链路相连节点的中的键
        """
        self.re_cli.del_value(f'{self.topo}_{src}', f'link_{self.name}')
        self.re_cli.del_value(f'{self.topo}_{tgt}', f'link_{self.name}')
        # kvm还需要额外修改NEvmconfig
        src_service = self.re_cli.get_value(f"{self.topo}_{src}", "NEservice")
        src_vmport = self.re_cli.get_value(f'{self.topo}_{self.name}', 'VMsourcePort')
        tgt_service = self.re_cli.get_value(f"{self.topo}_{tgt}", "NEservice")
        tgt_vmport = self.re_cli.get_value(f'{self.topo}_{self.name}', 'VMtargetPort')
        print(src_service, tgt_service)
        if src_service == 'kvm':
            config = self.re_cli.get_value(f"{self.topo}_{src}", "NEvmconfig")
            # 设置端口占用
            config['check_port'][src_vmport] = 0
            self.re_cli.set_value(f'{self.topo}_{src}', "NEvmconfig", config)
        if tgt_service == 'kvm':
            config = self.re_cli.get_value(f"{self.topo}_{tgt}", "NEvmconfig")
            # 设置端口占用
            config['check_port'][tgt_vmport] = 0
            self.re_cli.set_value(f'{self.topo}_{tgt}', "NEvmconfig", config) 

    def _del_plane_topo_list(self):
        """
        删除plane_topo_list中的条目
        """
        plane_table = 'plane_topo_list'
        info = self.re_cli.get_value(plane_table, self.topo)
        self.ori_info[plane_table] = info
        new_info = copy.deepcopy(info)
        ne_lst = new_info.get('links')
        ne_lst.remove(self.name)
        self.re_cli.set_value(plane_table, self.topo, new_info)

    def _del_plane_subtopo_list(self, src, tgt):
        """
        删除plane_subtopo_list 中的条目
        """
        plane_sub_table = 'plane_subtopo_list'
        plane_sub_info = self.ori_info.setdefault(plane_sub_table, {})
        src_loc = self.re_cli.get_value(f'{self.topo}_{src}', 'NEloc')
        tgt_loc = self.re_cli.get_value(f'{self.topo}_{tgt}', 'NEloc')
        # 若源宿在同一worker上
        if src_loc == tgt_loc:
            info = self.re_cli.get_value(plane_sub_table, src_loc)
            plane_sub_info[src_loc] = info
            new_info = copy.deepcopy(info)
            link_lst = new_info.get('links')
            link_lst.remove(self.name)
            self.re_cli.set_value(plane_sub_table, src_loc, new_info)
        else:
            # 源宿在不同的worker上， 需要修改plane_subtopo_list中
            # 两个subtopo 中的vxlanlink
            for loc in [src_loc, tgt_loc]:
                info = self.re_cli.get_value(plane_sub_table, loc)
                plane_sub_info[loc] = info
                new_info = copy.deepcopy(info)
                vxlink_lst = new_info.get('vxlanlinks')
                try:
                    vxlink_lst.remove(f'link_{self.name}_vxlan1')
                except ValueError:
                    vxlink_lst.remove(f'link_{self.name}_vxlan2')
                self.re_cli.set_value(plane_sub_table, loc, new_info)

    def rollback_db(self):
        pass


# TODO (vessalius) 修改节点的时候
#  interface里的信息不能简单的update可能被覆盖为空了, 有些信息是后来才加入的
class NeModifyHandler(DataHandler):
    """
    节点信息修改时, Redis数据信息维护类
    
    Attributes:
        topo (str): 拓扑名称
        name (str): 节点名称
        info (dict): 更新的信息
        ne_type (str): 节点类型
        re_cli (UserDB):Redis数据库连接
        table (str): 节点redis表名
        ori_info (dict): 节点的原始数据信息
    """

    def __init__(self, topo: str, name: str, info: dict, re_cli):
        """
        Args:
            topo (str):     拓扑名称
            name (str):     节点名称
            info (dict):    更新的信息
            re_cli (UserDB):Redis数据库连接
        """
        self.topo = topo
        self.name = name
        self.info = info
        self.ne_type = info['type']
        self.re_cli = re_cli
        self.table = f'{topo}_{name}'
        # 这里直接将属性update到实例属性
        self.ori_info = re_cli.get_all_values(self.table)

    def modify_db(self):
        """
        Args:
            None

        Returns:
            conf (dict): {'ip': worker_ip, 'changed': conf}
        """
        property_checker = get_ne_property_checker(self.ne_type, self.ori_info, self.info)
        changed = property_checker.get_diff()
        FLASK_LOGGER.debug(f'changed is {changed}...')
        # {"coordinate": {'NEx', 'NEy'}, "ne_config": {"interface": "", "NEconfig": ""}
        # 如果不需要 向worker发送请求， 就返回None, 如果需要， 就返回worker ip
        xy = changed.get('coordinate', None)
        if xy:
            # 直接更新原数据中的信息， 即 NEx, 或者  NEy 或者都有
            for k, v in xy.items():
                self.re_cli.set_value(self.table, k, v)
        conf = changed.get('ne_config', None)
        if not conf:
            return
        self._set_changed_ne_conf(conf)
        worker_ip = self.re_cli.get_worker_ip_by_ne_name(self.topo, self.name)
        # conf 中包含了接口的变化信息和ne_config中的变化信息
        return {'ip': worker_ip, 'changed': conf}

    def _set_changed_ne_conf(self, conf):
        """
        更新修改过的键值
        Args:
            conf (dict): 修改过的ne的键值

        Returns:
            None
        """
        intf = conf.get('interface', None)
        if intf:
            self._set_interface_info(intf)
        FLASK_LOGGER.debug(conf)
        FLASK_LOGGER.debug(f'ori_keys is {self.ori_info.keys()}')
        # 判断修改的键是在外面还是在NEconfig里面
        info_changed = self.ori_info.keys() & conf.keys()
        FLASK_LOGGER.debug(f'info_changed is {info_changed}')
        ne_config = self.ori_info['NEconfig']
        FLASK_LOGGER.debug(f"ne_config_config key is  {ne_config['config'].keys()}")
        ne_config_changed = ne_config['config'].keys() & conf.keys()
        FLASK_LOGGER.debug(f'ne_config_changed is {ne_config_changed}')
        # 这里目前主要匹配出来的就是NEgateway
        for k in info_changed:
            self.re_cli.set_value(self.table, k, conf[k])
        # 这里匹配出来的和NEconfig中的键保持一致
        for k in ne_config_changed:
            ne_config['config'].update({k: conf[k]})
        self.re_cli.set_value(self.table, 'NEconfig', ne_config)

    def _set_interface_info(self, intf: dict):
        """
        更新节点接口相关的信息
        Args:
            intf (dict): 节点接口信息
        """
        # "link_l1": {"ip", "mask"}
        FLASK_LOGGER.debug(f'current intf info is {intf}...')
        NEinterface_info = self.ori_info.get('NEinterface', [])
        new_NEinterface_info = {}
        for link_k, link_v in intf.items():
            # 这里update是因为link信息中还有其他的网卡、mac等信息，
            # 这里只对修改的IP或者掩码的信息做更新
            new_ne_table_link = self.ori_info[link_k]
            FLASK_LOGGER.debug(f'ne_table_link is {new_ne_table_link}')
            new_ne_table_link.update(link_v)
            FLASK_LOGGER.debug(f'new_ne_table_link is {new_ne_table_link}')
            self.re_cli.set_value(self.table, link_k, new_ne_table_link)
            new_NEinterface_info.update({new_ne_table_link['name']: new_ne_table_link})
            # 还需要去修改链路表中的信息  更新 IP地址和掩码
            link_table = f'{self.topo}_{link_k[5:]}'
            # 找到该节点对应的source 或者 target  然后进行链路信息的修改
            link_info = self.re_cli.get_all_values(link_table)
            prefix = 'source' if link_info['sourceNE'] == self.name else 'target'
            cidr = netmask2cidr(link_v['ip'], link_v['mask'])
            FLASK_LOGGER.debug(f'cidr is {cidr}')
            self.re_cli.set_value(link_table, f'{prefix}IP', cidr)
        #(lzl这里还需要修改NEinterface)
        for i in range(len(NEinterface_info)):
            if NEinterface_info[i]['name'] in new_NEinterface_info:
                need_attr = ['ip', 'mask', 'name']
                for attr in need_attr:
                    if attr == 'mask':
                        NEinterface_info[i]['netmask'] = new_NEinterface_info[NEinterface_info[i]['name']][attr]
                    else:    
                        NEinterface_info[i][attr] = new_NEinterface_info[NEinterface_info[i]['name']][attr]
                new_NEinterface_info.pop(NEinterface_info[i]['name'])
        for k, v in new_NEinterface_info.items():
            need_attr = ['ip', 'mask', 'name']
            intf_info = {}
            for attr in need_attr:
                if attr == 'mask':
                    intf_info['netmask'] = v[attr]
                else:
                    intf_info[attr] = v[attr]
            NEinterface_info.append(intf_info)
        self.re_cli.set_value(self.table, 'NEinterface', NEinterface_info)

    def rollback_db(self):
        pass


# 属性检查类的工厂函数
def get_ne_property_checker(ne_type, ori_info, income_info):
    """
    Args:
        ne_type (str):        节点类型
        ori_info (dict):      原始信息
        income_info (dict):   修改过的信息
    
    Returns:
        各节点类型的属性检查类
    """
    if ne_type == 'host':
        return HostPropertyChecker(ori_info, income_info)
    elif ne_type == 'switch':
        return OvsPropertyChecker(ori_info, income_info)
    elif ne_type == 'router':
        return RouterPropertyChecker(ori_info, income_info)
    else:
        return NEPropertyChecker(ori_info, income_info)


class NEPropertyChecker(object):
    """
    默认的属性检查代理类， 只检查坐标变化
    
    Attributes:
        ori    (dict): 原信息
        income (dict): 前端更新的信息
    """

    def __init__(self, ori, income):
        """
        Args:
            ori    (dict): 原信息
            income (dict): 前端更新的信息
        """
        self.ori = ori
        self.income = income

    def get_diff(self):
        """
        返回更新后的差异信息
        Args:
            None

        Returns:
            coordinate_diff (dict): 返回节点坐标变化
        """
        differ = {}
        ne_x, ne_y = self.income['x'], self.income['y']
        if ne_x != self.ori['NEx']:
            differ['NEx'] = ne_x
        if ne_y != self.ori['NEy']:
            differ['NEy'] = ne_y
        return {"coordinate": differ}


class InterfaceChecker(object):
    """
    检查存在接口节点的接口信息是否改变，包括IP地址、网络掩码
    
    Attributes:
        income_intf (dict): 传入的接口信息
        ori_info (dict): 原始的信息
    """

    def __init__(self, ori_info, income_intf):
        # 对传入的信息进行修改， 使其能够通过name进行索引
        # 将 {'name': '', 'ip': '', 'netmask': ''} 改成
        # {'name': {'name': '', 'ip': '', 'netmask': ''}}
        FLASK_LOGGER.debug(income_intf)
        self.income_intf = {intf['name']: intf for intf in income_intf}
        self.ori_info = ori_info

    def get_interfaces_diff(self):
        """
        检节点链路配置是否改变
        Args:
            None

        Returns:
            differ (dict): 返回链路配置的差异
        """
        # 检查链路配置
        differ = {'interface': {}}
        for key in self.ori_info.keys():
            if key.startswith('link_'):
                # 也要保证写入数据的时候好写入， 键的索引要和表里的键保持一致
                result = self._check_interface(self.ori_info[key])
                if result:
                    # 若有改变，则更新相对应的值， 没改变， 不会有键
                    # {'link_l1': {'ip': "", 'mask': ""}}
                    differ['interface'].update({key: result})
        return differ if differ['interface'] else {}

    def _check_interface(self, ori_intf):
        """
        Args:
            ori_intf (dict): 原始信息

        Returns:
            differ (dict): 链路配置的改变量
        """
        # 数据库里的接口信息: ori_info = {'ip': '', 'mask': '', 'nic': '', 'name': '', 'mac': ''}
        # 传入的信息包括 income_intf = {'name': '', 'ip': '', 'netmask': ''}
        # 这里应该只返回该变量
        # 这里的name是 r1r2
        name = ori_intf['name']
        differ ={}
        # host 只检查ip地址的变化和mask的变化
        ip, mask = self.income_intf[name]['ip'], self.income_intf[name]['netmask']
        if ip != ori_intf['ip'] or mask != ori_intf['mask']:
            differ['ip'], differ['mask'] = ip, mask
        return differ if differ else {}


class HostPropertyChecker(NEPropertyChecker):
    """
    检查host类型节点的属性改变
    对于host节点 能修改的只有接口信息， 只能进行IP地址、网关的设置
    对于 host 容器， 需要检查的是NEx, NEy, interface: [ip里新增加了mac地址], gateway
    
    Attributes:
        _differ (dict): 信息差别
        interface_checker (InterfaceChecker): 接口信息检查类
    """

    def __init__(self, ori: dict, income: dict):
        """
        Args:
            ori (dict):    原始信息
            income (dict): 输入的更新信息
        """
        self._differ = {}
        self.interface_checker = InterfaceChecker(ori, income['interfaces'])
        super().__init__(ori, income)

    def get_diff(self):
        """
        Args:
            None

        Returns:
            differ (dict): host类型节点的差异信息
        """
        diff = {}
        diff.update(super().get_diff())
        self._differ.update(self.interface_checker.get_interfaces_diff())
        self._get_gateway_diff()
        diff['ne_config'] = self._differ
        return diff

    def _get_gateway_diff(self):
        """
        检查gateway是否修改
        """
        gateway = self.income['gateway']
        if gateway != self.ori['NEgateway']:
            self._differ['NEgateway'] = gateway


class OvsPropertyChecker(NEPropertyChecker):
    """
    检查OvS类型节点的属性改变
    对于 switch 节点， 只能设置 stp 和 控制器
    对于 OVS容器， 需要检查的是：NEx, NEy, stp, controllers
    
    Attributes:
        _differ (dict): 信息差别
    
    """
    def __init__(self, ori: dict, income: dict):
        """
        Args:
            ori (dict): 原始信息
            income (dict): 前端输入的修改信息
        """
        self._differ = {}
        super().__init__(ori, income)

    def get_diff(self):
        """
        检查OvS类型节点的修改信息

        Args:
            None

        Returns:
            diff (dict): 检查OvS容器修改的属性

        """
        # 检查x, y 的变化
        diff = {}
        diff.update(super().get_diff())
        self._check_stp()
        self._check_controllers()
        diff['ne_config'] = self._differ
        return diff

    def _check_stp(self):
        """
        检查stp的属性
        """
        stp = self.income['config']['stp']
        if stp is not self.ori['NEconfig']['config']['stp']:
            self._differ['stp'] = stp

    def _check_controllers(self):
        # 这里有些麻烦了，ovs 添加命令的时候controllers是一起加的
        # 要删， 也是只有一起删掉的命令, 只要不一样了， 就得删除之前全部
        # 再创建最新的全部，所以增加和减少根本是无所谓的， 只要不一样，就有问题
        # 使用集合运算, 列表的相等的条件是每个索引对应的元素相等
        ctrs = self.income['config']['controllers']
        ori_ctrs = self.ori['NEconfig']['config']['controllers']
        if set(ctrs) != set(ori_ctrs):
            self._differ['controllers'] = ctrs


class RouterPropertyChecker(NEPropertyChecker):
    """
    检查router类型节点的属性改变
    对于router节点， 能够控制的有 接口信息， 协议的控制
    对于 router容器：NEx, NEy, interface:[ip, netmask]   NEconfig
    
        
    Attributes:
        _differ (dict): 信息差别
        interface_checker (InterfaceChecker): 接口信息检查类
    """

    def __init__(self, ori: dict, income: dict):
        """
        Args:
            ori (dict):    原始信息
            income (dict): 修改后的信息
        """
        self._differ = {}
        self.interface_checker = InterfaceChecker(ori, income['interfaces'])
        super().__init__(ori, income)

    def get_diff(self):
        """
        检查x, y 的变化
        Args:
            None

        Returns:
            diff (dict): 路由器节点的信息变化
        """
        diff = {}
        diff.update(super().get_diff())
        self._differ.update(self.interface_checker.get_interfaces_diff())
        self._differ.update(self._check_protocol())
        diff['ne_config'] = self._differ
        return diff

    def _check_protocol(self):
        """
        检查路由协议配置变化
        Args:
            None

        Returns:
            diff (dict): 协议配置的变化信息
        """
        diff = {}
        diff.update(self._check_rip())
        diff.update(self._check_ospf())
        diff.update(self._check_bgp())
        return diff

    def _check_rip(self):
        """
        检查rip路由协议配置变化
        Args:
            None

        Returns:
            diff (dict): 协议配置的变化信息
        """
        ori = self.ori['NEconfig']['config'].get('rip', None)
        income = self.income['config'].get('rip', None)
        FLASK_LOGGER.debug(f'ori rip info {ori}')
        FLASK_LOGGER.debug(f'income rip info {income}')
        FLASK_LOGGER.debug(f'ori == income is : {ori == income}')
        return {'rip': income} if ori != income else {}

    def _check_ospf(self):
        """
        检查OSPF路由协议配置变化
        Args:
            None

        Returns:
            diff (dict): 协议配置的变化信息
        """
        # 只记录改变量 ？？？？
        ori = self.ori['NEconfig']['config'].get('ospf', None)
        income = self.income['config'].get('ospf', None)
        return {'ospf': income} if ori != income else {}

    def _check_bgp(self):
        """
        检查BGP路由协议配置变化
        Args:
            None

        Returns:
            diff (dict): 协议配置的变化信息
        """
        ori = self.ori['NEconfig']['config'].get('bgp', None)
        income = self.income['config'].get('bgp', None)
        return {'bgp': income} if ori != income else {}
