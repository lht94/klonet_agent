# 马铁(821637074@qq.com)写于2021.10.08
# 本代码为华为TIP实验基于pkt_gen.py做了特别更改
# 将UDP数据包的payload的前4个字节作为产生的数据流的id，id从0开始依次递增
# 由于实验场景已知，没有考虑id超过最大值(2^32)的情况
# 由于要占用4个字节，因此当数据包长度小于4字节时会直接抛出异常并退出程序
import socket
import time
import numpy as np
import struct
import sys
import argparse
import ast
# on_time = 1  # 发包的on状态持续时间，拟定为1s，单位：s，考虑pareto分布，指定1.最小值和2.特性参数
# off_time = 1  # 不发包持续时间
# ave_rate = 30  # 期望平均速率，拟定为30Mbps，单位：Mbps
# pkt_gen_duration = 60  # 发包器持续时间，拟定为60s，单位：s

parser = argparse.ArgumentParser()

parser.add_argument("--rate", type=float, default=20)
parser.add_argument("--duration", type=float, default=20)
parser.add_argument("--src", type=str, default="192.168.1.1")
parser.add_argument("--dst", type=str, default="192.168.1.2")
parser.add_argument("--src_port", type=int, default=6001)
parser.add_argument("--dst_port", type=int, default=6002)
parser.add_argument("--on_k", type=float, default=2)
parser.add_argument("--on_min", type=float, default=1)
parser.add_argument("--off_k", type=float, default=2)
parser.add_argument("--off_min", type=float, default=2)
parser.add_argument("--cdf_file", type=str, default="{'40': '1'}")

args = parser.parse_args()

UDP_ID_SIZE_BYTE = 4

# 参数处理
# I：1.rate;2.duration;3.src_ip;4.dst_ip;5.on_k;6.on_min;7.off_k;8.off_min;9.cdf.txt
def parse_cdf_file():
    pkt_cdf = args.cdf_file
    pkt_cdf_dict = ast.literal_eval(pkt_cdf)
    print(pkt_cdf_dict)
    str_pkt_cdf_dict = {}
    # 需要将int和float转换为字符串
    for pkt_len, cdf in pkt_cdf_dict.items():
        str_pkt_cdf_dict[str(pkt_len)] = str(cdf)
    return str_pkt_cdf_dict


# 生成符合指定pareto分布的随机数；param：1.形状参数：k；2.尺度参数：x_min
def rand_pareto(k: float, x_min: float):
    x = (np.random.pareto(k) + 1) * x_min
    return x


# 根据I：1.发包器持续时间（单位：s）；2.on的pareto参数；3.off的pareto参数
# 计算O：1.on持续时间列表；2.off持续时间列表
def on_off_duration(total_time: float, on_k, on_x_min, off_k, off_x_min):
    on_time_list = []
    off_time_list = []
    while total_time > 0:
        on_temp = rand_pareto(on_k, on_x_min)
        off_temp = rand_pareto(off_k, off_x_min)
        if total_time - on_temp < 0:
            break
        else:
            total_time = total_time - on_temp
            on_time_list.append(on_temp)
        if total_time - off_temp < 0:
            off_time_list.append(off_temp)
            break
        else:
            total_time = total_time - off_temp
            off_time_list.append(off_temp)
    return on_time_list, off_time_list


# 包长分布的字典形式转换成元组的列表
def packet_length_cdf_dict2list(**kwargs):
    cdf_dict = kwargs
    cdf_list = sorted(cdf_dict.items(), key=lambda x: float(x[1]))
    return cdf_list


# 计算包长分布的均值 单位：Byte
def packet_length_mean(cdf_list: list):
    n = len(cdf_list)
    mean = 0
    for i in range(n):
        if i == 0:
            mean += int(float(cdf_list[i][1].strip(" ")) * int(cdf_list[i][0]))    # 向下取整
        else:
            mean += int((float(cdf_list[i][1].strip(" ")) - float(cdf_list[i-1][1])) * int(cdf_list[i][0]))
    return mean


# 根据I：1.期望平均速率（单位：Mbps）；2.on状态时间（单位：s）；3.包长均值（单位：Byte）
# 计算O：1.on状态发包数
def on_packet_nums(ave_rate: float, on_time: float, pkt_length_mean: int):
    total_bit = ave_rate * on_time * 10**6
    pkt_length_mean *= 8
    num = int(total_bit / pkt_length_mean)
    return num


# 生成指定字节的字符串（1个字符代表1个字节）
def payload_gen(size: int) -> str:
    payload = ''

    payload_size = size - UDP_ID_SIZE_BYTE # ID_SIZE字节用于添加id

    if payload_size < 0:
        raise ValueError(f"payload size should bigger than {size}, "
            "please check the cdf_list")

    for i in range(payload_size):
        payload = payload + '1'
    return payload


# 生成一个符合包长分布的随机数作为包长 单位：Byte
# 返回包长对应的序号:int
# cdf_list应该长这样：[[1400, 1], [500, 0.8]]
def packet_length_cdf(cdf_list: list):
    random_temp = np.random.rand()  # 生成[0,1]内服从均匀分布的随机数（浮点数）
    n = len(cdf_list)
    for i in range(n):
        packet_length = int(cdf_list[i][0])
        if i == 0:
            if 0 <= random_temp <= float(cdf_list[i][1]):
                return i
            else:
                pass
        else:
            if float(cdf_list[i-1][1]) < random_temp <= float(cdf_list[i][1]):
                return i
            else:
                pass


def payload_list_gen(cdf_list: list) -> list:
    '''
    生成cdf_list对应的payload_list
    
    如cdf_list为[[1400, 1], [500, 0.8]]
    则生成的payload_list为[长度为1400字节的载荷, 长度为500字节的载荷]
    '''
    n = len(cdf_list)
    data_list = []
    for i in range(n):
        temp = payload_gen(int(cdf_list[i][0]))
        data_list.append(temp)
    return data_list


def pkt_send(s: socket, udp_id: int, payload: str, src_ip: str, dst_ip: str, 
        src_port:int, dst_port: int, proto=17):
    ihl = 5
    version = 4 # ipv4
    tos = 0 # Header Length =5, 表示无options部分
    tot_len = 0 # left for kernel to fill
    id = 0 # fragment相关
    frag_off = 0 # fragment相关
    ttl = 255
    protocol = proto  # linux没法用socket.getprotobyname('udp')？？
    check = 0 # left for kernel to fill
    saddr = socket.inet_aton(src_ip)
    daddr = socket.inet_aton(dst_ip)
    ihl_version = (version << 4) + ihl
    udp_len = len(payload) + 8
    # https://www.cnblogs.com/gala/archive/2011/09/22/2184801.html
    # B is 8, H is 16
    # IP头
    ip_header = struct.pack('!BBHHHBBH4s4s', ihl_version, tos, tot_len, id, 
                            frag_off, ttl, protocol, check, saddr, daddr)
    # UDP头，计算checksum
    # checksum目前没用上
    # 参考：https://github.com/houluy/UDP/blob/master/udp.py
    checksum = 0
    zero = 0

    payload = struct.pack('!L', udp_id) + payload.encode() # 加udp_id进载荷中
    split_src_ip, split_dst_ip = ip2int(src_ip), ip2int(dst_ip)
    pack_src_ip = struct.pack('!4B', *split_src_ip)
    pack_dst_ip = struct.pack('!4B', *split_dst_ip)
    pseudo_header = struct.pack('!BBH', zero, protocol, udp_len)
    pseudo_header = pack_src_ip + pack_dst_ip + pseudo_header
    udp_header = struct.pack('!4H', src_port, dst_port, udp_len, checksum)
    checksum = cal_checksum(pseudo_header + udp_header + payload)
    udp_header = struct.pack('!4H', src_port, dst_port, udp_len, checksum)
    packet = ip_header + udp_header + payload
    s.sendto(packet, (dst_ip, dst_port))

def cal_checksum(data):
    """
    计算校验和
    """
    checksum = 0
    data_len = len(data)
    if (data_len % 2):
        data_len += 1
        data += struct.pack('!B', 0)
    
    for i in range(0, data_len, 2):
        w = (data[i] << 8) + (data[i + 1])
        checksum += w

    checksum = (checksum >> 16) + (checksum & 0xFFFF)
    checksum = ~checksum & 0xFFFF
    return checksum

def ip2int(ip_addr):
    if ip_addr == 'localhost':
        ip_addr = '127.0.0.1'
    return [int(x) for x in ip_addr.split('.')]

# 发包器时间轴控制--发包
def packet_timeline(on_time_list, off_time_list, cdf_list, rate: float, src_ip, dst_ip, src_port, dst_port):
    state_n = len(on_time_list) # on期长度列表(时间)
    data_list = payload_list_gen(cdf_list)
    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
    mean = packet_length_mean(cdf_list)

    current_sent_num = 0

    print('packet mean: %d' % mean)
    try:
        for i in range(state_n): # 遍历每个on期
            t1 = time.time()
            pkt_nums = on_packet_nums(rate, on_time_list[i], mean)
            pkt_sum = 0
            for j in range(pkt_nums):
                seq = packet_length_cdf(cdf_list)
                udp_id = current_sent_num
                pkt_send(s, udp_id ,data_list[seq], src_ip, dst_ip, src_port, dst_port)
                pkt_sum += int(cdf_list[seq][0])

                current_sent_num += 1
            t2 = time.time()
            pkt_send_time = t2 - t1
            print('state_seq: %d, pkt_nums: %d, real_on_time: %f, pkt_send_time: %f, pkt_sum_byte: %d'
                % (i, pkt_nums, on_time_list[i], pkt_send_time, pkt_sum))
            # 保证发送时间符合on_off
            if pkt_send_time < on_time_list[i]:
                time.sleep(on_time_list[i] - pkt_send_time)
            # 发送时间过长，应减少off等待时间
            else:
                off_time_list[i] -= (pkt_send_time - on_time_list[i])
                if off_time_list[i] < 0:
                    continue
                pass
            time.sleep(off_time_list[i])
        s.close()
    except Exception as e:
        print("error!!", e.args[0])
        with open('pkt_gen2_error.log', 'w') as f:
            f.write(e.args)


def run_pkt_gen():
    pkt_cdf = parse_cdf_file()
    pkt_list = packet_length_cdf_dict2list(**pkt_cdf)
    t1 = time.time()
    # 返回
    on_time_list1, off_time_list1 = on_off_duration(args.duration, args.on_k, 
                                                    args.on_min, args.off_k, args.off_min)
    t2 = time.time()
    total_time1 = sum(on_time_list1) + sum(off_time_list1)
    print(len(on_time_list1), len(off_time_list1))
    print(on_time_list1, '\n', off_time_list1, '\n', total_time1)
    print('time: ', t2-t1)
    packet_timeline(on_time_list1, off_time_list1, pkt_list, args.rate, 
                    args.src, args.dst, args.src_port, args.dst_port)


if __name__ == '__main__':
    # I参数
    # ave_rate = 30  # 期望平均速率，拟定为30Mbps，单位：Mbps
    # pkt_gen_duration = 60  # 发包器持续时间，拟定为60s，单位：s
    # source_ip = '192.168.1.1'
    # target_ip = '192.168.1.2'
    # ON_k = 2
    # ON_min = 1
    # OFF_k = 2
    # OFF_min = 2
    # protocol_type = 'udp'  # 协议类型（udp？tcp）
    # pkt_cdf = {'1454': '1'}  # 包长是整数字节
    #
    # pkt_list = packet_length_cdf_dict2list(**pkt_cdf)
    # t1 = time.time()
    # on_time_list1, off_time_list1 = on_off_duration(pkt_gen_duration, ON_k, ON_min, OFF_k, OFF_min)
    # t2 = time.time()
    # total_time1 = sum(on_time_list1) + sum(off_time_list1)
    # print(len(on_time_list1), len(off_time_list1))
    # print(on_time_list1, '\n', off_time_list1, '\n', total_time1)
    # print('time: ', t2-t1)
    # packet_timeline(on_time_list1, off_time_list1, pkt_list, ave_rate, source_ip, target_ip)
    run_pkt_gen()




# python3 pkt_gen.py rate=30 duration=60 src_ip=192.168.1.1 dst_ip=192.168.1.2 on_k=2 on_min=1 off_k=2 off_min=2 file=MY_CDF.txt

