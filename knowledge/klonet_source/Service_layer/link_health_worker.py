import enum
import multiprocessing
import os
import signal
import time

import nsenter
from vemu_uestc.Service_layer.redis_error import TableNotExistError
import requests
import psutil
from nsenter import Namespace
from gevent import subprocess

from ..tools.context import redis_context
from ..tools.get_ne_pid import get_container_pid
from ..tools.tools import get_ctn_nic_mac, remove_quotes_in_list
from ..vemu_config.config import PROJ_CONFIG
from ..Implement_layer.LinkManager.link_operate import shell_execute
from ..Implement_layer.LinkHealthManager.l2ping import L2PingRequester, L2PingReplyer


class LinkCheckerWorker:
    """
    子拓扑的链路健康状态检查

    Attributes:
        user: 用户名
        project_name: 项目拓扑名
        subtopo: 子拓扑名
    """
    def __init__(self, user, project_name, subtopo):
        self.user = user
        self.project_name = project_name
        self.subtopo = subtopo

    def _is_checklink_entry_exists(self):
        '''
        查看某子拓扑的链路检查进程表项是否存在

        Returns:
            存在则返回True，不存在则返回False
        '''
        is_exists = False
        with redis_context(self.user) as user_db_cli:
            is_exists = user_db_cli.check_exist(
                f"{self.project_name}_checklink_pids", self.subtopo)
        return is_exists

    def _is_replyer_entry_exists(self):
        '''
        查看l2replyer进程表项是否存在

        Returns:
            存在则返回True，不存在则返回False
        '''
        is_exists = False
        with redis_context(self.user) as user_db_cli:
            is_exists = user_db_cli.check_exist(
                f"{self.project_name}_l2ping_replyer_pids", self.subtopo)
        return is_exists

    def start_check_process(self, is_check_once=True,
                            check_round_interval_s=180):
        '''
        启动检查链路进程

        Args:
            is_check_once: 是否只检查一轮
            check_round_interval_s：每轮检查间隔时间（秒）

        Returns:
            has_vxlan: 子拓扑是否含有vxlan标志
            broken_vxlans: 子拓扑是否含有已损坏的vxlan标志
        '''
        # 参数赋默认值
        if is_check_once == None:
            is_check_once = False
        if check_round_interval_s == None:
            check_round_interval_s = 180
        # 如果表项已存在，则认为链路检查进程存在，抛出异常
        if self._is_checklink_entry_exists():
            raise RuntimeError("process has already started!")
        # 检查vxlan监控，只检查一次即可
        has_vxlan, broken_vxlans =  \
            self._check_vxlans_health(is_check_once, check_round_interval_s)
        # 返回检查结果
        return has_vxlan, broken_vxlans

    def start_replyer_processes(self, is_check_once):
        '''
        启动l2ping的replyer进程

        Args:
            is_check_once: 是否只检查一轮
        '''
        # 参数赋默认值
        if is_check_once == None:
            is_check_once = False
        # 如果表项已存在，则认为链路检查进程存在，抛出异常
        # if self._is_replyer_entry_exists():
        #     raise RuntimeError("processes has already started!")
        # 获取l2ping的src信息
        # 即本worker的本子拓扑中所有vxlan名、所连源容器id、源容器网卡
        l2ping_srcs = self.get_l2ping_srcs()

        # 创建进程列表
        procs = []
        # 对每个vxlan，在源节点容器的网络空间中创建一个l2ping_replyer进程
        for l2ping_src in l2ping_srcs:
            vxlan, ctn_id, sintf = l2ping_src
            p = multiprocessing.Process(target=self.deploy_l2ping_replyer,
                                        args=(ctn_id, sintf, is_check_once))
            procs.append(p)
        
        # 开始运行所有进程，并记录进程号
        pids = []
        for p in procs:
            p.start()
            pids.append(p.pid)
        # 将进程号写入数据库
        if pids:
            with redis_context(self.user) as user_db_cli:
                user_db_cli.set_value(f"{self.project_name}_l2ping_replyer_pids",
                                      self.subtopo, pids)
        # 输出提示信息
        print("start l2ping_replyer successfully!")

    def stop_processes(self, process_type):
        '''
        停止检查链路或l2ping_replyer进程
        
        Args:
            process_type: 类型，可选值为checklink或l2ping_replyer
        
        Returns:
            {"code": 1, "msg": "Stop processes successfully!"}
            TODO(tie): 失败情况
        
        Raises:
            RuntimeError: 当停止进程失败时触发此异常
        '''
        # 判断类型取值合法性
        assert(process_type=="checklink" or process_type=="l2ping_replyer")
        # 统计需kill的进程号
        pids = []
        with redis_context(self.user) as user_db_cli:
            try:
                # 获取数据库中存储的进程号
                result = user_db_cli.get_value(
                    f"{self.project_name}_{process_type}_pids", self.subtopo)
                # 按不同类型加入pids列表
                if isinstance(result, int):
                    pids.append(result)
                elif isinstance(result, list):
                    pids.extend(result)
            # 若数据库中无存储的进程号表，说明所有进程已停止
            except TableNotExistError:
                return {"code": 1, 
                        "msg": f"Stop {process_type} process successfully!"}

        # TODO(tie): 如果没有查到进程号，比如该进程其实已经退出了？
        # 对每个需要kill的进程号
        for pid in pids:
            try:
                os.kill(pid, signal.SIGUSR1)
                os.kill(pid, signal.SIGTERM)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
                # print(f"pid {pid} not exist.")

            # 等待进程退出
            time.sleep(0.5)

            # 是否有必要确认是否杀干净？还是发了信号就不管了？
            # TODO(mt): 目前的删除会有僵尸进程问题，后续考虑用celery解决
            
            # 检测进程是否已经顺利kill，默认没有
            is_del_done = False
            try:    
                p = psutil.Process(pid)
                print(p)
                if p.status() == "zombie":
                    is_del_done = True
            except psutil.NoSuchProcess or psutil.ZombieProcess:
                is_del_done = True
            # 若没有顺利kill，则抛出异常
            if not is_del_done:
                raise RuntimeError(f"Stop {self.subtopo}'s {process_type} "
                    f"process {pid} failed.")
        
        # 删除数据库表项
        user_db_cli.del_value(f"{self.project_name}_{process_type}_pids",
                              self.subtopo)
        # 输出提示信息
        return {"code": 1, "msg": "Stop processes successfully!"}
        
    def _check_vxlans_health(self, is_check_once, check_round_interval_s):
        '''
        检查vxlan健康

        Args:
            is_check_once: 是否只检查一轮
            check_round_interval_s：每轮检查间隔时间（秒）

        Returns:
            has_vxlan: 子拓扑是否含有vxlan标志
            broken_vxlans: 子拓扑是否含有已损坏的vxlan标志

        '''
        # def signal_handler(sig, frame):
            # '''
            # 信号处理
            # '''
            # print(f"receive signal: {sig}")
            # global is_exit
            # is_exit = True
            # print(f"is_exit: {is_exit}")
        # print(f"is_check_once: {is_check_once}")
        # print(f"check_round_interval_s: {check_round_interval_s}")
        # signals_to_handle = [signal.SIGUSR1, signal.SIGTERM, signal.SIGINT]
        # for s in signals_to_handle:
            # signal.signal(s, signal_handler)
        # 默认返回值
        has_vxlan = None
        broken_vxlans = []
        while True:
            # 检查vxlan健康
            has_vxlan, broken_vxlans = self._traverse_check()
            # 若子拓扑没有vxlan，则直接返回，无需再次检查
            if not has_vxlan:
                # print("do not has vxlan, exit check process.")
                break
            # 若仅检查一轮，直接退出
            if is_check_once:
                # TODO(tie): 优雅退出，释放资源
                # print("is_check_once=True, exit check process.")
                # with redis_context(self.user) as user_db_cli:
                #     user_db_cli.del_value(
                #         f"{self.project_name}_checklink_pids",
                #         self.subtopo)
                # print(f"del value: {self.project_name}_checklink_pids, " 
                #     f"{self.subtopo}")
                break
            # 等待时间间隔，再次检查
            # print(f"check round done. sleep {check_round_interval_s} s...")
            time.sleep(check_round_interval_s)
        # 返回
        return has_vxlan, broken_vxlans

    def _traverse_check(self):
        '''
        遍历检查所有vxlan的健康，检查方式为l2ping vxlan对端容器
        检查完成后，若有坏掉的vxlan，上报至master
        
        Returns: 元组，形为：(has_vxlan, broken_vxlan_list)
            has_vxlan: 若有可供遍历的vxlan则返回true，否则返回false
            broken_vxlan_list: 已损坏的vxlan列表
        '''
        # 获取l2ping的src信息
        l2ping_srcs = self.get_l2ping_srcs()
        # 若子拓扑中无vxlan
        if not l2ping_srcs:
            return False, []
        # 若子拓扑中有vxlan
        else:
            # 从l2ping_srcs上使用l2ping检查连通性
            broken_vxlan_list = self.l2ping_from_srcs(l2ping_srcs)
            return True, broken_vxlan_list

    @staticmethod
    def l2ping_from_srcs(l2ping_srcs):
        '''
        从l2ping_srcs上使用l2ping检查连通性
        
        Args:
            l2ping_srcs: [
                (vxlan名, vxlan所连源容器id, 源容器网卡),
                ...
            ]
        Returns:
            broken_vxlan_list: 损坏的vxlan列表
        '''
        # 损坏的vxlan列表，默认为空列表
        broken_vxlan_list = []
        # vxlan源节点个数
        src_num = len(l2ping_srcs)
        # 实时打印相关参数
        next_print_percent = 10
        print_percent_interval = 10

        # 对每个源节点
        for i, l2ping_src in enumerate(l2ping_srcs, 1):
            # 提取vxlan名（vxlan）、源节点容器id（sid）、源节点网卡（sintf）
            vxlan, sid, sintf = l2ping_src 
            # 获取容器pid
            ctn_pid = get_container_pid(sid)
            # 获取网卡mac地址
            sintf_mac = get_ctn_nic_mac(sid, sintf)
            
            # 在容器的网络空间内执行python代码
            with Namespace(ctn_pid, 'net'):
                l2_ping_requester = L2PingRequester(sintf, sintf_mac)
                if not l2_ping_requester.exec_l2ping():
                    broken_vxlan_list.append(vxlan)

            # 每10%打印进度，给出反馈
            task_percent = i / src_num * 100
            if task_percent >= next_print_percent:
                # print(f"check link health: {task_percent}%")
                next_print_percent += print_percent_interval

        # 返回
        return broken_vxlan_list

    def _report2master(self, broken_vxlan_list):
        '''
        向master报告本轮链路健康检查结果
        '''
        report_dict = {
            "user": self.user,
            "project_name": self.project_name,
            "subtopo": self.subtopo,
            "broken_vxlan_list": broken_vxlan_list
            # "broken_veth_list"待扩展
        }
        master_url = (f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}"
                       "/master/link_report/")
        resp = requests.post(master_url, json=report_dict)
        # print(f"send {report_dict} to {master_url}.")

        return resp

    def get_l2ping_srcs(self):
        '''
        获取l2ping的src信息，即worker的子拓扑中所有vxlan名、所连源容器id、源容器网卡

        Returns:
            l2ping_srcs: 列表，如
                [
                    (vxlan名, vxlan所连源容器id, 源容器网卡),
                    ...
                ]
        
        Raises:
            ValueError: 当获取到的源容器id和对端容器ip数量不相等时触发此异常
        '''
        with redis_context(self.user) as user_db_cli:
            # 获取vxlan列表
            plane_subtopo = user_db_cli.get_value("plane_subtopo_list",
                                                  self.subtopo)
            vxlans = plane_subtopo.get("vxlanlinks")
            
            # 若子拓扑无vxlan，直接返回空列表
            if not vxlans:
                return []

            # 创建redis管道
            pipe = user_db_cli._db_conn.pipeline()

            # 提取每个vxlan的源节点名
            for vxlan in vxlans:
                # e.g. vxlan = link_l7276_vxlan2
                pipe.hget(f"{self.project_name}_{vxlan}", "source")
            src_c_names_temp = pipe.execute()
            # 获取到的节点名带引号，要去掉
            src_c_names = remove_quotes_in_list(src_c_names_temp)
            
            # 提取每个vxlan的源节点容器id
            for src_c_name in src_c_names:
                pipe.hget(f"{self.project_name}_{src_c_name}", "NEid")
            src_c_ids_temp = pipe.execute()
            # 获取到的节点名带引号，要去掉
            src_c_ids = remove_quotes_in_list(src_c_ids_temp)

            # 提取每个vxlan的源节点网卡
            for vxlan in vxlans:
                # e.g. vxlan = link_l7276_vxlan2
                pipe.hget(f"{self.project_name}_{vxlan}", "sourcePort")
            src_intfs_temp = pipe.execute()
            # 获取到的节点名带引号，要去掉
            src_intfs = remove_quotes_in_list(src_intfs_temp)
            
            # 若vxlan个数、源节点容器id数、源节点网卡数不全都相等，则抛出异常
            if len(vxlans) != len(src_c_ids) or len(vxlans) != len(src_intfs):
                raise ValueError(f"len(vxlan)={len(vxlan)}, "
                                 f"len(src_c_ids)={len(src_c_ids)}, "
                                 f"len(src_intfs)={len(src_intfs)}, "
                                 f"but they should be equal!")

            # 数据整理并返回
            l2ping_srcs = list(zip(vxlans, src_c_ids, src_intfs))                
            return l2ping_srcs

    @staticmethod
    def erase_ip_mask(ips_with_mask):
        '''
        去掉ip字符串的掩码后缀。如192.168.1.1/24 -> 192.168.1.1

        Args:
            ips_with_mask: 带掩码后缀的ip地址数组
        Returns:
            ips_without_mask: 不带掩码后缀的ip地址数组
        Raises:
            ValueError: 当待处理的ip没有/时触发此异常
        '''
        ips_without_mask = []
        for ip_with_mask in ips_with_mask:
            if ip_with_mask.find("/") == -1:
                raise ValueError(f"ip [{ip_with_mask}] do not have \"/\" !")
            
            ip_without_mask = ip_with_mask.split("/")[0]
            ips_without_mask.append(ip_without_mask)

        return ips_without_mask

    def deploy_l2ping_replyer(self, ctn_id, intf, is_check_once):
        '''
        在容器的网络空间中创建l2ping_replyer

        Args:
            ctn_id: 容器的id
            intf: 监听和发送l2数据包的网卡
            is_check_once: 是否只检查一轮
        '''
        # 获取容器pid
        pid = get_container_pid(ctn_id)
        # 获取节点指定网卡mac地址
        intf_mac = get_ctn_nic_mac(ctn_id, intf)
        # 在容器的网络空间中创建l2ping_replyer
        with Namespace(pid, 'net'):
            l2_ping_replyer = L2PingReplyer(intf, intf_mac)
            l2_ping_replyer.start_sniff(is_check_once)
        # 当start_sniff返回时会执行以下代码
        # with redis_context(self.user) as user_db_cli:
        #     user_db_cli.del_value(
        #         f"{self.project_name}_l2ping_replyer_pids",
        #         self.subtopo)
            # print(f"del value: {self.project_name}_l2ping_replyer_pids, " 
            #     f"{self.subtopo}")
