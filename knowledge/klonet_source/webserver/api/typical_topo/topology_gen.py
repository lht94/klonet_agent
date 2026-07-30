import fnss
import IPy
import copy
from pprint import pprint


class Interface:
    def __init__(self):
        self.name = ''
        self.ip = ''
        self.netmask = ''

    def get_ip(self, counter, ip_prefix):
        ip_base, prefix = ip_prefix.split('/')
        self.netmask = self.cidr_netmask(int(prefix))
        dec_value = self.ip2decimalism(ip_base) + counter + 1
        self.ip = self.decimalism2ip(dec_value)

    @staticmethod
    def cidr_netmask(prefix):
        bin_arr = ['0' for i in range(32)]
        for i in range(prefix):
            bin_arr[i] = '1'
        tmpmask = [''.join(bin_arr[i * 8:i * 8 + 8]) for i in range(4)]
        tmpmask = [str(int(tmpstr, 2)) for tmpstr in tmpmask]
        return '.'.join(tmpmask)

    @staticmethod
    def ip2decimalism(ip):
        dec_value = 0
        v_list = ip.split('.')
        v_list.reverse()
        t = 1
        for v in v_list:
            dec_value += int(v) * t
            t = t * (2 ** 8)
        return dec_value

    @staticmethod
    def decimalism2ip(dec_value):
        ip = ''
        t = 2 ** 8
        for _ in range(4):
            v = dec_value % t
            ip = '.' + str(v) + ip
            dec_value = dec_value // t
        ip = ip[1:]
        return ip


class Host:
    def __init__(self, counter, ip_prefix, ip_counter):
        self.name = f'h{counter}'
        self.image_name = 'host/ubuntu'
        self.type = 'host'
        self.subtype = 'ubuntu'
        self.interfaces = []
        self.gateway = ''
        self.resource_limit = {'cpu': '', 'mem': ''}
        ifa = Interface()
        ifa.get_ip(ip_counter, ip_prefix)
        self.interfaces.append(ifa.__dict__)

    def ipnetmask2cidrip(self):
        ip = self.interfaces[0]['ip']
        netmask = self.interfaces[0]['netmask']
        if ip == '' or netmask == '':
            return ''
        else:
            # 计算二进制字符串中 '1' 的个数
            count_bit = lambda bin_str: len([i for i in bin_str if i == '1'])
            # 分割字符串格式的子网掩码为四段列表
            mask_splited = netmask.split('.')
            # 转换各段子网掩码为二进制, 计算十进制
            mask_count = [count_bit(bin(int(i))) for i in mask_splited]
            ip_cidr = ip + '/' + str(sum(mask_count))
            return ip_cidr


class Switch:
    def __init__(self, counter):
        self.name = f's{counter}'
        self.image_name = 'switch/ovs'
        self.type = 'switch'
        self.subtype = 'ovs'
        self.config = {'stp': True, 'controllers': []}
        self.resource_limit = {'cpu': '', 'mem': ''}


class Link:
    def __init__(self, counter, src, dst):
        self.name = f'l{counter}'
        self.source = getattr(src, 'name', '')
        self.sourceIP = ''
        self.sourceType = getattr(src, 'type', '')
        self.target = getattr(dst, 'name', '')
        self.targetIP = ''
        self.targetType = getattr(dst, 'type', '')
        if getattr(src, 'type') == 'host':
            src.interfaces[0]['name'] = src.name + dst.name
            self.sourceIP = src.ipnetmask2cidrip()
        if getattr(dst, 'type') == 'host':
            dst.interfaces[0]['name'] = dst.name + src.name
            self.targetIP = dst.ipnetmask2cidrip()


class Typical_topo:
    # 典型拓扑类型对应的请求中的参数
    topology_arg = {'fattree': ['fattree_k'], 'tree': ['tree_branches', 'tree_depths', 'tree_host_density'],
                     'linear': ['linear_m', 'linear_n'], 'star': ['star_n']}
    topology_method = {'fattree': '_fattree', 'tree': '_tree',
                       'linear': '_linear', 'star': '_star'}
    # 节点及链路类型
    Ne_type = {'hosts': {}, 'switches': {}, 'controllers': {}, 'routers': {}}
    Link_type = {'links': {}}

    def __init__(self, **kwargs):
        self.host_counter = 1
        self.switch_counter = 1
        self.link_counter = 1
        self.ip_prefix = '192.168.1.0/24'
        self.__dict__.update(kwargs)
        self.net = {}
        self.information = {}
        # ！！！！！！！！此处应该深拷贝，否则会改变类属性，导致之后的对象出问题！！！！！！！！！！！！！！！！！！
        self.net.update(**copy.deepcopy(Typical_topo.Ne_type), **copy.deepcopy(Typical_topo.Link_type))

    def __call__(self):
        try:
            topology_type = getattr(self, 'topology_type')
        except AttributeError:
            msg = {'msg': '缺少拓扑类型参数，fattree?tree?linear?star?', 'code': 0}
            return msg
        else:
            ip_nums = self.judge_ip_prefix(self.ip_prefix)
            if ip_nums:
                func = getattr(self, Typical_topo.topology_method.get(topology_type))
                result = func(ip_nums)
                return result
            else:
                return {'msg': '请输入合法的网络前缀，如192.168.1.0/24', 'code': 0}

    @staticmethod
    def judge_ip_prefix(ip_prefix):
        try:
            IPy.IP(ip_prefix)
        except:
            return False
        else:
            return IPy.IP(ip_prefix).len()-2

    @staticmethod
    def judge_positive_int(*num):
        for i in num:
            if isinstance(i, int) and i>0 :
                continue
            else:
                return False
        return True

    @staticmethod
    def judge_positivs_even(num):
        if isinstance(num, int) and num % 2 == 0:
            return True
        else:
            return False

    def _update_topo_info(self, links, nes_dict, hosts_dict, switches_dict):
        counter = self.link_counter
        for u, v in links:
            link = Link(counter, nes_dict[u], nes_dict[v])
            self.net['links'].update({link.name: link.__dict__})
            counter += 1
        for v in hosts_dict.values():
            self.net['hosts'][v.name] = v.__dict__
        for v in switches_dict.values():
            self.net['switches'][v.name] = v.__dict__
        return {'information': self.information, 'net': self.net, 'code': 1, 'msg': 'success'}

    def _fattree(self, ip_nums):
        print('fattree.....')
        try:
            fattree_k = getattr(self, 'fattree_k')
        except AttributeError:
            msg = {'msg': '缺少fattree的参数：k（偶数）', 'code': 0}
            return msg
        else:
            if self.judge_positivs_even(fattree_k):
                if fattree_k**3 / 4 > ip_nums:
                    msg = {'msg': f'网络地址数不足！需要：{fattree_k**3 / 4},支持：{ip_nums}',
                           'code': 0}
                    return msg
                else:
                    fattree = fnss.fat_tree_topology(fattree_k)
                    links = fattree.edges
                    hosts_dict = {}
                    switches_dict = {}
                    nes_dict = {}
                    counter = self.switch_counter
                    for k in fattree.switches():
                        ne = Switch(counter)
                        switches_dict[k] = ne
                        nes_dict[k] = ne
                        counter += 1
                    counter = self.host_counter
                    for k in fattree.hosts():
                        ne = Host(counter, self.ip_prefix, counter-self.host_counter+1)
                        hosts_dict[k] = ne
                        nes_dict[k] = ne
                        counter += 1
                    # 添加节点的位置信息
                    for k, v in fattree.nodes.data():
                        ne_name = getattr(nes_dict[k], 'name')
                        ne_layer_type = v['layer']
                        self.information.setdefault(ne_name, ne_layer_type)
                    msg = self._update_topo_info(links, nes_dict, hosts_dict, switches_dict)
                    return msg
            else:
                msg = {'msg': '请填写正确的fattree参数，k必须是正偶数', 'code': 0}
                return msg

    def _tree(self, ip_nums):
        print('tree.....')
        try:
            branches = getattr(self, 'tree_branches')
            depths = getattr(self, 'tree_depths')
            host_density = getattr(self, 'tree_host_density')
        except AttributeError:
            msg = {'msg': '缺少参数：分支数，深度，叶子节点密度'}
            return msg
        else:
            if self.judge_positive_int(*(branches, depths, host_density)):
                if branches <= 1:
                    msg = {'msg': '分支数必须大于1！', 'code': 0}
                    return msg
                if branches**depths*host_density >ip_nums:
                    msg = {'msg': f'网络地址数不足！需要：{branches**depths*host_density},支持：{ip_nums}',
                           'code': 0}
                    return msg
                else:
                    tree = fnss.k_ary_tree_topology(branches, depths)
                    links = list(tree.edges)
                    hosts_dict = {}
                    switches_dict = {}
                    leaf_switches = {}
                    nes_dict = {}
                    counter = self.switch_counter
                    for u, v in tree.nodes.data():
                        ne = Switch(counter)
                        if v['type'] == 'leaf':
                            leaf_switches[u] = ne
                        switches_dict[u] = ne
                        nes_dict[u] = ne
                        counter += 1
                    counter = self.host_counter
                    for k, v in leaf_switches.items():
                        for i in range(0, host_density):
                            ne = Host(counter, self.ip_prefix, counter-self.host_counter+1)
                            hosts_dict[ne.name] = ne
                            nes_dict[ne.name] = ne
                            links.append((k, ne.name))
                            counter += 1
                    # 添加节点的位置信息
                    for k, v in tree.nodes.data():
                        ne_name = getattr(nes_dict[k], 'name')
                        ne_depth = v['depth']
                        self.information.setdefault(ne_name, ne_depth)
                    for v in hosts_dict.values():
                        self.information.setdefault(v.name, depths+1)
                    msg = self._update_topo_info(links, nes_dict, hosts_dict, switches_dict)
                    return msg
            else:
                msg = {'msg': '请填写正确的tree参数:分支数，深度，叶子节点密度都须为正整数', 'code': 0}
                return msg

    def _linear(self, ip_nums):
        print('linear.....')
        try:
            m = getattr(self, 'linear_m')
            n = getattr(self, 'linear_n')
        except AttributeError:
            msg = {'msg': '缺少参数：交换机数和主机数'}
            return msg
        else:
            if self.judge_positive_int(*(m, n)):
                if n > ip_nums:
                    msg = {'msg': f'网络地址数不足！需要：{n},支持：{ip_nums}',
                           'code': 0}
                    return msg
                else:
                    linear = fnss.line_topology(m)
                    links = list(linear.edges)
                    hosts_dict = {}
                    switches_dict = {}
                    nes_dict = {}
                    counter = self.switch_counter
                    for k in linear.nodes():
                        ne = Switch(counter)
                        switches_dict[k] = ne
                        nes_dict[k] = ne
                        counter += 1
                    counter = self.host_counter
                    for side in range(2):
                        for k in range(n):
                            ne = Host(counter, self.ip_prefix, counter-self.host_counter+1)
                            hosts_dict[ne.name] = ne
                            nes_dict[ne.name] = ne
                            if side == 0:
                                links.append((ne.name, 0))
                            else:
                                links.append((ne.name, m - 1))
                            counter += 1
                    msg = self._update_topo_info(links, nes_dict, hosts_dict, switches_dict)
                    return msg
            else:
                msg = {'msg': '请填写正确的linear参数:交换机数和主机数须为正整数', 'code': 0}
                return msg

    def _star(self, ip_nums):
        print('star.....')
        try:
            n = getattr(self, 'star_n')
        except AttributeError:
            msg = {'msg': '请填写正确的star参数:主机数须为正整数', 'code': 0}
            return msg
        else:
            if self.judge_positive_int(n):
                if n > ip_nums:
                    msg = {'msg': f'网络地址数不足！需要：{n},支持：{ip_nums}',
                           'code': 0}
                    return msg
                else:
                    star = fnss.star_topology(n)
                    links = star.edges
                    hosts_dict = {}
                    switches_dict = {}
                    nes_dict = {}
                    ne = Switch(self.switch_counter)
                    switches_dict[0] = ne
                    nes_dict[0] = ne
                    counter = self.host_counter
                    for k in star.nodes():
                        if k == 0:
                            continue
                        ne = Host(counter, self.ip_prefix, counter-self.host_counter+1)
                        hosts_dict[k] = ne
                        nes_dict[k] = ne
                        counter += 1
                    msg = self._update_topo_info(links, nes_dict, hosts_dict, switches_dict)
                    return msg
            else:
                msg = {'msg': '请填写正确的star参数:主机数须为正整数', 'code': 0}
                return msg


if __name__ == '__main__':
    kwargs = {'topology_type': 'fattree', 'fattree_k': 2, 'host_counter': 10, 'switch_counter': 2}
    kwargs1 = {'topology_type': 'tree', 'tree_branches': 2, 'tree_depths': 2, 'tree_host_density': 2}
    kwargs2 = {'topology_type': 'linear', 'linear_m': 2, 'linear_n': 2}
    kwargs3 = {
    "topology_type": "fattree",
    "host_counter": 100,
    "switch_counter": 10,
    "link_counter": 10,
    "tree_branches": 2,
    "tree_depths": 1,
    "tree_host_density": 1,
    "star_n":10,
    "linear_m":3,
    "linear_n":2,
    "fattree_k":2,
    "ip_prefix": "192.188.1.0/24"
}
    kwargs4 = {
        "topology_type": "fattree",
        "host_counter": 10,
        "switch_counter": 10,
        "link_counter": 10,
        "tree_branches": 2,
        "tree_depths": 1,
        "tree_host_density": 1,
        "star_n": 10,
        "linear_m": 3,
        "linear_n": 2,
        "fattree_k": 2,
        "ip_prefix": "192.188.1.0/24"
    }
    t1 = Typical_topo(**kwargs3)
    t1()
    t2 = Typical_topo(**kwargs4)
    t2()
