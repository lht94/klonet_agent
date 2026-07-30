import requests
import json
# from ryu.base import app_manager
# from ryu.controller import ofp_event
# from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
# from ryu.controller.handler import set_ev_cls
# from ryu.ofproto import ofproto_v1_3
# from ryu.lib.packet import packet
# from ryu.lib.packet import ethernet, arp
# from ryu.lib.packet import ether_types
# from ryu.topology import event

class Topo(object):
    '''
    用于从服务器获得拓扑信息，主要包括交换机dpid，主机mac地址，链路端口信息

    '''
    def __init__(self,base_url="http://192.168.1.124:10014",
                 user="sw",topo="ryu_test"):
        self.base_url=base_url  # 替换为你的服务器IP
        self.user = user  # 替换为用户名
        self.topo = topo  # 替换为拓扑名称
        self.data ={
            "user": self.user,
            "topo": self.topo
        }
        self.switch_dpid=self.get_switch_dpid()
        self.host_mac=self.get_host_mac()
        self.link_port=self.get_link_port()
        

    def get_switch_dpid(self):
        url = f"{self.base_url}/switch_dpid/"
        response = requests.post(url, 
                                 data=json.dumps(self.data))
        switch_dpid = response.json()
        return switch_dpid
    
    def get_host_mac(self):
        url = f"{self.base_url}/host_mac/"
        response = requests.post(url, 
                                 data=json.dumps(self.data))
        host_mac = response.json()
        return host_mac
    
    def get_link_port(self):
        url = f"{self.base_url}/link_port/"
        response = requests.post(url, 
                                 data=json.dumps(self.data))
        link_port = response.json()
        return link_port


# class Flowschedule(app_manager.RyuApp):
#     pass

if __name__ == "__main__":
    topo_info=Topo(base_url="http://192.168.1.124:10014",topo="sdn")
    print(topo_info.host_mac)
    print(topo_info.switch_dpid)
    print(topo_info.link_port)
