from ast import Bytes
import docker
import eventlet
from ..tools.upper_level_redis_API import get_container_ids, get_domain_type, node_list_divide
from .vm_cmd_execer import vm_cmd_execer
# from .vm_cmd_execer_ssh import vm_cmd_execer

eventlet.monkey_patch()

def exec_cmd_in_node(user, topo, node_list, node_and_cmd, timeout_s, block):
    '''
    在指定容器列表node_list中执行该容器对应的多条命令

    Args:
        user: 用户名
        topo: 拓扑名（项目名）
        node_list: 容器列表
        node_and_cmd: 用户指定容器需要执行的多条命令
        timeout_s: 超时时间。若超时则改为detach模式执行
        block: 是否阻执行命令

    Returns:
        exec_results: {
            "容器名": {
                        "cmd1":{
                            "exit_code": "容器命令执行返回码",
                            "output": "容器命令执行输出"
                        }
                }
        }
    '''
    # 可以考虑之后用多线程经行处理
    docker_list, vm_list = node_list_divide(user, topo, node_list)
    timeout_s = float(timeout_s)
    
    if timeout_s <= 0 or timeout_s > 300:
        timeout_s = 1
    exec_results = {}
    if vm_list != []:
        vm_ids = get_container_ids(user, topo, vm_list)
        vm_NEtype_list = get_domain_type(user, topo, vm_list)
        for i in range(len(vm_list)):
            exec_results[vm_list[i]] = {}

            cmd_list = node_and_cmd[vm_list[i]]

            commands = cmd_list
            time_gap_list = [0]*len(commands)
            timeouts_list = [100]*len(commands) if block == 'true' else [timeout_s]*len(commands)
            mode = 1
            commands_execer = vm_cmd_execer(vm_id=vm_ids[i],
                                            # vm_id='lzl_ar1000_store',
                                            vm_NEtype=vm_NEtype_list[i],
                                            # vm_NEtype='router',
                                            cmd_list=commands,
                                            time_gap_list=time_gap_list,
                                            timeout_s_list=timeouts_list,
                                            mode_list=mode)
            commands_execer.exe_command()
            # commands_execer.close()
            for j in range(len(commands_execer.code_list)):
                exec_results[vm_list[i]][f"{j}_"+cmd_list[j]] = {}
                exec_results[vm_list[i]][f"{j}_"+cmd_list[j]]["exit_code"] = commands_execer.code_list[j]
                exec_results[vm_list[i]][f"{j}_"+cmd_list[j]]["output"] = commands_execer.result_list[j]

    if docker_list != []:
        c_ids = get_container_ids(user, topo, docker_list)
        my_client = docker.from_env()
        for i, c_id in enumerate(c_ids):
            try:
                c = my_client.containers.get(c_id)
                exec_results[docker_list[i]] = {}
                try:
                    if block == "true":
                        # 这里为什么不写成cmd_list = node_and_cmd[docker_list[i]]
                        for node, cmd_list in node_and_cmd.items():
                            # 如果cmd相同的话，则无法记录
                            if docker_list[i] == node:
                                index = -1
                                for cmd in cmd_list:
                                    index += 1
                                    exec_results[docker_list[i]][f'{index}_'+cmd] = {}
                                    result = c.exec_run(cmd)
                                    exec_results[docker_list[i]][f'{index}_'+cmd]["exit_code"] = result.exit_code
                                    if isinstance(result.output, bytes):
                                        exec_results[docker_list[i]][f'{index}_'+cmd]["output"] = result.output.decode("utf-8")
                                    else:
                                        exec_results[docker_list[i]][f'{index}_'+cmd]["output"] = result.output

                    else:
                        with eventlet.Timeout(timeout_s):
                            # 这里为什么不写成cmd_list = node_and_cmd[docker_list[i]]
                            for node, cmd_list in node_and_cmd.items():
                                # 如果cmd相同的话，则无法记录
                                if docker_list[i] == node:
                                    index = -1
                                    for cmd in cmd_list:
                                        index += 1
                                        exec_results[docker_list[i]][f'{index}_'+cmd] = {}
                                        result = c.exec_run(cmd)
                                        exec_results[docker_list[i]][f'{index}_'+cmd]["exit_code"] = result.exit_code
                                        if isinstance(result.output, bytes):
                                            exec_results[docker_list[i]][f'{index}_'+cmd]["output"] = result.output.decode("utf-8")
                                        else:
                                            exec_results[docker_list[i]][f'{index}_'+cmd]["output"] = result.output

                except eventlet.timeout.Timeout:
                    raise TimeoutError("This cmd has been executed in the background and "
                        f"its exit_code and output were not available within TIMEOUT={timeout_s}s.")


            except Exception as e:
                for cmd, _ in exec_results[docker_list[i]].items():
                    exec_results[docker_list[i]][cmd]["exit_code"] = None
                    exec_results[docker_list[i]][cmd]["output"] = f"{repr(e)}"

    return exec_results
