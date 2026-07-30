from flask.views import MethodView
from flask import request
from ....tools.log_tools import *
from ....Function_layer.deployed_proj_manager import retrieve_topo
from flask_login import login_required
import json
import copy


def contain_number(str):
    """
    检查整个字符串是否包含数字
    """
    for ch in str:
        if '0' <= ch <= '9':
            return True
    return False


def contain_alpha(str):
    """
    检查整个字符串是否包含说明节点类型的字母
    c: controllers, 
    r: routers, 
    s: switches, 
    h: hosts
    """
    ret = []
    for ch in str:
        if ch == 'c':
            ret.append('controller')
        if ch == 'r':
            ret.append('router')
        if ch == 's':
            ret.append('switch')
        if ch == 'h':
            ret.append('host')
    return ret


def dec_2_ip(num):
    """
    把一个32位十进制数转换为ip字符串
    """
    return '.'.join([str(num % (256 ** i) // (256 ** (i-1))) for i in range(4, 0, -1)])


def ip_2_dec(ip):
    """
    把一个ip字符串转换为32位十进制数
    """
    return sum([int(num)*(256**(3-i)) for i,num in enumerate(ip.split('.'))])


class topo_json():
    templates_all = {
        "user":"",
        "topo":"",
        "networks":{
            "controllers":{},
            "routers":{},
            "switches":{},
            "hosts":{},
            "links":{}
        }
    }

    templates_switch = {
        "config":{
            "controllers":[],
            "stp":True
        },
        "image_name":"switch/ovs",
        "linestyle":"solid",
        "resource_limit":{"cpu":"","mem":""},
        "subtype":"ovs",
        "type":"switch",
        "name":"s",
        "x":0,
        "y":0
    }

    templates_host = {
        "name":"h",
        "config":{},
        "gateway":"",
        "image_name":"host/ubuntu",
        "interfaces":[],
        "linestyle":"solid",
        "resource_limit":{"cpu":"100","mem":"1000"},
        "subtype":"ubuntu",
        "type":"host",
        "x":0,
        "y":0
    }

    templates_interface = {
        "ip":"",
        "name":"",
        "netmask":""
    }

    templates_link = {
        "name":"l",
        "source":"",
        "sourceIP":"",
        "sourceType":"",
        "target":"",
        "targetIP":"",
        "targetType":"",
        "config":{
            "source":{
                "bw_kbit":"",
                "queue_size_byte":"",
                "delay_us":"",
                "loss_rate":"",
                "jitter_us":"",
                "correlation":"",
                "delay_distribution":"normal"
            },
            "target":{
                "bw_kbit":"",
                "queue_size_byte":"",
                "delay_us":"",
                "loss_rate":"",
                "jitter_us":"",
                "correlation":"",
                "delay_distribution":"normal"
            }
        }
    }

    def __init__(self, user_name, topo_name, ip, netmask, res_lim, nodes, lines):
        self.user = user_name            # 用户名
        self.topo = topo_name            # 拓扑名
        self.ip = ip                     # 网络 ip 段
        self.netmask = netmask           # 掩码长度
        self.resource_limit = res_lim    # 资源大小
        
        self.nodes       = nodes
        self.controllers = [node[0] for node in nodes if node[1] == 'controller']  # 所有 host 类型的节点
        self.routers     = [node[0] for node in nodes if node[1] == 'router']      # 所有 host 类型的节点
        self.switches    = [node[0] for node in nodes if node[1] == 'switch']      # 所有 host 类型的节点
        self.hosts       = [node[0] for node in nodes if node[1] == 'host']        # 所有 host 类型的节点
        self.links       = lines               # 链路

        self.dec_ip = ip_2_dec(self.ip)
        if self.dec_ip % (2 ** (32 - self.netmask)):  # 先验网络号是否正确
            raise Exception("网络号填写错误！")
        self.ip_count = 1  # ip 分配从 x.x.x.2 开始分配

    def ip_distribute(self):
        self.ip_count += 1
        if self.ip_count >= 2 ** (32 - self.netmask) - 1:
            raise Exception("网络段不够分配！")
        return dec_2_ip(self.dec_ip  + self.ip_count)                    

    def controllers_json_create(self):
        return {}

    def routers_json_create(self):
        return {}

    def switches_json_create(self):
        switches_json = {}        # 初始时建立空字典
        for id in self.switches:  # 对于每一个 switch 类型节点
            node_name = 's' + id  # 节点名
            switch_json = copy.deepcopy(topo_json.templates_switch)  # 为单个节点创建一个小字典，并进行修改
            switch_json['resource_limit']['cpu'] = self.resource_limit[0]
            switch_json['resource_limit']['mem'] = self.resource_limit[1]
            switch_json['name'] = node_name
            switches_json[node_name] = switch_json
        return switches_json

    def hosts_json_create(self):
        hosts_json = {}        # 初始时建立空字典
        for id in self.hosts:  # 对于每一个 host 类型节点
            node_name = 'h' + id  # 节点名
            host_json = copy.deepcopy(topo_json.templates_host)  # 为单个节点创建一个小字典，并进行修改
            host_json['resource_limit']['cpu'] = self.resource_limit[0]
            host_json['resource_limit']['mem'] = self.resource_limit[1]
            host_json['name'] = node_name

            for link in self.links:  # 在所有链路里查找包含该节点的，分配ip
                for i in range(2):
                    if link[i] == id:
                        for node in self.nodes:
                            if link[1 - i] == node[0]:  # 查找对端类型
                                opposite_type = node[1]
                                break
                        interface_json = copy.deepcopy(topo_json.templates_interface)
                        interface_json['ip'] = self.ip_distribute()
                        interface_json['name'] = node_name + opposite_type[0] + link[1 - i]
                        interface_json['netmask'] = dec_2_ip(2 ** 32 - 2 ** (32 - self.netmask))
                        host_json['interfaces'].append(interface_json)
            
            hosts_json[node_name] = host_json
       
        return hosts_json

    def links_json_create(self):
        links_json = {}        # 初始时建立空字典
        for i, li in enumerate(self.links):  # 对于每一个链路
            link_json = copy.deepcopy(topo_json.templates_link)
            link_json['name'] = 'l' + str(i + 1)

            for node in self.nodes:
                if li[0] == node[0]:  # 查找source类型
                    source_type = node[1]
                    break
            link_json['sourceType'] = source_type
            link_json['source'] = source_type[0] + li[0]

            for node in self.nodes:
                if li[1] == node[0]:  # 查找target类型
                    target_type = node[1]
                    break
            link_json['targetType'] = target_type
            link_json['target'] = target_type[0] + li[1]

            links_json[link_json['name']] = link_json
        return links_json
            
    def all_json_create(self):
        all_json = topo_json.templates_all.copy()
        all_json['user'] = self.user
        all_json['topo'] = self.topo
        all_json['networks']['controllers'] = self.controllers_json_create()
        all_json['networks']['routers'] = self.routers_json_create()
        all_json['networks']['switches'] = self.switches_json_create()
        all_json['networks']['hosts'] = self.hosts_json_create()
        all_json['networks']['links'] = self.links_json_create()
        return json.dumps(all_json)


class JsonConvertAPI(MethodView):
    """
    文本转json拓扑文件的api
    """
 
    def post(self):
        lines = []  # 记录连结，节点数 * 2 的列表，分别为连结两端的节点标号
        nodes = []  # 记录节点，节点数 * 2 的列表，分别为节点编号和节点类型，默认为 host

        # 获取上传文件的二进制流
        req_user = request.values.get('user')
        req_topo = request.values.get('topo')
        FLASK_LOGGER.debug(f'{req_user} {req_topo}')

        req_netIP = request.values.get('ip')
        req_mask = int(request.values.get('mask'))
        req_resc = (str(request.values.get('cpu')), str(request.values.get('memory')))
        req_file = request.files.get('file')
        if req_file is None:
            return {'msg':"文件上传失败"}

        req_file.save('Kdl.txt')  #保存文件

        with open("Kdl.txt", "r", encoding='utf-8') as f:  # 打开文件
            for line in f.readlines():    # 读取一行
                line = line.strip('\n')   # 去掉列表中每一个元素的换行符
                elems = line.split()      # 一行内空格分开的所有元素

                if len(elems) > 2 and contain_number(''.join(elems)):  # 判断是否为有效行
                    lines.append(elems[:])
            
        for line in lines:
            node_type = contain_alpha(''.join(line))  # 获得节点类型
            if len(node_type) == 0:
                node_type = ['host', 'host']
            for i in range(2):
                if [line[i], node_type[i]] not in nodes:
                    nodes.append([line[i], node_type[i]])
        
        return topo_json(req_user, req_topo, req_netIP, req_mask, req_resc, nodes, lines).all_json_create()

 
    def delete(self):
        """
        处理删除服务的HTTP请求
        """
        return {'msg': 'this url can be routed', 'code': 1}

   
    def get(self):
        """
        处理GET服务的HTTP请求
        """
        return {'msg': 'this url can be routed', 'code': 1}
