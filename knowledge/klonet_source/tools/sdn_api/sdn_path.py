'''
SDN路径定制脚本。可实现ovs+host拓扑的路径定制。如以下拓扑
             s1
            /  \
           /     \
    h1---s2--------s3----h2
        /
       /
    h3
    
    可实现输入为[[h1, s2, s3, h2], [h3, s2, s1, s3, h2]]，实现流的路径定制。

运行方式：
    - 将此脚本（及vemu_api）上传至控制器，使用ryu-manager sdn_path.py运行
    - 若运行正确，则所打印信息的最后一行应为：Topology rediscovery done
'''
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet, arp
from ryu.lib.packet import ether_types
from ryu.topology import event
import vemu_api
from vemu_api import common
from pprint import pprint

# 请启动此控制器脚本后，再进行ping操作，避免引起广播风暴

# 请确保该IP和PORT可达
MASTER_IP = "192.168.1.124"
MASTER_PORT = "10021"

# 请确保用户名和拓扑名正确
user_name = "sw"
project_name = "demo_test"

# 注意：
# 1. 路径的首尾需要都是平台的host类型的节点
# 2. 两host之间不应有多条路径
# 3. 如有需要（通常都需要），请记得在两host间添加反向路径（如已有h1->h2的路径，记得要
#    添加h2->h1的路径），否则ping iperf等应用将因没有返回的路由而无法正常工作
paths = [
    ["h3", "s2", "s1", "s3", "h2"],
    ["h2", "s3", "s4", "s2", "h3"],
    ["h1", "s2", "s3", "h2"],
    ["h2", "s3", "s2", "h1"],
    ["h1", "s2", "h3"],
    ["h3", "s2", "h1"]
]

node_manager = vemu_api.NodeManager(user_name, project_name,
    backend_ip=MASTER_IP, backend_port=MASTER_PORT)
cmd_manager = vemu_api.CmdManager(user_name, project_name,
    backend_ip=MASTER_IP, backend_port=MASTER_PORT)
project_manager = vemu_api.ProjectManager(user_name, MASTER_IP, MASTER_PORT)

class Topo(object):
    def __init__(self):
        self.nodes = node_manager.get_nodes()
        self.datapaths = None
        self.host2mac =  {}
        self.mac2host =  {}
        self.switches = self.get_switch2dpid()
        self.switch_port = None
        self.host2ip = {}
        self.ip2host = {}
        self.paths = paths
        self.path_dict = self._convert2_path_dict(paths)

        # FLAG
        self.show_topo = 1

    def get_switch2dpid(self):
        '''
        获取交换机与dpid的对应关系

        Args:
            None

        Returns:
            {
                "<交换机名>": "<dpid(16进制)>",
                ...
            }
        '''
        switch2dpid = {}
         
        for node_name, node_obj in self.nodes.items():
            print(f"{node_name}:{node_obj.dictform()}")
            if node_obj.type == "switch":
                switch2dpid[node_name] = node_obj.config["dpid"]

        return switch2dpid

    def get_host2mac(self):
        '''
        获取拓扑中所有host类型节点的mac地址。注意：本函数只适应于Host类型只有一张网卡
        的情况，若网卡大于一张，将抛出异常。

        Args:
            None

        Returns:
            host2mac: 一个字典，包含各host节点与其mac地址的对应关系。格式为：
                {
                    "<host类型节点1名称，如h1>": "<mac地址，如 00:15:5d:c3:e7:31>",
                    ...
                }

        Raises:
            RuntimeError: 当节点的网卡大于一张时，触发此异常
        '''
        host2mac = {}

        # 获取host类型的节点列表
        hosts = []
        for node_name, node_obj in self.nodes.items():
            if node_obj.type == "host":
                hosts.append(node_name)

        node2cmds = {}
        cmd = "ls /sys/class/net/"
        for host in hosts:
            node2cmds[host] = [cmd]

        result = cmd_manager.exec_cmds_in_nodes(node2cmds)
        
        for host, exec_result in result.items():
            # output形式：a84739935a\nlo\n
            output = exec_result[cmd]["output"]
            # 去除尾部回车并切割。nic_list形式：['a84739935a', 'lo']
            nic_list = output.strip("\n").split("\n")
            try:
                nic_list.remove("lo")
                nic_list.remove("eth0")
            except:
                # 有就移除，没有也不应该报错
                pass
            if len(nic_list) > 1:
                raise RuntimeError(f"host节点网卡数量仅支持1个，而节点<{host}>有"
                    f"{len(nic_list)}个网卡：{nic_list}")

            node2cmds[host] = [f"cat /sys/class/net/{nic_list[0]}/address"]

        result = cmd_manager.exec_cmds_in_nodes(node2cmds)
        for host, exec_result in result.items():
            # output形式：5e:ee:bd:f8:08:67\n
            output = exec_result[node2cmds[host][0]]["output"].strip("\n")
            host2mac[host] = output

        return host2mac

    def get_mac2host(self, host2mac=None):
        '''
        获取拓扑中所有host类型节点的mac地址对应的节点名。注意：本函数只适应于Host类型
        只有一张网卡的情况，若网卡大于一张，将抛出异常。

        Args:
            None

        Returns:
            mac2host: 一个字典，包含各host节点与其mac地址的对应关系。格式为：
                {
                    "<host类型节点1名称，如h1>": "<mac地址，如 00:15:5d:c3:e7:31>",
                    ...
                }

        Raises:
            RuntimeError: 当节点的网卡大于一张时，触发此异常
        '''
        if not host2mac:
            host2mac = self.get_host2mac()
        mac2host = self._invert_k_v(host2mac)

        return mac2host

    def get_switch_nic2port(self, switch_names:list):
        '''
        获取交换机网卡名与ovs port的对应关系

        Args:
            switch_names: 交换机名列表

        Returns:
            switch_nic2port: 列表中交换机所有网卡名对应的ovs port。格式为：
                {
                    "<交换机名，如s1>": {
                        "<交换机网卡1的名称，如s1h1>": "<ovs port，如1>",
                    },
                    ...
                }
        '''
        switch_nic2port = {}
        
        cmd = "bash -c 'ovs-ofctl show init-br0 | grep addr'"
        node2cmds = {}
        for switch_name in switch_names:
            node2cmds[switch_name] = [cmd]
        # pprint(node2cmds)
        result = cmd_manager.exec_cmds_in_nodes(node2cmds)
        # pprint(result)

        for switch_name, exec_result in result.items():
            output = exec_result[cmd]["output"]
            port_list = [port.strip().split(':')[0] for port in output.split("\n")]
            # print(port_list)
            switch_nic2port[switch_name] = {}
            for port in port_list:
                # 切割后最后一个字符串为空
                if port and "LOCAL" not in port:
                    port_num = port.split('(')[0]
                    nic_realname = port.split('(')[1].split(')')[0]
                    nic_nickname = node_manager.get_nic_realname2nickname(
                        switch_name)[nic_realname]
                    
                    switch_nic2port[switch_name][nic_nickname] = port_num
        
        return switch_nic2port
    
    def find_op(self, dpid, path):
        '''
        寻找出端口。op=output port

        Args:
            dpid: 当前交换机的dpid
            path: 路径 
        ''' 
        dpid_0x = hex(dpid)#进制转换
        
        len_n0 = len(dpid_0x)-2
        len_0 = 16 - len_n0
        dpid_0000 = '0' * len_0 + dpid_0x[2:]
        print("#" * 100)
        print("Decimal dpid:", dpid, "Hexadecimal dpid:", dpid_0x)
        print("dpid from sdn_info_query:", dpid_0000)
        print("#" * 100)
        op = None
        
        # 查找dpid在路径上对应的交换机
        i = 1
        while i<len(path)-1:
            s1 = path[i]
            if self.switches[s1] == dpid_0000:
                break
            #print("selfswitch{} and dpid{}".format(self.switches[s1],dpid_0000))
            i += 1 
        if i == len(path)-1:
            print(f"Can't Find this switch! (dpid={dpid_0000})")
            return 0, 0, op
        
        else:
            s2 = path[i+1] # 找到下一跳的交换机
            try:
                op = self.switch_port[s1][s1+s2] # 找到通往下一跳交换机的port
            except KeyError:
                print("!!!!!! find the next hop failed, please MAKE SURE "
                    "that the paths are right !!!!!!")
                raise KeyError
            print(f"Find next hop switch_port[{s1}][{s1}{s2}] (port={op})")
            return s1, s2, op
    
    def get_host_ip(self):
        '''
        获取host节点与ip地址的对应关系

        Args:
            None

        Returns:
            host2ip: host节点的ip地址。格式为：
                {
                    "<host节点名>": "<host的ip地址>",
                    ...
                }
            ip2host: ip地址对应的host节点名称。格式为：
                {
                    "<host的ip地址>": "<host节点名>",
                    ...
                }
        '''
        topo_info = project_manager.get_topo(project_name).dictform()
        host2ip = {}
        ip2host = {}
        hosts = topo_info['hosts']
        for key in hosts.keys():
            host2ip[key] = hosts[key]["interfaces"][0]['ip']
            if not host2ip[key]:
                raise ValueError(f"Please config ip for {key}!")
        ip2host = self._invert_k_v(host2ip)

        return host2ip, ip2host

    def _invert_k_v(self, dict_to_be_inverted):
        '''
        颠倒字典的key和value，并返回新的字典

        Args:
            dict_to_be_inverted: 待颠倒的字典

        Returns:
            inverted_dict: 颠倒的字典
        '''
        inverted_dict = {}
        for k, v in dict_to_be_inverted.items():
            inverted_dict[v] = k

        return inverted_dict

    def _convert2_path_dict(self, paths):
        '''
        将路径数组转换为以(<源host节点名>, <目的host节点名>)为key的字典，便于检索

        Args:
            paths: 路径数组。如[[h1, s1, h2], [h1, s3, h3]]

        Returns:
            path_dict: 路径字典。如
                {
                    ("h1", "h2"): [h1, s1, h2],
                    ("h1", "h3"): [h1, s3, h3]
                }
        '''
        path_dict = {}
        for path in paths:
            if len(path) < 3:
                raise ValueError("path数组的长度应至少为3，即两端的主机+至少1个"
                    "交换机！")
            if path[0] == path[-1]:
                raise ValueError("The end nodes of the path should not be the "
                    "same node!")
            key = (path[0], path[-1])
            if key in path_dict.keys():
                print("key: ", key)
                print("path_dict.keys(): ", path_dict.keys())
                raise ValueError(f"The paths between {key} should not >1 !")
            path_dict[key] = path

            # 自动添加反向路径
            # key = (path[-1], path[0])
            # path_dict[key] = list(reversed(path))

        return path_dict

class FlowRouter(app_manager.RyuApp):
    
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        self.topo = Topo()
        print("clear flow tables...")
        self.clear_flow_tables(list(self.topo.switches.keys()))
        print("clear done!")
        print("init.....")
        self.topology_api_app = self
        self.count = 0
        super(FlowRouter, self).__init__(*args, **kwargs)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        '''ovs套路代码'''
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None,
                 idle_timeout=0):
        '''ovs套路代码'''
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match,
                                    instructions=inst, 
                                    idle_timeout=idle_timeout)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, instructions=inst,
                                    idle_timeout=idle_timeout)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn,MAIN_DISPATCHER)
    def packet_in_handler(self,event):
        #print("pkt in {}".format(self.count))
        self.count = self.count + 1
        msg = event.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        arp_pkt = pkt.get_protocol(arp.arp)
        
        dst_mac = eth.dst
        src_mac = eth.src
        dpid = datapath.id
        out_port = None

        Is_arp = 0
 
        # print(f"eth.ethertype: {eth.ethertype}")
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            # print("lldp return")
            return #!! 防止泛洪
        if eth.ethertype == 38:
            #print("llc return")
            return #!! 防止泛洪
        if arp_pkt:
            Is_arp = 1
            print("arp received~")

        src_host = self.topo.mac2host.get(src_mac)
        # 广播地址ff时会为dst_host会为None
        dst_host = self.topo.mac2host.get(dst_mac)
        if src_host and dst_host:
            path_key = (src_host, dst_host)
            print(f"path_key: {path_key}")
            if path_key in self.topo.path_dict.keys():
                print("get path by mac")
                # 根据mac地址匹配路径并下发流表
                print("we get an ev from {} to {}".format(src_mac, dst_mac))
                #在这里知晓 路径需要的主机然后把对应到allpath中的一个，赋值给path
                s1, s2, out_port = self.topo.find_op(
                    dpid, self.topo.path_dict[path_key])

        elif Is_arp:
            # 根据ip地址匹配路径并下发流表
            print("arp sending~")
            path_key = (self.topo.ip2host[arp_pkt.src_ip],
                self.topo.ip2host[arp_pkt.dst_ip])
                #print("choose path1 to send arp")
            _, _, out_port = self.topo.find_op(
                dpid, self.topo.path_dict[path_key])
        
        if out_port == None:
            print("Can't find the way to send pkt ")
            return
        else:
            out_port = int(out_port)
             
        actions = [parser.OFPActionOutput(out_port)]
 
        # 如果执行的动作不是flood，那么此时应该依据流表项进行转发操作，所以需要添加流表到交换机
        if out_port!=ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst_mac, eth_src=src_mac)
            if not Is_arp:
                print("from {} to {} the out_port is {}".format(s1,s2,out_port))
                print(" ")
            
            # 针对洪泛包，在不活跃1s后删除。否则所有洪泛包都会按既定路线转发，导致
            # 源mac相同时，只能ping通一个目的mac，而ping不通其它目的mac。
            idle_timeout = 1 if dst_mac == "ff:ff:ff:ff:ff:ff" else 0
            self.add_flow(datapath=datapath, priority=1, match=match, 
                idle_timeout=idle_timeout, actions=actions)
        else:
            print("FLOOD!")
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        
        # 控制器指导执行的命令
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                            in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)


    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self,event):
        if self.topo.show_topo:
            self.logger.info("A switch entered.Topology rediscovery...")
            print("===================================")
            self.switch_status_handler()
            self.logger.info('Topology rediscovery done')
          
    def switch_status_handler(self):#在这里get各端口信息，部署路径
        '''
        在EventSwitchEnter第一次发生时触发此函数
        '''
        self.topo.host2mac = self.topo.get_host2mac()
        self.topo.mac2host = self.topo.get_mac2host(self.topo.host2mac)
        self.topo.switch_port = self.topo.get_switch_nic2port(
            list(self.topo.switches.keys()))
        #这里把算法结果展示出来并且录入topo中
        self.topo.host2ip, self.topo.ip2host = self.topo.get_host_ip()

        print("host2mac-----------------------------------------")
        pprint(self.topo.host2mac)
        print("mac2host-----------------------------------------")
        pprint(self.topo.mac2host)
        print("host2ip-----------------------------------------")
        pprint(self.topo.host2ip)
        print("ip2host-----------------------------------------")
        pprint(self.topo.ip2host)

        print("We find {} switches as follow:".format(
            len(self.topo.switches.keys())))
        print(self.topo.switches)
        print("\n------------------------------")
        links_num = 0
        for s in self.topo.switch_port.keys():
            links_num += len(self.topo.switch_port[s].values())
        print("Number of links between switches {}".format(int(links_num-2)/2))

        print("------------------------------")
        print("\nhost is :")
        for h in self.topo.host2ip.keys():
            print("{}: {}".format(h,self.topo.host2ip[h]))
        
        print("\nthe path in config is:")
        for path in self.topo.paths:
            path_str = path[0]
            for s in path:
                if s is not path[0]:
                    path_str += ' <--> {}'.format(s)
            print(path_str)
        print("===================================")


        self.topo.show_topo = 0

    def clear_flow_tables(self, switch_names:list, match_rule=""):
        '''
        通过命令执行(ovs-ofctl del-flows init-br0)的方式清空流表，防止流表缓存，
        便于更改路径
        
        Args:
            switch_names: 交换机名列表
        '''
        cmd = "ovs-ofctl del-flows init-br0 " + match_rule
        node2cmds = {}
        for switch_name in switch_names:
            node2cmds[switch_name] = [cmd]
        cmd_manager.exec_cmds_in_nodes(node2cmds)
        

if __name__ == "__main__":
    '''TEST'''
    topo_obj = Topo()
    # switch2dpids = topo_obj.get_switch2dpid()
    # topo_obj.get_switch_nic2port(list(switch2dpids.keys()))
    # topo_obj.get_host_ip()