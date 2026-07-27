from flask.views import MethodView
from flask import request
import requests
import traceback
import re
import json
from ....tools.context import redis_context, judge_user_exist, check_table_key
from ....vemu_config.config import PROJ_CONFIG


class NodeGpuMonitorAPI(MethodView):
    '''
    /master/node_gpu_monitor/
    GET 指定节点的GPU用量显示，但由于其实所有节点的查询都应该是一样的数值
    
    GPT给出了一些更科学的监控工具NVIDIA DCGM + Prometheus + Grafana
    还有NVIDIA GPU Operator（适用于Kubernetes）
    '''
    def get(self):
        '''
        向user->topo下的某一个节点获取nvidia-smi的信息
        目前并非实时监控，仅做到处理节点查询信息的意思
        
        输入：
            'user': "用户名",
            'topo': "项目名（拓扑名）",
            'node_name', "节点名称"
        
        返回：
            该节点GPU资源用量信息
        '''
        user = request.args.get("user")
        topo = request.args.get("topo")
        node_name = request.args.get("node_name")
        topo_list_table = "topo_list"
        # 基本检查
        if not judge_user_exist(user):
            msg = {'code': 0, 'msg': f'用户{user}不存在'}
            return msg
        if not check_table_key(user, topo_list_table, topo):
            msg = {'code': 0, 'msg': f'拓扑{topo}不存在'}
            return msg
        try:
            pay_load = {
                'user': user,
                'topo': topo,
                'node_and_cmd':{
                    node_name: ['nvidia-smi']
                },
                'cmd_timeout_s': "1",
                'block': 'true'    # 阻塞执行
            }
            req_url = (f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/node_exec_cmd/")
            resp = requests.post(req_url, json=pay_load)
            format_res = list((list(resp.json()['exec_results'].values())[0]).values())[0]['output']
            print(format_res)
            util_output = _parse_nvidia_smi_output(format_res)
            print(util_output)
            if util_output:
                return {'code': 1, 'msg': '获取GPU用量信息成功', 'util_output': util_output}    # 返回会自动把list(dict)格式转换为json
            else:
                return {'code': 0, 'msg': '获取GPU用量信息失败，请确认该节点镜像是否支持开启GPU'}
        except Exception:
            traceback.print_exc()
            return {'code': 0, 'msg': '获取GPU用量信息失败，请确认是否有该节点'}
    
    
def _parse_nvidia_smi_output(output):
    '''解析 nvidia-smi 输出，提取 GPU 利用率和显存使用等信息
    输入：
        output(str):nvidia-smi的输出信息
            Sat Dec 14 13:48:17 2024       
            +-----------------------------------------------------------------------------------------+
            | NVIDIA-SMI 550.78                 Driver Version: 550.78         CUDA Version: 12.4     |
            |-----------------------------------------+------------------------+----------------------+
            | GPU  Name                 Persistence-M | Bus-Id          Disp.A | Volatile Uncorr. ECC |
            | Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |
            |                                         |                        |               MIG M. |
            |=========================================+========================+======================|
            |   0  NVIDIA GeForce RTX 2080 Ti     Off |   00000000:03:00.0 Off |                  N/A |
            | 35%   26C    P8              2W /  250W |      22MiB /  11264MiB |      0%      Default |
            |                                         |                        |                  N/A |
            +-----------------------------------------+------------------------+----------------------+
            |   1  NVIDIA GeForce RTX 2080 Ti     Off |   00000000:04:00.0 Off |                  N/A |
            | 35%   27C    P8             14W /  250W |      11MiB /  11264MiB |      0%      Default |
            |                                         |                        |                  N/A |
            +-----------------------------------------+------------------------+----------------------+
            |   2  NVIDIA GeForce RTX 2080 Ti     Off |   00000000:81:00.0 Off |                  N/A |
            | 40%   24C    P8              7W /  260W |      11MiB /  11264MiB |      0%      Default |
            |                                         |                        |                  N/A |
            +-----------------------------------------+------------------------+----------------------+
            |   3  NVIDIA GeForce RTX 2080 Ti     Off |   00000000:82:00.0 Off |                  N/A |
            | 35%   26C    P8             21W /  250W |      11MiB /  11264MiB |      0%      Default |
            |                                         |                        |                  N/A |
            +-----------------------------------------+------------------------+----------------------+
                                                                                                    
            +-----------------------------------------------------------------------------------------+
            | Processes:                                                                              |
            |  GPU   GI   CI        PID   Type   Process name                              GPU Memory |
            |        ID   ID                                                               Usage      |
            |=========================================================================================|
            |    0   N/A  N/A      2108      G   /usr/lib/xorg/Xorg                              9MiB |
            |    0   N/A  N/A      2629      G   /usr/bin/gnome-shell                            6MiB |
            |    1   N/A  N/A      2108      G   /usr/lib/xorg/Xorg                              5MiB |
            |    2   N/A  N/A      2108      G   /usr/lib/xorg/Xorg                              5MiB |
            |    3   N/A  N/A      2108      G   /usr/lib/xorg/Xorg                              5MiB |
            +-----------------------------------------------------------------------------------------+
    
    返回提取的到各GPU信息，示例：
        [
            {'GPU_ID': '0', 'Fan': '35%', 'Memory_Usage': '22MiB /  11264MiB', 'GPU_Util': '0%'}, 
            {'GPU_ID': '1', 'Fan': '35%', 'Memory_Usage': '11MiB /  11264MiB', 'GPU_Util': '0%'}, 
            {'GPU_ID': '2', 'Fan': '40%', 'Memory_Usage': '11MiB /  11264MiB', 'GPU_Util': '0%'}, 
            {'GPU_ID': '3', 'Fan': '35%', 'Memory_Usage': '11MiB /  11264MiB', 'GPU_Util': '0%'}
        ]
    '''
    pattern = re.compile(
    r'\|\s+'                                   # 行首的 |
    r'(?P<GPU_ID>\d+)\s+'                      # GPU ID，例如 0, 1
    r'.*?\s+\|\s+'                             # GPU 名称等信息（非贪婪匹配）
    r'.*?\s+\|\s+'                             # Bus-Id 等信息（非贪婪匹配）
    r'.*\n'                                    # 忽略第一行详细信息
    r'\|\s+'                                   # 行首的 |
    r'(?P<Fan>\d+%)\s+'                        # Fan: 数字加百分号
    r'\d+C\s+'                                 # Temp: 数字加C（例如 29C）
    r'\w+\s+'                                  # Perf: 单词字符（例如 P8）
    r'\d+W\s+/\s+\d+W\s+\|\s+'                 # Pwr:Usage/Cap（例如 16W / 350W）
    r'(?P<Memory_Usage>\d+MiB\s+/\s+\d+MiB)\s+\|\s+'  # Memory-Usage: 数字MiB / 数字MiB
    r'(?P<GPU_Util>\d+%)'                      # GPU-Util: 数字加百分号
    , re.MULTILINE
    )

    # 查找所有匹配
    matches = pattern.finditer(output)

    extracted_data = []
    for match in matches:
        gpu_info = {
            "GPU_ID": match.group("GPU_ID"),
            "Fan": match.group("Fan"),
            "Memory_Usage": match.group("Memory_Usage"),
            "GPU_Util": match.group("GPU_Util")
        }
        extracted_data.append(gpu_info)

    # 输出结果
    # return json.dumps(extracted_data, indent=2, ensure_ascii=False)
    return extracted_data