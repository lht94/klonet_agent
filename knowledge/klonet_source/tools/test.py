from vemu_api.traffic import TrafficManager, TrafficEvent
import requests
import json
import re
regex = ""

if __name__ == '__main__':
    # traffic = TrafficManager('ma', 'test')
    # x={"src":"h1","dst":"h2","src_ip":"192.168.1.1","dst_ip":"192.168.1.2","rate":"10","pkt_length":{"1000":"1"},"duration":"10","on_k":"2","on_min":"1","off_k":"2","off_min":"1"}
    # traffic.add_flow('pkt_gen1', **x)

    te = TrafficEvent("f20")
    te.add_flow("pkt_gen1", **{"src":"h1", "dst":"h2", "src_ip":"192.168.1.111", "dst_ip":"192.168.1.112",
    "rate":"100", "duration":"111", "pkt_length":"1500"})
    te.add_flow("pkt_gen1", **{"src":"h1", "dst":"h2", "src_ip":"192.168.1.111", "dst_ip":"192.168.1.112",
    "rate":"100", "duration":"111", "pkt_length":"1500"})
    te.add_flow("pkt_gen2", **{"src":"h1", "dst":"h2", "src_ip":"192.168.1.111", "dst_ip":"192.168.1.112",
    "rate":"100"})
    te.add_flow("traffic_gen", **{"server_list":["h2:192.168.1.2:5000"], "client_name":"hhh",
    "req_size_dist":{"100": "0.1","200": "0.4"}, "dscp":{"0": "50"}, "EARB":"1.5", "RT":"10", "SEED":"1"})
    
    te1 = TrafficEvent("f40")
    te1.add_flow("pkt_gen1", **{"src":"h1", "dst":"h2", "src_ip":"192.168.1.1", "dst_ip":"192.168.1.2",
    "rate":"100", "duration":"111", "pkt_length":"1500"})
    tm = TrafficManager("maa", "test","192.168.1.124", "10011")
    tm.add_event(te1)
    tm.save_event()
    tm.deploy_event("f40")

    # url = 'http://192.168.1.124:10011/re/project/test/traffic_app/'
    # p={
    #     "user": "maa",
    #     "traffics": {
    #         "f11": {
    #             "traffic_gen": [],
    #             "pkt_gen1": [{
    #                 "src": "h1",
    #                 "dst": "h2",
    #                 "src_ip": "192.168.1.1",
    #                 "dst_ip": "192.168.1.2",
    #                 "rate": "1",
    #                 "duration": "10",
    #                 "pkt_length": "1000",
    #                 "dist": "normal",
    #                 "normal_scale": "0.1",
    #                 "ip_tos": "0",
    #                 "ip_ttl": "64",
    #                 "ip_id": "1",
    #                 "proto": "tcp",
    #                 "tcp_header": {
    #                     "tcp_window": "1500",
    #                     "sport": "10000",
    #                     "dport": "10000"
    #                 },
    #                 "udp_header": {}
    #             }],
    #             "pkt_gen2": [],
    #             "trace": []
    #         },
    #         "f12": {
    #             "traffic_gen": [],
    #             "pkt_gen1": [{
    #                 "src": "h1",
    #                 "dst": "h2",
    #                 "src_ip": "192.168.1.1",
    #                 "dst_ip": "192.168.1.2",
    #                 "rate": "1",
    #                 "duration": "10",
    #                 "pkt_length": "1000",
    #                 "dist": "normal",
    #                 "normal_scale": "0.1",
    #                 "ip_tos": "0",
    #                 "ip_ttl": "64",
    #                 "ip_id": "1",
    #                 "proto": "tcp",
    #                 "tcp_header": {
    #                     "tcp_window": "1500",
    #                     "sport": "10000",
    #                     "dport": "10000"
    #                 },
    #                 "udp_header": {}
    #             }],
    #             "pkt_gen2": [],
    #             "trace": []
    #         },
    #         "f13": {
    #             "traffic_gen": [],
    #             "pkt_gen1": [{
    #                 "src": "h1",
    #                 "dst": "h2",
    #                 "src_ip": "192.168.1.1",
    #                 "dst_ip": "192.168.1.2",
    #                 "rate": "1",
    #                 "duration": "10",
    #                 "pkt_length": "1000",
    #                 "dist": "normal",
    #                 "normal_scale": "0.1",
    #                 "ip_tos": "0",
    #                 "ip_ttl": "64",
    #                 "ip_id": "1",
    #                 "proto": "tcp",
    #                 "tcp_header": {
    #                     "tcp_window": "1500",
    #                     "sport": "10000",
    #                     "dport": "10000"
    #                 },
    #                 "udp_header": {}
    #             }],
    #             "pkt_gen2": [],
    #             "trace": []
    #         }
    #     }
    # }
    # r=requests.post(url, json.dumps(p))
    # print(r.content)