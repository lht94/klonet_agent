"""
卫星相关接口
"""

from flask.views import MethodView
from flask import request
import grequests
import json, time

from ....vemu_config.config import PROJ_CONFIG
from ....tools.context import check_table_existence, redis_context
from ....satellite.satool import *


class SatelliteWalkerAPI(MethodView):
    """
    /satellite/walker/

    获得与修改星座参数
    """
    def post(self):
        """
        修改星座属性
        """
        try:
            # 从请求中的 json 获得用户数据
            data = json.loads(request.get_data(as_text=True))
            # 信息提取
            user, topo, config = data['user'], data['topo'], data['config']
            table_name = f"{topo}{PROJ_CONFIG.sat_table_name}"
            # 判断是否包含星座
            if not check_table_existence(user, table_name):
                return {'code': 0, 'msg': '无星座信息记录'}
            # config中的所有键
            keys = config.keys()
            # 判断各个参数合法性并应用
            with redis_context(user) as user_db_cli:
                # 参数提取
                walker = user_db_cli.get_value(table_name, 'walkers')
                link_config = user_db_cli.get_value(table_name, 'link-config')
                mode = user_db_cli.get_value(table_name, 'mode')
                gnd_dev = user_db_cli.get_value(table_name, 'gnd-dev')
                timer = user_db_cli.get_value(table_name, 'timer')
                # 写入临时位置的内容
                temp = user_db_cli.get_value(table_name, 'temp')

                ############################ walker ###########################
                # 只有单层walker可以修改参数
                if len(walker) == 1 and "walkers" in keys:
                    # 不变量有N和P
                    P = walker[0]["P"]
                    walker = config["walkers"]
                    if "i" in walker:
                        if not 0 <= walker["i"] < 360:
                            return {'code': 0,
                                    'msg': '星座参数错误，轨道倾角范围在0~360度间'}
                        else:
                            walker[0]["i"] = walker["i"]
                    if "h" in walker:
                        if walker["h"] < 6372:
                            return {'code': 0,
                                    'msg': '星座参数错误，轨道半径大于地球半径'}
                        else:
                            walker[0]["h"] = walker["h"]
                            if 400 <= walker["h"]-6372 <= 2000:
                                walker[0]["orbit"] = 'LEO'
                            elif 2000 <= walker["h"]-6372 <= 36000:
                                walker[0]["orbit"] = 'MEO'
                            elif walker["h"] == 42164:
                                walker[0]["orbit"] = 'GEO'
                            else:
                                return {'code': 0,
                                        'msg': '星座参数错误，轨道半径介于MEO和GEO间'}
                    if "F" in walker:
                        if not 1<= walker["F"] <= P-1 and P != 1:
                            return {'code': 0,
                                    'msg': '星座参数错误，相位因子范围在1~P-1间'}
                        else:
                            walker[0]["F"] = walker["F"]
                    if "sensor_angle" in walker:
                        if not 0 <= walker["sensor_angle"] <= 180:
                            return {'code': 0,
                                    'msg': '卫星可视角度在0~180间'}
                        else:
                            walker[0]["sensor_angle"] = walker["sensor_angle"]
                    temp['walkers'] = walker
                

                ############################ link-config  ####################
                # 星间转发延迟 - rs
                if "rs" in keys:
                    if not 1 <= config["rs"] <= PROJ_CONFIG.max_rs:
                        return {'code': 0, 'msg': f'星间路由延迟超过范围'}
                    else:
                        link_config[0] = config["rs"]
                # 链路带宽 - bw
                if "bw" in keys:
                    if not 1 <= config["bw"]:
                        return {'code': 0, 'msg': f'链路带宽为正整数'}
                    else:
                        link_config[1] = config["bw"]
                temp['link-config'] = link_config

                ########################## gnd-dev ###########################
                # 选星策略 - method
                if "method" in keys:
                    if config["method"] not in [1, 2]:
                        return {'code': 0,
                                'msg': '选星策略取值为1(最短距离)或2(最长可见时间)'}
                    else:
                        gnd_dev[1] = config["gnd-dev"]   

                ############################ mode #############################
                # 动态修改IP使能（本质上是模式修改，但优先于模式修改）
                if "modify_ip" in keys:
                    if type(config["modify_ip"]) != bool:
                        return {'code': 0, 'msg': f'动态修改IP使能应为布尔值'}
                    else:
                        mode[1] = 'IP-MODIFY' if config["modify_ip"] \
                                 else 'IP-NO-MODIFY'
                # 动态修改模式 - mode
                elif "mode" in keys:
                    # 新老模式
                    new_mode = config["mode"]
                    old_mode = mode[1]
                    # 判断模式变更是否可行
                    if new_mode not in ['SDN', 'STP', 'NO-STP', 'DHCP',
                                        'IP-NO-MODIFY', 'IP-MODIFY', 'IP-TUNNEL']:
                        return {'code': 0,
                                'msg': '星间转发模式取值为 SDN/STP/NO-STP/DHCP'
                                       '/IP-MODIFY/IP-NO-MODIFY/IP-TUNNEL'}
                    # 目前可行的模式变更只能在如下的模式之间进行
                    #       STP <--> NO-STP
                    #      DHCP <--> IP-TUNNEL
                    # IP-MODIFY <--> IP-NO-MODIFY
                    if {new_mode, old_mode} not in [{'STP', 'NO-STP'},
                                                    {'DHCP', 'IP-TUNNEL'},
                                                    {'IP-MODIFY', 'IP-NO-MODIFY'}]:
                        return {'code': 0, 'msg': '当前模式变更不可行！'}
                    # 进行模式变更赋值
                    mode[1] = new_mode
                temp['mode'] = mode

                ############################ timer ############################
                # 时间倍速 - time_speed
                if "time_speed" in keys:
                    # 时间参数超过范围检测
                    if not 0 <= config["time_speed"] <= PROJ_CONFIG.max_time_speed:
                        return {'code': 0,
                                'msg': '星座运行的时间加速速度在'
                                      f'1~{PROJ_CONFIG.max_time_speed}间'}
                    # 倍速赋值
                    else:
                        timer[1] = config["time_speed"]
                        temp['timer'] = timer

                # 写入临时存储空间，而不是直接修改参数
                # 待星座刷新进程更新参数，以保证一致性
                user_db_cli.set_value(table_name, 'temp', temp)

            return {'code': 1, 'msg': '星座信息修改成功'}
            
        except Exception as e:
            return {"code": 0, "msg": str(e)}

    def get(self):
        """
        获取卫星数据库中的所有信息

        可选的about字段：
        - link：星间链路连接情况（星间+星座链路）
        - walker（默认）：星座参数
        - timer：定时参数
        - spot_down：所有卫星的星下点信息
        - wgs84：所有卫星wgs84位置信息
        """
        try:
            data = request.args
            user, topo, about = data['user'], data['topo'], data['about']
            table_name = f"{topo}{PROJ_CONFIG.sat_table_name}"
            # 判断是否包含星座
            if not check_table_existence(user, table_name):
                return {'code': 0, 'msg': '无星座信息记录'}
            
            with redis_context(user) as user_db_cli: 
                if check_table_existence(user, table_name):
                    # 获取walker星座参数和定时参数
                    walkers = user_db_cli.get_value(table_name, 'walkers')
                    timer = user_db_cli.get_value(table_name, 'timer')

                    # 计算收到请求时，星座的模拟时刻
                    # time_now = timestamp2date(timer[0] - timer[3] + timer[1] * (time() - timer[2]))
                    # timer = [卫星世界初始时刻, 时间倍速, 真实世界初始时刻]
                    time_now = timestamp2date((time() - timer[2]) * timer[1] + timer[0])

                    # ?? 想知道星间链路连接情况 ??
                    if about == 'link':
                        # 星间链路
                        in_walkers = []
                        existed_sat_id = 0
                        for walker in walkers:
                            N, P, i, F, h, ang = get_walker_para(walker)
                            walker = Walker(time_now, N, P, i, h, F, ang)
                            in_walkers += [[link_list[0]+existed_sat_id,
                                            link_list[1]+existed_sat_id]
                                            for link_list in walker.get_intra_links_in_walker()[0]]
                            existed_sat_id += N
                        # 星座链路
                        between_walkers = []
                        for key, val in user_db_cli.get_value(
                                table_name, 'sat-highsat links').items():
                            if val[0]:
                                between_walkers.append([int(key), val[0]])

                        return {"in_walkers": in_walkers, "between_walkers": between_walkers}
                    
                    # ?? 想知道定时参数 ??
                    if about == 'timer':
                        return {"timer para": timer, "time now": time_now}
                    
                    # ?? 想知道所有卫星的星下点信息 ??
                    if about == 'spot_down':
                        ret = []
                        existed_sat_id = 0
                        for walker in walkers:
                            N, P, i, F, h, ang = get_walker_para(walker)
                            walker = Walker(time_now, N, P, i, h, F, ang)
                            ret += walker.get_spot_down()
                            existed_sat_id += N
                        return {'spot_down': ret}

                    # ?? 想知道卫星wgs84位置信息 ??
                    if about == 'wgs84':
                        ret = []
                        existed_sat_id = 0
                        for walker in walkers:
                            N, P, i, F, h, ang = get_walker_para(walker)
                            walker = Walker(time_now, N, P, i, h, F, ang)
                            ret += walker.get_wgs84_pos()
                            existed_sat_id += N
                        return {'wgs84': ret}
                    
                    # ?? 想知道卫星日志信息 ??
                    if about == 'log':
                        log = user_db_cli.get_value(table_name, 'sat log')
                        return {'log': log}

                    # ?? 如果不告诉想知道什么，就返回卫星基本参数 ??
                    return {"walker para": walkers}

                else:
                    return {'code': 0, 'msg': '数据库中无星座信息记录'}
        
        except Exception as e:
            return {"code": 0, "msg": str(e)}
       

class SatelliteGndAPI(MethodView):
    """
    /satellite/gnd/

    获得与修改地面站参数
    """
    def post(self):
        """
        （废弃）修改地面站目前所连卫星
        """
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            user, topo, dev, sat = data['user'], data['topo'], data['dev'], data['sat']
            table_name = f"{topo}{PROJ_CONFIG.sat_table_name}"
            
            # 判断是否包含星座
            if not check_table_existence(user, table_name):
                return {'code': 0, 'msg': '无星座信息记录'}
            
            # 修改星地连接
            with redis_context(user) as user_db_cli:
                # 星座参数
                walkers = user_db_cli.get_value(table_name, 'walkers')
                # 时间参数
                timer = user_db_cli.get_value(table_name, 'timer')
                time_now = timestamp2date((time() - timer[2]) * timer[1] + timer[0])
                # 地面站设备信息
                gnd_dev, method = user_db_cli.get_value(table_name, 'gnd-dev')
                # 地面站参数
                if dev not in gnd_dev.keys():
                    return {'code': 0, 
                            'msg': f'无地面站信息记录，拓扑中所有地面站包括{gnd_dev.keys()}'}
                para = gnd_dev[dev]
                # 卫星id规范检查
                sat_count = sum([walker['N'] for walker in walkers])  # 卫星总数
                if not isinstance(sat, int) or sat < 0 or sat >= sat_count:
                    return {'code': 0, 
                            'msg': f'卫星编号不合法，需要是int类型，范围为0~{sat_count-1}'}
                # 确定卫星所在哪层walker，从低层到高层遍历
                existed_sat_id = 0
                for walker in walkers:
                    # 参数提取，单轨道星座对象
                    N, P, F, i, h, sat_ang = get_walker_para(walker)
                    # 卫星编号太大，进入下层的循环
                    if sat >= existed_sat_id + N:
                        existed_sat_id += N
                        continue
                    # 所选卫星就在这一层
                    walker = Walker(time_now, N, P, i, h, F, sat_ang)
                    # 本高度轨道所有可见卫星
                    local_sat = walker.get_visible_sats(
                        [para['position'][0], para['position'][1],
                        PROJ_CONFIG.gnd_dev_level[para['antenna_level']-1][2]],
                        method
                    )
                    # 欲修改到的卫星为本层的可见卫星
                    if sat-existed_sat_id in local_sat.keys():
                        # 修改数据库并返回
                        sat_gnd_links = user_db_cli.get_value(table_name, 'sat-gnd links')
                        sat_gnd_links[dev] = [sat, local_sat[sat-existed_sat_id][0]]
                        user_db_cli.set_value(table_name, 'sat-gnd links', sat_gnd_links)
                        # 返回
                        return {'code': 1, 'msg': '地面站主动换星成功'}
                    # 否则说明所选卫星不可见
                    else:
                        return {'code': 0, 
                                'msg': f'所选卫星不可见，可以选择的可见卫星有{[sat+existed_sat_id for sat in local_sat.keys()]}'}
        
        except Exception as e:
            return {"code": 0, "msg": str(e)}

    def get(self):
        """
        获得地面站目前所连卫星及距离
        """
        try:
            data = request.args
            user, topo, dev = data['user'], data['topo'], data['dev']
            table_name = f"{topo}{PROJ_CONFIG.sat_table_name}"
            # 判断是否包含星座
            if not check_table_existence(user, table_name):
                return {'code': 0, 'msg': '无星座信息记录'}

            # 进入数据库，修改星地连接
            with redis_context(user) as user_db_cli:
                # 获得星地所有连接
                dev_dict = user_db_cli.get_value(table_name, 'sat-gnd links')
                # 若欲修改的地面站不在字典的keys中，则返回
                if dev not in dev_dict.keys():
                    return {'code': 0, 'msg': '无地面站信息记录'}
                # 提取所连卫星
                dev_list = dev_dict[dev]
                return {'code': 1, 'sat': dev_list[0],
                        'dist': dev_list[1],'msg': '提取成功'}
        
        except Exception as e:
            return {"code": 0, "msg": str(e)}


class SatelliteSatAPI(MethodView):
    """
    /satellite/sat/

    获得卫星相关参数
    """
    def get(self):
        """
        获得卫星与同轨相邻卫星链路连接的ip
        """         
        try:
            data = request.args
            user, topo, sat = data['user'], data['topo'], data['sat']
            #该卫星的表名
            table_name = f"{topo}_{sat}"
    
            # 判断是否存在该设备
            if not check_table_existence(user, table_name):
                return {'code': 0, 'msg': '无设备信息'}
            # 卫星不是路由器
            if sat[0] != 'r':
                return {'code': 0, 'msg': '该卫星不是路由器'}
            else:
                with redis_context(user) as user_db_cli:
                    nic_ip_list = []
                    #查询该设备所有link相关的key
                    all_keys_bytes = user_db_cli.get_all_keys(table_name)
                    # all_keys = [key.decode('utf-8') for key in all_keys_bytes]

                    for key in all_keys_bytes:
                        if key.startswith("link_"):
                            link_info = user_db_cli.get_value(table_name, key)
                            nic = link_info['nic']
                            ip = link_info['ip']
                            nic_ip_list.append({nic:ip})
                    return {'code': 1, 'ip': nic_ip_list, 'msg': '提取成功'}
                    

        except Exception as e:
            return {"code": 0, "msg": str(e)}


class SatelliteAllGndAPI(MethodView):
    """
    /satellite/allgnd/

    获得所有地面站的参数
    """
    def get(self):
        """
        获得地面站目前所连卫星及距离
        """
        try:
            data = request.args
            user, topo = data['user'], data['topo']
            table_name = f"{topo}{PROJ_CONFIG.sat_table_name}"
            # 判断是否包含星座
            if not check_table_existence(user, table_name):
                return {'code': 0, 'msg': '无星座信息记录'}
            # 进入数据库，修改星地连接
            ret = {}
            with redis_context(user) as user_db_cli:
                # 所有星地连接
                dev_dict = user_db_cli.get_value(table_name, 'sat-gnd links')
                # 所有地面站
                devs = user_db_cli.get_value(table_name, 'gnd-dev')[0]
                for dev, vals in devs.items():
                    # 提取所连卫星
                    dev_list = dev_dict[dev]
                    # 返回中添加字段
                    ret[dev] = {'position': vals['position'],
                                'sat': dev_list[0]}
            return {'code': 1, 'msg': '提取成功', 'data': ret}
        
        except Exception as e:
            return {"code": 0, "msg": str(e)}


class SatelliteSDN(MethodView):
    """
    /satellite/sdn/

    获得sdn所需信息
    """
    def get(self):
        """
        获得地面站目前所连卫星及dpid
        """
        try:
            data = request.args
            user, topo = data['user'], data['topo']
            table_name = f"{topo}{PROJ_CONFIG.sat_table_name}"
            # 判断是否包含星座
            if not check_table_existence(user, table_name):
                return {'code': 0, 'msg': '无星座信息记录'}
            # 进入数据库，修改星地连接
            ret = {}
            with redis_context(user) as user_db_cli:
                # 所有星地连接
                '''sat-gnd links
                {"h1": [4, 22069.883955940517], "h2": [4, 26140.326533001382], 
                "h3": [4, 26330.2570294], "h4": [2, 22531.460852227363], 
                "h5": [9, 21351.374026020298]}
                '''
                sat_gnd_links = user_db_cli.get_value(table_name, 'sat-gnd links')
                # 所有地面站,某些地面站可能没有星地连接
                devs = user_db_cli.get_value(table_name, 'gnd-dev')[0]
                for dev, vals in devs.items():
                    # 提取所连卫星,dev_list包含所连接卫星
                    dev_list = sat_gnd_links[dev]
                    sat_num = dev_list[0]+1
                    # 返回中添加字段
                    sat_table_name = f"{topo}_s{sat_num}"
                    sat_dict = user_db_cli.get_value(sat_table_name, 'NEconfig')
                    sat_dpid = sat_dict['config']['dpid']

                    host_table_name = f"{topo}_{dev}"
                    keys = user_db_cli.get_all_keys(host_table_name)
                    link_keys = [key for key in keys if key.startswith('link')]
                    eth_info = user_db_cli.get_value(host_table_name, link_keys[0])
                    ret[dev] = {'sat': sat_num, 'dpid': sat_dpid, "ip": eth_info['ip'], "mac": eth_info['mac']}
                    
            return {'code': 1, 'msg': '提取成功', 'data': ret}
        
        except Exception as e:
            return {"code": 0, "msg": str(e)}


class SatellitePreDraw(MethodView):
    """
    /satellite/predraw/

    预先绘制walker坐标
    """
    def post(self):
        """
        获得指定星座在指定时刻下各卫星的wgs84位置和星间链路
        """
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            walkers, time_now = data['walkers'], data['time']
            time_now = timestamp2date(time_now)
            
            # 计算wgs84位置和星间链路
            wgs84 = []
            in_walkers = []
            existed_sat_id = 0
            for walker in walkers:
                N, P, i, F, h, ang = get_walker_para(walker)
                walker = Walker(time_now, N, P, i, h, F, ang)
                links, _ = \
                    walker.get_links_in_walker()
                wgs84_pos = walker.get_wgs84_pos()
                wgs84 += wgs84_pos
                in_walkers += [[l[0] + existed_sat_id, l[1] + existed_sat_id]
                               for l in links]
                existed_sat_id += N
            return {"wgs84": wgs84, "in_walkers": in_walkers}
        
        except Exception as e:
            return {"code": 0, "msg": str(e)}


class SatelliteGenerateTraffic(MethodView):
    """
    /satellite/traffic/

    由于星座倍速，需进行流量生成包装
    """
    def post(self):
        """
        产生地面站之间的流量

        使用iperf指令生成流量，有关命令如下：
            -c <server>：指定客户端模式，并指定服务器的地址。
            -s：指定服务器模式，在指定的端口上等待客户端连接。
            -t <time>：指定测试运行的时间，单位为秒。
            -w <window>：设置TCP窗口大小。
            -n <bytes>：指定发送的总字节数。
            -b <bandwidth>：设置发送的带宽限制。
            -u：使用UDP协议进行测试。
            -l <length>：设置UDP数据包的长度。
        """
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            user = data['user']
            topo = data['topo']
            devs_server = data['devs_server']
            devs_client = data['devs_client']
            config = data['config']
            config_keys = config.keys()
            
            # 判断是否包含星座
            table_name = f"{topo}{PROJ_CONFIG.sat_table_name}"
            if not check_table_existence(user, table_name):
                return {'code': 0, 'msg': '无星座信息记录'}
            
            with redis_context(user) as user_db_cli:
                # 读数据库
                time_speed = user_db_cli.get_value(table_name, 'timer')[1]  # 时间倍速
                dev_para = user_db_cli.get_value(table_name, 'gnd-dev')[0]  # 地面站参数
                # 数据转换
                # 在卫星世界中的流量持续时长，在真实世界中更短
                last_time = config["last time"] / time_speed if "last time" \
                    in config_keys else 300
                # 在卫星世界中的流量带宽，在真实世界中更大
                band_width = f"-b {config['bw']}M" if "bw" in config_keys else ''
                # 在卫星世界中的想发送的总字节数，与在真实世界中的相同
                byte_num = f"-n {config['byte number']/time_speed}" if "byte number" in config_keys else ''
                
                # 请求列表
                reqs = []

                # 对每个server，启用iperf -s
                for dev0 in devs_server:
                    if dev0 not in dev_para:
                        return {'code': 0, 'msg': '无地面站信息记录'}
                    worker0 = user_db_cli.get_worker_ip_by_ne_name(topo, dev0)
                    reqs.append(grequests.post(
                        f'http://{worker0}:{PROJ_CONFIG.worker_port}/satellite/traffic/',
                        json={
                            "dev_id": user_db_cli.get_value(f'{topo}_{dev0}', 'NEid'),
                            "server_client": "s",
                            "action": "start",
                            "ip": "",
                            "last_t": "",
                            "bw": "",
                            "bytes": ""
                        }
                    ))
                
                # 对每对server-client，在client启用iperf -c
                for dev1 in devs_client:
                    if dev1 not in dev_para:
                        return {'code': 0, 'msg': '无地面站信息记录'}
                    dev_id1 = user_db_cli.get_value(f'{topo}_{dev1}', 'NEid')
                    worker1 = user_db_cli.get_worker_ip_by_ne_name(topo, dev1)
                    
                    for dev0 in devs_server:
                        reqs.append(grequests.post(
                            f'http://{worker1}:{PROJ_CONFIG.worker_port}/satellite/traffic/',
                            json={
                                "dev_id": dev_id1,
                                "server_client": "c",
                                "action": "start",
                                "ip": dev_para[dev0]['ip'],
                                "last_t": last_time,
                                "bw": band_width,
                                "bytes": byte_num
                            }
                        ))
                
                # 并发发送请求
                resp_result = grequests.map(reqs)
                # 检测请求结果
                resp_status = [resp.json()['code'] for resp in resp_result]
                if not all(resp_status):
                    return {'code': 0, 'msg': '流量产生失败'}
                else:
                    return {'code': 1, 'msg': '流量产生成功'}

        except Exception as e:
            return {"code": 0, "msg": str(e)}
        
    def delete(self):
        """
        停止地面站之间的流量
        """
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            user = data['user']
            topo = data['topo']
            dev0, dev1 = data['devs']
            
            # 判断是否包含星座
            table_name = f"{topo}{PROJ_CONFIG.sat_table_name}"
            if not check_table_existence(user, table_name):
                return {'code': 0, 'msg': '无星座信息记录'}
            
            with redis_context(user) as user_db_cli:
                # 地面站参数
                dev_para = user_db_cli.get_value(table_name, 'gnd-dev')[0]
                if dev0 not in dev_para or dev1 not in dev_para:
                    return {'code': 0, 'msg': '无地面站信息记录'}
                
                # server在后台不杀掉iperf -s进程，因为可能和其他client存在着流量
                # client在后台杀掉iperf -c进程
                dev_id1 = user_db_cli.get_value(f'{topo}_{dev1}', 'NEid')
                
                worker1 = user_db_cli.get_worker_ip_by_ne_name(topo, dev1)
                resp = requests.post(
                    f'http://{worker1}:{PROJ_CONFIG.worker_port}/satellite/traffic/',
                    json={
                        "dev_id": user_db_cli.get_value(f'{topo}_{dev1}', 'NEid'),
                        "server_client": "c",
                        "action": "stop",
                        "ip": "",
                        "last_t": "",
                        "bw": "",
                        "bytes": ""
                    }
                )
                return resp.json()
        
        except Exception as e:
            return {"code": 0, "msg": str(e)}


class MonitorRealtime(MethodView):
    """
    /satellite/monitor-realtime/
    
    实时监控、获得端到端路径
    """
    def post(self):
        """
        实时监控端到端的时延、丢包
        """
        try:
            # 信息提取
            data = json.loads(request.get_data(as_text=True))
            user = data['user']
            topo = data['topo']
            dev1, dev2 = data['devs']
            
            # 判断是否包含星座
            if not check_table_existence(user, f"{topo}_{dev1}") or \
               not check_table_existence(user, f"{topo}_{dev2}"):
                return {'code': 0, 'msg': '无地面站信息记录'}
            
            with redis_context(user) as user_db_cli:
                dev1_id = user_db_cli.get_value(f"{topo}_{dev1}", 'NEid')
                dev2_info = user_db_cli.get_all_values(f"{topo}_{dev2}")
                ip2 = [dev2_info[link]['ip'] for link in dev2_info
                       if link.startswith('link_')][0]
                worker1 = user_db_cli.get_worker_ip_by_ne_name(topo, dev1)
            
            # 请求worker，进行ping探针测试
            resp = requests.post(
                f"http://{worker1}:{PROJ_CONFIG.worker_port}/satellite/monitor-realtime/",
                json={
                    "dev_id": dev1_id,
                    "ip": ip2
                }
            )
            return resp.json()

        except Exception as e:
            return {"code": 0, "msg": str(e)}

    def get(self):
        """
        获得端到端路径
        """
        try:
            # 信息提取
            data = request.args
            user = data['user']
            topo = data['topo']
            dev_from = data['from']
            dev_to = data['to']

            if dev_from == dev_to:
                return {'code': 0, 'msg': '两地面站相同'}
            
            # 判断是否包含星座
            table_name = f"{topo}{PROJ_CONFIG.sat_table_name}"
            if not check_table_existence(user, table_name):
                return {'code': 0, 'msg': '无星座信息记录'}

            with redis_context(user) as user_db_cli:
                gnd_devs = user_db_cli.get_value(table_name, 'gnd-dev')[0]
                if dev_from not in gnd_devs or dev_to not in gnd_devs:
                    return {'code': 0, 'msg': '无地面站信息记录'}
                
                sat_identity, mode = user_db_cli.get_value(table_name, 'mode')
                sat_id1 = user_db_cli.get_value(table_name, 'virtual-para')[0]
                sat_gnd_links = user_db_cli.get_value(table_name, 'sat-gnd links')
                
                # 始末卫星
                sats_first_last = []
                for dev in [dev_from, dev_to]:
                    sat_id = sat_gnd_links[dev][0]
                    if sat_id == None:
                        return {'code': 0, 'msg': f'地面站{dev}无可见卫星'}
                    sats_first_last.append(f"{sat_identity[0]}{sat_id1+sat_id}")
                
                # 卫星路径，初始为第一颗星
                tracert = [sats_first_last[0]]
                
                # 若始末卫星相同，则已得路径
                # 若始末卫星不同，则需迭代查表得路径
                if sats_first_last[0] != sats_first_last[1]:

                    # 卫星是交换机
                    if sat_identity == "switch":
                        # to端网卡的ip和mac
                        for key, val in user_db_cli.get_all_values(f"{topo}_{dev_to}").items():
                            if key.startswith("link"):
                                to_ip, to_host_mac = val["ip"], val["mac"]
                                break
                        # 执行ping，否则表中无信息
                        worker_from = user_db_cli.get_worker_ip_by_ne_name(topo, dev_from)
                        resp = requests.post(
                            f'http://{worker_from}:{PROJ_CONFIG.worker_port}/satellite/monitor-realtime/',
                            json={
                                "dev_id": user_db_cli.get_value(f"{topo}_{dev_from}", "NEid"),
                                "ip": to_ip,
                                "pkt_num": 1
                            }
                        )
                        if resp.json()['code'] == 0:
                            return {"code": 0,
                                    "msg": '路径获取失败，ping执行失败'}
      
                    # 卫星是路由器
                    else:
                        # DHCP / IP-TUNNEL 模式下，地面站ip和卫星对地子网有关
                        if mode in ['DHCP', 'IP-TUNNEL']:
                            # 卫星对地子网映射
                            sat_gnd_nets = user_db_cli.get_value(table_name, 'ip-net')[1]
                            # 对地下一可用ip
                            ip = sat_gnd_nets[str(int(sats_first_last[1][1:]) - sat_id1)]
                            # 对地子网网络号
                            to_net = int2ip(ip & sat_gnd_subnet_mask_int)
                        # IP-MODIFY / IP-NO-MODIFY 模式下，地面站ip和用户配置有关
                        else:
                            # 地面站参数
                            devices = user_db_cli.get_value(table_name, 'gnd-dev')[0]
                            # 对地子网网络号
                            to_net = int2ip(ip2int(devices[dev_to]['ip']) & \
                                            ip2int(devices[dev_to]['netmask']))
                        
                    # 第一颗星开始，依次查表
                    while True:
                        print(tracert)
                        # 当前卫星为路径中最后一个
                        cur_sat = tracert[-1]
                        # 当前卫星所在worker
                        worker = user_db_cli.get_worker_ip_by_ne_name(topo, cur_sat)
                        # 请求下一跳
                        resp = requests.get(
                            f'http://{worker}:{PROJ_CONFIG.worker_port}/satellite/monitor-realtime/',
                            json={
                                "sat_identity": sat_identity,
                                "dev_id": user_db_cli.get_value(f"{topo}_{cur_sat}", 'NEid'),
                                "target_para": to_host_mac if sat_identity == "switch" else to_net
                            }
                        ).json()
                        # 响应解析
                        if resp['code'] == 0:
                            return {"code": 0,
                                    "msg": '路径获取失败，获取下一跳失败'}
                        next_sat = resp['next_sat']
                        # 加入卫星路径
                        tracert.append(next_sat)
                        # 若已达到最后一颗星，退出循环
                        if next_sat == sats_first_last[1]: break

                # 转卫星设备为卫星id
                tracert = [int(sat[1:]) - sat_id1 for sat in tracert]

                # 星座参数、星地连接、时间参数
                walkers = user_db_cli.get_value(table_name, 'walkers')
                dev_dict = user_db_cli.get_value(table_name, 'sat-gnd links')
                timer = user_db_cli.get_value(table_name, 'timer')
                time_now = timestamp2date((time() - timer[2]) * timer[1] + timer[0])
                
                # 各卫星位置，通过卫星id索引其xyz坐标
                all_pos = []
                for walker in walkers:
                    N, P, i, F, h, ang = get_walker_para(walker)
                    walker = Walker(time_now, N, P, i, h, F, ang)
                    all_pos += walker.get_wgs84_pos()
                
                # 路径中卫星位置
                pos = [all_pos[i] for i in tracert]
                
                # 计算路径总距离
                dist = [(
                            (pos[i][0]-pos[i+1][0])**2 + \
                            (pos[i][1]-pos[i+1][1])**2 + \
                            (pos[i][2]-pos[i+1][2])**2
                        )**.5 for i in range(len(pos) - 1)]
                
                # 星地距离
                dist = [dev_dict[dev_from][1]] + dist + [dev_dict[dev_to][1]]

                return {"code": 1, 'msg': '路径获取成功',
                        "trace": tracert,
                        "dist": dist,
                        "dist_sum": sum(dist)}
        
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"code": 0, "msg": str(e)}
