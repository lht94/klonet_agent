import socket
import random
import uuid
import re
import datetime
import subprocess
from pypinyin import lazy_pinyin

from vemu_uestc.vemu_config.config import PROJ_CONFIG
import ast
import time
import requests

_local_ip = None


def shell_execute(cmd, check=True) -> str:
    '''
        输入：要执行的shell命令
        输出：命令执行后的标准输出或错误输出
        功能描述：使用subprocess.run()执行shell命令，并能在出现错误时返回错误信息
    '''
    completed_process = subprocess.run(
        cmd, 
        shell=True,  # 执行shell命令
        capture_output=True,  # 效果与设置stdout=PIPE, stderr=PIPE一样
        text=True,  # 将stdin, stdout, stderr修改为string模式
        check=check,  # 开启检查，若出错则raise CalledProcessError
        )
    if check == True:
        return completed_process.stdout.rstrip() # 加rstrip去除字符串末尾的回车
    else:
        return completed_process.returncode


def get_host_ip():
    global _local_ip
    s = None
    try:
        if not _local_ip:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            _local_ip = s.getsockname()[0]
        return _local_ip
    finally:
        if s:
            s.close()


def get_vxlan_vni():
    return ''.join(str(random.randint(1, 9)) for _ in range(5))


def get_vxlan_ovs_id():
    return str(uuid.uuid4()).replace("-", '')[:10]


def generate_uuid_len_10() -> str:
    '''
        输入：无\n
        输出：10位的随机16进制id
        功能描述：通过python的uuid模块产生10位的随机16进制id，有极低概率产生重复id(16的10次方分之一)
    '''
    return str(uuid.uuid4()).replace("-", '')[0:10]


def is_ip_leagal(ip):
    '''
    检查ip地址的合法性

    Args:
        ip: ip地址，如192.168.1.1（合法），256.0.0.1（不合法），aaaa（不合法）

    Returns:
        合法则返回True，不合法返回False
    '''
    compile_ip = re.compile("^(1\d{2}|2[0-4]\d|25[0-5]|[1-9]\d|[1-9])\."
        "(1\d{2}|2[0-4]\d|25[0-5]|[1-9]\d|\d)\."
        "(1\d{2}|2[0-4]\d|25[0-5]|[1-9]\d|\d)\."
        "(1\d{2}|2[0-4]\d|25[0-5]|[1-9]\d|\d)$")
    if compile_ip.match(ip):
        return True
    else:    
        return False

def is_cidr_leagal(cidr):
    '''
    检查cidr形式的ip地址和掩码的合法性

    Args:
        ip: cidr形式的ip地址，如192.168.1.1/24（合法），256.0.0.1（不合法），
            aaaa（不合法），192.168.1.1/33（不合法）
    '''
    ip_and_mask = cidr.split("/")

    if len(ip_and_mask) != 2:
        return False
    else:
        try:
            mask = int(ip_and_mask[1])
        except:
            return False
        if mask < 0 or mask > 32:
            return False
        if not is_ip_leagal(ip_and_mask[0]):
            return False

    return True


def cidr_netmask(prefix):
    '''
    将int类型的掩码转换为地址类型的掩码

    Args:
        prefix: int类型的掩码，如24

    Returns:
        地址类型的掩码，如255.255.255.0
    '''
    bin_arr = ['0' for i in range(32)]
    for i in range(prefix):
        bin_arr[i] = '1'
    tmpmask = [''.join(bin_arr[i * 8:i * 8 + 8]) for i in range(4)]
    tmpmask = [str(int(tmpstr, 2)) for tmpstr in tmpmask]
    return '.'.join(tmpmask)


def netmask_cidr(mask):
    '''
    将地址类型的掩码转换为int类型的掩码

    Args:
        mask: 地址类型的掩码，如255.255.255.0

    Returns:
        int类型的掩码，在前面加上斜杠组成的字符串，如/24
    '''
    if mask == '':
        return ''
    prefix = 0
    for i in mask.split('.'):
        for j in bin(int(i))[2:]:
            prefix += int(j)
    return f'/{prefix}'


def netmask2cidr(ip, netmask):
    '''
    将ip和子网掩码的组合转换为cidr形式的地址

    Args:
        ip: 如192.168.1.1
        netmask: 如255.255.255.0

    Returns:
        cidr: cidr形式的地址，如192.168.1.1/24
    '''
    if ip == '' or netmask == '':
        return ''
    count_bit = lambda bin_str: len([i for i in bin_str if i == '1'])
    mask_splited = netmask.split('.')
    mask_count = [count_bit(bin(int(i))) for i in mask_splited]
    ip_cidr = ip + '/' + str(sum(mask_count))
    return ip_cidr

def cidr2ip_and_netmask(cidr):
    '''
    将cidr形式的地址转换为ip和子网掩码，如192.168.1.1/24转换为192.168.1.1和
    255.255.255.0。也可作为cidr形式的合法性检验函数。

    Args:
        cidr: cidr形式的地址，如192.168.1.1/24

    Returns:
        ip: 如192.168.1.1
        netmask: 如255.255.255.0

    Raises:
        ValueError: 
    '''
    if not is_cidr_leagal(cidr):
        raise ValueError(f"Address [{cidr}] is illegal, please check!")

    ip_and_netmask = cidr.split("/")
    ip = ip_and_netmask[0]
    netmask = ip_and_netmask[1]

    return ip, netmask

def get_ctn_nic_mac(container_id, nic_name):
    '''
    根据节点名和网卡名获取节点的指定网卡的mac地址
    '''
    mac_address = shell_execute(f"sudo docker exec {container_id} bash -c \""
                f"cat /sys/class/net/{nic_name}/address\"")
    return mac_address

def get_nic_mac(nic_name):
    '''
    根据节点名和网卡名获取节点的指定网卡的mac地址
    注意，若使用python的nsenter搭配此函数，则不能正确获取网卡mac。此种情况应使用
    get_ctn_nic_mac()函数代替。
    '''
    mac_address = shell_execute(f"cat /sys/class/net/{nic_name}/address")
    return mac_address

def remove_quotes(string):
    '''
    移除字符串两侧的引号
    '''
    return string.strip("\"")

def remove_quotes_in_list(list_with_quotes):
    '''
    对字符串组成的列表，移除每个元素两侧的引号
    '''
    return [remove_quotes(s) for s in list_with_quotes]

def str2bool(s):
    s_lower = s.lower()
    if s_lower == 'true':
        return True
    elif s_lower == 'false':
        return False
    else:
        raise ValueError(f"[{s}] is not true or false!")

def str2dict(s):
    ast.literal_eval(s)
    pass

def get_formatted_time():
    '''
    获取2016-03-20 11:45:39,123格式的时间
    '''
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]

def print_with_timestamp(value):
    '''
    以2016-03-20 11:45:39,123 - xxx的形式打印
    '''
    print(f"{get_formatted_time()} - {value}")

def register_worker():
    while True:
        try:
            req_url = (f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}"
                f"/master/worker/{get_host_ip()}/")
            result = requests.post(req_url)
            if result.json()['code']:
                print('注册成功')
                break
        except requests.exceptions.ConnectionError as e:
            print_with_timestamp(f"WARNING: Cannot connect to master when try "
                f"to register_worker, do you forget to start it? req_url="
                f"{req_url}")
            time.sleep(20)

def chinese_to_pinyin(string):
    name_list = lazy_pinyin(string)
    name = ""
    for word in name_list:
        name += word.title()
    return name
