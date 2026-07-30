# import subprocess
from gevent import subprocess
from nsenter import Namespace
import paramiko
import time
import uuid
import random
import threading
import os
import docker

from ...Service_layer.deploy_error import LinkOvsBridgePortDeleteError
from ...Service_layer.redisAPI import HardwareRedis

# TODO(MaTie): 代码风格规范化 

def shell_execute(cmd,check=True) -> str:
    '''
        输入：要执行的shell命令\n
        输出：命令执行后的标准输出\n
        功能描述：使用subprocess.run()执行shell命令
    '''
    completed_process = subprocess.run(
        cmd, 
        shell=True, # 执行shell命令
        capture_output=True, # 效果与设置stdout=PIPE, stderr=PIPE一样
        text=True, # 将stdin, stdout, stderr修改为string模式
        check=check, # 开启检查，若出错则raise CalledProcessError
        )

    #print('# ' + cmd)
    if check == True:
        return completed_process.stdout.rstrip() # 加rstrip去除字符串末尾的回车
    else:
        return completed_process.returncode

def get_interface_mac(bridge, interface) -> str:
    '''
        输入：网桥和网桥上的接口名称
        输出：该接口的mac地址
        功能描述：获取某网桥的接口的mac地址
    '''
    cmd = "sudo ovs-ofctl show " + bridge + " | grep " + interface + " | awk '{print $2}' | cut -d: -f 2-"
    mac = shell_execute(cmd)
    print('-----get_interfaace_mac-------------', mac)
    return mac
    
def get_pid(container_id) -> str:
    '''
        输入：容器id\n
        输出：网卡数目\n
        功能描述：利用grep对ifconfig -a的打印结果进行字段过滤，然后使用wc -l命令得到行数，即为当前容器网卡的数量
    '''
    return shell_execute("sudo docker inspect -f '{{.State.Pid}}' " + container_id)

def get_eth_num(container_id) -> int:
    '''
        输入：容器id\n
        输出：网卡数目\n
        功能描述：利用匹配[eth+数字]的方式对ifconfig -a的打印结果进行字段过滤，并得到结果的行数，即为当前容器以eth开头的网卡的数量
    '''
    # ^：行首匹配，[0-9]：匹配0~9数字中的一个
    # https://blog.csdn.net/lingfengliujian/article/details/78198110
    # 使用grep -c统计行数的话，若行数为0，则exit code为1。故用wc -l统计行数
    return int(shell_execute("sudo docker exec -it " + container_id + " ifconfig -a | grep '^eth[0-9]' | wc -l"))

def generate_eth_name(target_ctn, parallel) -> str:
    '''
        输入：目的网元名
        输出：网卡名称
        功能描述：网卡命令规则：网元名小于13位，命名为：to<目的网元名>，大于13位时，取为10位随机数
    '''
    if  len(target_ctn) <= 13:
        eth_name ="to"+ target_ctn + "_" + str(parallel)
    else:
        eth_name = generate_uuid_len_10()
    return str(eth_name)

def generate_uuid_len_10() -> str:
    '''
        输入：无\n
        输出：10位的随机16进制id
        功能描述：通过python的uuid模块产生10位的随机16进制id，有极低概率产生重复id(16的10次方分之一)
    '''
    return str(uuid.uuid4()).replace("-", '')[0:10]

def create_link_ctn_bridge(bridge, ctn_id, ctn_ip) -> dict:
    '''
        输入：bridge 网桥的名称，；容器的id和想要设置的ip
        输出：...
        功能描述：使用veth-pair连接一般容器和dpdk复合节点（连在standard网桥上）
    '''
    result = {}
    pid = get_pid(ctn_id)
    intf = generate_uuid_len_10()
    file_return_code = shell_execute("ls /var/run/netns/ | grep " + ctn_id, check=False)
    if file_return_code == 0:
        shell_execute("sudo ip netns del " + ctn_id)
        shell_execute("sudo ln -s /proc/" + str(pid) + "/ns/net /var/run/netns/" + ctn_id)
    else:
        shell_execute("sudo ln -s /proc/" + str(pid) + "/ns/net /var/run/netns/" + ctn_id)
    shell_execute("sudo ip link add " + intf + "0 type veth peer name " + intf + "1")
    mac0 = shell_execute("ifconfig " + intf + "0 | grep ether | awk '{print $2}'" )
    mac1 = shell_execute("ifconfig " + intf + "1 | grep ether | awk '{print $2}'" )
    shell_execute("sudo ip link set " + intf + "0 up")
    shell_execute("sudo ip link set " + intf + "1 up")
    shell_execute("sudo ovs-vsctl add-port " + bridge + " " + intf + "0")
    shell_execute("sudo ip link set " + intf + "1 netns " + ctn_id)
    shell_execute("sudo ip netns exec " + ctn_id + " ip link set dev " + intf + "1 up")
    if ctn_ip != "":
        try:
            shell_execute("sudo ip netns exec " + ctn_id + " ip addr add " + ctn_ip + " dev " + intf + "1")
        except:
            print(f"----someprob in set ip to {ctn_id}'s intf {intf}----")
    else:
        pass
    # shell_execute("sudo ip netns exec " + ctn_id + " route add -net " + ctn_ip + " netmask 255.255.255.0 dev " + intf + "1")
    result["bridge"] = {'nic': intf + '0', 'mac': mac0}
    result["ctn"] = {'nic': intf + '1', 'mac': mac1}
    return result

def create_link(src, dst, source_ctn,target_ctn, parallel, *args, src_type='ctn', dst_type='ctn') -> dict:
    '''
        输入：两端节点的id及其ip地址\n
        输出：返回一个字典。若正确执行，返回{节点id1: 网卡名1, 节点id2: 网卡名2}；若执行过程中报错，
        则字典中包含"error_msg"键值\n
        功能描述：使用veth-pair连接所给节点并配置ip地址，然后返回两端网卡名及其对应的节点id。
        调用规则：create_link(src, dst, (ip1,ip2),src_type='',dst_type=''),不指定type则为默认值ctn,
    '''
    result = {}
    if src_type == 'ctn' and dst_type == 'ctn':
        try:
            # 获取要创建的网卡名和容器pid
            # intf_1 = "to" + str(目的容器即容器2的名) + _cn（平行链路数目）
            # intf_2 = "eth" + str(目的容器即容器1的名) + _cn（平行链路数目）
            intf_1 = generate_eth_name(target_ctn, parallel)
            intf_2 = generate_eth_name(source_ctn, parallel)

            container_id_1 = src
            container_id_2 = dst
            ip_1 = args[0]
            ip_2 = args[1]
            pid_1 = get_pid(container_id_1)
            pid_2 = get_pid(container_id_2)


            #检查网卡名是否重复,如果网卡不重复，会抛出异常，此时不用做任何处理，继续创建网卡
            with Namespace(pid_1, 'net'):
                try:
                    eth_name_if_used = shell_execute("ifconfig -a|grep "+intf_1+":")
                    print(intf_1+"网卡名重复，变为10位随机数")
                    if str(eth_name_if_used) !='':
                        intf_1=generate_uuid_len_10()
                except Exception as e:
                    #这个判断是网卡名不重复的返回
                    if e.returncode == 1 and e.stderr == '' and e.stdout == '':
                        pass
                    else:
                        result['error_msg'] = "CREATE LINK ERROR when execute command '" + e.cmd + \
                "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()

            with Namespace(pid_2, 'net'):
                try:
                    eth_name_if_used = shell_execute("ifconfig -a|grep "+intf_2+":")
                    print(intf_2+"网卡名重复，变为10位随机数")
                    if str(eth_name_if_used) !='':
                        intf_2=generate_uuid_len_10()
                except Exception as e:
                    if e.returncode == 1 and e.stderr == '' and e.stdout == '':
                        pass
                    else:
                        result['error_msg'] = "CREATE LINK ERROR when execute command '" + e.cmd + \
                "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()



            with Namespace(pid_1, 'net'):
                # 在容器1中创建veth-pair并将网卡加入网卡2的namespace
                # 注意这里不能按下面的命令直接添加，因为若网卡名重复会出现问题
                # "sudo ip link add " + intf_1 + " netns " + pid_1 + " type veth peer name " + intf_2 + " netns " + pid_2
                # 参考：https://unix.stackexchange.com/questions/405805/connecting-two-network-namespaces-via-a-veth-interface-pair-where-each-endpoint        
                shell_execute("ip link add " + intf_1 + " type veth peer name " + intf_2 + " netns " + pid_2)
                # 配置ip地址
                shell_execute("ifconfig " + intf_1 + " " + ip_1 + " up")
            with Namespace(pid_2, 'net'):
                shell_execute("ifconfig " + intf_2 + " " + ip_2 + " up")

            # 这里还需要记录网卡的mac地址
            mac1 = shell_execute(f'sudo docker exec {container_id_1} bash -c "cat /sys/class/net/{intf_1}/address"')
            mac2 = shell_execute(f'sudo docker exec {container_id_2} bash -c "cat /sys/class/net/{intf_2}/address"')
            result[container_id_1] = {'nic': intf_1, 'mac': mac1}
            result[container_id_2] = {'nic': intf_2, 'mac': mac2}


        except subprocess.CalledProcessError as e:
            result['error_msg'] = "CREATE LINK ERROR when execute command '" + e.cmd + \
                "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
    elif src_type == 'bridge':
        result = create_link_ctn_bridge(src, dst, args[0])
    elif dst_type == 'bridge':
        result = create_link_ctn_bridge(dst, src, args[0])

    print(result)
    return result

def create_kvm_link(src_id, tgt_id, src_veth, tgt_veth, src_br, tgt_br):
    result = {}
    cmd1 = "sudo ip link add " + src_veth + " type veth peer name " + tgt_veth
    cmd2 = "sudo ip link set " + src_veth + " up"
    cmd3 = "sudo ip link set " + tgt_veth + " up"
    # cmd4 = "sudo ip link set dev veth_" + topo + "_" + src + tgt + " master br_" + topo + "_" + src + tgt
    # cmd5 = "sudo ip link set dev veth_" + topo + "_" + tgt + src + " master br_" + topo + "_" + tgt + src
    cmd4 = "sudo ip link set dev " + src_veth + " master " + src_br
    cmd5 = "sudo ip link set dev " + tgt_veth + " master " + tgt_br
    shell_execute(cmd1)
    shell_execute(cmd2)
    shell_execute(cmd3)
    shell_execute(cmd4)
    shell_execute(cmd5)
    result[src_id] = {'nic': src_br, 'mac': 'default'}
    result[tgt_id] = {'nic': tgt_br, 'mac': 'default'}
    return result

def create_kd_link(src_id, tgt_id, src_name, tgt_name, veth, br, ip, parallel):
    result = {}
    pid = get_pid(tgt_id)
    tgt_ip = ip
    print(pid)
    intf = generate_eth_name(src_name, parallel)
    cmd1 = "sudo ip link add " + veth + " type veth peer name " + intf + " netns " + pid
    cmd2 = "sudo ip link set " + veth + " up"
    # cmd3 = "sudo ip link set dev veth_" + topo + "_" + src + tgt + " master br_" + topo + "_" + src + tgt
    cmd3 = "sudo ip link set dev " + veth + " master " + br
    shell_execute(cmd1)
    shell_execute(cmd2)
    shell_execute(cmd3)
    with Namespace(pid, "net"):
        shell_execute("ifconfig " + intf + " " + tgt_ip + " up")
    mac = shell_execute(f'sudo docker exec {tgt_id} bash -c "cat /sys/class/net/{intf}/address"')
    result[src_id] = {'nic': br, 'mac': 'default'}
    result[tgt_id] = {'nic': 'to' + src_name + "_" + str(parallel), 'mac': mac}
    return result

def create_dk_link(src_id, tgt_id, src_name, tgt_name, veth, br, ip, parallel):
    result = {}
    pid = get_pid(src_id)
    src_ip = ip
    print(pid)
    intf = generate_eth_name(tgt_name, parallel)
    cmd1 = "sudo ip link add " + veth + " type veth peer name " + intf + " netns " + pid
    cmd2 = "sudo ip link set " + veth + " up"
    # cmd3 = "sudo ip link set dev veth_" + topo + "_" + tgt + src + " master br_" + topo + "_" + tgt + src
    cmd3 = "sudo ip link set dev " + veth + " master " + br
    shell_execute(cmd1)
    shell_execute(cmd2)
    shell_execute(cmd3)
    with Namespace(pid, "net"):
        shell_execute("ifconfig " + intf + " " + src_ip + " up")
    mac = shell_execute(f'sudo docker exec {src_id} bash -c "cat /sys/class/net/{intf}/address"')
    result[src_id] = {'nic': 'to' + tgt_name + "_" + str(parallel), 'mac': mac}
    result[tgt_id] = {'nic': br, 'mac': 'default'}
    return result

def delete_link(container_id, intf):
    result = {}
    try:
        shell_execute(f"sudo nsenter -t {get_pid(container_id)} --net ip link delete {intf}")
    except subprocess.CalledProcessError as e:
        result['error_msg'] = "DELETE LINK ERROR when execute command '" + e.cmd + \
                              "', exit code: " + str(
            e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
    print(result)
    return result

def delete_kvm_link(veth_infos):
    # 暂时没写返回 不知道有没有隐患
    for veth in veth_infos:
        cmd = "sudo ip link show type veth"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        output = result.stdout
        if veth in output:
            cmd1 = "sudo ip link delete " + veth
            shell_execute(cmd1)
        else:
            pass
    return {}

def delete_dkAkd_link(veth_infos):
    delete_kvm_link(veth_infos)
    return {}


def delete_vxlan(ovs_info):
    print(ovs_info)
    result = {}
    if ovs_info['src_service'] == 'hardware':
        delete_real_vxlan(ovs_info['ne_id'], ovs_info['vlan'], ovs_info['remote_ip'], ovs_info['vni'])
    elif ovs_info['tgt_service'] == 'hardware':
        if ovs_info['src_service'] == 'docker':
            cmd_del_br = "sudo ip link delete " + ovs_info['target']
            cmd_del_port = "sudo ip link delete " + ovs_info['target'] + "_p"
            shell_execute(cmd_del_br)
            shell_execute(cmd_del_port)      
            return {}
        else:
            cmd_del_br = "sudo ip link delete " + ovs_info['target']
            cmd_del_port = "sudo ip link delete " + ovs_info['target'] + "_p"
            shell_execute(cmd_del_br)
            shell_execute(cmd_del_port)
            src_veth = ovs_info['src_veth']
            cmd = "sudo ip link show type veth"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            output = result.stdout
            if src_veth in output:
                cmd1 = "sudo ip link delete " + src_veth
                shell_execute(cmd1)       
            return {}
    else:
        cmd_del_ovs = "sudo ovs-vsctl --if-exists del-br " + ovs_info['target']
        print(ovs_info['target'])
        shell_execute(cmd_del_ovs)
        src_veth = ovs_info['src_veth']
        cmd = "sudo ip link show type veth"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        output = result.stdout
        if src_veth in output:
            cmd1 = "sudo ip link delete " + src_veth
            shell_execute(cmd1)       
        return {}
    
def delete_real_vxlan(ne_id, vlan, remote, vni):
    result = {}
    # 交换机的主机名或IP地址
    hostname = '192.168.150.150'
    # SSH登录的用户名
    username = 'vemuv587'
    # SSH登录的密码
    password = '[REDACTED]'
    # 创建SSH客户端
    ssh_client = paramiko.SSHClient()
    # 自动添加主机密钥（不推荐用于生产环境）
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        # 连接到交换机
        ssh_client.connect(hostname, username=username, password=password)
        remote_conn = ssh_client.invoke_shell()
        # 发送命令
        remote_conn.send('system-view\n')
        remote_conn.send('bridge-domain ' + vlan + '\n')
        remote_conn.send('undo vxlan vni ' + vni + '\n')
        time.sleep(0.5)
        remote_conn.send('quit\n')
        remote_conn.send('interface nve1\n')
        remote_conn.send('undo vni ' + vni + ' head-end peer-list ' + remote + '\n')
        time.sleep(0.5)
        remote_conn.send('quit\n')
        remote_conn.send('commit\n')
        time.sleep(0.5)
    finally:
        # 关闭SSH连接
        ssh_client.close()
    hardware = HardwareRedis()
    hardware.update_ne_state(id = ne_id, state= False)
    return result


def delete_dynative_vxlan(ovs_info):
    print(ovs_info)
    result = {}
    # 删网桥
    cmd_del_ovs = "sudo ovs-vsctl --if-exists del-br " + ovs_info['target']
    print(ovs_info['target'])
    shell_execute(cmd_del_ovs)
    # 删容器网卡
    if ovs_info['service'] == 'docker':
        delete_link(ovs_info['id'],ovs_info['port'])
    elif ovs_info['service'] == 'kvm':
        src_veth = ovs_info['src_veth']
        cmd = "sudo ip link show type veth"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        output = result.stdout
        if src_veth in output:
            print("exist")
            cmd1 = "sudo ip link delete " + src_veth
            shell_execute(cmd1)     
    return {}

def delete_dpdk_br(ovs_name):
    '''
        删除创建dpdk复合节点时添加的网桥以及该网桥上的网卡
    '''
    result = {'code':1}
    try:
        if ovs_name[3] == 's':
            ports = shell_execute(f"sudo ovs-vsctl list-ports {ovs_name}").split('\n')
            print(ports)
            for port_name in ports:
                if port_name[0:3] != 'tap':
                    shell_execute(f"sudo ovs-vsctl --if-exists del-port {ovs_name} {port_name}")
    except subprocess.CalledProcessError as e:
        print('error'*10)
        result['code'] = 0
        result['error_msg'] = "DELETE DPDK ERROR when execute command " + e.cmd + \
            "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
    finally:
        try:
            shell_execute(f"sudo ovs-vsctl --if-exists del-br {ovs_name}")
        except subprocess.CalledProcessError as e:
            result['error_msg'] = f"Command \'sudo ovs-vsctl del-br {ovs_name}\' error, maybe there is no bridge {ovs_name}"
            result['code'] = 0
    return result

def delete_ovs_port(ovs_container_id, ovs_port_name, ovs_bridge_name):
    '''
    删除ovs网桥上的端口

    Args:
        container_id: 容器ID
        ovs_port_name: ovs网桥端口名
        ovs_bridge_name: ovs网桥名

    Returns:
        None

    Raises:
        LinkOvsBridgePortDeleteError

    '''
    # ovs不在容器网络空间，nsenter无效
    # cmd = f"sudo docker exec -it {ovs_container_id} ovs-vsctl --if-exists del-port init-br0 {ovs_port_name}"
    # print(cmd)
    # shell_execute(cmd)
    client =  docker.from_env()
    container =  client.containers.get(container_id = ovs_container_id)
    cmd = f"ovs-vsctl --if-exists del-port {ovs_bridge_name} {ovs_port_name}"
    result_code = container.exec_run(cmd=cmd, detach=True).exit_code
    # print(result_code)
    if result_code:
        raise LinkOvsBridgePortDeleteError(ovs_container_id, ovs_bridge_name, ovs_port_name)


def create_vxlan(*args, source_type = 'normal'):
    '''
        输入：（边缘节点的id，边缘节点的ip地址，连接边缘节点VTEP的ovs名字，远端宿主机ip，VNI标识符\n，源类型节点类型【默认normal，可指定dpdk】）
    '''
    result = {}
    if args[6] == 'docker':
        result = create_vxlan_normal(args[0], args[1], args[2], args[3], args[4], args[5], args[10])
        print(result)
    elif args[6] == 'kvm':
        result = create_vxlan_kvm(args[0], args[2], args[3], args[4], args[7], args[8], args[9])
    else:
        result = create_vxlan_dpdk(args[0], args[1], args[2], args[3])
    return result

def create_hardware_vxlan(*args):
    result = {}
    if args[6] == 'docker':
        result = create_virtual_docker_vxlan(args[0], args[1], args[2], args[3], args[4], args[5], args[8], args[9], args[10])
    elif args[6] == 'kvm':
        result = create_virtual_kvm_vxlan(args[0], args[2], args[3], args[4], args[7], args[8], args[9])
    else:
        result = create_real_vxlan(args[0], args[3], args[4], args[11])
    return result

def create_virtual_docker_vxlan(container_id, container_ip, vxlan_name, remote_ip, vni, target, veth1, veth2, parallel) -> dict:
    # 容器pid
    pid = get_pid(container_id)
    # 网桥网卡名
    bridge_intf_name = vxlan_name + "_p"
    # 容器内端口名
    interface = generate_eth_name(target, parallel)
    result = {}
    try:
        shell_execute("sudo modprobe vxlan")
        shell_execute("sudo ip link add " + bridge_intf_name + " type vxlan id " + vni + " dev br0 dstport 4789")
        shell_execute("sudo ip link set " + bridge_intf_name + " up")
        shell_execute("sudo bridge fdb append 00:00:00:00:00:00 dev " + bridge_intf_name + " dst " + remote_ip)
        shell_execute("sudo ip link add " + veth1 + " type veth peer name " + veth2 + " netns " + pid)
        shell_execute("sudo ip link set " + veth1 + " up")
        shell_execute("sudo brctl addbr " + vxlan_name)
        shell_execute("sudo brctl addif " + vxlan_name + " " + bridge_intf_name)
        shell_execute("sudo brctl addif " + vxlan_name + " " + veth1)
        shell_execute("sudo ip link set " + vxlan_name + " up")
        shell_execute("nsenter --target " + pid + " --net " +  "ip link set dev " + veth2 + " name " + interface)
        shell_execute("nsenter --target " + pid + " --net " +  "ip link set " + interface + " up")
        if container_ip != "":
            shell_execute("nsenter --target " + pid + " --net " +  "ip addr add " + container_ip + " dev " + interface)
        else:
            pass
        shell_execute("nsenter --target " + pid + " --net " +  "ip link set dev " + interface + " mtu 1450")
    except subprocess.CalledProcessError as e:
        result['error_msg'] = "CREATE (virtual_docker) VXLAN ERROR when execute command '" + e.cmd + \
            "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
    result[container_id] = interface
    result["ovs_name"] = vxlan_name
        # 添加源端节点MAC地址
    if container_ip !="":
        result['src_mac'] = shell_execute(
            f'sudo docker exec {container_id} bash -c '
            f'"cat /sys/class/net/{interface}/address"')
    else:
        result['src_mac'] = ""
    return result
    
def create_virtual_kvm_vxlan(kvm_id, vxlan_name, remote_ip, vni, br, veth1, veth2) -> dict:
    result = {}
    bridge_intf_name = vxlan_name + "_p"
    try:
        shell_execute("sudo modprobe vxlan")
        shell_execute("sudo ip link add " + bridge_intf_name + " type vxlan id " + vni + " dev br0 dstport 4789")
        shell_execute("sudo ip link set " + bridge_intf_name + " up")
        shell_execute("sudo bridge fdb append 00:00:00:00:00:00 dev " + bridge_intf_name + " dst " + remote_ip)
        shell_execute("sudo ip link add " + veth1 + " type veth peer name " + veth2)
        shell_execute("sudo ip link set " + veth1 + " up")
        shell_execute("sudo ip link set " + veth2 + " up")
        shell_execute("sudo brctl addbr " + vxlan_name)
        shell_execute("sudo brctl addif " + vxlan_name + " " + bridge_intf_name)
        shell_execute("sudo brctl addif " + vxlan_name + " " + veth1)
        shell_execute("sudo ip link set " + vxlan_name + " up")
        shell_execute("sudo ip link set "+ veth2 + " master " + br)
    except subprocess.CalledProcessError as e:
        result['error_msg'] = "CREATE VXLAN ERROR when execute command '" + e.cmd + \
            "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()    
    result[kvm_id] = br
    result["src_mac"] = "default"
    return result

def create_real_vxlan(ne_id, remote_ip, vni, vlan):
    result = {}
    # 交换机的主机名或IP地址
    hostname = '192.168.150.150'
    # SSH登录的用户名
    username = 'vemuv587'
    # SSH登录的密码
    password = '[REDACTED]'
    # 创建SSH客户端
    ssh_client = paramiko.SSHClient()
    # 自动添加主机密钥（不推荐用于生产环境）
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        # 连接到交换机
        ssh_client.connect(hostname, username=username, password=password)
        remote_conn = ssh_client.invoke_shell()
        # 发送命令
        remote_conn.send('system-view\n')
        remote_conn.send('bridge-domain ' + vlan + '\n')
        time.sleep(0.5)
        remote_conn.send('vxlan vni ' + vni + '\n')
        time.sleep(0.5)
        remote_conn.send('quit\n')
        remote_conn.send('interface nve1\n')
        remote_conn.send('source 192.168.150.150\n')
        remote_conn.send('vni ' + vni + ' head-end peer-list ' + remote_ip + '\n')
        time.sleep(0.5)
        remote_conn.send('quit\n')
        remote_conn.send('commit\n')
        time.sleep(0.5)
    finally:
        # 关闭SSH连接
        ssh_client.close()
    hardware = HardwareRedis()
    hardware.update_ne_state(id=ne_id, state=True)
    result[ne_id] = ""
    result["src_mac"] = "default"
    return result

    
def create_vxlan_dpdk(dpdk_standard_bridge, ovs_name, remote_ip, vxlan_id) -> dict:
    '''
        input: the standard bridge's name of dpdk node, the name of vxlan's bridge, remote ip, vxaln id
        output:  return a dict which consists of 
    '''
    result = {}
    try:
        ovs_intf_name = generate_uuid_len_10()

        shell_execute("sudo ovs-vsctl add-br " + ovs_name + f' -- set Bridge {ovs_name} stp_enable=true')
        shell_execute("sudo ip l a veth0 type veth peer name veth1")
        shell_execute("sudo ovs-vsctl add-port " + dpdk_standard_bridge + " veth0")
        shell_execute("sudo ovs-vsctl add-port " + ovs_name + " veth1")
        shell_execute("sudo ovs-vsctl add-port " + ovs_name + " " + ovs_intf_name + 
                        " -- set Interface " + ovs_intf_name + " type=vxlan options:remote_ip=" 
                        + remote_ip + " options:key=" + str(vxlan_id) + " options:dst_port=8472")
    except subprocess.CalledProcessError as e:
        result['error_msg'] = "CREATE (dpdk) VXLAN ERROR when execute command '" + e.cmd + \
            "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()



def create_vxlan_normal(container_id, container_ip, ovs_name, remote_ip, vxlan_id, target, parallel) -> dict:
    '''
        输入：边缘节点的id及其ip地址，连接边缘节点和VTEP的ovs名字，远端宿主机ip，VNI标识符,target是vxlan对端网元名\n
        输出：返回一个字典。若正确执行，返回{边缘节点id: 边缘节点网卡名, ovs名: ovs网卡名}；
        若执行过程中报错，则字典中包含"error_msg"键值\n
        功能描述：创建一个vxlan，与在远端宿主机上的另一个节点相连
    '''
    result = {}
    try:
        # 给网卡起名
        # 若有多个ovs，各ovs所连网卡名有重复的，则后面设置的重名ovs会覆盖掉之前被重名的ovs，因此需要随机取名
        # （突然发现没用...） ！
        # ！！！最好ovs接口名和边缘网卡名能全局给定！！！否则通过随机生成的方式会给平台留下隐患
        # TODO:VXLAN的mtu指定了，岂不是对网络实验有影响
        
        # 容器pid
        pid = get_pid(container_id)
        # ovs网卡名
        ovs_intf_name = generate_uuid_len_10()

        # 容器网卡名
        container_intf_name = generate_eth_name(target, parallel)
        # 检查网卡名是否重复
        # 如果网卡不重复，会抛出异常，此时不用做任何处理，继续创建网卡
        with Namespace(pid, 'net'):
            try:
                eth_name_if_used = shell_execute("ifconfig -a|grep "+container_intf_name)
                print(container_intf_name+"网卡名重复，变为10位随机数")
                if str(eth_name_if_used) !='':
                    container_intf_name=generate_uuid_len_10()
            except Exception as e:
                #这个判断是网卡名不重复的返回
                if e.returncode == 1 and e.stderr == '' and e.stdout == '':
                    pass
                else:
                    result['error_msg'] = "CREATE LINK ERROR when execute command '" + e.cmd + \
            "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
        
        # 添加ovs
        # pwd = /home/vemu4/test_xc, 只会打印到项目的根目录
        ovs_docker_path = f'{os.getcwd()}/vemu_uestc/Implement_layer/'
        shell_execute("sudo ovs-vsctl add-br " + ovs_name + f' -- set Bridge {ovs_name} stp_enable=true')

        # 把边缘节点连接至ovs
        cmd = 'sudo ' + f'{ovs_docker_path}ovs-docker.sh add-port ' + ovs_name + ' ' + container_intf_name \
              + ' ' + container_id + ' --mtu=1450'
        if container_ip:
            cmd = cmd + ' --ipaddress=' + container_ip
        shell_execute(cmd)
        
        # 创建vxlan
        shell_execute('sudo ovs-vsctl add-port ' + ovs_name + ' ' + ovs_intf_name + ' ' \
                    + '-- set Interface ' + ovs_intf_name \
                    + ' type=vxlan options:remote_ip=' + remote_ip \
                    + ' options:key=' + str(vxlan_id) \
                    + ' options:dst_port=8472')
        
        result[container_id] = container_intf_name
        result[ovs_name] = ovs_intf_name
        # 添加源端节点MAC地址
        result['src_mac'] = shell_execute(
            f'sudo docker exec {container_id} bash -c '
            f'"cat /sys/class/net/{container_intf_name}/address"')
        print("hello HAN")
    
    except subprocess.CalledProcessError as e:
        result['error_msg'] = "CREATE VXLAN ERROR when execute command '" + e.cmd + \
            "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
    
    return result

def create_vxlan_kvm(kvm_id, ovs_name, remote_ip, vxlan_id, br, src_veth, tgt_veth) -> dict:
    '''
        ----------------------gjh虚机vxlan相关----------------------------------------
        输入：连接边缘节点和VTEP的ovs名字，远端宿主机ip，VNI标识符,tgt是vxlan对端网元名,src是vxlan源端网元名，拓扑名topo\n
        输出：返回一个字典。若正确执行，返回{边缘节点id: 边缘节点网卡名, ovs名: ovs网卡名}；
        若执行过程中报错，则字典中包含"error_msg"键值\n
        功能描述：创建一个vxlan，与在远端宿主机上的另一个节点相连
    '''
    result = {}
    try:

        ovs_intf_name = generate_uuid_len_10()
        cmd1 = "sudo ovs-vsctl add-br " + ovs_name 
        cmd2 = "sudo ip link set " + ovs_name + " up"
        cmd3 = "sudo ip link add " + src_veth + " type veth peer name " + tgt_veth
        cmd5 = "sudo ip link set " + src_veth + " up"
        cmd6 = "sudo ip link set " + tgt_veth + " up"
        cmd7 = "sudo ovs-vsctl add-port " + ovs_name + " " + tgt_veth
        cmd8 = "sudo ip link set dev " + src_veth + " master " + br
        vxlan_cmd = 'sudo ovs-vsctl add-port ' + ovs_name + ' ' + ovs_intf_name + ' ' \
                        + '-- set Interface ' + ovs_intf_name \
                        + ' type=vxlan options:remote_ip=' + remote_ip \
                        + ' options:key=' + str(vxlan_id) \
                        + ' options:dst_port=8472'
        shell_execute(cmd1)
        shell_execute(cmd2)
        shell_execute(cmd3)
        shell_execute(cmd5)
        shell_execute(cmd6)
        shell_execute(cmd7)
        shell_execute(cmd8)
        shell_execute(vxlan_cmd)
        result[kvm_id] = br
        result[ovs_name] = ovs_intf_name
        result["src_mac"] = "default"
    except subprocess.CalledProcessError as e:
        result['error_msg'] = "CREATE VXLAN ERROR when execute command '" + e.cmd + \
            "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
    
    print(result)
    return result


def config_link(container_id, intf, bw_kbit, queue_size_byte=100000,
                delay_us=0, loss_rate=0, jitter_us=0, correlation=0,
                delay_distribution="normal", operate="replace",
                custom_command=[]) -> dict:
    """实际配置单条链路

    属性含义以及可选值参考Linux TC
    
    Args:
        container_id : 容器id
        intf : 接口名称
        bw_kbit : 链路带宽
        queue_size_byte (int, optional): 队列大小
        delay_us (int, optional): 时延
        loss_rate (int, optional): 丢包率
        jitter_us (int, optional): 时延抖动
        correlation (int, optional): 抖动相关率
        delay_distribution (str, optional): 抖动时延分布
        operate (str, optional):  TC命令
        custom_command (list, optional): 暂时未用

    Returns:
        dict: 执行结果
    """
    # 通过用户填充的属性构造完成的TC配置命令并执行
    
    result = {}
    loss_module ="" if(loss_rate == 0) else "loss " + str(loss_rate) + "% "
    jitter_module = ""
    # 如果没有定义抖动，则相关率和分布没有意义
    if jitter_us != None and jitter_us != "" and jitter_us != 0:
        jitter_module = str(jitter_us) + "us "
        if(correlation != 0):
            jitter_module += str(correlation) + "% "
        # distribution种类查看：http://www.unixunique.com/2018/07/linux-network-emulator-custom-delay_9.html
        jitter_module += "distribution " + delay_distribution + " " 
        
    # https://unix.stackexchange.com/questions/100785/bucket-size-in-tbf
    burst_kbyte = str(int(bw_kbit)/250/8*2) 
    pid = get_pid(container_id)

    try:
        with Namespace(pid, 'net'):
            # 写入TC规则
            cmd = "tc qdisc " + operate + \
                " dev " + intf + " root handle 5:0 tbf rate " + str(bw_kbit) + \
                "kbit burst " + burst_kbyte + "kb limit " + str(queue_size_byte) +\
                "b "
            shell_execute(cmd)
            # jitter_module需跟在delay部分后面
            cmd = "sudo nsenter -t "+ pid + " --net " + "tc qdisc replace dev " + \
                intf + " parent 5:0 handle 10:0 netem limit 2000000 delay " + \
                str(delay_us)+ "us "+ jitter_module + loss_module
            shell_execute(cmd)    

    except subprocess.CalledProcessError as e:
        result['error_msg'] = "CONFIG LINK ERROR when execute command '" + e.cmd + \
            "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()
    return result


def modify_intf(container_id, intf, ip, mask):
    '''
    修改节点的网卡ip地址和掩码
    
    Args:
        container_id: 节点ID
        intf: 网卡名
        ip: ip地址
        mask: 子网掩码
    '''
    pid = get_pid(container_id)
    with Namespace(pid, 'net'):
        cmd = f'ifconfig {intf} {ip} netmask {mask}'
        shell_execute(cmd)


def modify_gateway(container_id, gw):
    '''
    修改节点的默认网关
    
    Args:
        container_id: 节点ID
        gw: 默认网关
    '''
    pid = get_pid(container_id)
    with Namespace(pid, 'net'):
        cmd = f'route add default gw {gw}'
        shell_execute(cmd)


def clear_qdisc(container_id, intf) -> dict:
    '''清除某一网卡上的队列规则（可用于链路配置失败时清除已添加的队列规则）

    Args:
        container_id: 节点ID
        intf: 网卡名
    
    Returns:
        result: 若正确执行，则为{}，若执行过程中报错，则包含"error_msg"键值
    '''
    result = {}

    try:
        pid = get_pid(container_id)
        with Namespace(pid, 'net'):
            shell_execute("tc qdisc del dev " + intf + " root") # 不管root上有什么队列，都将其删除。
    except subprocess.CalledProcessError as e:
        result['error_msg'] = "CLEAR LINK ERROR when execute command '" + e.cmd + \
            "', exit code: " + str(e.returncode) + ", stderr: " + e.stderr.rstrip() + ", stdout: " + e.stdout.rstrip()

    return result

def change_link_parm_periodically(link, period_s=5, bw_start_kbit=10*1024, bw_stop_kbit=100*1024, 
                    queue_size_start_byte=100*1024, queue_size_stop_byte=100*1024,
                    delay_start_us=10*1000, delay_stop_us=100*1000, 
                    loss_rate_start=0, loss_rate_stop=1,
                    jitter_us=None, correlation="", delay_distribution="normal"):
    '''
        输入：要周期性改变参数的链路列表，变换周期，各参数变化范围\n
        输出：无\n
        功能描述：周期性的改变所给链路的参数，在一定范围内按均匀分布变化
    '''
    # 除改变的量外链路其余的默认值如何处理？可以和数据库配合吗？数据库如何存储链路的？
    # TODO(MaTie): 这部分写的较粗糙，参数检查？下界要小于等于上界。
    # TODO(MaTie): 入参也要改成单端口的
    finish = False
    result = None
    while not finish:
        bw_bps = random.uniform(bw_start_kbit, bw_stop_kbit)
        queue_size_byte = random.uniform(queue_size_start_byte, queue_size_stop_byte)
        delay_us = random.uniform(delay_start_us, delay_stop_us)
        loss = random.uniform(loss_rate_start, loss_rate_stop)
        result = config_link(link, bw_bps, queue_size_byte, delay_us, loss, jitter_us=jitter_us, 
                    correlation=correlation, delay_distribution=delay_distribution, operate="change")
        if 'error_msg' in result.keys(): # 出错则证明容器被删除，退出进程
            finish = True
            break
        # TODO: 可以让数据库记录每一时刻的链路状态
        print("link=" + str(link) + ", bw=" +  str(bw_bps))
        time.sleep(period_s)
    print("EXIT change link thread.")
        
    
if __name__ == "__main__":
    container_id_1 = "mth0"
    container_id_2 = "mth1"
    ip_1 = "10.0.1.10/24"
    ip_2 = "10.0.1.11/24"
    container_ip = "10.0.0.12/24"
    ovs_name = "vxbr1-mt"
    remote_ip = "10.1.1.105"
    vxlan_id = 100

    # config_link(container_id_1, "95b4474afc", 6*1024, 100*1024, 30*1000)
    clear_qdisc(container_id_1, "95b4474afc")
    exit(0)
    
    link = create_link(container_id_1, container_id_2, ip_1, ip_2)

    try:
        shell_execute("ovs-vsctl del-br vxbr1-mt") # TODO: ovs的删除是谁做？---日后再说
    except subprocess.CalledProcessError:
        pass

    create_vxlan(container_id_2, container_ip, ovs_name, remote_ip, vxlan_id)

    config_link(link, 5*1024, 100*1024, 30*1000)

    # TODO: 感觉每次都要传一大堆参数，好蠢。要是要把这个线程的启动再封一层函数的话又得传一遍...
    t = threading.Thread(target=change_link_parm_periodically, args=(link, ), kwargs=({
        "period_s":30, "bw_start_kbit":10 * 1024, "bw_stop_kbit":100 *1024, 
        "queue_size_start_byte":100 *1024, "queue_size_stop_byte":100 *1024, 
        "delay_start_us":10 *1000, "delay_stop_us":10 *1000, 
        "loss_rate_start":0, "loss_rate_stop":0, "jitter_us":None, "correlation":'', "delay_distribution":'normal'
    })) # 此线程用于维护各链路的变化
    t.start() # 用线程为了不阻塞剩余的程序




