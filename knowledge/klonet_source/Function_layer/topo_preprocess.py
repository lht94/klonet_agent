import copy
import random
import uuid
from pprint import pprint

from ..tools.generate_ne_id import SnowFlake, SnowFlakekvm

import copy
from ..Implement_layer.LinkManager import link_operate
from ..tools.log_tools import * 

class interface:
    """
    接口类
    """
    def __init__(self, ifa):
        """
        通过json中的网卡信息创建对象
        Args:
            ifa: (dict): 接口信息

        """
        self.nic = ''
        self.__dict__.update(ifa)

    def get_ifa2ip(self):
        """
        得到接口的属性
        Args:
            None

        Returns:
            (dict): 接口的属性字典
        """
        return {'ip': self.ip, 'mask': self.netmask, 'nic': self.nic, 'name': self.name}

    def ip_netmask2cidrip(self):
        """
        将IP掩码转换为 CIDR形式字符串
        Returns:
            ip_cidr (str): CIDR形式字符串
        """
        if self.ip == '' or self.netmask == '':
            return ''
        else:
            # 计算二进制字符串中 '1' 的个数
            def count_bit(bin_str): return len(
                [i for i in bin_str if i == '1'])
            # 分割字符串格式的子网掩码为四段列表
            mask_splited = self.netmask.split('.')
            # 转换各段子网掩码为二进制, 计算十进制
            mask_count = [count_bit(bin(int(i))) for i in mask_splited]
            ip_cidr = self.ip + '/' + str(sum(mask_count))
            return ip_cidr


class Ne_base:
    """
    节点信息类， 将redis数据库节点表的key转化为json中节点信息的key
    """
    ne_property = {'NEimage': 'image_name', 'NEtype': 'type',
                   'NEsubtype': 'subtype', 'NEx': 'x', 'NEy': 'y','NElinestyle':'linestyle',
                   'NEresource':'resource_limit', 'NEservice': 'service', 'NEvmconfig': 'vm_config', 
                   'NEinterface': 'interfaces', 'NEperformance': 'performance'}

    def __init__(self, ne: dict, topo: str):
        """
        使用json中的节点属性进行初始化
        Args:
            ne  (dict): 节点信息
            topo (str): 拓扑名
        """
        self.__dict__.update(ne)
        # <toponame_nename> :{'NEid':'', 'NEloc':'', ...}
        self.table = {}
        """
            self.table中的内容对应redis中的表项
            <toponame>_<nename> : {
                                        'NEid':
                                        'NEimage':
                                        'NEservice':
                                        'NEvmconfig':
                                        'NEtype':
                                        'NEloc':
                                        'NEx':
                                        'NEy':
                                        'NEconfig':
                                        'NEgateway':
                                        'NEinterface':
                                        'NEnic':
                                        'link_<linkname>':
                                    }
        """
        self.topo = topo
        self.table_name = f"{self.topo}_{self.name}"
        self.table_nodetointerface_name = f"{self.topo}_{self.name}_nodetointerface"
        self.table_interfacetoname_name = f"{self.topo}_{self.name}_interfacetoname"
        self.table_nodetoname_name = f"{self.topo}_{self.name}_nodetoname"
        

    def __call__(self, ne_id: str, nicid: list):
        """
        创建数据库节点表，所有节点都会包含的信息。
        Args:
            ne_id (str):
        """
        table_info = self.table.setdefault(self.table_name, {})
        for k, v in Ne_base.ne_property.items():
            table_info.setdefault(k, getattr(self, v, ''))
        table_info.setdefault('NEloc', f"{self.topo}_sub1")
        table_info.setdefault('NEid', ne_id)
        table_info.setdefault('NEnic', nicid)
        table_info.setdefault('NEnet', 0)
        # self.update_nenic()
        self.update_neconfig()
        service = getattr(self, 'service', 'docker')
        if service == 'kvm':
            self.get_nodetointerface(table_info['NEnic'])
            self.get_interfacetoname(table_info['NEnic'], getattr(self, 'portname'))
        else:
            pass
        
    def get_ne_id(self):
        """
        获取节点id
        :return:
        """
        ne_id = self.table[self.table_name].get('NEid', '')
        return ne_id

    def get_ne_loc(self):
        """
        获取节点所属拓扑名
        :return:
        """
        ne_loc = self.table[self.table_name].get('NEloc', '')
        return ne_loc
    
    def get_ne_switch_ip(self):
        """
        硬件设备的特殊处理
        获取设备所连接的switch管控ip
        :return:
        """
        switch_config = self.table[self.table_name].get('NEconfig', '')
        switch_ip = switch_config['config']['switch']
        return switch_ip

    def get_ne_type(self):
        ne_type = self.table[self.table_name].get('NEtype', '')
        return ne_type

    def update_neloc(self, loc):
        """
        更新节点所属拓扑（在哪个子拓扑中）
        :param loc:
        :return:
        """
        table_info = self.table.get(self.table_name)
        table_info['NEloc'] = loc

    def update_negateway(self, gateway):
        """
        更新节点网关
        :param gateway:
        :return:
        """
        table_info = self.table.get(self.table_name)
        table_info['NEgateway'] = gateway

    def update_neconfig(self, config=None, extra=None):
        """
        更新节点表中的配置信息
        :param config:
        :return:
        """
        table_info = self.table.get(self.table_name)
        if config:
            config_info = table_info.get('NEconfig')
            config_info['config'].update(config)
        else:
            config = {'config': getattr(self, 'config', {})}
            table_info.setdefault('NEconfig', config)
        if extra:
            other_info = table_info.get('NEconfig')
            other_info.update(extra)

    def update_nelinks(self, links):
        """
        更新节点表中相关联的链路信息
        :param links:
        :return:
        """
        table_info = self.table.get(self.table_name)
        table_info.update(links)

    def get_nodetointerface(self, interface):
        table_info = self.table.setdefault(self.table_nodetointerface_name, {})
        vm_config = getattr(self, 'vm_config')
        if vm_config['type'] == 'host':
            for i in range(len(interface)):
                table_info.setdefault('eth' + str(i+1), interface[i])
        elif vm_config['type'] == 'router':
            for i in range(len(interface)):
                table_info.setdefault("Ethernet1/0/" + str(i+1), interface[i])
        else:
            pass

    def get_interfacetoname(self, interface, name):
        table_info = self.table.setdefault(self.table_interfacetoname_name, {})
        for i in range(len(interface)):
            table_info.setdefault(interface[i], name[i])


    def get_nodetoname(self, interface, name):
        table_info = self.table.setdefault(self.table_nodetoname_name, {})
        for i in range(len(name)):
            table_info.setdefault(interface[i], name[i])
    

class Ne_host(Ne_base):
    def __call__(self, ne_id, nicid):
        super().__call__(ne_id, nicid)
        self.update_negateway(getattr(self, 'gateway', ''))
        # self.update_nenic(getattr(self, 'vm_config'), getattr(self, 'config'), getattr(self, 'service'))

class Ne_switch(Ne_base):
    pass


class Ne_router(Ne_base):
    def __call__(self, ne_id, nicid):
        super().__call__(ne_id, nicid)
        self.update_negateway(getattr(self, 'gateway', ''))

class Ne_dpdk(Ne_base):
    dpdk_image = 'dpdk/l2fwd'

    def __init__(self, ne, topo, tap_num):
        super().__init__(ne, topo)
        self.tap_num = tap_num
        if hasattr(self, 'image_name') is False:
            self.image_name = Ne_dpdk.dpdk_image

    def __call__(self, ne_id, nicid):
        super().__call__(ne_id, nicid)
        table_info = self.table.setdefault(self.table_name, {})
        dpdk_nums = (str(link_operate.generate_uuid_len_10()), str(link_operate.generate_uuid_len_10()), self.tap_num)
        table_info.setdefault('dpdk_nums', dpdk_nums)
        '''
        问题在于，这个地方是对于json文件的解析，拓扑的预处理，但我要存储dpdk容器和网桥的关系，我在这得不到网桥的name
        网桥的name在NEManager中定义的，怎么在数据库的table中添加一项新的key
        '''

class Ne_controller(Ne_base):
    controller_image = 'controller/floodlight'

    def __init__(self, ne, topo):
        super().__init__(ne, topo)
        if hasattr(self, 'image_name') is False:
            self.image_name = Ne_controller.controller_image

    def __call__(self, ne_id, ctl2sw, nic_id):
        super().__call__(ne_id, nic_id)
        sw_info = {'switches': ctl2sw}
        self.update_neconfig(extra=sw_info)


class Link_base:
    # redis数据库链路表的key -> json中链路信息的key
    link_property = {'sourceNE': 'source', 'targetNE': 'target',
                     'sourceType': 'sourceType', 'targetType': 'targetType', 
                     'sourceIP': 'sourceIP', 'targetIP': 'targetIP',
                     'sourcePort': 'sourcePort', 'targetPort': 'targetPort',
                     'VMsourcePort': 'VMsourcePort', 'VMtargetPort': 'VMtargetPort', 'parallel': 'count'}

    def __init__(self, link, topo):
        self.__dict__.update(link)
        self.table = {}
        """
            self.table中的内容对应redis中的表项
            <toponame>_<linkname> : {
                                        'sourceNE':
                                        'targetNE':
                                        'sourceservice':
                                        'targetservice':
                                        'sourceID':
                                        'targetID':
                                        'sourceIP':
                                        'targetIP':
                                        'sourcePort':
                                        'targetPort':
                                        'sourceType':
                                        'targetType':
                                        'VMsourcePort'
                                        'VMtargetPort'
                                        'sourceveth':
                                        'targetveth':
                                    }
        """
        self.topo = topo
        self.table_name = f"{self.topo}_{self.name}"

    def __call__(self, *args, **kwargs):
        """

        :param args:
        :param kwargs:
            {'sourceID':'', 'targetID':'', 'sourceIP':'', 'targetIP':''}
        :return:
        """
        table_info = self.table.setdefault(self.table_name, {})
        for k, v in Link_base.link_property.items():
            table_info.setdefault(k, getattr(self, v, ''))
        table_info.setdefault('sourcePort', '')
        table_info.setdefault('targetPort', '')
        table_info.update(kwargs)

    def get_link_ne_ip(self, src_ob, dst_ob, link):
        src_link = {}
        dst_link = {}
        link_ip = {'sourceIP': '', 'targetIP': ''}
        src_interfaces = getattr(src_ob, 'interfaces', [])
        dst_interfaces = getattr(dst_ob, 'interfaces', [])
        src_dst = src_ob.name + dst_ob.name + "_" + str(link['count'])
        dst_src = dst_ob.name + src_ob.name + "_" + str(link['count'])
        src_link_default = {f'link_{self.name}': {
            'ip': '', 'mask': '', 'nic': '', 'name': src_dst}}
        dst_link_default = {f'link_{self.name}': {
            'ip': '', 'mask': '', 'nic': '', 'name': dst_src}}
        # ovs的json中没有interfaces，单独处理
        if not src_interfaces:
            src_link.update(src_link_default)
        else:
            for ifa in src_interfaces:
                if ifa['name'] == src_dst or ifa['name'] == dst_src:
                    ifa_ob = interface(ifa)
                    src_link.setdefault(
                        f'link_{self.name}', ifa_ob.get_ifa2ip())
                    link_ip['sourceIP'] = ifa_ob.ip_netmask2cidrip()
        # ovs的json中没有interfaces，单独处理
        if not dst_interfaces:
            dst_link.update(dst_link_default)
        else:
            for ifa in dst_interfaces:
                if ifa['name'] == src_dst or ifa['name'] == dst_src:
                    ifa_ob = interface(ifa)
                    dst_link.setdefault(
                        f'link_{self.name}', ifa_ob.get_ifa2ip())
                    link_ip['targetIP'] = ifa_ob.ip_netmask2cidrip()
        return link_ip, src_link, dst_link
    
    def get_link_service(self, src_ob, dst_ob):
        link_service = {'sourceservice': '', 'targetservice': ''}
        src_service = src_ob.service
        dst_service = dst_ob.service
        link_service['sourceservice'] = src_service
        link_service["targetservice"] = dst_service
        return link_service

    def get_link_table(self):
        return self.table[self.table_name]

    def update_vxlan_info(self, *args):
        info = {'vxlan': []}
        info['vxlan'].extend(*args)
        self.table[self.table_name].update(info)


class Topo_process:
    snow = SnowFlake()
    snowkvm = SnowFlakekvm()
    # json中节点分类索引 -> 节点对应的处理方法
    ne_type = {'hosts': '_hosts_handle', 'switches': '_switches_handle', 'routers': '_routers_handle',
               'controllers': '_controllers_handle', 'dpdks': '_dpdks_handle'}
    # 节点类型 -> json中节点分类索引
    type2nes = {'host': 'hosts', 'switch': 'switches', 'router': 'routers',
                'controller': 'controllers', 'dpdk': 'dpdks'}
    # 表名是变量的哈希表
    # {<table_name>:{ <key> : <value> }, ... }
    var_table = {'ne_table_dict': {},
                 'link_table_dict': {}, 'vxlanlink_table_dict': {}}
    # 表名是常量的哈希表
    # {'plane_topo_list':{ <key> : <value> } }
    const_table = {'plane_topo_list': {}, 'topo_service': {}, 'topo2subtopo': {},
                   'subtopo2worker': {}, 'plane_subtopo_list': {},
                   'subtopo_service': {}, 'shared_topo_list': {} }

    def __init__(self, topo_json: dict, hardware, worker_list, scheme={}, option=0):
        self.user = topo_json.get('user', '')
        self.topo = topo_json.get('topo', '')
        # 共享项目相关：wtx
        self.project_type = topo_json.get('project_type','private')
        self.invited_user_group = topo_json.get('invited_user_group',[])
        if 'net1' in topo_json.get('networks', {}):
            self.net = topo_json.get('networks')['net1']
        else:
            self.net = topo_json.get('networks')
        self.scheme = scheme
        self.hardware = hardware
        self.workers = worker_list
        self.option = option
        self.__dict__.update(copy.deepcopy(Topo_process.var_table))
        self.__dict__.update(copy.deepcopy(Topo_process.const_table))
        self.ne2ob = {}  # ne_name:ne_object
        self.link2ob = {}  # link_name:link_object
        self.ctr2switch_dict = {}  # 控制器与交换机的映射

    def __call__(self):
        for k, v in Topo_process.ne_type.items():
            func = getattr(self, v)
            if func == '_controllers_handle':
                continue
            func(k)
        self._controllers_handle('controllers')
        self._links_handle()
        self._get_plane_topo_list()
        self._get_shared_topo_list()
        self._get_plane_topo_service()
        if self.scheme:
            self._scheme_split()
        else:
            self._split()
        split_result = {}
        for k in Topo_process.var_table:
            split_result[k] = getattr(self, k)
        for k in Topo_process.const_table:
            split_result[k] = getattr(self, k)
        return split_result

    def _hosts_handle(self, ne_type_index):
        hosts = self.net.get(ne_type_index, {})
        for v in hosts.values():
            nicid = []
            ne_ob = Ne_host(v, self.topo)
            if v['service'] == 'kvm':
                for _ in range(v["vm_config"]["port_num"]):
                    nicid.append(Topo_process.snowkvm.get_id())
            else:
                pass
            if v['service'] == 'hardware':
                ne_id = v["config"]["id"]
            else:
                ne_id = Topo_process.snow.get_id()
            ne_ob(ne_id, nicid)
            # 在汇总节点表中添加节点表信息
            self.ne_table_dict.update(ne_ob.table)
            self.ne2ob[ne_ob.name] = ne_ob

    def _switches_handle(self, ne_type_index):
        switches = self.net.get(ne_type_index, {})
        for v in switches.values():
            nicid = []
            ne_ob = Ne_switch(v, self.topo)
            if v['service'] == 'kvm':
                for _ in range(v["vm_config"]["port_num"]):
                    nicid.append(Topo_process.snowkvm.get_id())
            else:
                pass
            if v['service'] == 'hardware':
                ne_id = v["config"]["id"]
            else:
                ne_id = Topo_process.snow.get_id()
            ne_ob(ne_id, nicid)
            self.ne_table_dict.update(ne_ob.table)
            self.ne2ob[ne_ob.name] = ne_ob
            for ctl in getattr(ne_ob, 'config', {}).get('controllers', []):
                sws = self.ctr2switch_dict.setdefault(ctl, [])
                sws.append(ne_ob.name)

    def _controllers_handle(self, ne_type_index):
        controllers = self.net.get(ne_type_index, {})
        for v in controllers.values():
            nicid = []
            ne_ob = Ne_controller(v, self.topo)
            # wudx
            # 目前平台逻辑下这样并不合理，因为控制器并不需要实际的链路连接其他节点，不存在nicid一说
            # 仅仅是出于完整性考虑，也可能在未来某一天会派上用场
            if v['service'] == 'kvm':
                for _ in range(v["vm_config"]["port_num"]):
                    nicid.append(Topo_process.snowkvm.get_id())
            else:
                pass
            ne_id = Topo_process.snow.get_id()
            ctl2sw = self.ctr2switch_dict.get(ne_ob.name, [])
            ne_ob(ne_id, ctl2sw, nicid)
            self.ne_table_dict.update(ne_ob.table)
            self.ne2ob[ne_ob.name] = ne_ob

    def _routers_handle(self, ne_type_index):
        routers = self.net.get(ne_type_index, {})
        for v in routers.values():
            nicid = []
            ne_ob = Ne_router(v, self.topo)
            if v['service'] == 'kvm':
                for _ in range(v["vm_config"]["port_num"]):
                    nicid.append(Topo_process.snowkvm.get_id())
            else:
                pass
            if v['service'] == 'hardware':
                ne_id = v["config"]["id"]
            else:
                ne_id = Topo_process.snow.get_id()
            ne_ob(ne_id, nicid)
            self.ne_table_dict.update(ne_ob.table)
            self.ne2ob[ne_ob.name] = ne_ob
    
    def _dpdks_handle(self, ne_type_index):
        dpdks = self.net.get(ne_type_index, {})
        nicid = []
        tap_num = 0
        for v in dpdks.values():
            ne_ob = Ne_dpdk(v, self.topo, tap_num)
            ne_id = Topo_process.snow.get_id()
            ne_ob(ne_id, nicid)
            self.ne_table_dict.update(ne_ob.table)
            self.ne2ob[ne_ob.name] = ne_ob  #构建了一个数组，dpdk
            tap_num = tap_num + 2

    def _links_handle(self):
        links = self.net.get('links', {})
        for v in links.values():
            link_info = {}
            link_ob = Link_base(v, self.topo)
            ne_src_ob = self.ne2ob[link_ob.source]
            ne_dst_ob = self.ne2ob[link_ob.target]
            link_info['sourceID'] = ne_src_ob.get_ne_id()
            link_info['targetID'] = ne_dst_ob.get_ne_id()
            link_info['sourceveth'] = Topo_process.snowkvm.get_id()
            link_info['targetveth'] = Topo_process.snowkvm.get_id()
            link_ip, src_link, dst_link = link_ob.get_link_ne_ip(
                ne_src_ob, ne_dst_ob,v)
            link_info.update(link_ip)
            link_service = link_ob.get_link_service(ne_src_ob, ne_dst_ob)
            link_info.update(link_service)
            # 添加节点表中的链路信息并在汇总节点表中更新
            ne_src_ob.update_nelinks(src_link)
            ne_dst_ob.update_nelinks(dst_link)
            self.ne_table_dict.update(ne_src_ob.table)
            self.ne_table_dict.update(ne_dst_ob.table)
            # 添加链路表中的源目的id，ip, service信息
            link_ob(**link_info)
            # 在汇总链路表中添加链路表信息
            self.link_table_dict.update(link_ob.table)
            self.link2ob[link_ob.name] = link_ob

    def _get_plane_topo_list(self):
        if self.project_type == 'private':
            plane_topo_list = {}
            # project_type 与 user_group 为支持共享项目的标识字段
            plane_topo = plane_topo_list.setdefault(
                self.topo, {'NEs': [], 'links': [], 
                            'project_type':self.project_type, 
                            'creator':self.user})
            for k in self.ne2ob:
                plane_topo['NEs'].append(k)
            for k in self.link2ob:
                plane_topo['links'].append(k)
            self.plane_topo_list.update(plane_topo_list)
            # print(self.plane_topo_list)
        
    def _get_shared_topo_list(self):
        """多人共享项目的存储格式
        
        以hash表在新表shared_topo_list中存储。以创建者+'_'+项目名为key是为了避免命名
        冲突的问题。
        
        """
        # 多人共享项目存储
        if self.project_type == 'public':
            shared_topo_list = {}
            shared_topo = shared_topo_list.setdefault(
                self.topo + '_' + self.user, {'NEs': [], 'links': [], 
                            'project_type':self.project_type, 
                            'invited_user_group':self.invited_user_group,
                            'checked_user_group':[],
                            'creator':self.user
                            })
            for k in self.ne2ob:
                shared_topo['NEs'].append(k)
            for k in self.link2ob:
                shared_topo['links'].append(k)
            self.shared_topo_list.update(shared_topo_list)

    def _get_plane_topo_service(self):
        topo_service = {}
        topo_service_info = topo_service.setdefault(self.topo, {})
        for k in Topo_process.ne_type:
            topo_service_info.setdefault(k, [])
        for ne_ob in self.ne2ob.values():
            temp = Topo_process.type2nes.get(ne_ob.get_ne_type(), '')
            topo_service_info[temp].append(ne_ob.name)
        self.topo_service.update(topo_service)

    def _split(self):
        if not self.option:
            print("self._no_split()")
            self._no_split()
        else:
            # 后期加入的worker_ip靠后，需要排序 
            # 考虑修改为服务器名与IP的映射？？
            # tail_worker_num = 3
            # 包括vemu4共四台高性能的分割序列
            # 序列对应vemu24、vemu23、vemu22、vemu4、。。。
            split_list = [1]
            # split_list = [4, 4, 4]
            self.workers = sorted(self.workers, 
                                  key=lambda x: int(x.split(".")[-1]),
                                  reverse=True)
            # self._custom_sort_worker(tail_worker_num)
            self._split_init()
            self._balance_split()
            # self._vemu4_more_split()
            # self._custom_more_split(split_list)
    
    def _custom_sort_worker(self, tail_worker_num):
        worker_list = []
        # 后三个为高性能
        for _ in range(tail_worker_num):
            worker_list.append(self.workers.pop())
        worker_list.extend(self.workers)
        self.workers = worker_list

    def _no_split(self):
        if not self.hardware:
            subtopo_name = f'{self.topo}_sub1'
            self.topo2subtopo = {self.topo: [subtopo_name]}
            # print("self.ne2ob:", self.ne2ob)
            self.subtopo2worker = {subtopo_name: self.workers[0]}
            # 没切分的时候这里需要在subtopo_list中加入vxlanlinks:[]
            plane_topo = copy.deepcopy(getattr(self, "plane_topo_list"))
            plane_topo[self.topo]['vxlanlinks'] = []
            # print(plane_topo)
            self.plane_subtopo_list = copy.deepcopy(plane_topo)
            self.plane_subtopo_list[subtopo_name] = self.plane_subtopo_list.pop(
                self.topo)
            self.subtopo_service = copy.deepcopy(self.topo_service)
            self.subtopo_service[subtopo_name] = self.subtopo_service.pop(self.topo)
        else:
            subtopo1 = f'{self.topo}_sub1'
            subtopo2 = f'{self.topo}_hardware'
            self.topo2subtopo = {self.topo: [subtopo1, subtopo2]}
            # print("self.ne2ob:", self.ne2ob)
            self.subtopo2worker = {subtopo1: self.workers[0], subtopo2: 'hardware'}
            subtopo_1 = {'NEs': [], 'links': [], 'vxlanlinks': []}
            subtopo_2 = {'NEs': [], 'links': [], 'vxlanlinks': []}
            subtopo_service_1 = {'switches': [], 'hosts': [],
                                'routers': [], 'controllers': [], 'dpdks': []}
            subtopo_service_2 = {'switches': [], 'hosts': [],
                                'routers': [], 'controllers': [], 'dpdks': []}
            self.plane_subtopo_list.setdefault(subtopo1, subtopo_1)
            self.plane_subtopo_list.setdefault(subtopo2, subtopo_2)
            self.subtopo_service.setdefault(subtopo1, subtopo_service_1)
            self.subtopo_service.setdefault(subtopo2, subtopo_service_2)
            ne_list = []
            ne_hardware = []
            for ne, ne_ob in self.ne2ob.items():
                table_info = ne_ob.table.get(ne_ob.table_name)
                service = table_info['NEservice']
                if service == 'hardware':
                    ne_hardware.append(ne)
                else:
                    ne_list.append(ne)
            if len(ne_hardware) != 0:
                self._ne_divide(subtopo2, ne_hardware)
            self._ne_divide(subtopo1, ne_list)
            # 链路划分
            self._link_divide()
            # 服务划分
            self._service_divide()
            

    def _split_init(self):
        """
        建立子拓扑相应表项：
            topo2subtopo        拓扑->子拓扑列表
            plane_subtopo_list  子拓扑->子拓扑包含的节点及链路（包括vxlan链路）
            subtopo2worker      子拓扑->worker ip（子拓扑创建位置）
            subtopo_service     子拓扑->子拓扑包含的需要启服务的各类节点（实质上是对子拓扑的NEs的分类）
        :return:
        """
        subtopo_list = self.topo2subtopo.setdefault(self.topo, [])
        if not self.hardware:
            for i, worker in enumerate(self.workers, start=1):
                if worker == 'hardware':
                    subtopo_i = f'{self.topo}_hardware'
                    self.subtopo2worker.setdefault(subtopo_i, 'hardware')
                else:
                    subtopo_i = f'{self.topo}_sub{i}'
                    self.subtopo2worker.setdefault(subtopo_i, self.workers[i - 1])
                print(f"subtopo_i: {subtopo_i}")
                subtopo = {'NEs': [], 'links': [], 'vxlanlinks': []}
                subtopo_service = {'switches': [], 'hosts': [],
                                'routers': [], 'controllers': [], 'dpdks': []}
                self.plane_subtopo_list.setdefault(subtopo_i, subtopo)
                self.subtopo_service.setdefault(subtopo_i, subtopo_service)
                subtopo_list.append(subtopo_i)
        else:
            for i in range(1, len(self.workers) + 2):
                if i == len(self.workers)+1:
                    subtopo_i = f'{self.topo}_hardware'
                else:
                    subtopo_i = f'{self.topo}_sub{i}'
                print(f"subtopo_i: {subtopo_i}")
                subtopo = {'NEs': [], 'links': [], 'vxlanlinks': []}
                subtopo_service = {'switches': [], 'hosts': [],
                                'routers': [], 'controllers': [], 'dpdks': []}
                self.plane_subtopo_list.setdefault(subtopo_i, subtopo)
                if i == len(self.workers)+1:
                    self.subtopo2worker.setdefault(subtopo_i, 'hardware')
                else:
                    self.subtopo2worker.setdefault(subtopo_i, self.workers[i - 1])
                self.subtopo_service.setdefault(subtopo_i, subtopo_service)
                subtopo_list.append(subtopo_i)
    
    def _scheme_split(self):
        self._split_init()
        for i, worker_ip in enumerate(self.workers, start=1):
            if worker_ip == 'hardware':
                subtopo_i = f'{self.topo}_hardware'
            else:
                subtopo_i = f'{self.topo}_sub{i}'
            ne_list = self.scheme[worker_ip]['ne_list']
            self._ne_divide(subtopo_i, ne_list)
        self._link_divide()
        self._service_divide()

    def _balance_split(self):
        print("-"*10,"均分","-"*10)
        ne_list = []
        ne_hardware = []
        for ne, ne_ob in self.ne2ob.items():
            table_info = ne_ob.table.get(ne_ob.table_name)
            service = table_info['NEservice']
            if service == 'hardware':
                ne_hardware.append(ne)
            else:
                ne_list.append(ne)
        if len(ne_hardware) != 0:
            subtopo = f'{self.topo}_hardware'
            self._ne_divide(subtopo, ne_hardware)
        ne_nums = len(ne_list)
        worker_nums = len(self.workers)
        p = ne_nums // worker_nums
        # 节点划分
        for i in range(1, worker_nums + 1):
            subtopo_i = f'{self.topo}_sub{i}'
            if i == worker_nums:
                nes = ne_list[(worker_nums-1)*p:]
            else:
                nes = ne_list[p * (i - 1):p * i]
            self._ne_divide(subtopo_i, nes)
        # 链路划分
        self._link_divide()
        # 服务划分
        self._service_divide()

    def _vemu4_more_split(self):
        ne_list = list(self.ne2ob.keys())
        ne_nums = len(ne_list)
        worker_nums = len(self.workers)
        print(self.workers)
        vemu4_ne_num = 82
        if "10.1.1.105" in self.workers:
            if worker_nums == 1:
                nes = ne_list
            else:
                nes = ne_list[:vemu4_ne_num]
            self._ne_divide(f'{self.topo}_sub1', nes)
            ne_nums -= vemu4_ne_num
            worker_nums -= 1
        if worker_nums == 0:
            # 链路划分
            self._link_divide()
            # 服务划分
            self._service_divide()
        else:
            p = ne_nums // worker_nums
            ne_list = ne_list[vemu4_ne_num:]
            # 节点划分
            for i in range(1, worker_nums + 1):
                subtopo_i = f'{self.topo}_sub{i+1}'
                if i == worker_nums:
                    nes = ne_list[(worker_nums-1)*p:]
                else:
                    nes = ne_list[p * (i - 1):p * i]
                self._ne_divide(subtopo_i, nes)
            # 链路划分
            self._link_divide()
            # 服务划分
            self._service_divide()
    
    def _certain_worker_split(self, scheme):
        pass
            
    def _custom_more_split(self, split_list):
        ne_list = list(self.ne2ob.keys())
        ne_list.sort()
        # print(ne_list)
        ne_nums = len(ne_list)
        worker_nums = len(self.workers)
        # print(self.workers)
        # 不能多于worker数
        assert(len(split_list) <= worker_nums)
        for i in range(len(split_list)):
            nes = ne_list[:split_list[i]]
            print("nes:", nes, f'{self.topo}_sub{i+1}')
            # 推进ne_list
            ne_list = ne_list[split_list[i]:]
            ne_nums -= split_list[i]
            self._ne_divide(f'{self.topo}_sub{i+1}', nes)
            worker_nums -= 1
        if worker_nums == 0:
            # 链路划分
            self._link_divide()
            # 服务划分
            self._service_divide()
        else:
            p = ne_nums // worker_nums
            # 节点划分
            for i in range(1, worker_nums + 1):
                subtopo_i = f'{self.topo}_sub{i+len(split_list)}'
                if i == worker_nums:
                    nes = ne_list[(worker_nums-1)*p:]
                else:
                    nes = ne_list[p * (i - 1):p * i]
                self._ne_divide(subtopo_i, nes)
            # 链路划分
            self._link_divide()
            # 服务划分
            self._service_divide()
    
    def _ne_divide(self, subtopo_index, nes):
        ne_list = self.plane_subtopo_list[subtopo_index]['NEs']
        ne_list.extend(nes)
        for ne in ne_list:
            ne_ob = self.ne2ob[ne]
            ne_ob.update_neloc(subtopo_index)
            self.ne_table_dict.update(ne_ob.table)

    def _link_divide(self):
        link_list = self.link2ob.keys()
        for k in link_list:
            link_ob = self.link2ob[k]
            link_table = link_ob.get_link_table()
            src = link_table['sourceNE']
            dst = link_table['targetNE']
            srcip = link_table['sourceIP']
            dstip = link_table['targetIP']
            fmveth = link_table['sourceveth']
            toveth = link_table['targetveth']
            src_loc = self.ne2ob[src].get_ne_loc()
            dst_loc = self.ne2ob[dst].get_ne_loc()
            if src_loc == dst_loc:
                self.plane_subtopo_list[src_loc]['links'].append(link_ob.name)
            else:
                # l1 l2分别为两条vxlan链路名
                l1 = f'link_{link_ob.name}_vxlan1'
                l2 = f'link_{link_ob.name}_vxlan2'
                vni = ''.join([str(random.randint(1, 9)) for i in range(5)])
                id1 = str(uuid.uuid4()).replace("-", '')[:10]
                id2 = str(uuid.uuid4()).replace("-", '')[:10]
                self.plane_subtopo_list[src_loc]['vxlanlinks'].append(l1)
                self.plane_subtopo_list[dst_loc]['vxlanlinks'].append(l2)
                l1_temp = self.vxlanlink_table_dict.setdefault(
                    f'{self.topo}_{l1}', {})
                l2_temp = self.vxlanlink_table_dict.setdefault(
                    f'{self.topo}_{l2}', {})
                remote_ip_dst = self.subtopo2worker[dst_loc]
                remote_ip_src = self.subtopo2worker[src_loc]
                if remote_ip_dst == 'hardware':
                    ip_dst = self.ne2ob[dst].get_ne_switch_ip()
                else:
                    ip_dst = remote_ip_dst
                if remote_ip_src == 'hardware':
                    ip_src = self.ne2ob[src].get_ne_switch_ip()
                else:
                    ip_src = remote_ip_src
                l1_temp.update({'VNI': vni, 'remoteIP': ip_dst, 'source': src, 'target': id1,
                                'sourcePort': '', 'partof': link_ob.name, 'sourceIP': srcip, 'sourceveth': str(fmveth) + '0', 'targetveth': str(fmveth) + '1'})
                l2_temp.update({'VNI': vni, 'remoteIP': ip_src, 'source': dst, 'target': id2,
                                'sourcePort': '', 'partof': link_ob.name, 'sourceIP': dstip, 'sourceveth': str(toveth) + '0', 'targetveth': str(toveth) + '1'})
                link_ob.update_vxlan_info((l1, l2))
                self.link_table_dict.update(link_ob.table)

    def _service_divide(self):
        for key in Topo_process.ne_type:
            nes = self.topo_service[self.topo].get(key, [])
            for ne in nes:
                ne_loc = self.ne2ob[ne].get_ne_loc()
                self.subtopo_service[ne_loc][key].append(ne)


if __name__ == '__main__':
    topo_json = {
        "user": "test",
        "topo": "topo1",
        "networks": {
                "hosts": {
                    "h1": {
                        "name": "h1",
                        "image_name": "host/ubuntu",
                        "type": "host",
                        "subtype": "ubuntu",
                        "interfaces": [
                            {
                                "name": "h1s3",
                                "ip": "192.168.1.2",
                                "netmask": "255.255.255.0"

                            }
                        ],
                        "gateway": "192.168.1.1",
                        "x":0,
                        "y":0
                    },
                    "h2": {
                        "name": "h2",
                        "image_name": "host/ubuntu",
                        "type": "host",
                        "subtype": "ubuntu",
                        "interfaces": [
                            {
                                "name": "h2s5",
                                "ip": "192.168.1.3",
                                "netmask": "255.255.255.0",
                            }
                        ],
                        "gateway": "192.168.1.1",
                        "x": 0,
                        "y": 0
                    },
                    "h3": {
                        "name": "h3",
                        "image_name": "host/ubuntu",
                        "type": "host",
                        "subtype": "ubuntu",
                        "interfaces": [
                            {
                                "name": "h3r2",
                                "ip": "192.168.2.2",
                                "netmask": "255.255.255.0"
                            }
                        ],
                        "gateway": "192.168.2.1",
                        "x": 0,
                        "y": 0
                    }
                },
                "switches": {
                    "s1": {
                        "name": "s1",
                        "type": "switch",
                        "subtype": "ovs",
                        "stp": "true",
                        "image_name": "switch/ovs",
                        "x": 0,
                        "y": 0,
                        "controllers": ['ctr1', 'ctr2']
                    },
                    "s2": {
                        "name": "s2",
                        "type": "switch",
                        "subtype": "ovs",
                        "image_name": "switch/ovs",
                        "stp": "true",
                        "x": 0,
                        "y": 0,
                        "controllers": ['ctr2', 'ctr3']
                    },
                    "s3": {
                        "name": "s3",
                        "type": "switch",
                        "subtype": "ovs",
                        "stp": "true",
                        "image_name": "switch/ovs",
                        "x": 0,
                        "y": 0,
                        "controllers": ['ctr1', 'ctr3']
                    },
                    "s4": {
                        "name": "s4",
                        "type": "switch",
                        "subtype": "ovs",
                        "stp": "true",
                        "image_name": "switch/ovs",
                        "x": 0,
                        "y": 0,
                        "controllers": ['ctr3', ]
                    },
                    "s5": {
                        "name": "s5",
                        "type": "switch",
                        "subtype": "ovs",
                        "stp": "true",
                        "image_name": "switch/ovs",
                        "x": 0,
                        "y": 0,
                        "controllers": []
                    }
                },
                "routers": {
                    "r1": {
                        "name": "r1",
                        "type": "router",
                        "subtype": "quagga",
                        "gateway": "",
                        "image_name": "router/quagga",
                        "interfaces": [
                            {
                                "name": "r1s1",
                                "ip": "192.168.1.1",
                                "netmask": "255.255.255.0"
                            },
                            {
                                "name": "r1r2",
                                "ip": "10.0.0.1",
                                "netmask": "255.0.0.0"
                            }
                        ],
                        "config": {
                            "rip": {
                                "networks": [],
                                "neighbors": [],
                                "version": 2
                            },
                            "ospf": {
                                "router_id": "a.b.c.d (可缺省)",
                                "networks": [
                                    ["networks/m", "area这些就还是要用户自己输入就好了"],
                                    ["a.b.c.d/m", "0.0.0.0"],
                                    ["a.b.c.d/m", "1.1.1.1"]
                                ],
                                "areas": {
                                    "area_id": ["range1", "range2"]
                                }
                            },
                            "bgp": {
                                "asn": "1",
                                "router_id": "",
                                "networks": ["192.168.1.0/24"],
                                "neighbors": [
                                    ["10.0.0.2", "2"]
                                ]
                            }
                        }
                    },
                    "r2": {
                        "name": "r2",
                        "type": "router",
                        "subtype": "quagga",
                        "gateway": "",
                        "image_name": "router/quagga",
                        "interfaces": [
                            {
                                "name": "r2r1",
                                "ip": "10.0.0.2",
                                "netmask": "255.0.0.0"
                            },
                            {
                                "name": "r2h3",
                                "ip": "192.168.2.1",
                                "netmask": "255.255.255.0"
                            }
                        ],
                        "config": {
                            "rip": {
                                "networks": [],
                                "neighbors": [],
                                "version": 2
                            },
                            "ospf": {
                                "router_id": "a.b.c.d (可缺省)",
                                "networks": [
                                    ["networks/m", "area这些就还是要用户自己输入就好了"],
                                    ["a.b.c.d/m", "0.0.0.0"],
                                    ["a.b.c.d/m", "1.1.1.1"]
                                ],
                                "areas": {
                                    "area_id": ["range1", "range2"]
                                }
                            },
                            "bgp": {
                                "asn": "2",
                                "router_id": "",
                                "networks": ["192.168.2.0/24"],
                                "neighbors": [
                                    ["10.0.0.1", "1"]
                                ]
                            }
                        }
                    }
                },
                "controllers": {
                    "ctr1": {
                        "name": "ctr1",
                        "image_name": "controller/floodlight",
                        "type": "controller",
                        "subtype": "floodlight"
                    },
                    "ctr2": {
                        "name": "ctr2",
                        "image_name": "controller/floodlight",
                        "type": "controller",
                        "subtype": "floodlight"
                    },
                    "ctr3": {
                        "name": "ctr3",
                        "image_name": "controller/floodlight",
                        "type": "controller",
                        "subtype": "floodlight"
                    },
                },
                "dpdks": {
                    "dpdk1":{
                        "name":"dpdk1",
                        "image_name": "dpdk/l2fwd",
                        "type": "dpdk",
                        "subtype": "l2fwd"
                    },
                    "dpdk2":{
                        "name":"dpdk2",
                        "image_name": "dpdk/l2fwd",
                        "type": "dpdk",
                        "subtype": "l2fwd"
                    },
                    "dpdk3":{
                        "name":"dpdk3",
                        "image_name": "dpdk/l2fwd",
                        "type": "dpdk",
                        "subtype": "l2fwd"
                    },
                },
                "links": {
                    "l1": {
                        "name": "l1",
                        "source": "s1",
                        "sourceIP": "",
                        "sourceType": "switch/ovs",
                        "target": "s2",
                        "targetIP": "",
                        "targetType": "switch/ovs"
                    },
                    "l2": {
                        "name": "l2",
                        "source": "s1",
                        "sourceIP": "",
                        "sourceType": "switch/ovs",
                        "target": "s4",
                        "targetIP": "",
                        "targetType": "switch/ovs"
                    },
                    "l3": {
                        "name": "l3",
                        "source": "s2",
                        "sourceIP": "",
                        "sourceType": "switch/ovs",
                        "target": "s3",
                        "targetIP": "",
                        "targetType": "switch/ovs"
                    },
                    "l4": {
                        "name": "l4",
                        "source": "s3",
                        "sourceIP": "",
                        "sourceType": "switch/ovs",
                        "target": "h1",
                        "targetIP": "",
                        "targetType": "host/ubuntu",
                    },
                    "l5": {
                        "name": "l5",
                        "source": "s4",
                        "sourceIP": "",
                        "sourceType": "switch/ovs",
                        "target": "s5",
                        "targetIP": "",
                        "targetType": "switch/ovs"
                    },
                    "l6": {
                        "name": "l6",
                        "source": "s5",
                        "sourceIP": "",
                        "sourceType": "switch/ovs",
                        "target": "h2",
                        "targetIP": "",
                        "targetType": "host/ubuntu"
                    },
                    "l7": {
                        "name": "l7",
                        "source": "s1",
                        "sourceIP": "",
                        "sourceType": "switch/ovs",
                        "target": "r1",
                        "targetIP": "",
                        "targetType": "router/quagga"
                    },
                    "l8": {
                        "name": "l8",
                        "source": "r1",
                        "sourceIP": "",
                        "sourceType": "router/quagga",
                        "target": "r2",
                        "targetIP": "",
                        "targetType": "router/quagga"
                    },
                    "l9": {
                        "name": "l9",
                        "source": "r2",
                        "sourceIP": "",
                        "sourceType": "router/quagga",
                        "target": "h3",
                        "targetIP": "",
                        "targetType": "host/ubuntu"
                    },
                    "l10": {
                        "name": "l10",
                        "source": "h1",
                        "sourceIP": "192.168.1.2",
                        "sourceType": "host/ubuntu",
                        "target": "dpdk1",
                        "taretIP": "",
                        "targetType": "dpdk/l2fwd"
                    },
                    "l11": {
                        "name": "l11",
                        "source": "h2",
                        "sourceIP": "192.168.1.3",
                        "sourceType": "host/ubuntu",
                        "target": "dpdk1",
                        "taretIP": "",
                        "targetType": "dpdk/l2fwd"
                    }
                }
        }
    }
    worker_list = ['10.1.1.105', '10.1.1.104']
    topo_processed = Topo_process(topo_json, worker_list, option=1)
    topo_processed()
    for key in topo_processed.var_table:
        pprint(getattr(topo_processed, key))
    for key in topo_processed.const_table:
        print(key+'*'*60)
        pprint(getattr(topo_processed, key))
    for key in topo_processed.ne2ob:
        print(f'{key}: {topo_processed.ne2ob[key].get_ne_id()}')

