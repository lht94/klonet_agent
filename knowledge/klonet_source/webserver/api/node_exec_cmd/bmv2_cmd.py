from flask.views import MethodView
from flask import request
import requests
from ....tools.context import redis_context, judge_user_exist, check_table_key
from ....vemu_config.config import PROJ_CONFIG
import json
import traceback

class Bmv2CmdAPI(MethodView):
    '''
    /master/bmv2/
    POST 指定bmv2容器执行动作
    
    '''
    def post(self):
        '''
        向user->topo下的某一个bmv2容器发送命令
        容器范围：仅限于bmv2容器，有检查机制
        
        输入：
            {
                'user': "用户名",
                'topo': "项目名（拓扑名）",
                'node_name', "节点名称"
                'option': "执行的动作类型",
                'p4_file_path': p4文件的绝对路径
                'compiled_path': 编译后的json和p4i的绝对输出路径
            }
        
        返回：
            执行情况，如：
            {
                'code': 1,
                'exec_results': {
                    'h1': {
                        '命令': {
                            'exit_code': 0,
                            'output': '执行结果'
                            }
                        }
                    },
                'msg': 'success'
            }
        '''
        data = json.loads(request.get_data(as_text=True))
        user = data['user']
        topo = data['topo']
        node_name = data['node_name']
        option = data['option']
        topo_list_table = "topo_list"
        # 基本检查
        if not judge_user_exist(user):
            msg = {'code': 0, 'msg': f'用户{user}不存在'}
            return msg
        if not check_table_key(user, topo_list_table, topo):
            msg = {'code': 0, 'msg': f'拓扑{topo}不存在'}
            return msg
        # 检查节点是否为bmv2
        with redis_context(user)as user_db_cli:
            host_list = user_db_cli.get_value(topo_list_table, topo)['networks']['hosts']
            bmv2_list = []
            for key, value in host_list.items():
                if value['image_name'] == 'host/bmv2':     # 仅支持平台默认的bmv2镜像
                    bmv2_list.append(key)
            if not node_name in bmv2_list:
                return {'code': 0, 'msg': f'bmv2节点{node_name}不存在，bmv2的节点有{bmv2_list}'}
        # 执行配置命令
        # 调用容器执行命令接口，拼接payload
        try:
            # 基于p4c编译p4文件
            if option == 'compile':
                cmd = (f"p4c -b bmv2 -o {data['compiled_path']} {data['p4_file_path']}") # 编译成功返回通常是quiet的
                pay_load = {
                    'user': user,
                    'topo': topo,
                    'node_and_cmd':{
                        node_name: [cmd],  
                    },
                    'cmd_timeout_s': "1",
                    'block': 'true'    # 阻塞执行
                }
                req_url = (f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/node_exec_cmd/")
                resp = requests.post(req_url, json=pay_load)
                print(resp.json())
                return resp.json()
            # bmv2服务相关
            elif option == 'service':    
                if data['state'] == 'start':    # 后台启动服务
                    interfaces = ""
                    for mapping in data['port_mappings']:
                        interfaces += " --interface " + mapping
                    cmd = (f"{data['target']}{interfaces} {data['json_path']} &")
                    pay_load = {
                        'user': user,
                        'topo': topo,
                        'node_and_cmd':{
                            node_name: [cmd],  
                        },
                        'cmd_timeout_s': "2",
                        'block': 'false'    # 非阻塞，后台运行
                    }
                    req_url = (f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/node_exec_cmd/")
                    resp = requests.post(req_url, json=pay_load)
                    print(resp.json())
                    # bmv2进程状态检查
                    pay_load = {
                        'user': user,
                        'topo': topo,
                        'node_and_cmd':{
                            node_name: [f"bash -c \"ps aux | pgrep -f '{cmd}'\""],
                        },
                        'cmd_timeout_s': "1",
                        'block': 'true'
                    }
                    req_url = (f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/node_exec_cmd/")
                    resp = requests.post(req_url, json=pay_load)
                    print(self._output_trans(resp.json()["exec_results"]).split("\n"))  # 返回格式['bmv2服务进程号', 'grep进程号2', '']
                    flag = len(self._output_trans(resp.json()["exec_results"]).split("\n"))  # 判断列表长度
                    if flag == 3:
                        # 命令记录在数据库方便后续停止服务时匹配
                        config = user_db_cli.get_value(f'{topo}_{node_name}', 'NEconfig')
                        config['config']['bmv2_exec'] = cmd
                        user_db_cli.set_value(f'{topo}_{node_name}', 'NEconfig', config)
                        return {"code": 1, "msg": f"{node_name}节点bmv2服务启动成功！"}
                    elif flag == 2:
                        return {"code": 0, "msg": f"{node_name}节点bmv2服务启动失败，请检查参数后重试！"}
                    else:
                        return {"code": 0, "msg": f"{node_name}节点bmv2服务启动时发生未知错误"}
                elif data['state'] == 'stop':   # 停止服务
                    # 1.获取pid
                    try:
                        cmd = user_db_cli.get_value(f'{topo}_{node_name}', 'NEconfig')['config']['bmv2_exec']   # 数据库中获取
                    except:
                        return {'code':1 , 'msg': f'{node_name}节点上平台启动的bmv2服务已经停止'}
                    pay_load = {
                        'user': user,
                        'topo': topo,
                        'node_and_cmd':{
                            node_name: [f"bash -c \"ps aux | pgrep -f '{cmd}'\""],
                        },
                        'cmd_timeout_s': "1",
                        'block': 'true'
                    }
                    req_url = (f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/node_exec_cmd/")
                    resp = requests.post(req_url, json=pay_load)
                    pid = self._output_trans(resp.json()["exec_results"]).split("\n")[0]
                    # 2.kill进程
                    pay_load = {
                        'user': user,
                        'topo': topo,
                        'node_and_cmd':{
                            node_name: [f"kill -9 {pid}"],
                        },
                        'cmd_timeout_s': "1",
                        'block': 'true'
                    }
                    req_url = (f"http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/node_exec_cmd/")
                    resp = requests.post(req_url, json=pay_load)
                    print(resp.json()["exec_results"])
                    if self._output_trans(resp.json()["exec_results"]) == '':
                        user_db_cli.set_value(f'{topo}_{node_name}', 'NEconfig', {"config":{}})
                        return {'code': 1, 'msg': f'{node_name}节点bmv2服务停止成功'}
                    else:
                        return {"code": 0, "msg": f"{node_name}节点bmv2服务停止失败，请重试或进入终端kill进程！"}
                    
        except Exception:
            traceback.print_exc()
            return {'code': 0, 'msg': '后台执行命令发生错误'}
            
    def _output_trans(self, exec_results):
        '''内部函数用于格式化输出命令执行结果
        
        Args:
            "exec_results": 平台执行命令返回的嵌套字典
        '''
        output = list((list(exec_results.values())[0]).values())[0]['output']
        return output