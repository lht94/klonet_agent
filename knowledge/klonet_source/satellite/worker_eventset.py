"""
worker - 卫星事件执行
"""

from .satool import *
from traceback import print_exc


#################### 通用方法 ####################
def docker_exec(dev_id, cmd):
    """
    t: 虚拟时刻在某容器内运行命令
    """
    return shell_execute(f"docker exec {dev_id} {cmd}")

def get_pid(dev_id):
    """
    （事件执行）获取容器主进程的 pid
    """
    return shell_execute(
        f"docker inspect {dev_id} | grep 'Pid\"' | sed 's/[^0-9]//g'")

################# 和日志记录相关 #################
def ctn_satlog(dev_id, msg):
    """
    （事件执行）在容器中写入卫星相关日志信息
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
        msg: 写入字符串
    """
    docker_exec(
        dev_id, 
        f'sh -c \"echo \'{msg}\' >> {PROJ_CONFIG.container_log_file}\"')

################## 和tc配置相关 ##################
def tc_create(dev_id, ne, bw, qsize, delay, loss):
    """
    （事件执行）链路新增tc配置

    Args:
        dev_id: 设备容器id
        ne: 网卡名
        bw: 带宽配置值
        qsize: 队列大小配置值
        delay: 延迟配置值
        loss: 丢包配置值
    """
    prefix = f"sudo nsenter -t {get_pid(dev_id)} --net"
    shell_execute(
        f"{prefix} tc qdisc replace dev {ne}"
        f" root handle 5:0 tbf rate {bw}kbit"
        f" burst {bw/1000}kb limit {qsize}b")
    shell_execute(
        f"{prefix} tc qdisc replace dev {ne}"
        f" parent 5:0 handle 10:0"
        f" netem limit 100 delay {delay}us {loss}")        

################# 和链路删除相关 #################
def veth_delete(dev1_id, dev2_id, ne1, ne2, 
                type1, type2, ne_up_down):
    """
    （事件执行）veth-pair 删除命令

    Args:
        dev1_id、dev2_id: veth-pair 两端容器id
        ne1, ne2: veth-pair 两端网卡名
        type1, type2: veth-pair 两端容器类型
        ne_up_down: 布尔值，若为True，则使用网卡up/down模拟增删链路
    """
    # 获得容器pid
    pid1, pid2 = get_pid(dev1_id), get_pid(dev2_id)
    # 对两端网卡进行up/down，或删除veth-pair
    if ne_up_down:
        shell_execute(f"sudo nsenter -t {pid1} --net ifconfig {ne1} down")
        shell_execute(f"sudo nsenter -t {pid2} --net ifconfig {ne2} down")
    else:
        shell_execute(f"sudo nsenter -t {pid1} --net ip link delete {ne1}")
    # 删除ovs网桥端口
    if type1 == "switch":
        docker_exec(dev1_id, f"ovs-vsctl --if-exists del-port init-br0 {ne1}")
    if type2 == "switch":
        docker_exec(dev2_id, f"ovs-vsctl --if-exists del-port init-br0 {ne2}")

def vxlan_delete(dev_id, ne, ne_up_down,
                 ovs_name):    # vxlan 额外传入参数
    """
    （事件执行）vxlan 删除命令

    Args:
        dev_id: vxlan在本worker上的容器id
        ne: 容器中的网卡名
        ne_up_down: 布尔值，若为True，则使用网卡up/down模拟增删链路
        ovs_name: vxlan交换机名
    """
    shell_cmd_prefix = f"sudo nsenter -t {get_pid(dev_id)} --net "
    
    # 对两端网卡进行 up/down
    if ne_up_down:
        shell_execute(shell_cmd_prefix + f"ifconfig {ne} down")
    
    # 删除 vxlan
    else:
        # 从 OVS 删除端口
        shell_execute(f"sudo {os.getcwd()}/vemu_uestc/Implement_layer/ovs-docker.sh"
                        f" del-port {ovs_name} {ne} {dev_id}")
        # 删除 OVS 桥接
        shell_execute(f"sudo ovs-vsctl --if-exists del-br {ovs_name}")

################# 和链路创建相关 #################
def veth_create(dev1_id, dev2_id, 
                ne1, ne2, ip1, ip2, mask,
                sat_identity, ne_up_down):
    """
    （事件执行）veth 创建命令，并配置链路两侧ip

    Args:
        dev1_id, dev1_id: 容器id
        ne1, ne2: 容器中的网卡名
        ip1, ip2, mask: veth-pair两端网卡ip及掩码
        sat_identity: 卫星身份
        ne_up_down: 布尔值，若为True，则使用网卡up/down模拟增删链路
    """
    pid2 = get_pid(dev2_id)
    shell_cmd_prefix1 = f"sudo nsenter -t {get_pid(dev1_id)} --net "
    shell_cmd_prefix2 = f"sudo nsenter -t {pid2} --net "
    
    # 方案选择
    #  | ne_up_down | ne exist    | 方案 (up_down)   |
    #  | √ 已使能   | √ 网卡已存在 | √ 进行up/down     |
    #  | √ 已使能   | x 网卡不存在 | x 不进行up/down   |
    #  | x 未使能   | 无论网卡状态 | x 不进行up/down   |
    up_down = ne_up_down and \
        ne1 in shell_execute(shell_cmd_prefix1 + "ip link show")
    
    # 进行网卡 up/down
    if up_down:
        shell_execute(shell_cmd_prefix1 + f"ifconfig {ne1} up")
        shell_execute(shell_cmd_prefix2 + f"ifconfig {ne2} up")
    
    # 不进行网卡 up/down，创建 veth
    else:
        shell_execute(shell_cmd_prefix1 + f"ip link add {ne1} "
                        f"type veth peer name {ne2} netns {pid2}")
        shell_execute(shell_cmd_prefix1 + f"ip link set {ne1} up")
        shell_execute(shell_cmd_prefix2 + f"ip link set {ne2} up")
        # 配置ip
        if sat_identity == "router":
            docker_exec(dev1_id, f"ifconfig {ne1} {ip1} netmask {mask}")
            docker_exec(dev2_id, f"ifconfig {ne2} {ip2} netmask {mask}")
    
    # 新增ovs的端口
    if sat_identity == "switch":
        docker_exec(dev1_id, f"ovs-vsctl add-port init-br0 {ne1}")
        docker_exec(dev2_id, f"ovs-vsctl add-port init-br0 {ne2}")

    # 回调：获得相应网卡的mac地址
    # mac1 = shell_execute(shell_cmd_prefix1 + f"ifconfig {ne1} "
    #                      "| grep ether | awk '{{print $2}}'")
    # mac2 = shell_execute(shell_cmd_prefix2 + f"ifconfig {ne2} "
    #                      "| grep ether | awk '{{print $2}}'")

def vxlan_create(dev_id, ne, sat_identity, ne_up_down,
                 ovs_name, vni, remote_ip,  # vxlan 额外传入参数
                 ne_ip=""): 
    """
    （事件执行）vxlan 删除命令，并配置网卡ip

    Args:
        dev_id: vxlan在本worker上的容器id
        ne: 容器中的网卡名
        sat_identity: 卫星身份
        ne_up_down: 布尔值，若为True，则使用网卡up/down模拟增删链路
        ovs_name: vxlan交换机名
        vni: vxlan ID
        remote_ip: 远端宿主机ip
        ne_ip: 端口ip，格式为 x.x.x.x/xx
    """
    shell_cmd_prefix = f"sudo nsenter -t {get_pid(dev_id)} --net "
    
    # 创建 vxlan
    #  | ne_up_down | ne exist    | output (up_down) |
    #  | √ 已使能   | √ 网卡已存在 | √ 进行up/down     |
    #  | √ 已使能   | x 网卡不存在 | x 不进行up/down   |
    #  | x 未使能   | 无论网卡状态 | x 不进行up/down   |
    up_down = ne_up_down and ne in shell_execute(shell_cmd_prefix + \
                                                    f"ip link show")
    
    # 进行up/down，对两端网卡
    if up_down:
        shell_execute(shell_cmd_prefix + f"ifconfig {ne} up")
    
    # 不进行up/down，创建vxlan
    else:
        # OVS 网卡名
        ovs_ne = generate_uuid_len_10()

        # 创建 OVS 桥接
        shell_execute(f"sudo ovs-vsctl add-br {ovs_name}"
                        f"-- set Bridge {ovs_name} stp_enable=true")

        # 添加端口到 OVS
        shell_execute(f"sudo {os.getcwd()}/vemu_uestc/Implement_layer/ovs-docker.sh"
                        f" add-port {ovs_name} {ne} {dev_id} --mtu=1450"
                        f" {('--ipaddress=' + ne_ip) if sat_identity == 'router' else ''}")
        
        # 配置 VXLAN 接口
        shell_execute(
            f"sudo ovs-vsctl add-port {ovs_name} {ovs_ne}"
            f" -- set Interface {ovs_ne} type=vxlan"
            f"  options:remote_ip={remote_ip}"
            f"  options:key={vni}   options:dst_port=8472"
        )

    # 新增ovs的端口
    if sat_identity == "switch":
        docker_exec(dev_id, f"ovs-vsctl add-port init-br0 {ne}")

    # 回调：获得相应网卡的mac地址
    # mac = shell_execute(shell_cmd_prefix1 + f"ifconfig {ne1} "
    #                     "| grep ether | awk '{{print $2}}'")

################# 和链路迁移相关 #################
def veth_move(dev_from_id, dev_to_id, dev_stable_id,
              ne, type_from, type_to, ip, mask):
    """
    （事件执行）veth 换绑

    Args:
        dev_from_id: 迁出容器id
        dev_to_id: 迁入容器id
        dev_stable_id: 不动侧容器id
        ne: 被迁移的网卡
        type_from: 迁出容器类型
        type_to: 迁出容器类型
        ip, mask: 迁移网卡的ip
    """
    from_pid = get_pid(dev_from_id)
    to_pid = get_pid(dev_to_id)
    cmd_prefix_from = f"sudo nsenter -t {from_pid} --net "
    cmd_prefix_to = f"sudo nsenter -t {to_pid} --net "

    # veth 迁移
    shell_execute(cmd_prefix_from + f"ip link set {ne} netns {to_pid}")
    shell_execute(cmd_prefix_to + f"ip link set {ne} up")

    # 加入ovs网桥，端口序号自增
    if type_from == "switch":
        docker_exec(dev_from_id, f"ovs-vsctl del-port init-br0 {ne}")
    if type_to == "switch":
        docker_exec(dev_to_id, f"ovs-vsctl add-port init-br0 {ne}")
    
    # 若需进行ip配置
    if ip != "":
        docker_exec(dev_to_id, f"ifconfig {ne} {ip} netmask {mask}")

    """
    回调：
    if type_to == "switch":
        link_data['port'] = shell_execute(
            f"sudo docker exec {dev_to_id} ovs-ofctl show init-br0"
            f" | grep {ne} | sed 's/(.*//'")
    """  

def vxlan_move(dev_from_id, dev_to_id,
               ne, type_from, type_to,
               ovs_name,  # vxlan 额外传入参数
               ip, mask):
    """
    （事件执行）vxlan 换绑

    Args:
        dev_from_id: 迁出容器id
        dev_to_id: 迁入容器id
        ne: 被迁移的网卡
        type_from: 迁出容器类型
        type_to: 迁出容器类型
        ovs_name: vxlan交换机名
        ip, mask: 迁移网卡的ip
    """
    # ip配置字段
    ip_addr = (f'--ipaddress={ip}/{netmask2cidr(mask)}') if ip != "" else ""

    # vxlan 迁移
    # 删除绑定到 OVS 的端口
    shell_execute(f"sudo {os.getcwd()}/vemu_uestc/Implement_layer/ovs-docker.sh"
                    f" del-port {ovs_name} {ne} {dev_from_id}")
    # 添加新端口到 OVS，并配置ip
    shell_execute(f"sudo {os.getcwd()}/vemu_uestc/Implement_layer/ovs-docker.sh"
                    f" add-port {ovs_name} {ne} {dev_to_id} --mtu=1450 {ip_addr}")

    # 加入ovs网桥
    if type_from == "switch":
        docker_exec(dev_from_id, f"ovs-vsctl del-port init-br0 {ne}")
    if type_to == "switch":
        docker_exec(dev_to_id, f"ovs-vsctl add-port init-br0 {ne}")

    """
    回调：
    if type_to == "switch":
        link_data['port'] = shell_execute(
            f"sudo docker exec {dev_to_id} ovs-ofctl show init-br0"
            f" | grep {ne} | sed 's/(.*//'")
    """

################ 和 IP / 路由相关 ################
def ip_config(dev_id, ip, ne="default"):
    """
    （事件执行）配置地面站ip

    Args:
        dev_id: 容器id
        ip: 配置的ip地址
        ne: 网卡名
    """
    # 网卡名
    if ne == "default":
        ne = docker_exec(dev_id, "ifconfig | grep to | awk '{print $1}'")[:-1]
    # 修改ip
    docker_exec(dev_id, f"ifconfig {ne} {ip} netmask {PROJ_CONFIG.sat_gnd_subnet_mask}")

def gw_config(dev_id, gw):
    """
    （事件执行）配置新网关，删除旧网关

    Args:
        dev_id: 容器id
        ip: 配置的网关地址
    """
    # 原来没有网关时，屏蔽删除网关命令的报错
    try: docker_exec(dev_id, f'ip route del default')
    except: pass
    # 配置新网关
    docker_exec(dev_id, f'route add default gw {gw}')

def rt_config(dev, dev_id, info, protocol):
    """
    （事件执行）路由协议配置
    """
    container = docker_cli.containers.get(dev_id)
    container.exec_run(f"sh -c 'kill $(cat /var/run/quagga/{protocol}d.pid)'")
    qr = QuaggaRunner(dev, info, container)
    protocol_func = getattr(qr, f'_{protocol}_conf')
    protocol_func()

################### 和DHCP相关 ###################
def start_dhcp_server(dev_id, ne, subnet_int, broadcast_ip_int):
    """
    （事件执行）开启dhcp服务
    预先已运行以下命令
        - chmod 777 /tmp
        - apt update
        - apt install isc-dhcp-server
    
    Args:
        dev_id: 容器id
        ne: 网卡名
        subnet_int: 子网网络号对应的int
        broadcast_ip_int: 网段内广播ip
    """
    # 修改 /etc/dhcp/dhcpd.conf
    content = \
        'option domain-name \\' + '"example.org\\' + '";default-lease-time 600;' + \
        'max-lease-time 7200;ddns-update-style none;' + 'subnet ' + \
        int2ip(subnet_int) + ' netmask ' + PROJ_CONFIG.sat_gnd_subnet_mask + \
        '{\n    range ' + int2ip(subnet_int + 2) + ' ' + int2ip(broadcast_ip_int - 1) + \
        ';\n    option routers ' + int2ip(subnet_int + 1) + \
        ';\n    option subnet-mask ' + PROJ_CONFIG.sat_gnd_subnet_mask + \
        ';\n    option broadcast-address ' + int2ip(broadcast_ip_int) + \
        ';\n    option domain-name-servers ' + int2ip(subnet_int + 1) + \
        ';\n}'
    docker_exec(dev_id, f"sh -c \"echo '{content}' > /etc/dhcp/dhcpd.conf\"")
    
    # 修改 /etc/default/isc-dhcp-server
    content = 'INTERFACESv4=\\' + f'"to{ne}\\' + '"\n' + 'INTERFACESv6=\\"\\"'
    docker_exec(dev_id, f"sh -c \"echo '{content}' > /etc/default/isc-dhcp-server\"")
    
    # 启动服务
    docker_exec(dev_id, "service isc-dhcp-server restart")

def start_dhcp_client(dev_id, ip):
    """
    （事件执行）开启dhcp客户端连接

    Args:
        dev_id: 容器id
        ip: 小子网中的一个ip，用来求出卫星上dhcp服务器的ip
    
    Returns:
        dict，其中code字段说明是否成功
    """
    # 撤销DHCP参数，释放DHCP租约
    docker_exec(dev_id, "dhclient -r")

    # 若有新卫星连接，则更新DHCP参数
    if ip:
        # 卫星上dhcp服务器的ip
        server_ip = int2ip((ip & sat_gnd_subnet_mask_int) + 1)
        # 修改 /etc/resolv.conf
        docker_exec(dev_id, f"echo 'nameserver {server_ip}' > /etc/resolv.conf")
        # 启动服务
        docker_exec(dev_id, "dhclient")

def stop_dhcp_server(dev_id):
    """
    （事件执行）关闭dhcp服务
    
    Args:
        dev_id: 容器id
    """
    docker_exec(dev_id, "service isc-dhcp-server stop")

def stop_dhcp_client(dev_id):
    """
    （事件执行）关闭dhcp客户端连接

    Args:
        dev_id: 容器id
    """
    # 撤销DHCP参数，释放DHCP租约
    docker_exec(dev_id, "dhclient -r")

################### 和隧道相关 ###################
def _get_existed_tunnel(dev_id):
    """
    获取设备上已存在的隧道
    """
    return [t[: -1] for t in \
            docker_exec(dev_id, "ip tunnel show | awk '{print $1}'").split()]

def create_tunnel(dev_id, tunnel_name,
                  ip_in, ip_in_peer, ip_out, ip_out_peer):
    """
    （事件执行）创建隧道
    
    Args:
        dev_id: 容器id
        tunnel_name: 隧道名
        ip_in, ip_in_peer: 本端和对端内网ip
        ip_out, ip_out_peer: 本端和对端外网ip
    """
    # 创建隧道（外网）
    docker_exec(dev_id, f"ip tunnel add {tunnel_name}"
                        f" mode {PROJ_CONFIG.tunnel_mode}"
                        f" remote {ip_out_peer} local {ip_out} ttl 255")
    # 添加隧道的接口地址（内网）
    docker_exec(dev_id, f"ip addr add dev {tunnel_name}"
                        f" {ip_in} peer {ip_in_peer}")
    # 开启隧道虚拟网卡
    docker_exec(dev_id, f"ip link set {tunnel_name} up")

def change_tunnel(dev_id, ip_out, peer_ip_out, tunnel_name):
    """
    （事件执行）修改隧道
    
    Args:
        topo: 拓扑名
        user_db_cli: redis数据库的用户db
        dev: 设备名称
        peer_dev: 隧道对端设备
        ip_out: 设备外网IP，不带cidr的斜杠格式
        link_name: 链路名称
        exec_already: 主机内执行部分代码仅执行一次
    """
    if tunnel_name in _get_existed_tunnel(dev_id):
        docker_exec(dev_id, 
                    f"ip tunnel change {tunnel_name}"
                    f" mode {PROJ_CONFIG.tunnel_mode}"
                    f" remote {peer_ip_out} local {ip_out} ttl 255")
    else:
        print('隧道不存在')

def delete_tunnel(dev_id, tunnel_name):
    """
    （事件执行）删除隧道
    
    Args:
        dev_id: 容器id
        tunnel_name: 隧道名
    """
    if tunnel_name in _get_existed_tunnel(dev_id):
        docker_exec(dev_id, f"ip tunnel del {tunnel_name}")

############### 和celery异步注册相关 ##############
func_map = {
    "docker_exec": docker_exec,
    "ctn_satlog": ctn_satlog,
    "tc_create": tc_create,
    "veth_delete": veth_delete,
    "vxlan_delete": vxlan_delete,
    "veth_create": veth_create,
    "vxlan_create": vxlan_create,
    "veth_move": veth_move,
    "vxlan_move": vxlan_move,
    "ip_config": ip_config,
    "gw_config": gw_config,
    "rt_config": rt_config,
    "start_dhcp_server": start_dhcp_server,
    "start_dhcp_client": start_dhcp_client,
    "stop_dhcp_server": stop_dhcp_server,
    "stop_dhcp_client": stop_dhcp_client,
    "create_tunnel": create_tunnel,
    "change_tunnel": change_tunnel,
    "delete_tunnel": delete_tunnel,
}

@celery.task(track_started=True)
def celery_asy_func(user, topo, func, para) -> None:
    """
    注册上述函数为异步函数，可异步执行
    """
    try:
        func_map[func](**para)
    except:
        if check_table_existence(user, f'{topo}{PROJ_CONFIG.sat_table_name}'):
            print_exc()
