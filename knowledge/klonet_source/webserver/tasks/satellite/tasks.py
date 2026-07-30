import json
from ....Function_layer.satellite import *
from ....webserver import celery
from ....Service_layer.redisAPI import UserMapRedis
from ....vemu_config.config import PROJ_CONFIG
from ....tools.context import check_table_existence


user_db_map = UserMapRedis()


@celery.task
def master_sat_calculate(user, topo):
    """
    通过用户 + 拓扑定位到星座，对星座实时更新的死循环
    """

    def _get_link_id(link_dict, dev1, dev2):
        """
        从链路字典中，反向查询链路编号
        """
        for link_id, devs in link_dict.items():
            if dev1 in devs and dev2 in devs:
                return f'l{link_id}'
    

    ############################ 星座刷新初始化 ############################
    table_name = f'{topo}{PROJ_CONFIG.sat_table_name}'  # 用户db里的卫星表名
    user_db_cli = user_db_map.get_user_db(user)  # 返回时关闭数据库连接，不用with
    no_run_too_long_count = 0  # 未超时的次数统计
    time_speed = -1  # 时间倍速初始化，初值是-1。为探测倍速变化而设立
    last_mode = ''  # 初始模式，对比上次和本次模式间区别，以确定是否进行模式初始化

     ############################# 静态数据读取 #############################
    # 小子网创建映射
    ip_dict, sat_gnd_nets = user_db_cli.get_value(table_name, "ip-net")
    # 第一个卫星编号, 预备主机, 第一个星地链路
    # 地面站星下ovs，卫星 -> 用于连接地面站的星下ovs
    sat_id1, spare_conn, l_id = user_db_cli.get_value(table_name, "virtual-para")
    sat_ovs_gnd, sat_ovs_walker = user_db_cli.get_value(table_name, "sat-ovs")
    sat_ovs_gnd[None] = spare_conn
    # 星座星下ovs，卫星 -> 用于连接星座的星下ovs
    sat_ovs_walker[None] = spare_conn
    # 链路字典，链路编号（数字）-> 两端设备名列表
    link_dict = user_db_cli.get_value(table_name, "links2dev")
    # 网卡启停
    ne_up_down = user_db_cli.get_value(table_name, "ne-up-down")
    
    
    # 循环模拟星座运行刷新，每次循环变化星座链路通断和质量
    print("星座开始刷新！")
    while True:
        try:
            # 刷新初始时刻
            time_start = time()

            ###################### （1）参数变更和提取 ######################
            # 参数变更应用到星座
            temp = user_db_cli.get_value(table_name, 'temp')
            if temp:
                for key, val in temp.items():
                    user_db_cli.set_value(table_name, key, val)
                user_db_cli.set_value(table_name, 'temp', {})
            # 星座参数
            walkers = user_db_cli.get_value(table_name, 'walkers')
            # 地面站参数字典, 卫星身份, 选星策略, 星间转发延迟
            gnd_devices, method = user_db_cli.get_value(table_name, 'gnd-dev')
            rs, bw = user_db_cli.get_value(table_name, 'link-config')
            sat_identity, mode = user_db_cli.get_value(table_name, 'mode')
            # 地面站列表
            dev_list = list(gnd_devices.keys())
            # 星地链路，地面站 -> 卫星
            sat_gnd_links = user_db_cli.get_value(table_name, 'sat-gnd links')
            # 星座链路，较低轨卫星 -> 较高轨卫星
            all_sat_highsat_links = user_db_cli.get_value(table_name, 'sat-highsat links')
            # 定时参数
            timer = user_db_cli.get_value(table_name, 'timer')
            # 初始状态日志记录
            if time_speed == -1:
                for dev, sat in sat_gnd_links.items():
                    ctn_satlog(topo, user_db_cli, dev, f"本地面站初始连接卫星: {sat[0]}")

            ###################### （2）参数变更和计时 ######################
            # 若时间已暂停
            if timer[1] == 0:
                sleep(60 / PROJ_CONFIG.max_time_speed)
                continue
            # 首次进行星座刷新，则记录当前倍速
            if time_speed == -1:
                time_speed = timer[1]
                refresh_interval = PROJ_CONFIG.refresh_interval_para * \
                    time_speed * sum([walker['N']*2 - walker['N']/walker['P']
                                      for walker in walkers])
            # 非首次进行星座刷新，则判断倍速是否变化。若变化，调整星座世界刷新间隔
            elif time_speed != timer[1]:
                refresh_interval *= timer[1] / time_speed
                time_speed = timer[1]
            # 计算时间参数
            time_now = timestamp2date(timer[0])  # 当前对应时戳
            timer[0] += refresh_interval         # 下次模拟时刻
            timer[2] = time_start                # 当前真实时刻
            timer[3] = refresh_interval          # 本次卫星世界刷新周期
            # 时间参数写入数据库
            user_db_cli.set_value(table_name, 'timer', timer)

            ####################### （3）模式变化处理 #######################
            last_mode = sat_change_mode(topo, user_db_cli, last_mode, mode,
                                        walkers, sat_gnd_links, sat_id1,
                                        sat_gnd_nets, sat_ovs_gnd,
                                        dev_list, gnd_devices)
            
            ######################### （4）星间链路 #########################
            # 拓扑原有链路
            existed_links = user_db_cli.get_value("plane_topo_list", topo)["links"]
            # 已有卫星编号偏移
            existed_sat_id = 0
            # 对每层walker星座
            for walker in walkers:
                # 0）参数提取，创建单轨道星座对象
                N, P, i, F, h, ang = get_walker_para(walker)
                walker = Walker(N, P, i, h, F, ang)
                # 星间连接关系
                all_links, no_link = walker.get_links(time_now)

                # 1）删除链路：原有星间链路不连接【通断发生在极区和赤道】
                for link in existed_links:
                    # 若链路与星座有关
                    if link[1:] in link_dict.keys():
                        # 链路两端设备名
                        devs = link_dict[link[1:]]
                        # 判断是否为与地面站关联的链路
                        if any([dev in devs for dev in gnd_devices.keys()]):
                            continue
                        # 卫星编号
                        devs_id = [int(dev[1:]) - sat_id1 - existed_sat_id
                                   for dev in devs]
                        # 若无连接，则删除veth-pair
                        if devs_id in no_link:
                            print(f"{user}, {topo}, 删除星间链路{link}：{devs_id}")
                            rsp = veth_delete(user, topo, user_db_cli, link, *devs,
                                              ne_up_down=ne_up_down)
                            if rsp['code'] == 0:
                                print(rsp['msg'])
                
                # 2）新增链路：需连接的链路原不存在【通断发生在极区和赤道】
                for link in all_links:
                    # 局部编号、设备名
                    devs = [f"{sat_identity[0]}{dev_id+sat_id1+existed_sat_id}"
                            for dev_id in link[:-1]]
                    # 获取对应设备的链路名
                    link_name = _get_link_id(link_dict, *devs)
                    # 若原不存在，则新增veth-pair
                    if link_name not in existed_links:
                        print(f"{user}, {topo}, 新增星间链路{link_name}：{link[:-1]}")
                        rsp = veth_create(user, topo, user_db_cli,
                                          link_name, *devs,
                                          subnet_ip=ip_dict[link_name[1:]] if sat_identity=="router" else "",
                                          ne_up_down=ne_up_down)
                        if rsp['code'] == 0:
                            print(rsp['msg'])
                
                # 3）tc：对所有存在的星间链路【较多发生】
                for link in all_links:
                    # 卫星设备名
                    sat1 = f"{sat_identity[0]}{link[0]+sat_id1+existed_sat_id}"
                    sat2 = f"{sat_identity[0]}{link[1]+sat_id1+existed_sat_id}"
                    # 获取链路名称
                    link_name = _get_link_id(link_dict, sat1, sat2)
                    # 替换tc规则
                    rsp = tc_create(user, topo, user_db_cli, link_name, 
                                    sat1, sat2, rs, bw, dist=link[2])
                    if rsp['code'] == 0:
                        print(rsp['msg'])

                # 4）从低轨到高轨进行卫星编号
                existed_sat_id += N

            ######################### （5）星地链路 #########################
            # 对每个地面站情况(case)匹配
            #  | case | action       | veth | tc |
            #  | -1   | undetermined | -    | -  |
            #  | 0    | s1 -> s1     | x    | √  |
            #  | 1    | s1 -> s2     | √    | √  |
            #  | 2    | s1 -> None   | √    | x  |
            #  | 3    | None -> s1   | √    | √  |
            #  | 4    | None -> None | x    | x  |
            link_id = l_id
            for dev, para in gnd_devices.items():
                # 1）初始化
                old_con = sat_gnd_links[dev][0]   # 旧连接的卫星id
                link_name = f"l{link_id}"  # 地面站链路名称
                link_id += 1               # 每个设备对应一个链路编号
                global_best_sat = [None, np.inf, 0]  # 全局最优卫星
                case = -1  # 初始时处于未决情况
                
                # 2）遍历各高度轨道
                existed_sat_id = 0
                for walker in walkers:
                    # 2.1）参数提取，单轨道星座对象
                    N, P, i, F, h, ang = get_walker_para(walker)
                    walker = Walker(N, P, i, h, F, ang)
                    # 2.2）计算可见卫星的输入参数
                    input_para = (time_now,
                                  [para['position'][0], para['position'][1],
                                   PROJ_CONFIG.gnd_dev_level[para['antenna_level']-1][2]],
                                  method)
                    # 2.3）本高度轨道的所有可见卫星的判断
                    local_sat = walker.get_visible_sats(*input_para)
                    # 旧连接卫星是本次中的可见卫星，则本地面站无需更新
                    # 情况0
                    if old_con != None and \
                       old_con - existed_sat_id in local_sat.keys():
                        veth_info = False
                        tc_flag = True
                        case = 0
                        break
                    # 2.4）旧连接卫星不可见，计算本层最佳卫星，并更新全局最优
                    local_best_sat = walker.get_best_visible_sat(*input_para)
                    # 若局部无最优卫星，则不更新
                    # 若局部有最优卫星，则按照策略进行对比更新
                    if local_best_sat[0] != None and \
                      (method == 1 and global_best_sat[1] > local_best_sat[1] or \
                       method == 2 and global_best_sat[2] < local_best_sat[2]):
                        local_best_sat[0] += existed_sat_id
                        global_best_sat = local_best_sat
                    # 2.5）从低轨到高轨进行卫星编号
                    existed_sat_id += N
                
                # 3）情况未决，旧卫星全局不可见
                if case == -1:
                    # 新连接的卫星及距离，更新字典
                    sat_gnd_links[dev] = global_best_sat
                    # 新连接的卫星id
                    new_con = global_best_sat[0]
                    # 分情况(case)讨论
                    # 情况1
                    if new_con != None and old_con != None:
                        veth_info = [str(old_con), str(new_con)]
                        tc_flag = True
                    # 情况2
                    elif new_con == None and old_con != None:
                        veth_info = [str(old_con), None]
                        tc_flag = False
                    # 情况3
                    elif new_con != None and old_con == None:
                        veth_info = [None, str(new_con)]
                        tc_flag = True
                    # 情况4
                    else:
                        veth_info = tc_flag = False
                    # 星地切换输出、日志记录
                    if tc_flag or veth_info:
                        # 准备工作
                        sat_change = f"{'无可见' if old_con==None else old_con}" + \
                                  f" > {'无可见' if new_con==None else new_con}"
                        timestamp = round(timer[0])
                        # 打印输出
                        print(f"🔺 {user}, {topo}, {dev}换星: {sat_change}, "
                              f"时戳：{timestamp}秒 ({time_now[3]}时"
                              f"{time_now[4]}分{time_now[5]}秒)")
                        # 日志记录
                        logs = user_db_cli.get_value(table_name, 'sat log')
                        logs.append(f"时戳{timestamp}秒，地面站{dev}换星: {sat_change}")
                        user_db_cli.set_value(table_name, 'sat log', 
                                              logs[-PROJ_CONFIG.max_satlog_length:])

                # 4）如进行veth迁移
                if veth_info:
                    # 隧道方案，且存在隧道两端设备换星
                    if mode == 'IP-TUNNEL' and new_con:
                        new_con = str(new_con)
                        # 卫星IP
                        sat_ip = int2ip((sat_gnd_nets[new_con] & ip2int(PROJ_CONFIG.sat_gnd_subnet_mask)) + 1)
                        # 地面站IP
                        gnd_ip = int2ip(sat_gnd_nets[new_con])
                        sat_gnd_nets[new_con] += 1
                        if sat_gnd_nets[new_con] >= ip2int(sat_ip) + \
                           2*(2**(32-netmask2cidr(PROJ_CONFIG.sat_gnd_subnet_mask)) - 1):
                            sat_gnd_nets[new_con] = int2ip(ip2int(sat_ip) + 1)
                        
                        # 更改换星地面站与所有其他地面站之间的tunnel
                        exec_already = False
                        for other_dev in dev_list:
                            # 排除本地面站
                            if other_dev == dev:
                                continue
                            # 根据地面站顺序，确定隧道名称
                            if dev_list.index(dev) < dev_list.index(other_dev):
                                tunnel_name = dev + other_dev
                            else:
                                tunnel_name = other_dev + dev
                            # 修改隧道
                            change_tunnel_between(topo, user_db_cli,
                                                  dev, other_dev,
                                                  gnd_ip, 'link_'+link_name,
                                                  tunnel_name=tunnel_name,
                                                  exec_already=exec_already)
                            exec_already = True
                    
                    # DHCP方案
                    elif mode == 'DHCP' and new_con:
                        new_con = str(new_con)
                        sat_ip = int2ip((sat_gnd_nets[new_con] & ip2int(PROJ_CONFIG.sat_gnd_subnet_mask)) + 1)
                    
                    # 不配置IP方案
                    elif mode == 'IP-NO-MODIFY':
                        sat_ip = "no modify"
                    
                    # 其他方案
                    else:
                        # 计算卫星IP
                        if dev[0] == 'h':
                            sat_ip = para['gateway']
                        else:
                            # 链路地面站侧已占用的ip
                            occupied_ip = ip2int(para['ip'])
                            # 子网号
                            net = occupied_ip & ip2int(para['netmask'])
                            # 第一个子网内可用ip，作为链路卫星侧的ip
                            for ip in range(net+1, net+2**(32-netmask2cidr(para['netmask']))-1):
                                if ip != occupied_ip:
                                    sat_ip = int2ip(ip)
                                    break
                    
                    # veth迁移
                    rsp = veth_move(user, topo, user_db_cli, link_name, dev,
                                    sat_ovs_gnd[veth_info[0]],
                                    sat_ovs_gnd[veth_info[1]],
                                    mode, sat_ip, para['netmask'])
                    if rsp['code'] == 0:
                        print(rsp['msg'])
                    
                    # 更新地面站 DHCP client
                    if mode == 'DHCP':
                        start_dhcp_client(topo, user_db_cli, dev, ip2int(sat_ip) if new_con else "")

                    # 地面站日志记录
                    # 若为TUNNEL模式，对每个地面站都需写日志
                    if mode == 'IP-TUNNEL':
                        for d in gnd_devices:
                            log_str = ('本地面站' if d == dev else '对端地面站') + \
                                f'换星 - {veth_info[0]}>{veth_info[1]}, 新IP: {gnd_ip}'
                            ctn_satlog(topo, user_db_cli, d, log_str)
                    # 若非TUNNEL模式，只有换星的地面站需写日志
                    else:
                        ctn_satlog(topo, user_db_cli, dev,
                                   f'本地面站换星{veth_info[0]}>{veth_info[1]}')

                # 5）如进行tc配置
                if tc_flag:
                    # 和卫星相连的节点名
                    connect_with_dev = sat_ovs_gnd[str(sat_gnd_links[dev][0])]
                    # 替换tc规则
                    rsp = tc_create(user, topo, user_db_cli, link_name,
                                    connect_with_dev, dev, rs, bw,
                                    target_para=PROJ_CONFIG.gnd_dev_level[para['antenna_level']-1][:-1],
                                    dist=sat_gnd_links[dev][1],
                                    place='sat-gnd')
                    if rsp['code'] == 0:
                        print(rsp['msg'])

            ######################### （6）星座链路 #########################
            # 对每个下层卫星情况(case)匹配
            #  | case | action       | veth | tc |
            #  | -1   | undetermined | -    | -  |
            #  | 0    | s1 -> s1     | x    | √  |
            #  | 1    | s1 -> s2     | √    | √  |
            #  | 2    | s1 -> None   | √    | x  |
            #  | 3    | None -> s1   | √    | √  |
            #  | 4    | None -> None | x    | x  |
            existed_sat_id = 0
            for i in range(len(walkers)-1):  # 对每个较低层星座
                # 1）参数提取
                # 下层星座
                N1, P1, i1, F1, h1, ang1 = get_walker_para(walkers[i])
                walker1 = Walker(N1, P1, i1, h1, F1, ang1)
                # 上层星座
                N2, P2, i2, F2, h2, ang2 = get_walker_para(walkers[i+1])
                walker2 = Walker(N2, P2, i2, h2, F2, ang2)
                # 2）对每个下层卫星，查看上层卫星连接
                for j in range(N1):
                    # 2.1）初始化
                    # 下层卫星id
                    sat_id = existed_sat_id + j
                    # 下层卫星设备名
                    sat_dev = f"{sat_identity[0]}{sat_id1+sat_id}"
                    # 星座链路名称，链路编号自增
                    link_name = f"l{link_id}"
                    link_id += 1
                    # 上层旧连接卫星
                    old_con = all_sat_highsat_links[str(sat_id)][0]
                    # 2.2）输入参数
                    max_dist = get_limit_elevation_ang_or_dist(
                        h1, h2, ang1, ang2, output="dist")
                    input_para = (time_now, sat_id, walker1, walker2,
                                  max_dist, existed_sat_id)
                    # 2.3）情况匹配
                    # 旧连接卫星是本次可见的卫星（情况0）
                    if old_con != None and \
                       old_con in get_visible_sats(*input_para):
                        veth_info = False
                        tc_flag = True
                    # 旧连接为非上层可见卫星，或无旧连接卫星（情况1~4），进行卫星切换
                    else:
                        # 当前最佳卫星
                        best_sat = get_best_visible_sat(*input_para)
                        # 更新字典，记录新连接卫星及距离
                        all_sat_highsat_links[str(sat_id)] = best_sat
                        # 新连接卫星id
                        new_con = best_sat[0]
                        # 分情况(case)讨论
                        # 情况1
                        if new_con != None and old_con != None:
                            veth_info = [str(old_con), str(new_con)]
                            tc_flag = True
                        # 情况2
                        elif new_con == None and old_con != None:
                            veth_info = [str(old_con), None]
                            tc_flag = False
                        # 情况3
                        elif new_con != None and old_con == None:
                            veth_info = [None, str(new_con)]
                            tc_flag = True
                        # 情况4
                        else:
                            veth_info = tc_flag = False
                        # 切换输出
                        if tc_flag or veth_info:
                            timestamp = round(timer[0])
                            print(f"🔹 {user}, {topo}, 下层卫星{sat_id}换星: "
                                  f"{'无可见' if old_con==None else old_con} > "
                                  f"{'无可见' if new_con==None else new_con}, "
                                  f"时戳：{timestamp}秒 ({time_now[3]}时{time_now[4]}分{time_now[5]}秒)")
                    # 2.4）如进行veth迁移
                    if veth_info:
                        rsp = veth_move(user, topo, user_db_cli, link_name, sat_dev,
                                        sat_ovs_walker[veth_info[0]],
                                        sat_ovs_walker[veth_info[1]], 
                                        mode, ip="no modify" if mode == 'IP-NO-MODIFY' else "")
                        if rsp['code'] == 0:
                            print(rsp['msg'])
                    # 2.5）如进行tc配置
                    if tc_flag:
                        # 和卫星相连的节点名
                        connect_with_dev = sat_ovs_walker[str(all_sat_highsat_links[str(sat_id)][0])]
                        # 替换tc规则
                        rsp = tc_create(user, topo, user_db_cli, link_name, 
                                        connect_with_dev, sat_dev, rs, bw,
                                        dist=all_sat_highsat_links[str(sat_id)][1])
                        if rsp['code'] == 0:
                            print(rsp['msg'])
                    
                # 3）从低轨到高轨进行卫星编号，因此需进行已有编号的偏移
                existed_sat_id += N1

            ######################## （7）更新数据库 ########################
            # 星地链路，地面站 -> 卫星
            user_db_cli.set_value(table_name, 'sat-gnd links', sat_gnd_links)
            # 星座链路，较低轨卫星 -> 较高轨卫星
            user_db_cli.set_value(table_name, 'sat-highsat links', all_sat_highsat_links)

            ####################### （8）动态定时刷新 #######################
            run_time = time() - time_start                # 本次运行时长
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
            refresh_interval = round(timer[1] * run_time)

        # 忽略请求连接和json的问题
        except requests.exceptions.ConnectionError:
            print('Requests connectionError occured!')
            pass
        except json.decoder.JSONDecodeError:
            print('Requests JSONDecodeError occured!')
            pass

        # 若表不存在，说明拓扑删除时数据库被删除，则退出循环，星座停止更新
        except sat_update_error:
            print("星座结束刷新！")
            # 防止发生Error后继续写表的问题
            table_name = f'{topo}{PROJ_CONFIG.sat_table_name}'
            if check_table_existence(user, table_name):
                user_db_cli.del_table(table_name)
            # 关闭数据库连接
            user_db_cli.close()
            # 刷新函数结束
            return
