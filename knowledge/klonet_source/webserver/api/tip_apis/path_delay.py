import csv
import json
import os
import random

from flask.views import MethodView
from flask import request


'''
API说明
TIP 实验相关API
显示逐条时延
'''


flows = []
ne_id_map = {i: f's{i}' if i <= 10 else f'h{i}' for i in range(1, 21)}
delay_file = 'delay_report_weights.json'
flows_file = 'allflows.csv'


class Flow(object):
    """
    流量对象
    """
    flow_info_key = ["flow_id", "ne_path", "SLA_q999", "measured_max",
                     "measured_q999", "measured_q9999"]

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        path = json.loads(kwargs['path'])
        self.__dict__['path'] = path
        self.ne_path = [ne_id_map[ne] for ne in path]
        self.flow_info = {}

    def get_flow_info(self):
        """
        从allflows文件中得到流信息，并进行初始化
        """
        if not self.flow_info:
            for key in self.flow_info_key:
                self.flow_info[key] = self.__dict__[key]
        return self.flow_info

    def get_random_delay(self):
        """
        获得随机时延
        """
        num = len(self.ne_path) - 1
        while num > 0:
            yield random.randint(0, 1000)
            num -= 1


root_dir = os.getcwd()
relative_path = 'vemu_uestc/webserver/api/tip_apis'
with open(f'{root_dir}/{relative_path}/{flows_file}', "r") as all_flows:
    reader = csv.DictReader(all_flows)
    for row in reader:
        flows.append(Flow(**row))

delay_json = open(f"{root_dir}/{relative_path}/{delay_file}", 'r')
delay_info = json.load(delay_json)
delay_json.close()


class TipFlowsApi(MethodView):
    """
    用get 参数来指定page, 每个page需要返回固定个数的流的信息
    """

    def get(self):
        try:
            # 没有的话，是有默认值的
            page = int(request.args.get('page', 0))
            items = int(request.args.get('items', 10))
            assert page >= 0
            assert items > 0
        except (ValueError, AssertionError):
            return {'code': 0, 'msg': "GET请求参数错误"}
        resp_flows = []
        index = page * items
        tail_index = index + items -1 if index + items - 1 <= 100 else 100
        msg = f'查询范围为{index}-{tail_index}'
        if index >= 100:
            msg = '参数范围溢出'
        for flow in flows[index: tail_index]:
            resp_flows.append(flow.get_flow_info())
        return {"flows": resp_flows, 'code': 1, 'msg': msg}


class TipPathDelayApi(MethodView):
    """
    该API返回每一条流中的逐跳时延, 用于在拓扑中显示
    http://<ip>:<port>/tip/flow/<flow_id>/hop_delay/
    给了flow id  需要返回
    ne_path  [h17, s7, s5, s3, s4, s6, s10, h20]
    {
        "flow_id": 0
        "ne_path": ["h17", "s7", "s5", "s3", "s4", "s6", "s10", "h20"],
        "delay": [33, 34, 22, 44, 55, 66, 77]
    }
    """
    def get(self, flow_id):
        """
        Args:
            flow_id (str): 流编号

        Returns:
            info (dict): 该条流的节点信息和时延信息
        """
        flow = flows[flow_id]
        info = dict(flow_id=flow_id, nepath=flow.ne_path, code=1, msg="success")
        try:
            hop_delays = delay_info[flow_id]['hopbyhop_delay']
            measured_means = [hop['measured_mean_ms'] for hop in hop_delays]
            info['measured_means'] = measured_means
            return info
        except:
            info['code'], info['msg'] = '0', '参数错误，flow_id应该>=0, <=99'
            return info


class PathDelayResultApi(MethodView):
    """
    http://<ip>:<port>/tip/end_delay/?page=0&items=12
    该函数返回的是端到端的具体的时延 多条流一并返回的
        "0": {
        "end2end_delay": {
            "measured_mean_ms": 281.5483440949936,
            "measured_max_ms": 562.277,
            "measured_q99_ms": 535.558,
            "measured_q999_ms": 554.661,
            "measured_q9999_ms": 561.24
        }
    """

    def get(self):
        """
        Returns:
            (dict): 流的测量信息
        """
        try:
            # 没有的话，设置默认值
            page = int(request.args.get('page', 0))
            items = int(request.args.get('items', 8))
            assert page >= 0
            assert items > 0
        except (ValueError, AssertionError):
            return {'code': 0, 'msg': "GET请求参数错误"}
        measured_resp = []
        index = page * items
        tail_index = index + items - 1 if index + items - 1 <= 100 else 100
        msg = f'查询范围为{index}-{tail_index}'
        if index >= 100:
            msg = '参数范围溢出'

        for i in range(index, tail_index):
            flow, measured_delay = flows[i], delay_info[i]['end2end_delay']
            temp = {
                'flow_id': i, 'sla_q999': getattr(flow, 'SLA_q999'),
                'measured_max': measured_delay['measured_max_ms'],
                'measured_mean': measured_delay['measured_mean_ms'],
            }
            measured_resp.append(temp)
        return {"flows": measured_resp, 'code': 1, 'msg': msg}
