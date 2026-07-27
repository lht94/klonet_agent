from scapy.all import *

REQUEST_ETH_TYPE = 0x8310
REPLY_ETH_TYPE = REQUEST_ETH_TYPE

class L2PingReplyPacket(Ether):
    def answers(self, other):
        # type: (Packet) -> int
        if isinstance(other, Ether):
            if other.type == REPLY_ETH_TYPE:
                return self.payload.answers(other.payload)
        return 0

class L2PingRequester:
    '''
    L2ping请求器，在上层使用nsenter进入容器网络空间进行发包
    '''
    def __init__(self, intf, intf_mac, timeout_s=2, retry_time=0):
        '''
        intf: 发送请求包的网卡名（即与vxlan相连的网卡名）
        retry_time: 重试的次数
        '''
        self.intf = intf
        self.intf_mac = intf_mac
        self.timeout_s = timeout_s
        self.retry_time = retry_time
    
    def _build_req_pkt(self):
        '''
        构建request包
        '''
        # 由于vxlan的特殊性，因此使用广播地址
        pkt = Ether(src = self.intf_mac, dst = ETHER_BROADCAST, 
            type = REQUEST_ETH_TYPE)
        # self._myprint("l2ping request pkt:")
        # pkt.show()
        return pkt

    # def _send_receive_pkt(self, pkt, timeout_s, retry):
    #     '''
    #     发送一个l2数据包，并等待返回的第一个结果。
    #     若经过self.timeout_s还未收到，则视为超时
    #     超时后的重试次数为self.retry_time次
    #     '''
    #     sendp(pkt, iface=self.intf)
    #     receive_pkt = sniff(count=1, iface=self.intf, 
    #         filter=f"ether proto {REPLY_ETH_TYPE}")

    #     return receive_pkt

    def exec_l2ping(self):
        '''
        执行l2ping命令，该命令的效果类似于ping
        
        Returns:
            若可达返回True，否则返回False
        '''
        req_pkt = self._build_req_pkt()
        # 1. 如果不指定发送端口将可能会因为源mac不符合而发送不出去。
        # 2. srp1判定“answer”包的依据是执行接收到的数据包的answers方法。
        # 这里接收到的数据包的class为Ether，其answers方法的判定机制为
        # 两个Ether包的type相等，因此只要收到REPLY_ETH_TYPE的数据包，
        # 就算收到了回复（尽管是对端发过来的），但这也代表着该link是通的
        # 3. 修改Ether的answers方法是困难的，因此这里选择将request和reply包的
        # type设置为一致的。
        reply_pkt = srp1(req_pkt, iface=self.intf,
            timeout=self.timeout_s, retry=self.retry_time,
            verbose=False) # verbose为True则开启scapy自带的打印

        # print(f"srp1 result: {reply_pkt}")

        return True if reply_pkt else False

    def _myprint(self, string):
        print(f"[l2ping_requester]: {string}")



class L2PingReplyer:
    '''
    L2ping回复器，在上层使用nsenter开启进程，开启链路检查期间常驻于容器的网络空间
    '''
    def __init__(self, intf, intf_mac, reply_time=3):
        '''
        intf: 监听的网卡名（即与vxlan相连的网卡名）
        reply_time: 回复的次数
        '''
        self.intf = intf
        self.intf_mac = intf_mac
        self.reply_time = reply_time

    def _build_reply_pkt(self, request_pkt):
        '''
        根据request_pkt构建reply_pkt

        Args:
            request数据包
        '''
        reply_pkt = L2PingReplyPacket(
            src = self.intf_mac, 
            dst = request_pkt["Ethernet"].src, 
            type = REPLY_ETH_TYPE)

        return reply_pkt
    
    def _send_reply(self, request_pkt):
        # self._myprint("----------------request_pkt------------------")
        # request_pkt.show()

        # self._myprint("----------------reply_pkt------------------")
        reply_pkt = self._build_reply_pkt(request_pkt)
        # reply_pkt.show()

        reply_pkts = [reply_pkt for _ in range(self.reply_time)]
        # 如果不指定发送端口将可能会因为源mac不符合而发送不出去
        sendp(reply_pkts, iface=self.intf, verbose=False)

    def start_sniff(self, is_check_once):
        '''
        开始监听端口，若收到request则将回复reply
        '''
        # self._myprint(f"sniffing l2ping_pkt on {self.intf}...")
        sniff(iface=self.intf,
            count=1 if is_check_once else 0,
            filter=f"ether proto {REQUEST_ETH_TYPE}  and ether broadcast "
                f"and ! ether src {self.intf_mac}",  # 只抓request包
            prn=self._send_reply)
        # self._myprint("L2PingReplyer exit.")

    def _myprint(self, string):
        print(f"[l2ping_replyer]: {string}")

