from abc import ABCMeta, abstractmethod

import docker
import docker.errors

from ..Implement_layer import LinkManager as link_manager
from ..tools.log_tools import FLASK_LOGGER

docker_cli = docker.from_env()
SUCCESS_RESULT_MSG = {'code': 0, 'msg': 'success'}
NONE_NET = docker_cli.networks.get('none')


# 节点删除和链路删除目前来看是不需要单独的API的
# 对于单个节点的删除， 需要删除节点本身，以及可能的vxlanlink
# 对于链路的删除？？？？ 对于普通的veth pair (好删除)
# 对于vxlanlink， 需要删除相对应的OVS， 都是很简单的操作

def get_image_init_para(**kwargs):
    default_para = {'privileged': True, 'oom_kill_disable': True, 'detach': True,
                    'network_mode': 'bridge', 'stdin_open': True, 'tty': True}
    for k, v in kwargs.items():
        default_para[k] = v
    return default_para


def get_container_exec_para(**kwargs):
    return {'privileged': True, 'detach': True}


def get_overlay_net(net_name):
    try:
        overlay = docker_cli.networks.get(net_name)
    except docker.errors.NotFound:
        net_para = {'name': net_name, 'driver': 'overlay', 'attachable': True}
        overlay = docker_cli.networks.create(**net_para)
    return overlay


def delete_overlay_net(name):
    try:
        net = docker_cli.networks.get(name)
        net.remove()
    except docker.errors.NotFound:
        pass
    except docker.errors.APIError as e:
        FLASK_LOGGER.error(e.args)
        if e.status_code == 403:
            pass


class NECreator(metaclass=ABCMeta):

    @abstractmethod
    def create_and_run(self):
        raise NotImplementedError


class NERunner(metaclass=ABCMeta):

    @abstractmethod
    def start_service(self):
        raise NotImplementedError


class NEEditor(metaclass=ABCMeta):

    @abstractmethod
    def modify(self):
        raise NotImplementedError


class LinkCreator(metaclass=ABCMeta):

    # 添加链路
    @abstractmethod
    def create_link(self):
        raise NotImplementedError

    # 写入生成的信息
    @abstractmethod
    def write_info(self):
        raise NotImplementedError

    @staticmethod
    def _add_nic_to_ovs_ctr(ctr_id, nic):
        cmd = f'sudo docker exec {ctr_id} ovs-vsctl add-port init-br0 {nic}'
        link_manager.shell_execute(cmd)


class DynamicNeCreator:

    def __init__(self, user, topo, name, re_cli):
        self.user = user
        self.topo = topo
        self.name = name
        self.table = f'{topo}_{name}'
        self.re_cli = re_cli
        self.info = self.re_cli.get_all_values(self.table)
        self.init_para = {'image': self.info['NEimage'], 'name': self.info['NEid'], 'hostname': name}
        self.init_para.update(get_image_init_para())

    def create_and_run(self):
        ne_type = self.info['NEtype']
        if ne_type in ["host", "router"]:
            self._ne_creator = DefaultNECreator(self.init_para)
            self._ne_creator.create_and_run()
        elif ne_type == 'switch':
            self._ne_creator = OvsCreator(self.init_para)
            self._ne_creator.create_and_run()
        elif ne_type == 'controller':
            self._ne_creator = ControllerCreator(self.init_para, self.re_cli)
            net = f'{self.user}-{self.topo}-sdn'
            self._ne_creator.create_and_run(net, self.table)

    def close(self):
        self.re_cli.close()


class DefaultNECreator(NECreator):

    def __init__(self, conf):
        self.conf = conf

    def create_and_run(self):
        docker_cli.containers.run(**self.conf)


# 这里在创建的时候， 还要回传写入的信息， 确实应该和创建overlay网络分开
# 按理说应该是不需要返回值的, 所有的工作， 在这个类里面就做完
# 创建网络的时候，还需要传入用户名
# 创建节点的时候， 应该是不需要是不需要ne_config
# 所以创建的时候， 是不需要读conf的
# 这里的信息是不是也需要写入到config里面哦, 不用， 写在外面更好，
# 因为做编辑的返回的时候， 直接返回config里面的信息就可以了
class ControllerCreator(NECreator):

    def __init__(self, conf, re_cli=None):
        self.conf = conf
        self.re_cli = re_cli

    def create_and_run(self, net_name=None, table_name=None):
        ctr = docker_cli.containers.run(**self.conf)
        overlay = get_overlay_net(net_name)
        none_net = docker_cli.networks.get('none')
        none_net.disconnect(ctr)
        overlay.connect(ctr)
        ctr_ip = link_manager.shell_execute(
            "sudo docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "
            + ctr.id
        )
        ne_config = self.re_cli.get_value(table_name, 'NEconfig')
        ne_config.update({'overlay': net_name, 'ip': ctr_ip})
        self.re_cli.set_value(table_name, 'NEconfig', ne_config)


class OvsCreator(NECreator):

    def __init__(self, conf, re_cli=None):
        self.conf = conf
        self.re_cli = re_cli

    def create_and_run(self):
        container = docker_cli.containers.run(**self.conf)
        code = container.exec_run('service openvswitch-switch start').exit_code
        if code:
            raise RuntimeError('OVS启动失败')
        code = container.exec_run('ovs-vsctl add-br init-br0').exit_code
        if code:
            raise RuntimeError('OVS创建网桥失败')
        # 写入dpid, 这时候，基本表项的信息都写完了
        # 应该在启动服务的时候，写入dpid, 就是配置完成的时候


class DefaultRunner(NERunner):

    def __init__(self, name, ne_conf, container):
        self.container = container
        self.ne_conf = ne_conf
        self.name = name

    def start_service(self):
        # 0 表示执行成功， 和docker exec 的接口保持一致
        return


class HostRunner(DefaultRunner):

    # 这里直接访问NEgateway就可以了，网关信息单独写在外面了
    def start_service(self):
        # 这里为0就是正常运行
        code = 0
        gw = self.ne_conf['NEgateway']
        if gw:
            FLASK_LOGGER.debug(f'add gateway info of host:{self.name}')
            code = self.container.exec_run(f'route add default gw {gw}').exit_code
        if code:
            raise RuntimeError(f'主机{self.ne_conf["name"]}添加网关失败')


class OvsRunner(DefaultRunner):

    def __init__(self, name, ne_conf, container, topo=None, re_cli=None):
        super().__init__(name, ne_conf, container)
        self.topo = topo
        self.re_cli = re_cli

    def start_service(self):
        # 这里的异常捕捉留到外面
        # self._start_ovs_service()
        self._config_stp()
        self._add_link()
        self._add_controller()
        self._config_dpid()

    def _config_dpid(self):
        # 这里应该判断一下dpid的值是否存在
        # 还要提供修改dpid的接口
        dpid = self.ne_conf['NEconfig']['config'].get('dpid', None)
        if not dpid:
            cmd = "sh -c 'ovs-ofctl show init-br0 | grep dpid' "
            result = self.container.exec_run(cmd)
            dpid = result.output.decode().strip().split(':')[-1]
        else:
            # TODO(VESSALIUS) 根据用户上传的dpid修改ovs的dpid
            pass
        sw_table = f'{self.topo}_{self.name}'
        self.ne_conf['NEconfig']['config'].update({'dpid': dpid})
        self.re_cli.set_value(sw_table, 'NEconfig', self.ne_conf['NEconfig'])

    def _config_stp(self):
        FLASK_LOGGER.debug( self.ne_conf['NEconfig'])
        if_stp = self.ne_conf['NEconfig']['config']['stp']
        # 这里stp不可能为空， 默认值是true, 还有是false
        cmd = f'ovs-vsctl set bridge init-br0 stp_enable={str(if_stp).lower()}'
        FLASK_LOGGER.debug(cmd)
        code = self.container.exec_run(cmd).exit_code
        if code:
            raise RuntimeError(f'配置OVS:{self.name} stp失败')

    def _add_link(self):
        # 这时候link都是以link-开头作为前缀的
        for key, value in self.ne_conf.items():
            if key.startswith('link'):
                nic = value.get('nic')
                if nic:
                    FLASK_LOGGER.debug(f'now add {nic} in {self.name}')
                    code = self.container.exec_run(f'ovs-vsctl add-port init-br0 {nic}').exit_code
                    if code:
                        raise RuntimeError(f'为OVS{self.name}添加网卡{nic}失败')

    def _add_controller(self):
        # TODO(vessalius): 目前就是一个topo的所有的SDN节点就在一个overlay网络中。之后在想办法改进
        FLASK_LOGGER.debug(f'add controllers in {self.name}')
        ctrs = self.ne_conf['NEconfig']['config']['controllers']
        if not ctrs:
            return
        cmd = 'ovs-vsctl set-controller init-br0 '
        ctr = ctrs[0]
        ctr_db_name = f'{self.topo}_{ctr}'
        ctr_info = self.re_cli.get_value(ctr_db_name, 'NEconfig')
        # 得到该拓扑的 overlay net
        net = docker_cli.networks.get(ctr_info['overlay'])
        # 这里需要注意后面留出一个空格
        FLASK_LOGGER.debug(ctr_info)
        for ctr in ctrs:
            ctr_db_name = f'{self.topo}_{ctr}'
            ctr_info = self.re_cli.get_value(ctr_db_name, 'NEconfig')
            # 这里需要注意后面留出一个空格
            # 同时需要读出来port的信息
            port = ctr_info.get("port")
            if port:
                cmd += f"tcp:{ctr_info['ip']}:{ctr_info['port']} "
            else:
                cmd += f"tcp:{ctr_info['ip']}:6653 "
        FLASK_LOGGER.debug(f'ctr cmd {self.name} {cmd}')
        NONE_NET.disconnect(self.container)
        net.connect(self.container)
        code= self.container.exec_run(cmd).exit_code
        if code:
            raise RuntimeError(f'为OVS{self.name}添加控制器失败')


RIP_CONF_COMMON = '''
hostname rip
password zebra
debug rip events
debug rip packet
router rip
'''

OSPF_COMMON_CONF ="""
hostname ospfd
password zebra
debug ospf event
debug ospf packet all
router ospf
"""

BGP_COMMON_CONF = """
hostname bgpd
password zebra
log stdout
debug bgp events
debug bgp zebra
"""

ISIS_COMMON_CONF = """
hostname <name>
password zebra
debug isis events
debug isis adj-packets
"""

ZEBRA_COMMON_CONF = """
hostname <name>
password zebra
debug zebra events
debug zebra packet
debug zebra rib
"""

class QuaggaRunner(DefaultRunner):

    def __init__(self, name, ne_conf, container):
        super().__init__(name, ne_conf, container)
        self.ripd = ne_conf['NEconfig']['config']['rip']
        self.ospfd = ne_conf['NEconfig']['config']['ospf']
        self.bgpd = ne_conf['NEconfig']['config']['bgp']
        self.isisd = ne_conf['NEconfig']['config']['isis']
        self.ne_conf = ne_conf

    def start_service(self):
        # 首先启动zebra
        self._zebra_conf()
        if not (self.ripd or self.ospfd or self.bgpd or self.isisd):
            return
        FLASK_LOGGER.debug('start router protocols...')
        if self.ripd['enable']:
            self._rip_conf()
        if self.ospfd['enable']:
            self._ospf_conf()
        if self.bgpd['enable']:
            self._bgp_conf()
        if self.isisd['enable']:
            self._isis_conf()
    
    def _ip_netmask2cidrip(self, ip, netmask):
        # 计算二进制字符串中 '1' 的个数
        def count_bit(bin_str): 
            return len([i for i in bin_str if i == '1'])
        # 分割字符串格式的子网掩码为四段列表
        mask_splited = netmask.split('.')
        # 转换各段子网掩码为二进制, 计算十进制
        mask_count = [count_bit(bin(int(i))) for i in mask_splited]
        ip_cidr = ip + '/' + str(sum(mask_count))
        return ip_cidr

    def _zebra_conf(self):
        zebra_conf = ZEBRA_COMMON_CONF.replace("<name>", self.name)
        FLASK_LOGGER.debug('zebra_conf...')
        if self.isisd['enable']:
        # 添加ISIS接口配置,ISIS需要知道网卡名
            for k in self.ne_conf:
                if "link_l" in k:
                    mac = self.ne_conf[k]["nic"]
                    ip, netmask = self.ne_conf[k]["ip"], self.ne_conf[k]["mask"]
                    ip_cidr = self._ip_netmask2cidrip(ip, netmask)
                    zebra_conf += f"\n!\ninterface {mac}\n"
                    zebra_conf += f" ip address {ip_cidr}"
        FLASK_LOGGER.debug(zebra_conf)
        code = self.container.exec_run(
            'sh -c "echo \'{}\' > /etc/quagga/zebra.conf"'.format(zebra_conf)).exit_code
        if code:
            raise RuntimeError('写入zebra.conf 失败')
        code = self.container.exec_run('zebra -d').exit_code
        FLASK_LOGGER.debug(f'start zebra code: {code}')
        if code:
            raise RuntimeError('启动zebra服务失败')

    def _rip_conf(self):
        version = self.ripd['version'] if self.ripd['version'] else 2
        conf = RIP_CONF_COMMON + f'version {version}\n'
        for net in self.ripd['networks']:
            conf += f'network {net}\n'
        for neighbor in self.ripd['neighbors']:
            conf += f'neighbor {neighbor}\n'
        FLASK_LOGGER.debug('rip_conf...')
        FLASK_LOGGER.debug(conf)
        # 将文件内容重定向到 ripd.conf
        code = self.container.exec_run(
            'sh -c "echo \'{}\' > /etc/quagga/ripd.conf"'.format(conf)).exit_code
        if code:
            raise RuntimeError('写入ripd.conf 失败')
        code = self.container.exec_run('ripd -d').exit_code
        if code:
            raise RuntimeError('启动RIP路由协议失败')

    def _ospf_conf(self):
        conf = OSPF_COMMON_CONF
        FLASK_LOGGER.debug('ospf_conf...')
        if self.ospfd['router_id']:
            conf += f"ospf router-id {self.ospfd['router_id']}\n"
        for net, area in self.ospfd['networks']:
            conf += f"network {net} area {area}\n"
        try:
            for area, ranges in self.ospfd['areas'].items():
                for range in ranges:
                    conf += f"area {area} range {range}\n"
        except KeyError:
            pass
        FLASK_LOGGER.debug('ospf_conf...')
        FLASK_LOGGER.debug(conf)
        # 将文件内容重定向到 ospfd.conf
        code = self.container.exec_run(
            'sh -c "echo \'{}\' > /etc/quagga/ospfd.conf"'.format(conf)).exit_code
        if code:
            raise RuntimeError('写入ospfd.conf 失败')
        code = self.container.exec_run('ospfd -d').exit_code
        if code:
            raise RuntimeError('启动OSPF路由协议失败')

    def _bgp_conf(self):
        asn = self.bgpd['asn']
        conf = BGP_COMMON_CONF + f'router bgp {asn}\n'
        if self.bgpd['router_id']:
            conf += f"bgp router-id {self.bgpd['router_id']}\n"
        for net in self.bgpd['networks']:
            conf += f'network {net}\n'
        for neighbor, remote_as in self.bgpd['neighbors']:
            conf += f'neighbor {neighbor} remote-as {remote_as}\n'
        FLASK_LOGGER.debug('bgp_conf...')
        FLASK_LOGGER.debug(conf)
        code = self.container.exec_run(
            'sh -c "echo \'{}\' > /etc/quagga/bgpd.conf"'.format(conf)).exit_code
        if code:
            raise RuntimeError('写入bgpd.conf 失败')
        code = self.container.exec_run('bgpd -d').exit_code
        if code:
            raise RuntimeError('启动BGP路由协议失败')
    
    def _isis_conf(self):
        conf = ISIS_COMMON_CONF.replace("<name>", self.name)
        for k in self.ne_conf:
            if "link_l" in k:
                mac = self.ne_conf[k]["nic"]
                conf += f"\n!\ninterface {mac}\n"
                conf += f" ip router isis {self.isisd['area']}\n"
                # if self.ne_conf[k]["config"]:
                #     pass
                # TODO(sw):以后接口信息可能需要填一些接口配置
                if self.isisd.get("network_type", ''):
                    net_type = self.isisd["network_type"]
                    conf += f" isis network {net_type}\n"
                conf += f" isis network point-to-point\n"
                # 才能添加circuit_type，用于配置接口邻接类型
                # if self.isisd.get("circuit_type", ""):
                #     pass
        # 添加ISIS NET
        net = self.isisd["net"]
        conf += f"router isis {self.isisd['area']}\n net {net}\n"
        # 默认是level-1-2
        if self.isisd["is_type"]:
            conf += f" is-type {self.isisd['is_type']}\n"
        # TODO(sw):路由汇总和
        FLASK_LOGGER.debug("isis_conf...")
        FLASK_LOGGER.debug(conf)
        code = self.container.exec_run(
            'sh -c "echo \'{}\' > /etc/quagga/isisd.conf"'.format(conf)).exit_code
        if code:
            raise RuntimeError('写入isisd.conf 失败')
        code = self.container.exec_run('isisd -d').exit_code
        if code:
            raise RuntimeError('启动ISIS路由协议失败')


# 在这一步需要检查， 创建连线的时候，需要检查两端的节点类型
# 如果节点类型为OVS的话， 需要在veth pair创建成功的时候
# 将两端网卡加入init-br0, vethlink 两端的在同一宿主机上
class VethLink(LinkCreator):

    def __init__(self, topo, name, re_cli):
        self.topo = topo
        self.name = name
        self.table = f'{topo}_{name}'
        self.re_cli = re_cli
        self.info = re_cli.get_all_values(self.table)
        self.src_id, self.tgt_id = self.info['sourceID'], self.info['targetID']
        self.src, self.tgt = self.info['sourceNE'], self.info['targetNE']
        self.src_type, self.tgt_type = self.info['sourceType'], self.info['targetType']

    def create_link(self):
        result = link_manager.create_link(self.src_id, self.tgt_id,
                                          self.info['sourceIP'], self.info['targetIP'])
        FLASK_LOGGER.debug(result)
        if result.get('error_msg'):
            raise RuntimeError(f'创建veth链路{self.name}失败')
        return result

    # 检查链路两端节点是否为OVS， 若为OVS， 网卡信息加入OVS的 init-br0
    # 但是在拓扑创建的时候， 是不需要调用该方法的，OVS自己会有add_link的操作
    def add_nic_to_ovs_ctr(self, result):
        # TODO(VESSALIUS): 这里是写死的， 按理说还应该检查是不是OVS
        if self.src_type == 'switch':
            src_port = result[self.src_id]['nic']
            self._add_nic_to_ovs_ctr(self.src_id, src_port)
        if self.tgt_type == 'switch':
            tgt_port = result[self.tgt_id]['nic']
            self._add_nic_to_ovs_ctr(self.tgt_id, tgt_port)

    def write_info(self, result=None):
        src_port, src_mac = result[self.src_id]['nic'], result[self.src_id]['mac']
        tgt_port, tgt_mac = result[self.tgt_id]['nic'], result[self.tgt_id]['mac']
        # 写入link_table
        self.re_cli.set_value(self.table, 'sourcePort', src_port)
        self.re_cli.set_value(self.table, 'targetPort', tgt_port)
        # 写入 <toponame>_<nename>
        src_table = f'{self.topo}_{self.src}'
        link_key = f'link_{self.name}'
        FLASK_LOGGER.debug(f'current link_key is {link_key}')
        tgt_table = f'{self.topo}_{self.tgt}'
        src_link_info = self.re_cli.get_value(src_table, link_key)
        src_link_info.update({'nic': src_port, 'mac': src_mac})
        tgt_link_info = self.re_cli.get_value(tgt_table, link_key)
        tgt_link_info.update({'nic': tgt_port, 'mac': tgt_mac})
        self.re_cli.set_value(src_table, link_key, src_link_info)
        self.re_cli.set_value(tgt_table, link_key, tgt_link_info)


# vxlan的创建逻辑, 也是
# 1. 创建并生成网卡信息
# 2. 写表
# 动态创建的时候， 给我的信息会有两个嘛
# 在这一步需要检查， 创建连线的时候，需要检查两端的节点类型
# 如果节点类型为OVS的话， 需要在veth pair创建成功的时候
# 将两端网卡加入init-br0
class VxLANLink(LinkCreator):

    def __init__(self, topo, name, re_cli):
        self.topo = topo
        self.name = name
        self.re_cli = re_cli
        # 这里的表应该是vxlan的表
        self.table = f'{topo}_{name}'
        info = re_cli.get_all_values(self.table)
        self.info = info
        self.src_id = re_cli.get_value(f'{self.topo}_{info["source"]}', 'NEid')
        self.src_type = re_cli.get_value(f'{self.topo}_{info["source"]}', 'NEtype')

    def create_link(self):
        FLASK_LOGGER.debug(self.info)

        result = link_manager.create_vxlan(self.src_id, self.info['sourceIP'], self.info['target'],
                                           self.info['remoteIP'], self.info['VNI'])
        if result.get('error_msg'):
            raise RuntimeError(f'创建vxlan链路{self.name}失败')
        FLASK_LOGGER.debug(f'result in vxlan_creator: {result} ')
        return result

    def add_nic_to_ovs_ctr(self, result):
        if self.src_type == 'switch':
            src_port = result[self.src_id]
            self._add_nic_to_ovs_ctr(self.src_id, src_port)

    def write_info(self, result=None):
        FLASK_LOGGER.debug(result)
        src_port = result[self.src_id]
        # 写入toponame_vxlanlinkname toponame_linkname topoName_NEname
        self.re_cli.set_value(self.table, 'sourcePort', src_port)
        ori_link_table = '{}_{}'.format(self.topo, self.info['partof'])
        # 通过source_id 来反查表项的前缀
        temp = self.re_cli.get_value(ori_link_table, 'sourceID')
        # 因为这里的source和targe在节点处没有区分
        key_prefix = 'source' if temp == self.src_id else 'target'
        self.re_cli.set_value(ori_link_table, '{}Port'.format(key_prefix), src_port)
        topo_ne_table = f"{self.topo}_{self.info['source']}"
        ne_link_key = f'link_{self.info["partof"]}'
        ne_detail = self.re_cli.get_value(topo_ne_table, ne_link_key)
        ne_detail['nic'] = src_port
        ne_detail['mac'] = result['src_mac']
        self.re_cli.set_value(topo_ne_table, ne_link_key, ne_detail)


class DefaultNEDeleter(object):

    def __init__(self, ne_id):
        self.ne_id = ne_id

    def stop_and_delete(self):
        try:
            ne = docker_cli.containers.get(self.ne_id)
            ne.stop()
            ne.remove()
        except docker.errors.NotFound:
            pass


class HostEditor(NEEditor):
    """
    Host 能改变的属性包括，接口地址， 网关
    定义传入的参数的数据类型
    Args:
        changed: dict {'interface': {}, 'NEgateway'}
    这里changed的参数就是为了传递作了修改的键
    """

    def __init__(self, topo, name, changed, info, re_cli=None):
        self.changed = changed
        self.topo = topo
        self.name = name
        self.ne_id = info['NEid']
        self.info = info
        self.re_cli= re_cli

    def modify(self):
        intf_conf = self.changed.get('interface', None)
        if intf_conf:
            self._modify_intf(intf_conf)
        gateway = self.changed.get('NEgateway', None)
        if gateway:
            self._modify_gateway(gateway)

    def _modify_intf(self, intf_conf):
        # 这里直接找key 用changed的key, 在info里面进行索引
        for link in intf_conf.keys():
            info = self.info[link]
            link_manager.modify_intf(self.ne_id, info['nic'], info['ip'], info['mask'])

    def _modify_gateway(self, gateway):
        link_manager.modify_gateway(self.ne_id, gateway)


class UbuntuEditor(HostEditor):
    pass


# 其实这个应该是和SwitchRunner组合起来的
class SwitchEditor(NEEditor):
    """
    # 就算不是动态创建也是需要实现的
    OVS 能改变的属性包括 stp, controllers 增加或者删除控制器
    Args:
        changed: dict {'stp': true, 'controllers': []}
        之后可能还需要加上
    """

    def __init__(self, topo, name, changed, info, re_cli=None):
        self.topo = topo
        self.name = name
        self.changed = changed
        self.container = docker_cli.containers.get(info['NEid'])
        self.info = info
        self.re_cli = re_cli

    def modify(self):
        for key in self.changed.keys():
            modify_func = getattr(self, f'_modify_{key}')
            modify_func(self.info['NEconfig']['config'][key])

    def _modify_stp(self, stp_flag):
        cmd = f'ovs-vsctl set bridge init-br0 stp_enable={str(stp_flag).lower()}'
        code = self.container.exec_run(cmd).exit_code
        if code:
            raise RuntimeError("设置stp失败")

    def _modify_controllers(self, ctrs):
        code = self.container.exec_run('ovs-vsctl del-controller init-br0').exit_code
        if code:
            raise RuntimeError('删除网桥上的控制器失败')
        if not ctrs:
            return
        cmd = 'ovs-vsctl set-controller init-br0 '
        for ctr in ctrs:
            ctr_db = f'{self.topo}_{ctr}'
            ctr_info = self.re_cli.get_value(ctr_db, 'NEconfig')
            port = ctr_info['config'].get('port')
            if port:
                cmd += f"tcp:{ctr_info['ip']}:{port} "
            else:
                cmd += f"tcp:{ctr_info['ip']}:6653 "
            overlay = ctr_info['overlay']
        FLASK_LOGGER.debug(f'ctr cmd {self.name} {cmd}')
        # 这里循环到最后， 也是会有ctr这个变量的, 因为前面已经判断了ctrs不为空
        # 我这里没有controllers的话， controller字段里面是不应该有值的
        overlay_net = get_overlay_net(overlay)
        # edit的时候， 如果之前没有， 动态编辑的时候也无法确定
        # 是否已经创建了overlay网络，也得检查是第一次加入还是第二次加入, 比创建拓扑的时候还是要麻烦一些
        # 这里需要检查是否已经将container添加到了net中
        try:
            NONE_NET.disconnect(self.container)
        except docker.errors.APIError:
            pass
        try:
            overlay_net.connect(self.container)
        except docker.errors.APIError as e:
            FLASK_LOGGER.error(e.args)
            if e.status_code == 409:
                pass
        code = self.container.exec_run(cmd).exit_code
        if code:
            raise RuntimeError(f'为OVS {self.name}添加控制器失败')


class OvsEditor(SwitchEditor):
    pass


class QuaggaEditor(NEEditor):
    """
    quagga 能改变的属性包括：   quagga 用户自己去容器里面改
    网络协议的启停、 网络协议的相关配置， 编辑面板的属性， 增加或者删除配置？
    这还不如直接让他们进到容器里面去修改
    只用抽象出部分的属性就可以了
    Args:
        changed: dict {'interface': {}, 'rip': {}, 'ospf': {}, 'bgp': {}}
    """

    def __init__(self, topo, name, changed, info, re_cli=None):
        # 这里的info 最好就是使用最外层的info
        self.info = info
        self.re_cli = re_cli
        self.topo = topo
        self.name = name
        self.container = docker_cli.containers.get(info['NEid'])
        self.changed = changed

    def modify(self):
        intf_conf = self.changed.pop('interface', None)
        if intf_conf:
            self._modify_intf(intf_conf)
        for protocol in self.changed.keys():
            self._modify_protocol(protocol)

    def _modify_intf(self, intf_conf):
        for link in intf_conf.keys():
            info = self.info[link]
            link_manager.modify_intf(self.info['NEid'], info['nic'], info['ip'], info['mask'])

    def _modify_protocol(self, protocol):
        quagga_runner = QuaggaRunner(self.name, self.info, self.container)
        # 这里有可能是运行也有可能是关闭某个运行的协议
        self.container.exec_run(f"sh -c 'kill $(cat /var/run/quagga/{protocol}d.pid)' ")
        if self.info['NEconfig']['config'][protocol]['enable']:
            protocol_func = getattr(quagga_runner, f'_{protocol}_conf')
            protocol_func()


class ControllerEditor(NEEditor):
    """
    控制器修改的属性包括什么？？？
    Args:
        changed: {'dpid': "string", 'mac': "addr_string"}
    """
    def __init__(self, topo, name, changed, info, re_cli=None):
        self.topo = topo
        self.name = name
        self.changed = changed
        self.info = info
        self.re_cli = re_cli

    def modify(self):
        pass

    def _modify_dpid(self):
        pass

    def _modify_mac(self):
        pass
