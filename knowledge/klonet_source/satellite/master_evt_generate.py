"""
master 产生事件，并将事件发布给中间件
"""

from .master_eventset import *
from .satool import *


def _get_link_id(link_dict, dev1, dev2):
    """
    从链路字典反向查询链路编号
    """
    for link_id, devs in link_dict.items():
        if dev1 in devs and dev2 in devs:
            return 'l' + link_id

def _get_move_tc(old_con, new_con):
    """
    | case |               情况描述                | 链路迁移 | tc配置 |
    |------|--------------------------------------|----------|-------|
    |  -1  |  初始未决状态，待判断                  |     -    |   -   |
    |   0  |  无卫星切换，s1->s1                    |    x    |   √   |
    |   1  |  有卫星切换，s1->s2                    |    √    |   √   |
    |   2  |  原有卫星可见，现无卫星可见，s1->None   |    √    |   x   |
    |   3  |  原无卫星可见，现有卫星可见，None->s1   |    √    |   √   |
    |   4  |  原无卫星可见，现无卫星可见，None->None |    x    |   x   |
    """
    # case 0: s1->s1
    if old_con != None and old_con == new_con:
        return False, True
    # case 1: s1->s2
    elif old_con != None and new_con != None:
        return True, True
    # case 2: s1->None
    elif old_con != None and new_con == None:
        return True, False
    # case 3: None->s1
    elif  old_con == None and new_con != None:
        return True, True
    # case 4: None->None
    else:
        return False, False


@celery.task(track_started=True)
def sat_evt_generate(user, topo):
    """
    master 主进程创建的进程，进行星座事件产生与发布
    通过 “用户+拓扑” 定位星座，对星座实时刷新并进行事件发布
    """

    try:
        ############################ 静态数据 ############################
        sat_table = f'{topo}{PROJ_CONFIG.sat_table_name}'  # 用户db里的卫星表名
        user_db_cli = UserMapRedis().get_user_db(user)  # 返回时关闭数据库连接，不用with
        
        """ 参数示意
        - ip_dict
        含义：卫星链路子网映射：链路编号 -> 整形网络号
        例子：{'1': 167772736, '2': 167772740, '3': 167772744, ...}
        - sat_gnd_nets
        含义：卫星对地子网映射：卫星编号 -> 整形网络号
        例子：{'0': 167772162, '1': 167772178, '2': 167772194, ...}
        - sat_id1
        含义：第一个卫星编号
        例子：int(..)
        - sat_id1
        含义：预备主机
        例子：'h22'
        - l_id1
        含义：第一个星地链路
        例子：int(..)
        - sat_ovs_gnd
        含义：地面站星下ovs，卫星 -> 用于连接地面站的星下ovs
        例子：{'0': 's1', '1': 's2', '2': 's3', 'None': 'h3'}
        - link_dict
        含义：链路字典，链路编号（数字）-> 两端设备名列表
        例子：{'1': ['s1', 's2'], '2': ['s2', 's3']}
        - ne_up_down
        含义：网卡启停
        例子：bool(..)
        """
        ip_dict, sat_gnd_nets = user_db_cli.get_value(sat_table, "ip-net")
        sat_id1, spare_conn, l_id1 = \
            user_db_cli.get_value(sat_table, "virtual-para")
        sat_ovs_gnd = user_db_cli.get_value(sat_table, "sat-ovs")
        sat_ovs_gnd['None'] = spare_conn
        link_dict = user_db_cli.get_value(sat_table, "links2dev")
        ne_up_down = user_db_cli.get_value(sat_table, "ne-up-down")
        

        ########################### 星座初始化 ###########################
        # 数据提取
        walkers_data = user_db_cli.get_value(sat_table, 'walkers')
        gnd_devices, _ = user_db_cli.get_value(sat_table, 'gnd-dev')
        _, last_mode = user_db_cli.get_value(sat_table, 'mode')
        dev_list = list(gnd_devices.keys())
        sat_gnd_links = user_db_cli.get_value(sat_table, 'sat-gnd links')
        # 初始化事件集
        event_set = EventScheduler()
        # 日志记录
        for dev, sat in sat_gnd_links.items():
            pub_ctn_satlog(
                event_set, topo, user_db_cli,dev, f"本站初始连接卫星> {sat[0]}")
        # 模式初始化
        pub_sat_change_mode(
            event_set, topo, user_db_cli, '', last_mode,
            walkers_data, sat_gnd_links, sat_id1, l_id1,
            sat_gnd_nets, sat_ovs_gnd, dev_list, gnd_devices
        )
        # 发布初始化事件集，立即执行
        event_set.publish_all(time(), user, topo)


        ############################ 星座刷新 ############################
        satlog(user, topo, "刷新开始")
        while True:
            #################### 参数变更应用、提取 ####################
            temp = user_db_cli.get_value(sat_table, 'temp')
            if temp:
                for k, v in temp.items():
                    user_db_cli.set_value(sat_table, k, v)
                user_db_cli.set_value(sat_table, 'temp', {})
            
            """ 参数示意
            - walkers
               含义：星座参数
               例子：[{'orbit': 'LEO',
                      'N': 25,
                      'P': 5,
                      'i': 88,
                      'h': 8000,
                      'F': 1,
                      'sensor_angle': 170}]
            - gnd_devices
               含义：地面站参数
               例子：{'h1': {'position': [5, 0],
                            'antenna_level': 1,
                            'gateway': '192.168.1.1',
                            'ip': '192.168.1.2',
                            'netmask': '255.255.255.0'}}
            - method
               含义：选星策略
               例子：int(..)
            - rs
               含义：星间转发延迟
               例子：int(..)
            - bw
               含义：链路带宽
               例子：{'sat-sat': 850000,
                     'sat-gnd up': 700000,
                     'sat-gnd down': 800000}
            - sat_identity
               含义：卫星身份
               例子："switch"
            - mode
               含义：星间转发模式
               例子："STP"
            - dev_list
               含义：地面站列表
               例子：['h1', 'h2']
            - sat_gnd_links
               含义：星地链路，地面站 -> 卫星
               例子：{'h1': [17, 3252], 'h2': [23, 2518]}
            - sat_highsat_links
               含义：星座链路，较低轨卫星 -> 较高轨卫星
               例子：{'0': [None, None], '1': [27, 39435], ...}
            - timer
               含义：定时参数，[卫星世界初始时刻, 时间倍速, 真实世界初始时刻]
               例子：[21122, 22, 1714890019]
            """
            walkers_data = user_db_cli.get_value(sat_table, 'walkers')
            gnd_devices, method = user_db_cli.get_value(sat_table, 'gnd-dev')
            rs, bw = user_db_cli.get_value(sat_table, 'link-config')
            sat_identity, mode = user_db_cli.get_value(sat_table, 'mode')
            dev_list = list(gnd_devices.keys())
            sat_gnd_links = user_db_cli.get_value(sat_table, 'sat-gnd links')
            sat_highsat_links = user_db_cli.get_value(sat_table, 'sat-highsat links')
            timer = user_db_cli.get_value(sat_table, 'timer')
            
            
            ####################### 计时、事件集 #######################
            # 时间暂停
            if timer[1] == 0: continue
            # 本次刷新开始真实时刻
            real_time_gen = time()
            # 超前时长
            time_ahead = 10
            # 事件执行时刻，相对事件产生是滞后的
            real_time_exe = real_time_gen + time_ahead
            # 卫星星座仿真时刻
            virtual_time = timestamp2date(
                (real_time_exe - timer[2]) * timer[1] + timer[0])
            # 记录本次刷新的两个时刻
            satlog(user, topo, f"> {int(real_time_gen)}, {virtual_time}")
            # 本次刷新事件集，每当产生事件，则加入事件集
            event_set = EventScheduler()


            ######################### 模式变化 #########################
            pub_sat_change_mode(
                event_set, topo, user_db_cli, last_mode, mode,
                walkers_data, sat_gnd_links, sat_id1, l_id1,
                sat_gnd_nets, sat_ovs_gnd, dev_list, gnd_devices
            )
            last_mode = mode
            

            ####################### 星间邻轨链路 #######################
            # 多层星座对象
            walkers = Walkers(virtual_time, walkers_data, gnd_devices)
            # 同层walker里，星间链路中的所有邻轨链路
            # 邻轨：需考虑迁移，需替换tc
            # 同轨：不考虑迁移，不替换tc
            all_links, no_link = walkers.get_inter_links_in_walker()
            # 当前拓扑存在的链路
            cur_links = user_db_cli.get_value("plane_topo_list", topo)["links"]
            
            # 删除链路：当前存在的链路不再连接
            for link in cur_links:
                # 链路编号字符串
                lid = link[1:]
                # 过滤星地链路、星座链路、地面网络
                if int(lid) >= l_id1 or lid not in link_dict: continue
                # 链路两端卫星设备
                devs = link_dict[lid]
                # 链路两端卫星编号
                devs_id = [int(d[1:])-sat_id1 for d in devs]
                # 无连接，则删除链路
                if devs_id in no_link:
                    satlog(user, topo, f"删除星间链路l{lid}：{devs_id}")
                    # 删除链路
                    pub_link_delete(event_set, topo, user_db_cli,
                                    "l"+lid, *devs, ne_up_down)
            
            # 新增链路和tc配置
            # 对所有需连接的链路
            for sat1_id, sat2_id, dist in all_links:
                # 卫星设备
                sat1 = f"{sat_identity[0]}{sat1_id+sat_id1}"
                sat2 = f"{sat_identity[0]}{sat2_id+sat_id1}"
                # 链路名称
                l_name = _get_link_id(link_dict, sat1, sat2)
                
                # tc事件传入参数
                tc_args = (topo, user_db_cli, l_name, sat1, sat2, rs, bw)
                tc_kwargs = {"dist": dist}

                # 新增链路，需连接的链路原不存在
                if l_name not in cur_links:
                    satlog(user, topo, f"新增星间链路{l_name}：{sat1_id, sat2_id}")
                    # 注册创建链路事件和tc事件
                    pub_link_create(
                        event_set, topo, user_db_cli, l_name, sat1, sat2,
                        ip_dict[l_name[1:]] if sat_identity=="router" else "",
                        ne_up_down=ne_up_down,
                        tc_args=tc_args,
                        tc_kwargs=tc_kwargs
                    )
                # 不新增链路
                else:
                    pub_tc_create(event_set, tc_args, tc_kwargs)


            ######################### 星地链路 #########################
            link_id = l_id1
            for dev, para in gnd_devices.items():
                # 链路名
                l_name = f"l{link_id}"
                link_id += 1
                # 旧连接卫星id
                old_con = sat_gnd_links[dev][0]
                # 新连接卫星id
                new_con, dist = walkers.get_links_gnd_sat(dev, para, old_con, method)
                # 更新星地链路字典
                sat_gnd_links[dev] = [new_con, dist]
                # 链路迁移、tc标志位
                move, tc = _get_move_tc(old_con, new_con)

                # tc事件传入参数
                if tc:
                    tc_args = (
                        topo, user_db_cli, l_name,
                        sat_ovs_gnd[str(new_con)], dev, rs, bw
                    )
                    tc_kwargs = {
                        "target_para": PROJ_CONFIG.gnd_dev_level[para['antenna_level']-1][:-1],
                        "dist": dist,
                        "place": 'sat-gnd'
                    }
                else:
                    tc_args = tuple()
                    tc_kwargs = {}
    
                # 链路迁移
                if move:
                    pub_link_move(
                        event_set, topo, user_db_cli, l_name, 
                        dev, sat_ovs_gnd[str(old_con)], sat_ovs_gnd[str(new_con)],
                        mode, "sat-gnd",
                        str(old_con), str(new_con),
                        sat_gnd_nets, gnd_devices, l_id1,
                        tc_args=tc_args, tc_kwargs=tc_kwargs
                    )
                    # 日志
                    satlog(user, topo, f"🔺 {dev}换星{old_con} > {new_con}")
                    logs = user_db_cli.get_value(sat_table, 'sat log')
                    logs.append(f"{virtual_time[3]}:{virtual_time[4]}:{virtual_time[5]}，"
                                f"{dev}换星，{'无连接' if old_con == None else '卫星'+str(old_con)}"
                                f" > {'无连接' if new_con == None else '卫星'+str(new_con)}")
                    user_db_cli.set_value(sat_table, 'sat log', 
                                          logs[-PROJ_CONFIG.max_satlog_length:])
                # 无链路迁移
                else:
                    pub_tc_create(event_set, tc_args, tc_kwargs)


            ######################### 星座链路 #########################
            new_sat_highsat_links = \
                walkers.get_links_between_walkers(sat_highsat_links)
            
            for low_sat_id, old_data in sat_highsat_links.items():
                # 链路名
                l_name = f"l{link_id}"
                link_id += 1
                # 旧连接卫星id
                old_con = old_data[0]
                # 新连接卫星id
                new_con, dist = new_sat_highsat_links[int(low_sat_id)]
                # 新连接的设备
                low_sat_dev = f"{sat_identity[0]}{sat_id1 + int(low_sat_id)}"
                # 更新星地链路
                sat_highsat_links[low_sat_id] = [new_con, dist]
                # 链路迁移、tc标志位
                move, tc = _get_move_tc(old_con, new_con)

                # tc事件传入参数
                if tc:
                    tc_args = (
                        topo, user_db_cli, l_name,
                        f"{sat_identity[0]}{sat_id1 + new_con}",
                        low_sat_dev, rs, bw
                    )
                    tc_kwargs = {"dist": dist}
                else:
                    tc_args = tuple()
                    tc_kwargs = {}
                
                # 链路迁移
                if move:
                    pub_link_move(
                        event_set, topo, user_db_cli, l_name, low_sat_dev, 
                        dev_from=spare_conn if old_con==None else f"{sat_identity[0]}{sat_id1+old_con}",
                        dev_to=spare_conn if new_con==None else f"{sat_identity[0]}{sat_id1+new_con}",
                        mode=mode,
                        place="sat-highsat",
                        tc_args=tc_args, tc_kwargs=tc_kwargs
                    )
                    # 日志
                    satlog(user, topo,f"🔹 {low_sat_id}换星{old_con} > {new_con}")
                # 无链路迁移
                else:
                    pub_tc_create(event_set, tc_args, tc_kwargs)
            

            ################### 发布事件、更新数据库 ###################
            # 统一事件发布
            event_set.publish_all(real_time_exe, user, topo)
            # 星地、星座连接
            user_db_cli.set_value(sat_table, 'sat-gnd links', sat_gnd_links)
            user_db_cli.set_value(sat_table, 'sat-highsat links', sat_highsat_links)


            ####################### 动态定时刷新 #######################
            # run_time = time() - real_time_gen                # 本次运行时长
            # max_run_time = refresh_interval / time_speed  # 又刷新周期推算的最大运行时长
            # # 更新卫星世界刷新周期
            # if run_time <= max_run_time:
            #     # 统计未超时的次数
            #     no_run_too_long_count += 1
            #     # 若未超时次数等于该值最大值，则次数重置，内部刷新时长减小
            #     if no_run_too_long_count >= PROJ_CONFIG.no_run_too_long_max_count:
            #         no_run_too_long_count = 0
            #         refresh_interval = round(timer[1] * run_time)
            #     sleep(max_run_time - run_time)
            # else: 
            #     no_run_too_long_count = 0
            #     refresh_interval = round(timer[1] * run_time)
            # refresh_interval = round(timer[1] * run_time)

    # 忽略请求连接问题
    except requests.exceptions.ConnectionError:
        satlog(user, topo, 'Requests connectionError')
    
    # 忽略json问题
    except json.decoder.JSONDecodeError:
        satlog(user, topo, 'Requests JSONDecodeError')

    # 若表不存在，说明拓扑删除时数据库被删除，则退出循环，星座停止更新
    except sat_update_error:
        # 防止发生Error后继续写表的问题
        if check_table_existence(user, sat_table):
            user_db_cli.del_table(sat_table)
        # 关闭数据库连接
        user_db_cli.close()
        # 刷新函数结束
        satlog(user, topo, "刷新结束")
        return
