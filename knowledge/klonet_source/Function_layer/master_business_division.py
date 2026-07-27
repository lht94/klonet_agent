import pprint

from ..Service_layer.redisAPI import UserMapRedis, WorkerRedis


worker_db_cli = WorkerRedis()
user_db_map = UserMapRedis()


# 分别处理server和client,分到不同worker，并考虑存储
# 要不就在这里存储了
# 这里如果写成是一个类的话， 查询数据库的操作就会减少很多了
# 这样子的话， 执行的耗时会更短一点的
# 这里主要写的就是把信息写入数据库
# !!!!!! 待改 !!!!!!!
class BusiDivison:

    def __init__(self, total_traffic):
        self.total_traffic = total_traffic
        self.topo = total_traffic["topo"]
        self.user_db_cli = user_db_map.get_user_db(total_traffic['user'])
   
    def traffic_gen_total_to_sub(self):
        '''
            输入:
                total_traffic:总服务信息
            输出:
                server_in_worker:分割为不同worker的server配置信息
                client_in_worker:分割为不同worker的client配置信息
            功能描述：
                分割流量发生器的总服务信息,将其为server和client部分,并最终存到数据库中
        '''
        # 考虑数据库的存储格式，方便后端创建调用
        server_in_worker = {}
        client_in_worker = {}
        for business in self.total_traffic["traffic_gen"]:
            for server in business["server_list"]:
                server_name = server.split(":")[0]
                worker_ip = self.user_db_cli.get_worker_ip_by_ne_name(self.topo, server_name)
                worker_list = server_in_worker.setdefault(worker_ip, [])
                if server not in worker_list:  # server可能被多个client请求
                    worker_list.append(server)
            # client
            worker_ip = self.user_db_cli.get_worker_ip_by_ne_name(self.topo, business['client']['client_name'])
            worker_list = client_in_worker.setdefault(worker_ip, [])
            client_dict = {}
            client_dict["mode"] = business["mode"]
            # 原始数据已有server端的IP,直接加入key值
            for key in business["client"].keys():
                client_dict[key] = business["client"][key]
            worker_list.append(client_dict)  # client参数不会完全一致
        pprint.pprint(server_in_worker)
        pprint.pprint(client_in_worker)
        return server_in_worker, client_in_worker
    
    def pkt_gen2_total_to_sub(self, pkt_gen_type="pkt_gen2"):
        '''
        输入:
            total_traffic:前端传给后端的全部流量服务信息
        输出：
            src_in_worker:分割为不同worker的包发生器源端配置信息
        功能描述：
            分割pkt_gen2的总服务信息，并根据节点所在worker分割为server和client,并最终存到数据库中
        '''
        # 1）分成toponame_appname_workerip_c和toponame_appname_workerip_s
        src_in_worker = {}
        topo = self.total_traffic['topo']
        user_db_cli = user_db_map.get_user_db(self.total_traffic['user'])
        for business in self.total_traffic[pkt_gen_type]:
            # src节点获取worker_ip
            worker_ip = user_db_cli.get_worker_ip_by_ne_name(topo, business['src'])
            worker_list = src_in_worker.setdefault(worker_ip, [])
            worker_list.append(business)
        pprint.pprint(src_in_worker)
        return src_in_worker
    

def traffic_gen_total_to_sub(total_traffic):
    '''
        输入:
            total_traffic:总服务信息
        输出:
            server_in_worker:分割为不同worker的server配置信息
            client_in_worker:分割为不同worker的client配置信息
        功能描述：
            分割流量发生器的总服务信息,将其为server和client部分,并最终存到数据库中
    '''
    # 考虑数据库的存储格式，方便后端创建调用
    server_in_worker = {}
    client_in_worker = {}
    # 在进行服务端数据拆分的时候就需要先查一遍，
    # 这个时候先存一下，之后写入的时候就不需要再查询了
    topo = total_traffic['topo']
    user_db_cli = user_db_map.get_user_db(total_traffic['user'])

    for business in total_traffic["traffic_gen"]:
        for server in business["server_list"]:
            server_name = server.split(":")[0]
            worker_ip = user_db_cli.get_worker_ip_by_ne_name(topo, server_name)
            worker_list = server_in_worker.setdefault(worker_ip, [])
            if server not in worker_list:  # server可能被多个client请求
                worker_list.append(server)
        # client
        worker_ip = user_db_cli.get_worker_ip_by_ne_name(topo, business['client']['client_name'])
        worker_list = client_in_worker.setdefault(worker_ip, [])
        client_dict = {}
        client_dict["mode"] = business["mode"]
        # 原始数据已有server端的IP,直接加入key值
        for key in business["client"].keys():
            client_dict[key] = business["client"][key]
        worker_list.append(client_dict)  # client参数不会完全一致
    pprint.pprint(server_in_worker)
    pprint.pprint(client_in_worker)
    return server_in_worker, client_in_worker


def pkt_gen_total_to_sub(total_traffic, pkt_gen_type="pkt_gen2"):
    '''
        输入:
            total_traffic:前端传给后端的全部流量服务信息
        输出：
            src_in_worker:分割为不同worker的包发生器源端配置信息
        功能描述：
            分割pkt_gen2的总服务信息，并根据节点所在worker分割为server和client,并最终存到数据库中
    '''
    # 1）分成toponame_appname_workerip_c和toponame_appname_workerip_s
    src_in_worker = {}
    topo = total_traffic['topo']
    user_db_cli = user_db_map.get_user_db(total_traffic['user'])
    for business in total_traffic[pkt_gen_type]:
        # src节点获取worker_ip
        worker_ip = user_db_cli.get_worker_ip_by_ne_name(topo, business['src'])
        worker_list = src_in_worker.setdefault(worker_ip, [])
        worker_list.append(business)
    pprint.pprint(src_in_worker)
    return src_in_worker
    # 2）TODO:存储worker字典,即存储对应的toponame_appname_workerip_c,考虑存储到数据库的格式
    # save_to_redis(user, topo, app_seq, src_in_worker)


if __name__ == "__main__":
    # 测试服务切分
    traffic_gen_total_traffic = {
        "user": "sw",
        "topo": "topo1",
        "app_seq": "test1",
        "traffic_gen": [
            {
                "mode": "0",
                "server_list": [
                    "h2:192.168.1.2:5001", 
                    "h3:192.168.1.3:5001", 
                    "h4:192.168.1.4:5001"
                ],
                "client": {
                    "client_name": "h1",
                    "client_config": {
                        "server_list": [
                            "h2:192.168.1.2:5001", 
                            "h3:192.168.1.3:5001", 
                            "h4:192.168.1.4:5001"
                        ],
                        "req_size_dist": {
                            "100": "0.1",  # 大小分布CDF
                            "200": "0.4",
                            "1000": "0.7",
                            "10000": "1"
                        },
                        "dscp": {
                            "0": "25",
                            "1": "25",
                            "2": "50"
                        },
                        "rate": {
                            "1Mbps": "50",
                            "2Mbps": "50"
                        },
                        "fanout": {
                            "1": "10",  # 这个根据client的mode读取
                            "2": "50",
                            "3": "40"
                        },
                    },
                    "cli_param": {
                        "-b": "1",  # 以Mbps为单位 
                        "-t": "",
                        "-n": "100",
                        "-s": "12",
                    }
                }
            },
            {
                "mode": "1",
                "server_list": [
                    "h1:192.168.1.1:5001", 
                    "h2:192.168.1.2:5001"
                ],
                "client": {
                    "client_name": "h4",
                    "client_config": {
                        "server_list": [
                            "h1:192.168.1.1:5001", 
                            "h2:192.168.1.2:5001"
                        ],
                        "req_size_dist": {
                            "100": "0.1",  # 大小分布CDF
                            "500": "0.4",
                            "2000": "0.7",
                            "10000": "1"
                        },
                        "dscp": {
                            "0": "25",
                            "1": "25",
                            "2": "50"
                        },
                        "rate": {
                            "1Mbps": "50",
                            "2Mbps": "50"
                        },
                        "fanout": {
                            "1": "10",  # 这个根据client的mode读取
                            "2": "50",
                            "3": "40"
                        },
                    },
                    "cli_param": {
                        "-b": "1",  # 以Mbps为单位 
                        "-t": "",
                        "-n": "200",
                        "-s": "20",
                    }
                }
            }
        ]
    }

    traffic_gen_total_to_sub(traffic_gen_total_traffic)

    pkt_gen2_total_traffic = {
        "user": "sw",
        "topo": "topo1",
        "app_seq": "test1",
        "pkt_gen2": [
            {
                "src": "h1",
                "dst": "h2",
                "src_ip": "192.168.1.1",
                "dst_ip": "192.168.1.2",
                "rate": "10",
                "pkt_length": {
                    "40": "0.7",
                    "200": "0.9",
                    "500": "1"
                }, 
                "duration": "40",
                "on_k": "2",
                "on_min": "1",
                "off_k": "2",
                "off_min": "2"
            },
            {
                "src": "h2",
                "dst": "h4",
                "src_ip": "192.168.1.2",
                "dst_ip": "192.168.1.4",
                "rate": "20",
                "pkt_length": {
                    "50": "0.7",
                    "300": "0.9",
                    "500": "1"
                }, 
                "duration": "60",
                "on_k": "2",
                "on_min": "1",
                "off_k": "2",
                "off_min": "2"
            },
            {
                "src": "h3",
                "dst": "h1",
                "src_ip": "192.168.1.3",
                "dst_ip": "192.168.1.1",
                "rate": "20",
                "pkt_length": {
                    "40": "0.7",
                    "200": "0.9",
                    "500": "1"
                },
                "duration": "30",
                "on_k": "2",
                "on_min": "1",
                "off_k": "2",
                "off_min": "2"
            },
        ]
    }

    traffic_gen_total_to_sub(total_traffic)

    # pkt_gen2_total_traffic = {
    #     "user": "sw",
    #     "topo_name": "topo1",
    #     "app_seq": "test1",
    #     "pkt_gen2": [
    #         {
    #             "src": "swh1",
    #             "dst": "swh2",
    #             "rate": "30",
    #             "pkt_length": {
    #                 "40": "0.7",
    #                 "200": "0.9",
    #                 "500": "1"
    #             }, 
    #             "duration": "60",
    #             "on_k": "2",
    #             "on_min": "1",
    #             "off_k": "2",
    #             "off_min": "2"
    #         },
    #         {
    #             "src": "swh3",
    #             "dst": "swh4",
    #             "rate": "20",
    #             "pkt_length": {
    #                 "40": "0.7",
    #                 "200": "0.9",
    #                 "500": "1"
    #             }, 
    #             "duration": "40",
    #             "on_k": "2",
    #             "on_min": "1",
    #             "off_k": "2",
    #             "off_min": "2"
    #         },
    #         {
    #             "src": "swh2",
    #             "dst": "swh4",
    #             "rate": "30",
    #             "pkt_length": {
    #                 "40": "0.7",
    #                 "200": "0.9",
    #                 "500": "1"
    #             },
    #             "duration": "30",
    #             "on_k": "2",
    #             "on_min": "1",
    #             "off_k": "2",
    #             "off_min": "2"
    #         },
    #     ]
    # }

    pkt_gen2_total_to_sub(total_traffic)

