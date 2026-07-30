"""
卫星工具函数库
"""

import os
from math import atan2, atan, acos, asin, sin, cos, pi, pow, sqrt, erfc
import numpy as np
from collections import defaultdict, deque
from time import strftime, localtime, time, sleep
from datetime import datetime
from sympy import symbols, solve
from nsenter import Namespace
import requests, multiprocessing, json
from celery import chain, chord, group
from skyfield.api import EarthSatellite, load
from skyfield.toposlib import wgs84

from ..vemu_config.config import PROJ_CONFIG
from ..Service_layer.NEManager import QuaggaEditor, QuaggaRunner, docker_cli
from ..Service_layer.redis_error import KeyNotExistError, TableNotExistError
from ..Service_layer.redisAPI import UserMapRedis, WorkerRedis
from ..tools.context import check_table_existence
from ..tools.tools import shell_execute, get_vxlan_vni, get_vxlan_ovs_id, \
    get_host_ip, generate_uuid_len_10
from ..webserver import celery
from ..Service_layer.redisAPI import PubSubRedis


# 默认使用的路由协议，包含 'ospf' / 'bgp' / 'rip'
router_protocol = 'ospf'

# 卫星跳出刷新循环时发生的Error
sat_update_error = (TableNotExistError, KeyNotExistError)


################### 和事件相关 ###################
# 管理事件发布/订阅的管理对象
pub_sub_redis = PubSubRedis()

class Event():
    """
    单个事件对象
    """
    def __init__(self, worker: str, func: str, para: dict) -> None:
        """
        单个事件初始化
        """
        self.worker = worker
        self.func = func
        self.para = para
    
    def __repr__(self):
        """
        打印字符串表示
        """
        return f"Event({self.worker}, {self.func})"

    def __hash__(self):
        return hash((self.worker, self.func, frozenset(self.para.items())))

    def __eq__(self, other):
        return self.worker == other.worker and \
            self.func == other.func and self.para == other.para
    
    def publish(self, pub_time, user, topo) -> None:
        """
        单个事件发布
        """
        pub_sub_redis.publish(self.worker, json.dumps({
            "time": pub_time,
            "user": user,
            "topo": topo,
            "func": self.func,
            "para": self.para
        }))

class EventSet():
    """
    事件集对象
    """
    def __init__(self) -> None:
        """
        单个事件初始化

        - self.evt_chains: 
          事件链，如 [ [e11, e12, ...], [e21, e22, ...], ... ]
        - self.wkr_chains: 
          每个事件链对应worker，如 ['10.1.1.1', '10.1.1.2', ...]
        - 要保证一个chain中的事件隶属于同一worker
        """
        self.wkr_chains = []
        self.evt_chains = []
    
    def show(self) -> str:
        """
        展示事件集
        """
        ret_str = ""
        for i in range(len(self.evt_chains)):
            ret_str += self.wkr_chains[i] + ": " + \
                ", ".join([e.func for e in self.evt_chains[i]]) + '\n'
        return ret_str

    def _split_list(self, events: list) -> dict:
        """
        把事件对象组成的列表，拆分为若干子列表
        每对子列表不能有相同的worker
        """
        groups = {}
        for e in events:
            wk = e.worker
            if wk in groups:
                groups[wk].append(e)
            else:
                groups[wk] = [e]
        return groups

    def add_chain(self, events: list):
        """
        新建事件链，本事件直接运行于事件集的发布时刻
        """
        groups = self._split_list(events)
        for k, v in groups.items():
            self.wkr_chains.append(k)
            self.evt_chains.append(v)

    def add_event_after_chain(self, events: list):
        """
        在上一事件链后插入事件，本事件将在事件链中顺次运行
        """
        groups = self._split_list(events)
        # 已存在的事件链数
        existed_chains_cnt = len(self.wkr_chains)
        
        # 对于新事件链
        for wk, chain in groups.items():
            # 获取最下面的相同worker的事件链序号
            index = existed_chains_cnt - 1
            while index >= 0:
                if self.wkr_chains[index] == wk: break
                index -= 1
            # 在此事件链后添加事件
            self.evt_chains[index] += chain
    
    def publish_all(self, pub_time, user, topo):
        """
        事件集所有事件发布
        """
        for i in range(len(self.wkr_chains)):
            chain = self.evt_chains[i]
            if len(chain) == 0: continue
            worker = self.wkr_chains[i]
            pub_sub_redis.publish(
                worker, json.dumps({
                    "time": pub_time,
                    "user": user,
                    "topo": topo,
                    "chain": [[event.func, event.para] for event in chain]
                })
            )

class EventScheduler():
    """
    事件调度器
    """
    def __init__(self):
        # 已注册的事件
        self.events = defaultdict(list)
        # DAG图的有向边
        self.edges = defaultdict(list)
        # 已分好组的事件集信息
        self.grouped_dict = {}

    def __repr__(self):
        """
        打印字符串表示
        """
        # 事件集数据处理
        if self.grouped_dict == {}:
            self.grouped_dict = self._group_events()
        return str(self.grouped_dict)

    def register_event(self, event: Event, dependencies=[]):
        """
        注册新事件，指定事件依赖的其他事件

        Args:
            event: 待注册的事件
            dependencies: 该事件依赖的其他事件名称列表
        """
        worker = event.worker
        if event in self.events[worker]:
            print(f"事件{event}已被注册")
        self.events[worker].append(event)
        event_index = len(self.events[worker]) - 1
        
        for dep in dependencies:
            try:
                dep_index = self.events[worker].index(dep)
                self.edges[worker].append([dep_index, event_index])
            except:
                print(f"依赖事件{dep}未找到")

    def register_events_without_dependency(self, events: list):
        """
        批量注册无依赖的新事件

        Args:
            events: 待注册的事件列表
        """
        for event in events:
            self.register_event(event)

    def _split_horizontal(self, nodes: list, edges: list) -> list:
        """
        横向划分

        Args:
            nodes: 当前考虑点的集合
            edges: 所有边的集合
        """
        if len(nodes) == 1: return nodes
        class UnionFind:
            def __init__(self, nodes):
                self.root = {}
                self.rank = {}
                for n in nodes:
                    self.root[n] = n
                    self.rank[n] = 1

            def find(self, x):
                if x in self.root:
                    if self.root[x] != x:
                        self.root[x] = self.find(self.root[x])  # 路径压缩
                    return self.root[x]
                return None

            def union(self, x, y):
                rootX = self.find(x)
                if (rootX == None): return
                rootY = self.find(y)
                if (rootY == None): return

                if rootX != rootY:
                    if self.rank[rootX] > self.rank[rootY]:
                        self.root[rootY] = rootX
                    elif self.rank[rootX] < self.rank[rootY]:
                        self.root[rootX] = rootY
                    else:
                        self.root[rootY] = rootX
                        self.rank[rootX] += 1

        uf = UnionFind(nodes)
        for x, y in edges:
            uf.union(x, y)

        # 将每个节点的根节点找出，根节点相同的属于同一连通分量
        components = {}
        for n in nodes:
            root = uf.find(n)
            if root not in components:
                components[root] = []
            components[root].append(n)
        
        # 把字典的值转换为列表输出
        return list(components.values())

    def _split_vertical(self, nodes, edges: list) -> list:
        """
        纵向划分

        Args:
            nodes: 当前考虑点的集合
            edges: 所有边的集合
        """
        if isinstance(nodes, int):
            return nodes
        if len(nodes) == 1:
            return nodes
        
        # 节点入度 / 出度
        in_degree = {n: 0 for n in nodes}
        out_degree = {n: 0 for n in nodes}
        # 节点后继
        next = defaultdict(list)
        # 统计度和后继
        for u, v in edges:
            if u in nodes and v in nodes:
                next[u].append(v)
                out_degree[u] += 1
                in_degree[v] += 1
        in_degree_tmp = dict(in_degree)

        # 所有入度为0的点
        qu = deque([n for n in nodes if in_degree[n] == 0])
        # 节点层级: 层级 -> 该层级节点列表
        levels = defaultdict(list)
        # 当前层级
        cur_level = 0

        # 用队列实现的拓扑排序（BFS）
        while qu:
            # 当前层级的点数
            level_size = len(qu)
            for _ in range(level_size):
                node = qu.popleft()
                levels[cur_level].append(node)
                # 将当前节点的所有邻接节点的入度减1
                for neighbor in next[node]:
                    in_degree_tmp[neighbor] -= 1
                    if in_degree_tmp[neighbor] == 0:
                        qu.append(neighbor)
            # 层级自增
            cur_level += 1

        # 搜索层间连接关系
        dets = []
        # 可直接分割的层级，存在全连接
        lvs = []
        # 遍历每一个层间关系
        for i in range(len(levels) - 1):
            # 层间全连接的边数的2倍
            full_conn_edge_cnt = len(levels[i]) * len(levels[i + 1]) * 2
            # 层间真实连接的上下层度之和
            real_conn_edge_cnt = 0
            for u, v in edges:
                if u in nodes and v in nodes:
                    if u in levels[i]:
                        real_conn_edge_cnt += 1
                    if v in levels[i + 1]:
                        real_conn_edge_cnt += 1
            # 两者差值
            det = full_conn_edge_cnt - real_conn_edge_cnt
            if det <= 0: lvs.append(i)
            dets.append(det)
        
        # 若无层间全连接，则在与全连接差最小处分割
        if lvs == []:
            lvs = [dets.index(min(dets))]
        lvs.append(len(levels) - 1)
        
        # 点集分割
        from_l = 0
        ret = []
        for l in lvs:
            ret.append(sum([levels[i] for i in range(from_l, l + 1)], []))
            from_l = l + 1
        return ret

    def _split_dag(self, nodes: list, edges: list) -> list:
        """
        混合划分

        Args:
            nodes: 当前考虑点的集合
            edges: 所有边的集合
        """
        # 横向划分
        h_parts = self._split_horizontal(nodes, edges)
        ret = list(h_parts)
        
        for i, h_nodes in enumerate(h_parts):
            # 纵向划分
            v_parts = self._split_vertical(h_nodes, edges)
            if  h_nodes == v_parts:
                continue
            ret[i] = list(v_parts)
            
            for j, v_nodes in enumerate(v_parts):
                ret[i][j] = self._split_dag(v_nodes, edges)
        
        return ret

    def _replace_elements(self, nested_list):
        for i in range(len(nested_list)):
            if isinstance(nested_list[i], list):
                if len(nested_list[i]) == 1 and isinstance(nested_list[i][0], int):
                    nested_list[i] = nested_list[i][0]
                else:
                    self._replace_elements(nested_list[i])
        return nested_list

    def _group_events(self):
        """
        根据事件的依赖关系对事件进行分组，使每个组内的事件可以并行执行
        
        Returns: 
            列表，其中每个元素是一组可以并行执行的事件
        """
        groups = defaultdict(list)
        for worker, events in self.events.items():
            # worker上子图的点集
            worker_nodes = [i for i in range(len(events))]
            # worker上子图的边集
            worker_edges = self.edges[worker]
            # 对于每个worker的DAG图，对图进行横竖分解，得到workflow列表
            groups[worker] = self._split_dag(worker_nodes, worker_edges)
        return groups

    def publish_all(self, exe_time, user, topo):
        """
        处理并发布事件集所有事件

        Args:
            exe_time: 任务执行时刻
            user: 用户名
            topo: 拓扑名
        """
        # 事件集数据处理
        if self.grouped_dict == {}:
            self.grouped_dict = self._group_events()
        # 事件发布
        for worker, workflow in self.grouped_dict.items():
            pub_sub_redis.publish(
                worker, json.dumps({
                    "time": exe_time,
                    "user": user,
                    "topo": topo,
                    "workflow": workflow,
                    "events": [[event.func, event.para] for event in self.events[worker]]
                })
            )

################### 和组网相关 ###################
def int2ip(number: int):
    """
    将一个int数转化为ipv4
    """
    ret = ''
    for i in range(4):
        ret += str(int(number / 256 ** (3-i) % 256)) + '.'
    return ret[:-1]

def ip2int(ip: str):
    """
    将一个ipv4字符串转化为int数
    """
    ret = 0
    for i, num in enumerate(ip.split('.')):
        ret += int(num) * 256 ** (3-i)
    return ret

def netmask2cidr(netmask: str):
    """
    将掩码转换为CIDR中“/”后面的数字
    """
    return ''.join(format(int(part), '08b')
                   for part in netmask.split('.')).count('1')

def cidr2netmask(cidr_num: int):
    """
    将CIDR中“/”后面的数字转换为掩码
    """
    parts = [0, 0, 0, 0]
    for i in range(cidr_num):
        parts[i // 8] |= (1 << (7 - i % 8))
    return '.'.join(map(str, parts))

def _subnet_count(ip_mask_list: list):
    """
    子网数统计

    Args:
        ip_mask_list: 列表，每个元素是一个长度为2的列表，第0个元素是ip，第1个元素是掩码

    Return:
        返回列表中各个ip所属于的子网数
    """
    subnets = set()
    for ip_mask in ip_mask_list:
        subnets.add(ip2int(ip_mask[0]) & ip2int(ip_mask[1]))
    return len(subnets)

def is_subnet_of(cidr_net: str, ip: str):
    """
    判断ip是否在cidr_net网段中
    """
    net_ip, cidr_num = cidr_net.split('/')
    return int2ip(ip2int(ip) & ip2int(cidr2netmask(int(cidr_num)))) == net_ip

# 卫星对地子网掩码的int类型
sat_gnd_subnet_mask_int = ip2int(PROJ_CONFIG.sat_gnd_subnet_mask)

def get_next_ip(sat_gnd_nets, sat):
    """
    更改子网字典中的下一可用ip

    Args:
        sat_gnd_nets: 卫星对地子网映射字典
        sat: 卫星
        
    Returns:
        bool，为True说明发生溢出，否则为False
    """
    # 子网内下一可用ip自增
    # list是可变类型，故这样修改有效
    sat_gnd_nets[sat] += 1
    # 网络号
    net = sat_gnd_nets[sat] & sat_gnd_subnet_mask_int
    # 最后一个可用ip
    last = net +  2 ** (32 - netmask2cidr(PROJ_CONFIG.sat_gnd_subnet_mask)) - 2
    # 判断是否溢出
    if sat_gnd_nets[sat] >= last:
        sat_gnd_nets[sat] = net + 2
        return True
    return False

################### 和设备相关 ###################
ctn_type = {
    "h": "host",
    "r": "router",
    "s": "switch"
}

def is_on_same_worker(topo, user_db_cli, dev1, dev2):
    """
    判断两容器节点是否在同一worker上
    """
    return user_db_cli.get_worker_ip_by_ne_name(topo, dev1) == \
        user_db_cli.get_worker_ip_by_ne_name(topo, dev2)

################### 和星座相关 ###################
def dBW2W(db):
    """
    单位转换，将dBW转换为W
    """
    return 10 ** (db/10)

def get_walker_para(walker_dict):
    """
    从星座字典中获得N、P、i、F、h、sensor_angle等参数
    以创建walker对象
    """
    return [walker_dict[key] for key in ["N", "P", "i", "F", "h", "sensor_angle"]]

def timestamp2date(timestamp):
    """
    将时间戳转化为日期的格式

    Args:
        timestamp: 时间戳，单位为秒

    Returns:
        list，包含年、月、日、时、分、秒六个元素
    """
    timeArray = localtime(timestamp)
    time_list = strftime("%Y-%m-%d-%H-%M-%S", timeArray).split('-')
    return [int(val) for val in time_list]

def wgs84_to_spotdown(wgs84):
    """
    将wgs84坐标转化为星下点和海拔高度
    """
    # 经度：东经正数，西经负数
    lon = atan2(wgs84[1], wgs84[0]) * 180 / pi
    # 海拔：距地心高度
    alt = np.linalg.norm(wgs84)
    # 纬度：北纬正数，南纬负数
    try:
        lat = asin(wgs84[2] / alt) * 180 / pi
    except ValueError:
        lat = 90 if wgs84[2] / alt > 1 else -90
    # 返回
    return lon, lat, alt 

def spotdown_to_wgs84(lon, lat, alt):
    """
    将星下点和海拔高度转化为wgs84直角坐标
    """
    x = alt * cos(lat/180*pi) * cos(lon/180*pi)
    y = alt * cos(lat/180*pi) * sin(lon/180*pi)
    z = alt * sin(lat/180*pi)
    return x, y, z

class Walker():
    """
    单层 Walker 星座对象
    """
    def __init__(self, t, N, P, i, h, F, sensor_angle=170):
        # 时间参数
        self.time = t

        # 星座参数
        self.N = N         # 卫星总数
        self.P = P         # 星座轨道面数
        self.i = i         # 卫星轨道倾角，单位：°
        self.h = h         # 卫星轨道半径，单位：km
        self.F = F         # 相位因子，0~(S-1)间的整数，代表相邻两轨道面星间相位关系
        self.S = int(N/P)  # 每轨道卫星数
        self.sat_ang = sensor_angle  # 天线张角
        
        # 邻轨星间链路天线旋转的最大角度，体现在两星距离上
        self.dist_min = \
            cos(acos(sin(i*pi/180)**2 * cos(pi/P) + cos(i*pi/180)**2) / 2) \
            * F * h / N / sin(sensor_angle/360*pi) * 2 * pi
        
        # 所有卫星对象
        self.sats = []
        TLE_LINE1 = '1 48580U 21041AD  23059.20970124  .00000000  00000+0  00000+0 0  000'
        for tle_line2 in self._generate_tles_line2():
            self.sats.append(EarthSatellite(TLE_LINE1, tle_line2))

        # 卫星位置，惰性更新
        self.wgs84_pos = []

    def _generate_tles_line2(self):
        """
        生成walker星座中所有卫星的TLE星历的第二行
        
        Returns:
            list，包含所有卫星的第二行TLE，例：
            ['2 48580 070.0000 270.0000 0000000 000.0000 459.0000 12.13298926 99386', ...]
        """
        def _tle_format(num, all=8, dec=4):
            return str(f"%.{dec}f"%num).zfill(all)
        
        tles = []                     # 计算各卫星tle，并加入该列表
        detu = 360 / self.N * self.F  # 邻轨对应卫星间的相位差
        # walker星座各卫星每天绕地圈数
        circles = sqrt(PROJ_CONFIG.GM) * 12 * 3600 / pi / pow(self.h*1000, 1.5)

        for sat_id in range(self.N):
            Pm = int(sat_id / self.S)  # 轨道面编号，0 ~ P-1
            Nm = sat_id % self.S       # 轨道内编号，0 ~ S-1
            omega_m = 180 / self.P * Pm          # 升交点赤经
            u_m = 360 / self.S * Nm + detu * Pm  # 升交点角距
            tles.append(
                f'2 48580 {_tle_format(self.i)} {_tle_format(omega_m)} 0000000 000.0000 '
                f'{_tle_format(u_m)} {_tle_format(circles,11,8)} 99386'
            )
        
        return tles

    def _get_sat_dist(self, pos1, pos2):
        """
        计算卫星间的距离
        
        Args: 
            pos1, pos2: 两星wgs84坐标
        
        Returns:
            卫星间的角度，不可见则返回inf
        """
        # 计算向量夹角，保证acos不出错
        cosL = (pos1[0]*pos2[0]+pos1[1]*pos2[1]+pos1[2]*pos2[2])/self.h/self.h
        if cosL <= -1:
            L = pi
        elif cosL >= 1:
            L = 0
        else:
            L = acos(cosL)
        # 若被地球挡住，则不可见，否则返回两星距离
        return np.inf if self.h * cos(L/2) <= PROJ_CONFIG.earth_r \
            else 2 * self.h * sin(L/2)

    def _satellite_run(self, yr, mon, day, hr, mins, sec, sat, pos='xyz'):
        """
        计算某时刻卫星对象的位置

        Args:
            yr, mon, day, hr, mins, sec: 年月日时分秒
            sat: 卫星对象
            pos: 输出格式
                - 'spt': 星下点（经度、纬度、高度）输出
                - 'xyz': WGS84三维坐标（x/y/z）输出

        Return: 
            坐标列表
        """
        t = load.timescale().utc(yr, mon, day, hr, mins, sec)
        geocentric = sat.at(t)
        # 转化为wgs84
        wgs84_pos = wgs84.geographic_position_of(geocentric)
        lon = wgs84_pos.longitude.degrees
        lat = wgs84_pos.latitude.degrees
        alt = wgs84_pos.elevation.km
        # 按格式输出
        if pos == 'xyz':
            return [
                (alt + wgs84.radius.km) * cos(lat/180*pi) * cos(lon/180*pi),
                (alt + wgs84.radius.km) * cos(lat/180*pi) * sin(lon/180*pi),
                (alt + wgs84.radius.km) * sin(lat/180*pi)
            ]
        return [lon, lat, alt+wgs84.radius.km] 
    
    def get_spot_down(self):
        """
        获取walker星座中所有卫星某一时刻的星下点，包括：经度+纬度+高度

        Returns: 
            list，包含所有卫星的这三个量，这三个量用list呈现
        """
        spot_down = []
        for sat in self.sats:
            spot_down.append(
                self._satellite_run(*self.time, sat, pos='spot_down'))
        
        return spot_down
    
    def get_onesat_wgs84_pos(self, sat_id):
        """
        获取walker星座中单个卫星某一时刻的wgs84位置，包括xyz三维

        Args:
            sat_id: 卫星编号

        Returns: 
            list，包含卫星的xyz三个量
        """
        if self.wgs84_pos:
            return self.wgs84_pos[sat_id]
        return self._satellite_run(*self.time, self.sats[sat_id])
    
    def get_wgs84_pos(self):
        """
        获取walker星座中所有卫星某一时刻的wgs84位置，包括xyz三维

        Returns: 
            list，包含所有卫星的这三个量，这三个量用list呈现
        """
        if self.wgs84_pos == []:
            for sat in self.sats:
                self.wgs84_pos.append(self._satellite_run(*self.time, sat))
        return self.wgs84_pos

    def get_intra_links_in_walker(self):
        """
        获取walker星座中所有同轨连接

        Returns: 
            links: list，星间连接关系
                         如[[1, 2, 2000], [2, 3, 3000], [3, 1, 4000]]
                         说明三者依次连接，前两个数字代表卫星ID，
                         最后的数字代表星间距离，单位km
            no_link: list，无星间连接
                           如[[2, 4], [3, 6]]
                           说明卫星ID之间不存在链路
        """
        # 卫星xyz位置
        wgs84_pos = self.get_wgs84_pos()
        # 有连接的链路
        links = []
        # 无连接的链路
        no_link = []
        # 同轨顺次连接
        for i in range(self.P):
            for j in range(self.S):
                a = i * self.S + j                 # 同轨两颗卫星 a
                b = i * self.S + (j + 1) % self.S  # 同轨两颗卫星 b
                if a != b:
                    dist = self._get_sat_dist(wgs84_pos[a], wgs84_pos[b])
                    if dist != np.inf:
                        links.append([a, b, dist])
                    else:
                        no_link.append([a, b])

        return links, no_link

    def get_inter_links_in_walker(self):
        """
        获取walker星座中所有临轨连接

        Returns: 
            links: list，星间连接关系
                         如[[1, 2, 2000], [2, 3, 3000], [3, 1, 4000]]
                         说明三者依次连接，前两个数字代表卫星ID，
                         最后的数字代表星间距离，单位km
            no_link: list，无星间连接
                           如[[2, 4], [3, 6]]
                           说明卫星ID之间不存在链路
        """
        # 卫星xyz位置
        wgs84_pos = self.get_wgs84_pos()
        # 有连接的链路
        links = []
        # 无连接的链路
        no_link = []
        # 邻轨相互连接：和临轨差一个相位且可见的卫星相连
        for sat_id in range(self.N):
            if int(sat_id / self.S) == self.P-1:
                break
            target_id = sat_id + self.S
            dist = self._get_sat_dist(wgs84_pos[sat_id], wgs84_pos[target_id])
            if dist != np.inf and dist > self.dist_min:
                links.append([sat_id, target_id, dist])
            else:
                no_link.append([sat_id, target_id])

        return links, no_link

    def get_links_in_walker(self):
        """
        获取walker星座中所有需要连接的链路
        +grid方式，同轨直连，临轨和差一个相位的卫星相连

        Returns: 
            links: list，星间连接关系
                         如[[1, 2, 2000], [2, 3, 3000], [3, 1, 4000]]
                         说明三者依次连接，前两个数字代表卫星ID，
                         最后的数字代表星间距离，单位km
            no_link: list，无星间连接
                           如[[2, 4], [3, 6]]
                           说明卫星ID之间不存在链路
        """
        links_intra, no_link_intra = self.get_intra_links_in_walker()
        links_inter, no_link_inter = self.get_inter_links_in_walker()
        return links_intra + links_inter, no_link_intra + no_link_inter

class Walkers():
    """
    多层Walker星座对象
    """
    def __init__(self, t, walkers_para, gnd_devs_para):
        # 时间参数
        self.time = t
        # 创建多层星座
        self.walkers = [Walker(t, *list(p.values())[1:]) for p in walkers_para]
        # 星地链路极限值
        self.dev_sat_limit_val = defaultdict(list)
        for dev, para in gnd_devs_para.items():
            for walker in self.walkers:
                self.dev_sat_limit_val[dev].append(
                    self._get_limit_elevation_ang_or_dist(
                        PROJ_CONFIG.earth_r,
                        walker.h,
                        PROJ_CONFIG.gnd_dev_level[para['antenna_level'] - 1][2],
                        walker.sat_ang
                    )
                )
        # 星座链路极限值
        self.sat_sat_limit_val = []
        for i in range(len(self.walkers) - 1):
            self.sat_sat_limit_val.append(
                self._get_limit_elevation_ang_or_dist(
                    self.walkers[i].h, self.walkers[i+1].h,
                    self.walkers[i].sat_ang, self.walkers[i+1].sat_ang,
                    output="dist"
                )
            )

    def _get_limit_elevation_ang_or_dist(self, h1, h2, up_ang, down_ang, output="elevation_ang"):
        """
        获取两设备间的极限值，值的类型是最小仰角/最大距离
        计算星地链路和不同高度轨道间的星座链路使用

        Args:
            h1, h2: 两设备高度
            up_ang: 位于低处的设备向上看的最大张角
            down_ang: 位于高处的设备向下看的最大张角
            output: 输出内容，"elevation_ang"指输出最小仰角，"dist"指输出最大距离
            
        Returns:
            单位为度的最小仰角
        """
        # 模型准备
        h = max(h1, h2)
        R = min(h1, h2)
        K = h * h - R * R
        cos_2 = cos(down_ang/360*pi)
        cos_1 = cos(up_ang/360*pi)
        # 模型求解
        l = symbols('l', real=True)
        f1 = l * l - 2 * l * h * cos_2 + K
        f2 = l * l + 2 * l * R * cos_1 - K
        ans1 = solve([f1])  # 第一个方程的解集，可能0~2个解
        ans2 = solve([f2])  # 第二个方程的解集，有2个解
        if len(ans1) != 2:
            l = ans2[1][l]  # 设备距离最大值
        else:
            if ans1[1][l] <= ans2[1][l]:
                l = ans2[1][l]
            else:
                l = min(ans1[0][l], ans2[1][l])
        if output == "dist":
            return l
        # 满足约束的最优值
        M = acos((h*h + R*R - l*l) /2 /R /h)  # ∠3的最大值
        if M >= (up_ang + down_ang) / 2:
            return 90 - up_ang / 2
        else:
            return 90 - down_ang / 2 - M
    
    # 星间链路
    def get_intra_links_in_walker(self):
        """
        获取多层walker星座中所有需连接的同层的同轨链路

        Returns: 
            links: list，星间连接关系
                        如[[1, 2, 2000], [2, 3, 3000], [3, 1, 4000]]
                        说明三者依次连接，前两个数字代表卫星ID，
                        最后的数字代表星间距离，单位km
            no_link: list，无星间连接
                        如[[2, 4], [3, 6]]
                        说明卫星ID之间不存在链路
        """
        links = []
        no_link = []
        existed_sat_id = 0
        for walker in self.walkers:
            tmp1, tmp2 = walker.get_intra_links_in_walker()
            links += [[e[0] + existed_sat_id, e[1] + existed_sat_id, e[2]]
                      for e in tmp1]
            no_link += [[e[0] + existed_sat_id, e[1] + existed_sat_id]
                        for e in tmp2]
            existed_sat_id += walker.N
        return links, no_link
    
    def get_inter_links_in_walker(self):
        """
        获取多层walker星座中所有需连接的同层的临轨链路

        Returns: 
            links: list，星间连接关系
                        如[[1, 2, 2000], [2, 3, 3000], [3, 1, 4000]]
                        说明三者依次连接，前两个数字代表卫星ID，
                        最后的数字代表星间距离，单位km
            no_link: list，无星间连接
                        如[[2, 4], [3, 6]]
                        说明卫星ID之间不存在链路
        """
        links = []
        no_link = []
        existed_sat_id = 0
        for walker in self.walkers:
            tmp1, tmp2 = walker.get_inter_links_in_walker()
            links += [[e[0] + existed_sat_id, e[1] + existed_sat_id, e[2]]
                      for e in tmp1]
            no_link += [[e[0] + existed_sat_id, e[1] + existed_sat_id]
                        for e in tmp2]
            existed_sat_id += walker.N
        return links, no_link

    def get_links_in_walker(self):
        """
        获取多层walker星座中所有需连接的同层链路

        Returns: 
            links: list，星间连接关系
                        如[[1, 2, 2000], [2, 3, 3000], [3, 1, 4000]]
                        说明三者依次连接，前两个数字代表卫星ID，
                        最后的数字代表星间距离，单位km
            no_link: list，无星间连接
                        如[[2, 4], [3, 6]]
                        说明卫星ID之间不存在链路
        """
        links_intra, no_link_intra = self.get_intra_links_in_walker()
        links_inter, no_link_inter = self.get_inter_links_in_walker()
        return links_intra + links_inter, no_link_intra + no_link_inter
    
    # 星地链路
    def is_visible_sat_gnd_sat(self, dev: str, dev_para: dict, sat_id) -> int:
        """
        判断卫星是否被地面设备可见

        Args:
            dev: 地面站名
            dev_para: 包含地面站经纬度和可视角度的list
            sat_id: 所判断的卫星id

        Returns: 
            int，若为0，表示卫星不可见；否则表示星地距离
        """
        if sat_id == None:
            return False
        # 地面站经纬度，单位rad
        lon = dev_para['position'][0] / 180 * pi
        lat = dev_para['position'][1] / 180 * pi

        # 判断旧卫星是否可见
        existed_sat_id = 0
        for i, walker in enumerate(self.walkers):
            if sat_id < existed_sat_id + walker.N:
                # 本层卫星的最小仰角
                min_zeta = self.dev_sat_limit_val[dev][i]
                # 计算当前旧卫星的高度角
                pos = walker.get_wgs84_pos()[sat_id]
                L = acos(sin(lat) * sin(pos[1]/180*pi) + \
                         cos(lat) * cos(pos[1]/180*pi) * cos(lon - pos[0]/180*pi))
                alt_zeta = atan((cos(L) - PROJ_CONFIG.earth_r / pos[2]) / sin(L)) * 180 / pi
                # 高度角 >= 最小仰角，则卫星可见
                if alt_zeta >= min_zeta:
                    # 星地距离
                    return sqrt(PROJ_CONFIG.earth_r ** 2 + walker.h ** 2 - \
                                2 * PROJ_CONFIG.earth_r * walker.h * cos(L))
                else:
                    return 0
            existed_sat_id += walker.N
        return False

    def get_all_visible_sats_gnd_sat(self, dev: str, dev_para: dict) -> list:
        """
        获取walker星座所有地面设备可见卫星

        Args:
            dev: 地面站名
            dev_para: 包含地面站经纬度和可视角度的list

        Returns: 
            list，包含所有可见卫星的卫星id（不带距离）
        """
        # 地面站经纬度，单位rad
        lon = dev_para['position'][0] / 180 * pi
        lat = dev_para['position'][1] / 180 * pi
        # 仰角最小值
        limit = self.dev_sat_limit_val[dev]
        # 所有的可见卫星，其中为卫星的全局id
        all_visible_sats = []

        existed_sat_id = 0
        for i, walker in enumerate(self.walkers):
            # 本层walker的卫星位置
            wgs84_pos = walker.get_wgs84_pos()
            # 本层walker的仰角最小值
            min_zeta = limit[i]
            
            # 对本层walker的各个卫星
            for sat_id, _ in enumerate(walker.sats):
                # 卫星位置
                pos = wgs84_pos[sat_id]
                # 计算高度角
                L = acos(sin(lat) * sin(pos[1]/180*pi) + \
                        cos(lat) * cos(pos[1]/180*pi) * cos(lon - pos[0]/180*pi))
                alt_zeta = atan((cos(L) - PROJ_CONFIG.earth_r / pos[2]) / sin(L)) * 180 / pi
                # 高度角 >= 最小仰角，则卫星可见
                if alt_zeta >= min_zeta:
                    all_visible_sats.append(sat_id + existed_sat_id)
            existed_sat_id += walker.N

        return all_visible_sats

    def get_links_gnd_sat(self, dev: str, dev_para: dict,
                          old_sat, method=1) -> tuple:
        """
        获取多层walker星座中可连接到地面设备的卫星

        Args:
            dev: 地面站名
            dev_para: 包含地面站经纬度和可视角度的list
            old_sat: 旧连接卫星id
            method: 1(最短距离) / 2(最大剩余可见时间)

        Returns: 
            tuple，第一个值是本次连接卫星，第二个值是星地距离
        """
        # 若旧连接卫星依旧可见，则直接返回旧连接卫星
        visible = self.is_visible_sat_gnd_sat(dev, dev_para, old_sat)
        if visible:
            return old_sat, visible

        # 地面站经纬度，单位rad
        lon = dev_para['position'][0] / 180 * pi
        lat = dev_para['position'][1] / 180 * pi
        
        # 仰角最小值
        limit = self.dev_sat_limit_val[dev]

        # 计算新的最佳卫星
        best_sat = best_dist = best_val = None
        existed_sat_id = 0
        for i, walker in enumerate(self.walkers):
            # 本层walker的卫星位置
            wgs84_pos = walker.get_wgs84_pos()
            # 本层walker的仰角最小值
            min_zeta = limit[i]

            # 对本层walker的各个卫星
            for sat_id, sat in enumerate(walker.sats):
                # 卫星位置
                pos = wgs84_pos[sat_id]
                # 计算高度角
                L = acos(sin(lat) * sin(pos[1]/180*pi) + \
                         cos(lat) * cos(pos[1]/180*pi) * cos(lon - pos[0]/180*pi))
                alt_zeta = atan((cos(L) - PROJ_CONFIG.earth_r / pos[2]) / sin(L)) * 180 / pi
                # 高度角 >= 最小仰角，则卫星可见
                if alt_zeta >= min_zeta:
                    # 星地距离
                    dist = sqrt(PROJ_CONFIG.earth_r ** 2 + walker.h ** 2 - \
                                2 * PROJ_CONFIG.earth_r * walker.h * cos(L))
                    # 最短距离
                    if method == 1:
                        if best_val == None or best_val > dist:
                            best_sat = sat_id
                            best_dist = best_val = dist

                    # 最长剩余可见时间
                    else:
                        # 通过试探法，得到卫星剩余可见时长
                        this_last_time = 0
                        for k in range(1, PROJ_CONFIG.max_try_visible_time_left):
                            # 试探时刻
                            time_try = self.time[:]
                            time_try[-2] += k * PROJ_CONFIG.det_t_visible_time_left
                            # 试探时刻的卫星位置
                            r = walker._satellite_run(*time_try, sat)
                            # 试探时刻的卫星高度角
                            L = acos(sin(lat) * sin(r[1]/180*pi) + \
                                     cos(lat) * cos(r[1]/180*pi) * cos(lon - r[0]/180*pi))
                            alt_zeta = atan((cos(L) - PROJ_CONFIG.earth_r / r[2]) / sin(L)) * 180 / pi
                            # 试探时刻的卫星高度角 <= 仰角，则不可见
                            if alt_zeta < min_zeta:
                                this_last_time = (k - 1) * PROJ_CONFIG.det_t_visible_time_left
                                break
                        if this_last_time == 0:
                            this_last_time = (PROJ_CONFIG.max_try_visible_time_left - 1) * \
                                PROJ_CONFIG.det_t_visible_time_left
                        
                        if best_val == None or best_val < this_last_time:
                            best_sat = sat_id
                            best_val = this_last_time
                            best_dist = dist
            
            existed_sat_id += walker.N

        return best_sat, best_dist

    # 星座链路
    def is_visible_sat_sat_sat(self, level: int, low_sat: int, high_sat) -> int:
        """
        判断高层卫星是否被低层卫星可见

        Args:
            level: 星座链路层次
            low_sat: 低层卫星id
            high_sat: 高层卫星id

        Returns: 
            int，若为0，表示卫星不可见；否则表示星地距离
        """
        if high_sat == None:
            return False
        existed_sat_id = 0 if level == 0 else self.walkers[0]
        low_pos = self.walkers[level].get_onesat_wgs84_pos(
            low_sat - existed_sat_id
        )
        high_pos = self.walkers[level+1].get_onesat_wgs84_pos(
            high_sat - existed_sat_id - self.walkers[level].N
        )
        # 计算两星距离
        dist = sqrt((low_pos[0] - high_pos[0])**2 + \
                    (low_pos[1] - high_pos[1])**2 + \
                    (low_pos[2] - high_pos[2])**2)
        # 返回可见性
        return dist if dist < self.sat_sat_limit_val[level] else 0
    
    def get_all_visible_sats_sat_sat(self):
        """
        获取walker星座中不同高度轨道间低轨卫星可见的所有高轨卫星
            
        Returns:
            dict，key是卫星id，value是所有可见卫星（不带距离）
        """
        all_visible_sats = defaultdict(list)
        
        for level in range(len(self.walkers) - 1):
            # 两星最大连接距离
            max_dist = self.sat_sat_limit_val[level]
            # 较低轨卫星的wgs84坐标
            low_sat_poses = self.walkers[level].get_wgs84_pos()
            # 较高轨星座中所有卫星的wgs84坐标
            high_sat_poses = self.walkers[level+1].get_wgs84_pos()
            # 已经存在了的卫星编号
            existed_sat_id = 0 if level == 0 else self.walkers[0]

            for low_sat_id, low_sat_pos in enumerate(low_sat_poses):
                for high_sat_id, high_sat_pos in enumerate(high_sat_poses):
                    # 计算两星距离
                    dist = sqrt((high_sat_pos[0] - low_sat_pos[0])**2 + \
                                (high_sat_pos[1] - low_sat_pos[1])**2 + \
                                (high_sat_pos[2] - low_sat_pos[2])**2)
                    # 若两星距离不超过阈值，则说明两星可见，加入返回值字典
                    if dist < max_dist:
                        all_visible_sats[existed_sat_id + low_sat_id].append(
                            existed_sat_id + self.walkers[level].N + high_sat_id
                        )
        return all_visible_sats

    def get_links_between_walkers(self, connect_now: dict) -> dict:
        """
        获取星座链路

        Args:
            connect_now: dict, 当前低层卫星的星座链路连接
            
        Returns:
            dict，表示更新后的星座连接关系（带距离）
        """
        ret = {}
        for low_sat_id, old_high_sat_data in connect_now.items():
            # key类型转换为int
            if isinstance(low_sat_id, str): low_sat_id = int(low_sat_id)
            # 旧上层卫星
            old_high_sat = old_high_sat_data[0]
            # 星座链路层级，LEO和MEO间为0级，MEO和GEO间为1级
            level = 0 if low_sat_id < self.walkers[0].N else 1
            # 判断旧上层卫星的可见性
            visible = self.is_visible_sat_sat_sat(
                level, low_sat_id, old_high_sat)
            
            # 若可见，则更新新连接
            if visible:
                ret[low_sat_id] = [old_high_sat, visible]
            
            # 不可见，则获取距离最近最近的卫星
            else:
                best_sat = best_dist = None
                max_dist = self.sat_sat_limit_val[level]
                existed_sat_id = 0 if level == 0 else self.walkers[0]
                low_sat_pos = self.walkers[level].get_onesat_wgs84_pos(
                    low_sat_id - existed_sat_id
                )
                high_sat_poses = self.walkers[level+1].get_wgs84_pos()
                for high_sat_id, high_sat_pos in enumerate(high_sat_poses):
                    # 计算两星距离
                    dist = sqrt((high_sat_pos[0] - low_sat_pos[0]) ** 2 + \
                                (high_sat_pos[1] - low_sat_pos[1]) ** 2 + \
                                (high_sat_pos[2] - low_sat_pos[2]) ** 2)
                    # 若两星距离不超过阈值，则说明两星可见，加入返回值字典
                    if dist < max_dist:
                        if best_dist == None or best_dist > dist:
                            best_sat = high_sat_id + existed_sat_id + self.walkers[level].N
                            best_dist = dist
                ret[low_sat_id] = [best_sat, best_dist]     
        
        return ret

############## 和星座拓扑总部署相关 ##############
def _check_sat_para(walkers, devs,
                    t0, t_speed, sat_identity, method, rs, bw,
                    mode, topo_json):
    """
    检查卫星参数

    Args:
        walkers: list，包含若干不同高度的wallker星座
        devs: 地面站设备及参数
        t0: 初始时刻
        t_speed: 时间加速速度
        sat_identity: 卫星身份
        method: 选星策略
        rs: 星间路由延迟
        bw: 链路带宽
        mode: 星间路由转发模式
        topo_json: 拓扑json，用以检查地面站是否存在
        
    Returns:
        字典，包括code字段和msg字段
    """
    ###################### 检查walker星座参数 ######################
    # 检查1 - walker星座个数不超过3
    if len(walkers) > 3:
        return {'code': 0, 'msg': '不同高度的轨道数不可超过3'}
    existed_orbits = []
    for walker in walkers:
        # 参数提取
        orbit, N, P, F, i, h, sensor_ang = walker["orbit"], walker["N"], \
            walker["P"], walker["F"], walker["i"], walker["h"], walker["sensor_angle"]
        # 检查2 - 轨道名称不重复
        if orbit in existed_orbits:
            return {'code': 0, 'msg': '存在相同轨道高度的walker'}
        else:
            existed_orbits.append(orbit)
        # 检查3 - 轨道名称合法，且每个walker的高度在所属轨道的范围内
        if orbit == 'LEO':
            if not 400 <= h-6372 <= 2000:
                return {'code': 0, 'msg': 'LEO轨道高度超过范围'}
        elif orbit == 'MEO':
            if not 2000 <= h-6372 <= 36000:
                return {'code': 0, 'msg': 'MEO轨道高度超过范围'}
        elif orbit == 'GEO':
            if h != 42164:
                return {'code': 0, 'msg': 'GEO轨道高度超过范围，必须是42164km'}
        else:
            return {'code': 0, 'msg': 'walker的轨道名称不正确'}
        # 检查4 - 每个walker星座的参数正确
        if N % P != 0:
            return {'code': 0, 'msg': '星座参数错误，卫星总数不能被轨道数整除'}
        if not 0 <= i < 360:
            return {'code': 0, 'msg': '星座参数错误，轨道倾角范围在0~180度间'}
        if h < 6372:
            return {'code': 0, 'msg': '星座参数错误，轨道半径大于地球半径'}
        if not 1<= F <= P-1 and P != 1:
            return {'code': 0, 'msg': '星座参数错误，相位因子范围在1~P-1间'}
        # 检查5 - 卫星可视角度
        if not 0 <= sensor_ang <= 180:
            return {'code': 0, 'msg': '卫星可视角度在0°~180°间'}

    ####################### 检查星座公共参数 #######################
    # 检查1 - 卫星节点的身份，仅可是router(路由器)或switch(交换机)
    if sat_identity not in ["router", "switch"]:
        return {'code': 0, 'msg': '卫星节点身份不是router或switch'}
    # 检查2 - 初始时间限制
    if not 0 <= t0 <= PROJ_CONFIG.max_time_start:
        return {'code': 0, 'msg': '星座运行的初始时间超过范围'}
    # 检查3 - 时间加速速度限制
    if not 1 <= t_speed <= PROJ_CONFIG.max_time_speed:
        return {'code': 0, 'msg': f'星座运行的时间加速速度在1~{PROJ_CONFIG.max_time_speed}间'}
    # 检查4 - 选星策略
    if method not in [1, 2]:
        return {'code': 0, 'msg': '选星策略取值为1 (最短距离) 或2 (最长可见时间)'}
    # 检查5 - 星间路由延迟
    if not 1 <= rs <= PROJ_CONFIG.max_rs:
        return {'code': 0, 'msg': '星间路由延迟超过范围'}
    # 检查6 - 链路带宽
    if set(bw.keys()) != {"sat-sat", "sat-gnd up", "sat-gnd down"}:
        return {'code': 0, 'msg': '链路带宽仅存在字段"sat-sat"、"sat-gnd up"和"sat-gnd down"'}
    if not all([val >= 1 for val in bw.values()]):
        return {'code': 0, 'msg': '链路带宽值为正整数'}

    ######################## 检查地面站参数 ########################
    # 检查1 - 设备是存在的主机或路由器
    hosts = topo_json['networks']['hosts'].keys()
    routers = topo_json['networks']['routers'].keys()
    for dev in devs.keys():
        if dev not in hosts and dev not in routers:
            return {'code': 0, 'msg': '请求与星座连接的设备不存在'}
    # 检查2 - 地面站的经纬度、天线等级
    for val in devs.values():
        if not -180 <= val['position'][0] <= 180:
            return {'code': 0, 'msg': '用户站经度范围在-180°~180°间'}
        if not -90 <= val['position'][1] <= 90:
            return {'code': 0, 'msg': '用户站纬度范围在-90°~90°间'}
        if not 1 <= val['antenna_level'] <= len(PROJ_CONFIG.gnd_dev_level):
            return {'code': 0,
                    'msg': f'用户站天线等级在1~{len(PROJ_CONFIG.gnd_dev_level)}间'}
    # 检查3 - 各地面站是否在同一子网
    all_ip_data = [[val['ip'], val['netmask']] for val in devs.values()]
    # router且不是tunnel模式，不在同一子网
    if sat_identity == "router":
        if mode != 'IP-TUNNEL' and _subnet_count(all_ip_data) != len(all_ip_data):
            return {'code': 0, 'msg': '卫星是路由器时，地面站应配置于不同子网'}
    # switch，在同一子网
    else:
        if _subnet_count(all_ip_data) != 1:
            return {'code': 0, 'msg': '卫星是交换机时，地面站应配置于同一子网'}
    # 检查4 - 若地面站主机有配置网关，则网关和网卡ip在同一子网
    for dev, val in devs.items():
        if dev[0] == 'h' and _subnet_count([[val['ip'], val['netmask']],
                                            [val['gateway'], val['netmask']]]) != 1:
            return {'code': 0, 'msg': '地面是主机时，网关和网卡ip应配置于同一子网'}
    
    ######################### 检查配置参数 #########################
    # 检查1 - 星间转发模式
    if mode not in ['SDN', 'STP', 'NO-STP', 'DHCP', 'IP-NO-MODIFY', 'IP-MODIFY', 'IP-TUNNEL']:
        return {'code': 0, 'msg': f'星间转发模式取值为 SDN/STP/NO-STP/DHCP/IP-MODIFY/IP-NO-MODIFY/IP-TUNNEL'}
    if mode in ['SDN', 'STP', 'NO-STP'] and sat_identity == 'router':
        return {'code': 0, 'msg': f'{mode}模式仅当卫星是交换机时可开启'}
    if mode in ['DHCP', 'IP-MODIFY', 'IP-NO-MODIFY', 'IP-TUNNEL'] and sat_identity == 'switch':
        return {'code': 0, 'msg': f'{mode}模式仅当卫星是路由器时可开启'}
    # 检查2 - DHCP和IP-TUNNRL模式下，所有地面站都是主机
    if mode in ['DHCP', 'IP-TUNNEL'] and \
       not all([dev[0] == 'h' for dev in devs.keys()]):
        return {'code': 0, 'msg': 'DHCP和IP-TUNNRL模式下，所有地面站都是主机'}

    ####################### 检查没问题，返回 #######################
    return {'code': 1, 'msg': '卫星参数正确'}

def _get_node_json(node_name, 
                  sdn=False, stp=True,
                  position=[0,0]):
    """
    对每个交换机、路由器、主机、控制器，返回拓扑json中的字典信息

    Args:
        node_name: 网元名称
        sdn: bool，开启SDN标志，对交换机有效
        stp: bool，开启STP标志，对交换机有效
        position: 卫星前端坐标显示

    Returns:
        拓扑json中的设备对应字典
    """
    if node_name[0] == 'r':
        ret = {
            "name": node_name,
            "config":{
                "bgp":{
                    "asn": "",
                    "enable": 0,
                    "neighbors": [],
                    "networks":[],
                    "router_id":""
                },
                "ospf":{
                    "areas":{},
                    "enable": 0,
                    "networks":[],
                    "router_id":""
                },
                "rip":{
                    "enable":0,
                    "neighbors":[],
                    "networks":[],
                    "version":2
                }
            },
            "gateway":"", 
            "image_name":"router/quagga",
            "interfaces":[],
            "linestyle":"solid",
            "resource_limit":{
                "cpu":"20",
                "mem":"200"
            },
            "subtype":"quagga",
            "type":"router",
            "x":position[0],
            "y":position[1]
        }
    elif node_name[0] == 's':
        if sdn:
            ret = {
                "name": node_name,
                "config":{
                    "controllers":[PROJ_CONFIG.default_ryu_name],
                    "stp": False
                },
                "image_name":"switch/ovs",
                "linestyle":"solid",
                "resource_limit":{
                    "cpu":"20",
                    "mem":"200"
                },
                "subtype":"ovs",
                "type":"switch",
                "x":position[0],
                "y":position[1]
            }
        else:
            ret = {
                "name": node_name,
                "config":{
                    "controllers":[],
                    "stp": stp
                },
                "image_name":"switch/ovs",
                "linestyle":"solid",
                "resource_limit":{
                    "cpu":"20",
                    "mem":"200"
                },
                "subtype":"ovs",
                "type":"switch",
                "x":position[0],
                "y":position[1]
            }
    elif node_name[0] == 'c':
        ret = {
            "name":node_name,
            "config":{
                "port": 6633
            },
            "image_name":"controller/ryu",
            "linestyle":"solid",
            "resource_limit":{
                "cpu":"20",
                "mem":"200"
            },
            "subtype":"ryu",
            "type":"controller",
            "x":position[0],
            "y":position[1]
        }
    else:
        ret = {
            "name":node_name,
            "config":{},
            "gateway":"",
            "image_name":"host/ubuntu",
            "interfaces":[],
            "linestyle":"solid",
            "resource_limit":{
                "cpu":"20",
                "mem":"200"
            },
            "subtype":"ubuntu",
            "type":"host",
            "x":position[0],
            "y":position[1]
        }
    return ret

def _get_link_json(link_name, source, target,
                  rs, bw,
                  sourceIP="", targetIP="",
                  source_para=[PROJ_CONFIG.sat_EIRP, PROJ_CONFIG.sat_GT],
                  target_para=[PROJ_CONFIG.sat_EIRP, PROJ_CONFIG.sat_GT],
                  dist=0, place='sat-sat'):
    """
    对每条链路，返回拓扑json中的字典信息
    
    Args:
        link_name: 链路名称
        source: 源设备
        target: 目的设备
        rs: 星间存储转发延迟，单位是毫秒
        bw: 上行、下行、星间的带宽
        sourceIP / targetIP: 链路两端的IP，经研究，链路无需配置这个
        sourceIP: 源设备网卡IP
        targetIP: 目的设备网卡IP
        source_para: 源设备的参数，需为长度为2的list
                     [天线发射功率（单位瓦）, 天线等效面积（单位平方米）]
        target_para: 目的设备的参数，需为长度为2的list
        dist: （可选）两设备之间的距离（单位千米）
        place: 'sat-sat'（默认）或'sat-gnd'，标定链路是星地还是星间的

    Return: 返回链路json信息
    """

    # 获取源、目的设备的类型
    if source[0]=='r':
        source_type = 'router'
    elif source[0]=='s':
        source_type = 'switch'
    else:
        source_type = 'host'
    if target[0]=='r':
        target_type = 'router'
    elif target[0]=='s':
        target_type = 'switch'
    else:
        target_type = 'host'
    
    # 若定义距离，则计算链路属性
    if dist:
        # 开启链路配置
        flag = True

        # 延迟：根据光速计算
        # delay_us = str(int(dist * 1e9 / PROJ_CONFIG.light_speed))
        delay_us1 = delay_us2 = int(dist * 1e9 / PROJ_CONFIG.light_speed) + rs*1000
        # 仅有星地链路的地面站处无Rs
        if place != 'sat-sat':
            delay_us2 -= rs*1000

        # 带宽：赋予设备频率和比特率
        if place == 'sat-sat':  # 星间链路
            freq1 = freq2 = PROJ_CONFIG.freq["sat-sat"]
            bw1 = bw2 = bw["sat-sat"]
        else:                   # 星地链路，默认第一个设备是卫星节点，第二个是地面节点
            freq1 = PROJ_CONFIG.freq["sat-gnd down"]
            freq2 = PROJ_CONFIG.freq["sat-gnd up"]
            bw1 = bw["sat-gnd down"]
            bw2 = bw["sat-gnd up"]

        # 丢包：保留五位小数
        ber0 = erfc(PROJ_CONFIG.light_speed / 4 / pi / dist / freq1 *sqrt(
                    dBW2W(source_para[0] + target_para[1] - PROJ_CONFIG.L_a) \
                    / PROJ_CONFIG.boltzmann_k / bw1 / 1e3 )) / 2
        pkt_loss0 = '{:.5f}'.format((1 - pow(1 - ber0, PROJ_CONFIG.pkt_avg_len))*100)
        ber1 = erfc(PROJ_CONFIG.light_speed / 4 / pi / dist / freq2 *sqrt(
                    dBW2W(target_para[0] + source_para[1] - PROJ_CONFIG.L_a) \
                    / PROJ_CONFIG.boltzmann_k / bw2 / 1e3)) / 2
        pkt_loss1 = '{:.5f}'.format((1 - pow(1 - ber1, PROJ_CONFIG.pkt_avg_len))*100)
        pkt_loss0 = pkt_loss1 = "0.00000"
        
        # 返回结果
        return {
            "name":link_name,
            "source":source,
            "sourceIP":sourceIP,
            "sourceType":source_type,
            "target":target,
            "targetIP":targetIP,
            "targetType":target_type,
            "config": {
                "flag": True,
                "source": {
                    "bw_kbps": str(bw1),
                    "correlation":"0",
                    "delay_distribution":"uniform",
                    "delay_us": str(delay_us1),
                    "jitter_us": "0",
                    "loss": pkt_loss0,
                    "queue_size_bytes":"100000",
                    "linkchoice": "static",
                    "link": f"link_{link_name}",
                    "ne": source
                },
                "target": {
                    "bw_kbps": str(bw2),
                    "correlation":"0",
                    "delay_distribution":"uniform",
                    "delay_us": str(delay_us2),
                    "jitter_us": "0",
                    "loss": pkt_loss1,
                    "queue_size_bytes":"100000",
                    "linkchoice": "static",
                    "link": f"link_{link_name}",
                    "ne": target
                }
            }
        }
    
    # 否则忽略链路属性
    else:
        return {
            "name":link_name,
            "source":source,
            "sourceIP":sourceIP,
            "sourceType":source_type,
            "target":target,
            "targetIP":targetIP,
            "targetType":target_type,
            "config": {
                "flag": False,
                "source": {
                    "linkchoice": "static",
                    "link": f"link_{link_name}",
                    "ne": source,
                    "bw_kbps": "20000000",
                    "correlation": "",
                    "delay_distribution": "normal",
                    "delay_us": "",
                    "jitter_us": "",
                    "loss": "",
                    "queue_size_bytes": ""
                },
                "target": {
                    "linkchoice": "static",
                    "link": f"link_{link_name}",
                    "ne": target,
                    "bw_kbps": "20000000",
                    "correlation": "",
                    "delay_distribution": "normal",
                    "delay_us": "",
                    "jitter_us": "",
                    "loss": "",
                    "queue_size_bytes": ""
                }
            }
        }

def _get_router_json(user_topo_info, sat, router_id):
    if router_protocol == 'ospf':
        return {
            "enable": True,
            "areas": {},
            "networks":[[f"{int2ip(ip2int(intf['ip']) & ip2int(intf['netmask']))}/{netmask2cidr(intf['netmask'])}",
                            "0.0.0.0"] for intf in user_topo_info['networks']['routers'][sat]['interfaces']],
            "router_id": router_id
        }
    if router_protocol == 'rip':
        return {
            'enable': True,
            'neighbors': [],
            'networks':[f"{int2ip(ip2int(intf['ip']) & ip2int(intf['netmask']))}/{netmask2cidr(intf['netmask'])}"
                        for intf in user_topo_info['networks']['routers'][sat]['interfaces']],
            'version': 2
        }
    if router_protocol == 'bgp':
        return {
            'enable': True,
            'asn': '',
            'neighbors': [],
            'networks': [],
            'router_id': router_id
        }

def _modify_2d_front_node_y(json_dic, add_y):
    """
    将拓扑json中所有节点向下平移add_y个单位
    """
    for k, v in json_dic.items():
        if k == 'y':
            json_dic[k] += add_y
        if isinstance(v, dict):
            _modify_2d_front_node_y(v, add_y)

def _get_2d_front_position(S, P, i, cnt, walker_center, type='sat') -> list:
    """
    计算卫星在2d前端的位置
    """
    radius = int((int(i / S) + 1)*PROJ_CONFIG.frontend_sat_a/P)
    ang = i % S * 2 * pi / S
    offset = 2 * PROJ_CONFIG.frontend_sat_offset \
        if type == 'ovs' else PROJ_CONFIG.frontend_sat_offset
    return [radius * cos(ang) + walker_center[cnt][0] + offset,
            radius * sin(ang) * PROJ_CONFIG.frontend_sat_b / \
            PROJ_CONFIG.frontend_sat_a + walker_center[cnt][1] + offset]

def sat_topo_config(user_topo_info, user_db_cli, topo):
    """
    卫星部署配置
    部署拓扑时，若发现是含星座的拓扑，将执行该函数

    Args:
        user_topo_info: 原始拓扑描述json
        user_db_cli: 用户数据库DB
        topo: 拓扑名称
        
    Returns:
        字典，包括code字段、msg字段、json字段
    """
    print('星座计算中...')
    
    ############################## json字段提取 ##############################
    # 提取卫星信息，并从json去掉satellite字段
    sat = user_topo_info['networks']['satellite']
    user_topo_info['networks'].pop('satellite')


    ############################# 参数提取及检查 #############################
    try:
        # 星座参数提取，可包含LEO、MEO、GEO星座各一
        walkers_data = sat['walkers']
        # 公用参数提取
        ts0, t_speed, sat_identity, method, rs, bw = \
            sat['time_start'], sat['time_speed'], sat['sat_identity'], \
            sat['select_sat_method'], sat['rs'], sat['bw']
        # 地面站参数提取
        devices = sat['devices']
        # 网卡启停使能，默认False
        ne_up_down_enable = sat['nic_up_down_enable'] \
            if 'nic_up_down_enable' in sat else False
        # 星间转发模式，必须指定
        mode = sat['mode']
    except KeyError as e:
        return {'code': 0, 'msg': f'星座参数缺失，{e.args[0]}'}
    # 参数检查
    ret = _check_sat_para(walkers_data, devices, ts0, t_speed, sat_identity,
                          method, rs, bw, mode, user_topo_info)
    if ret['code'] == 0:
        return ret


    ################################ 预备工作 ################################
    # 1）初始时间
    t0 = timestamp2date(ts0)
    
    # 2）低轨到高轨排序
    sort_wk = []
    for orbit in ['LEO', 'MEO', 'GEO']:
        for walker in walkers_data:
            if walker['orbit'] == orbit:
                sort_wk.append(walker)
                break
    walkers_data = sort_wk
    
    # 3）卫星身份复数
    pl_sat_identity = "routers" if sat_identity == "router" else "switches"
    
    # 4）重要变量
    link_cnt_dict = {}  # 字典，星间链路编号（数字）-> 包含两端卫星节点名的列表
    ip_dict = {}  # 字典，链路编号（数字）-> 整数对应的ip网络号，仅卫星为路由器有效
    sat_ovs_gnd = {}            # 【星地链路】字典，卫星 -> 用于连接地面站的星下ovs（或地面站本身，若无星下ovs）
    all_sat_gnd_links = {}      # 【星地链路】星地链路字典，地面站 -> 卫星
    all_sat_highsat_links = {}  # 【星座链路】星座链路字典，较低轨卫星 -> 较高轨卫星
    sat_gnd_nets = {}   # 字典，表示每个卫星分配的对地小网段
    
    # 5）链路IP
    if sat_identity == "router":
        ip_splt = PROJ_CONFIG.sat_link_ip.split('/')
        # 下个可用IP网段初始位置
        ip_next = ip2int(ip_splt[0])
        # 最后一个可用IP，到达则说明网段太小
        ip_last = ip_next + 2 ** (32 - int(ip_splt[1])) - 1
    
    # 6）模式使能
    stp_enable = mode == 'STP'                        # STP功能
    sdn_enable = mode == 'SDN'                        # SDN功能
    ovs_below_enable = mode in ['IP-TUNNEL', 'DHCP']  # 星下ovs：DHCP或IP-TUNNEL下
    
    # 7）前端展示
    # 各星座中心
    walker_center = [(PROJ_CONFIG.frontend_sat_a,
                      (2*i+1)*PROJ_CONFIG.frontend_sat_b)
                     for i in range(len(walkers_data))][::-1]
    # 地面网络所有网元，向下平移
    _modify_2d_front_node_y(user_topo_info, 
                            len(walkers_data) * 2 * PROJ_CONFIG.frontend_sat_b)


    ############################## 星下ovs建立 ###############################
    if ovs_below_enable:
        # 第一个可用星下ovs编号
        s_id = len(user_topo_info['networks']["switches"].keys()) + 1
        # 已有卫星编号偏移
        existed_sat_id = 0
        # 对每层walker星座
        for cnt, walker in enumerate(walkers_data):
            N = walker["N"]  # 本层卫星数
            P = walker["P"]  # 本层轨道数
            S = N / P        # 本层每轨道卫星数
            for i in range(N):
                # 卫星全局编号
                sat_id = existed_sat_id + i
                # 进行卫星节点到ovs节点的映射，方便在ovs网桥上完成链路切换
                sat_ovs_gnd[sat_id] = ovs_name = f's{s_id}'
                # ovs编号自增
                s_id += 1
                # json里建立连接地面站星下ovs
                user_topo_info['networks']["switches"][ovs_name] = \
                    _get_node_json(ovs_name, sdn_enable, stp_enable,
                                   _get_2d_front_position(S, P, i, cnt,
                                                          walker_center, 'ovs'))
            # 从低轨到高轨进行卫星编号
            existed_sat_id += N


    ############################## 卫星节点建立 ##############################
    # 第一个可用卫星编号
    sat_id1 = len(user_topo_info['networks'][pl_sat_identity].keys()) + 1
    # 第一个可用链路编号
    l_id = len(user_topo_info['networks']['links'].keys()) + 1
    # 已有卫星编号偏移
    existed_sat_id = 0
    # 对每层walker星座
    for cnt, walker in enumerate(walkers_data):
        # 参数提取
        N, P, i, F, h, ang = get_walker_para(walker)
        # 对每个卫星
        for i in range(N):
            # 卫星全局编号
            sat_id = existed_sat_id + i
            # 卫星节点名称
            sat_name = f'{sat_identity[0]}{sat_id1 + sat_id}'
            # 建立卫星设备节点的json
            user_topo_info['networks'][pl_sat_identity][sat_name] = \
                _get_node_json(sat_name, sdn_enable, stp_enable,
                               _get_2d_front_position(N / P, P, i, cnt,
                                                      walker_center, 'sat'))
            
            # 有星下ovs时，建立星下ovs及卫星间的链路
            if ovs_below_enable:
                # 星下ovs名称
                ovs_name = sat_ovs_gnd[sat_id]
                # 链路名称
                link_to_ovs = f'l{l_id}'
                # 链路编号编号自增
                l_id += 1
                # 建立连接到地面站星下ovs的链路
                user_topo_info['networks']['links'][link_to_ovs] = \
                    _get_link_json(link_to_ovs, sat_name, ovs_name, rs, bw)
            # 无星下ovs时，星下ovs映射字典中存放卫星名
            else:
                sat_ovs_gnd[sat_id] = sat_name
             
            # 卫星为路由器时，需划分对地小子网
            if sat_identity == "router":
                # 记录网段里的下一个可用地面站IP
                sat_gnd_nets[sat_id] = ip_next + 2
                # 若存在星下ovs，配置卫星连接到星下ovs的网卡ip
                if ovs_below_enable:
                    user_topo_info['networks']['routers'][sat_name]['interfaces'].append({
                        'name': f'{sat_name}{sat_ovs_gnd[sat_id]}',
                        'ip': int2ip(ip_next + 1),
                        'netmask': PROJ_CONFIG.sat_gnd_subnet_mask
                    })
                # 链路ip自增
                ip_next += 2 ** (32 - netmask2cidr(PROJ_CONFIG.sat_gnd_subnet_mask))
                if ip_next >= ip_last:
                    return {'code': 0, 'msg': f'星座可分配IP不足'}
        
        # 从低轨到高轨进行卫星编号
        existed_sat_id += N


    ############################## 辅助节点建立 ##############################
    # 1）添加预备主机
    spare_name = f'h{len(user_topo_info["networks"]["hosts"].keys()) + 1}'
    # 2d前端中，其位于最低轨的中心
    user_topo_info['networks']["hosts"][spare_name] = \
        _get_node_json(
            spare_name,
            position=[
                walker_center[0][0] + PROJ_CONFIG.frontend_sat_offset,
                walker_center[0][1] + PROJ_CONFIG.frontend_sat_offset]
        )
    # 2）添加SDN的RYU控制器
    if sdn_enable:
        user_topo_info['networks']["controllers"][PROJ_CONFIG.default_ryu_name] = \
            _get_node_json(
                PROJ_CONFIG.default_ryu_name,
                position=[walker_center[0][0] + PROJ_CONFIG.frontend_sat_offset + \
                            2 * PROJ_CONFIG.frontend_sat_offset,
                          walker_center[0][1] + PROJ_CONFIG.frontend_sat_offset + \
                            2 * PROJ_CONFIG.frontend_sat_offset]
            )

    print('[👌] 节点创建')

    ################################ 星间链路 ################################
    # 创建多层星座对象
    walkers = Walkers(t0, walkers_data, devices)
    # 多层星座中的所有星间链路
    all_links, no_link = walkers.get_links_in_walker()

    # 为所有可能的链路连接分配链路
    for link in all_links + no_link:
        # 链路名称
        link_name = f'l{l_id}'
        # 链路两端卫星
        sat1 = f'{sat_identity[0]}{sat_id1 + link[0]}'
        sat2 = f'{sat_identity[0]}{sat_id1 + link[1]}'
        # 对存在的链路
        if link in all_links:
            # 写入json，并考虑链路质量
            user_topo_info['networks']['links'][link_name] = _get_link_json(
                link_name, sat1, sat2, rs, bw, dist=link[2])
            # 卫星是路由器时，配置网卡ip
            if sat_identity == "router":
                # 卫星id大，则赋予更大的ip
                ip1, ip2 = int2ip(ip_next + 1), int2ip(ip_next + 2)
                if int(sat1[1:]) > int(sat2[1:]):
                    ip1, ip2 = ip2, ip1
                # 配置网卡ip
                user_topo_info['networks']['routers'][sat1]['interfaces'].append({
                    'name': f'{sat1}{sat2}',
                    'ip': ip1,
                    'netmask': PROJ_CONFIG.link_subnet_mask
                })
                user_topo_info['networks']['routers'][sat2]['interfaces'].append({
                    'name': f'{sat2}{sat1}',
                    'ip': ip2,
                    'netmask': PROJ_CONFIG.link_subnet_mask
                })
        
        # 对存在/不存在的链路，均需记录链路网段
        if sat_identity == "router":
            # 写入ip字典
            ip_dict[l_id] = ip_next
            # 链路ip自增
            ip_next += 4
            if ip_next >= ip_last:
                return {'code': 0, 'msg': f'星座可分配IP不足'}
        
        # 写入链路字典
        link_cnt_dict[l_id] = [sat1, sat2]
        l_id += 1

    print('[👌] 星间链路')

    ########################## 星地链路：地面站->卫星 ##########################
    # 第一个星地链路
    link_gnd_id1 = l_id
    # 对每个地面站
    for dev, para in devices.items():
        # 地面站类型
        dev_type = 'routers' if dev[0] == 'r' else 'hosts'
        # 星地连接
        best_sat, best_dist = all_sat_gnd_links[dev] = \
            walkers.get_links_gnd_sat(dev, para, None, method)
        # 新增链路
        link_name = f'l{l_id}'
        l_id += 1
        # 地面站有相连卫星，连接到星下ovs
        if best_sat != None:
            connect_to = sat_ovs_gnd[best_sat]
            user_topo_info['networks']['links'][link_name] = _get_link_json(
                link_name, connect_to, dev, rs, bw,
                target_para=PROJ_CONFIG.gnd_dev_level[devices[dev]['antenna_level']-1][:-1],
                dist=best_dist, place='sat-gnd')
        # 地面站无可连卫星，连接到预备主机
        else:
            connect_to = spare_name
            user_topo_info['networks']['links'][link_name] = _get_link_json(
                link_name, connect_to, dev, rs=0, bw=bw)
        
        # 3）地面站网卡配置
        # IP-TUNNEL模式
        #     - 统一在卫星对地网段内分配地面站IP，暂不用DHCP
        #     - 下一步还需配置IP隧道，才可实现通信
        if mode == 'IP-TUNNEL':
            # 无卫星连接
            if best_sat == None:
                # ip配置
                user_topo_info['networks'][dev_type][dev]['interfaces'].append({
                    'name': f'{dev}{connect_to}',
                    'ip': '', 'netmask': ''
                })
            # 有卫星连接
            else:
                # 连接的卫星名
                connect_to_sat = best_sat
                # 网关配置
                user_topo_info['networks'][dev_type][dev]['gateway'] = \
                    int2ip((sat_gnd_nets[connect_to_sat] & sat_gnd_subnet_mask_int) + 1)
                # ip配置
                user_topo_info['networks'][dev_type][dev]['interfaces'].append({
                    'name': f'{dev}{connect_to}',
                    'ip': int2ip(sat_gnd_nets[connect_to_sat]),
                    'netmask': int2ip(sat_gnd_subnet_mask_int)
                })
                # 子网内下一可用ip自增
                if get_next_ip(sat_gnd_nets, connect_to_sat):
                    return {'code': 0, 'msg': f'星座可分配IP不足'}
        
        # DHCP模式
        #     - 不配置地面站的IP、掩码、网关
        elif mode == 'DHCP':
            # ip配置
            user_topo_info['networks'][dev_type][dev]['interfaces'].append({
                'name': f'{dev}{connect_to}',
                'ip': '', 'netmask': ''
            })
        
        # IP-MODIFY模式、IP-NO-MODIFY模式
        #     - 按用户指定，配置地面站的IP、掩码、网关
        #     - 卫星侧也需更新IP
        else:
            # ip配置
            user_topo_info['networks'][dev_type][dev]['interfaces'].append({
                'name': f'{dev}{connect_to}',
                'ip': devices[dev]['ip'],
                'netmask': devices[dev]['netmask']
            })
            # 卫星为路由器，且地面站有卫星连接，则配置卫星网卡ip
            if sat_identity == "router" and best_sat != None:
                
                # 地面站为主机，卫星ip和地面站网关均为用户指定网关
                if dev_type == "hosts":
                    sat_ip = user_topo_info['networks'][dev_type][dev]['gateway'] = \
                        devices[dev]['gateway']
                
                # 地面站为路由器，则在子网中查找可用ip作为卫星ip
                else:
                    # 链路地面站侧已占用的ip
                    occupied_ip = ip2int(devices[dev]['ip'])
                    # 子网号
                    net = occupied_ip & ip2int(devices[dev]['netmask'])
                    # 第一个子网内可用ip，作为链路卫星侧的ip
                    for ip in range(net+1, net+2**(32-netmask2cidr(devices[dev]['netmask']))-1):
                        if ip != occupied_ip:
                            sat_ip = int2ip(ip)
                            break

                # 配置所连接卫星的ip
                user_topo_info['networks']['routers'][connect_to]['interfaces'].append({
                    'name': f'{connect_to}{dev}',
                    'ip': sat_ip,
                    'netmask': devices[dev]['netmask']
                })

    print('[👌] 星地链路')

    ################################ 星座链路 ################################
    # 低层卫星数
    low_sat_cnt = 0
    for i in range(len(walkers.walkers) - 1):
        low_sat_cnt += walkers.walkers[i].N
    # 初始卫星连接
    init_dict = {i: [None, None] for i in range(low_sat_cnt)}
    # 获取星座链路
    all_sat_highsat_links = walkers.get_links_between_walkers(init_dict)
    # 
    for low_sat_id, high_sat_data in all_sat_highsat_links.items():
        # 下层卫星设备名
        low_sat = f'{sat_identity[0]}{sat_id1 + low_sat_id}'
        # 链路名称
        link_name = f'l{l_id}'
        l_id += 1
        # 上层卫星
        high_sat_id = high_sat_data[0]
        dist = high_sat_data[1]
        # 连接设备
        connect_to = f'{sat_identity[0]}{sat_id1+high_sat_id}' \
            if high_sat_id != None else spare_name
        # 生成链路json
        user_topo_info['networks']['links'][link_name] = _get_link_json(
            link_name, connect_to, low_sat,
            rs = rs if high_sat_id != None else 0,
            bw = bw,
            dist = dist if high_sat_id != None else 0
        )
        # 卫星是路由器，配置IP
        if sat_identity == "router":
            # 较小IP：下层卫星
            user_topo_info['networks']['routers'][low_sat]['interfaces'].append({
                'name': f'{low_sat}{connect_to}',
                'ip': int2ip(ip_next + 1),
                'netmask': PROJ_CONFIG.link_subnet_mask
            })
            # 较大IP：上层卫星，仅对有卫星连接进行配置
            if high_sat_id != None:
                user_topo_info['networks']['routers'][connect_to]['interfaces'].append({
                    'name': f'{connect_to}{low_sat}',
                    'ip': int2ip(ip_next + 2),
                    'netmask': PROJ_CONFIG.link_subnet_mask
                })
            # ip自增
            ip_next += 4
            if ip_next >= ip_last:
                return {'code': 0, 'msg': f'星座可分配IP不足'}   
        
    print('[👌] 星座链路')


    ################################ 路由配置 ################################
    if sat_identity == "router":
        # 对每颗卫星
        for i in range(sum([walker['N'] for walker in walkers_data])):
            # 卫星名
            sat = f'r{sat_id1 + i}'
            # 路由协议用的路由器标识符
            router_id = int2ip(i)
            # 配置路由协议
            user_topo_info['networks']['routers'][sat]['config'][router_protocol] = \
                _get_router_json(user_topo_info, sat, router_id)

    print('[👌] 路由配置')
    
    ########################## 持久化、信息打印、返回 #########################
    # 刷新时间间隔
    # refresh_interval = PROJ_CONFIG.refresh_interval_para * t_speed \
    #     * sum([walker['N']*2-walker['N']/walker['F'] for walker in walkers])
    # 写入redis
    user_db_cli.set_all_values(f'{topo}{PROJ_CONFIG.sat_table_name}', {
        # 星座参数
        'walkers': walkers_data,
        # 定时参数
        # [下次模拟时刻, 倍速, 当前真实时刻, 本次卫星世界刷新周期]
        # 'timer': [ts0+refresh_interval, t_speed, time(), refresh_interval],
        # [卫星世界初始时刻, 时间倍速, 真实世界初始时刻]
        'timer': [ts0, t_speed, time()],
        # 星地链路
        'sat-gnd links': all_sat_gnd_links,
        # 星座链路
        'sat-highsat links': all_sat_highsat_links,
        # 卫星身份, 星间转发模式
        'mode': [sat_identity, mode],
        # 编号偏移
        'virtual-para': [sat_id1,       # 第一个卫星设备编号
                        spare_name,     # 预备主机
                        link_gnd_id1],  # 第一个星地链路(星座链路继续往后编号)
        # 星地链路连接, 星座链路连接
        'sat-ovs': sat_ovs_gnd,
        # 星间链路连接
        'links2dev': link_cnt_dict,
        # 星间转发延迟, 链路带宽配置
        'link-config': [rs, bw],
        # 星间链路IP, IP隧道小子网映射(网段里的下一个可用地面站IP)
        'ip-net': [ip_dict, sat_gnd_nets],
        # 网卡启停
        'ne-up-down': ne_up_down_enable,
        # 地面站设备信息, 选星策略
        'gnd-dev': [devices, method],
        # 地面站换星日志
        'sat log': [f"初始时戳{round(ts0)}秒，地面站{dev}连接卫星: {all_sat_gnd_links[dev][0]}"
                    for dev in devices.keys()],
        # 临时存储
        'temp': {}
    })

    print("星座信息"
        f"\n 🛰 - 星座结构: " + ", ".join([f'{walker["orbit"]}: {walker["N"]}' for walker in walkers_data]) + \
        f"\n 🛰 - 初始时间: {round(ts0)}秒 ({t0[0]}年{t0[1]}月{t0[2]}日{t0[3]}时{t0[4]}分{t0[5]}秒)"
        f"\n 🛰 - 星地连接: " + ", ".join([f"{dev}-{all_sat_gnd_links[dev][0]}" for dev in devices.keys()]) + \
        f"\n 🛰 - 转发模式: {sat_identity}, {mode}"
        f"\n 🛰 - 网卡启停: {ne_up_down_enable}"
    )
    return {
        'code': 1,
        'msg': '卫星json修改成功',
        'json': user_topo_info
    }

############### 和卫星日志记录相关 ###############
def satlog(user, topo, msg):
    if msg == "刷新结束":
        with open(f'{user}-{topo}-sat.log', 'w') as f:
            f.write('未部署拓扑' + '\n')
    elif msg == "刷新开始":
        with open(f'{user}-{topo}-sat.log', 'w') as f:
            f.write('刷新开始' + '\n')
    else:
        # 文件追加内容
        with open(f'{user}-{topo}-sat.log', 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
