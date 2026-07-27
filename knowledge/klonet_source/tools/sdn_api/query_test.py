import requests
import json

# 定义接口地址和参数
base_url = "http://192.168.1.124:10014"  # 替换为你的服务器IP
user = "sw"  # 替换为用户名
topo = "sdn"  # 替换为拓扑名称

# 调用获取交换机DPID的接口
switch_dpid_url = f"{base_url}/switch_dpid/"
switch_dpid_data = {
    "user": user,
    "topo": topo
}
response = requests.post(switch_dpid_url, data=json.dumps(switch_dpid_data))
switch_dpid = response.json()
print("Switch DPIDs:", switch_dpid)

# 调用获取主机MAC地址的接口
host_mac_url = f"{base_url}/host_mac/"
host_mac_data = {
    "user": user,
    "topo": topo
}
response = requests.post(host_mac_url, data=json.dumps(host_mac_data))
host_mac = response.json()
print("Host MAC addresses:", host_mac)

# 调用获取交换机端口信息的接口
link_port_url = f"{base_url}/link_port/"
link_port_data = {
    "user": user,
    "topo": topo
}
response = requests.post(link_port_url, data=json.dumps(link_port_data))
link_port = response.json()
print("Switch Port information:", link_port)
