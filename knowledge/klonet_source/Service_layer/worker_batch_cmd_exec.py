from ast import Bytes
import docker
import eventlet
from ..tools.upper_level_redis_API import get_container_ids, get_domain_type, node_list_divide
from .vm_cmd_execer import vm_cmd_execer
#from .vm_cmd_execer_ssh import vm_cmd_execer

eventlet.monkey_patch()


def batch_exec_cmd_in_ctns(user, topo, ctn_list, cmd, timeout_s, block):
    '''
    在指定容器列表ctn_list中批量执行命令cmd

    Args:
        user: 用户名
        topo: 拓扑名（项目名）
        ctn_list: 容器列表
        cmd: 命令
        timeout_s: 超时时间。若超时则改为detach模式执行
        block: 是否阻塞执行命令
    Returns:
        exec_results: {
            "容器名": "执行后的输出"
        }
    '''
    # 或许可以改成多线程执行，后面再说
    docker_list, vm_list = node_list_divide(user, topo, ctn_list)
    timeout_s = float(timeout_s)
    if timeout_s < 0 or timeout_s > 300:
        timeout_s = 1
    exec_results = {}
    if vm_list != []:
        vm_NEtype_list = get_domain_type(user, topo, vm_list)
        vm_ids = get_container_ids(user, topo, vm_list)
        for i in range(len(vm_list)):
            exec_results[vm_list[i]] = {}

            commands = [cmd]
            time_gap_list = [0]*len(commands)
            timeouts_list = [100000000000]*len(commands) if block == 'true' else [timeout_s]*len(commands)
            mode = 1
            commands_execer =vm_cmd_execer( vm_id=vm_ids[i],
                                            #vm_id ='lzl_ar1000_store',
                                            vm_NEtype=vm_NEtype_list[i],
                                            #vm_NEtype='router', 
                                            cmd_list=commands, 
                                            time_gap_list=time_gap_list,
                                            timeout_s_list=timeouts_list,
                                            mode_list=mode)
            commands_execer.exe_command()
            # commands_execer.close()
            exec_results[vm_list[i]]["exit_code"] = commands_execer.code_list[0]
            exec_results[vm_list[i]]["output"] = commands_execer.result_list[0]

    if docker_list != []:
        c_ids = get_container_ids(user, topo, docker_list)
        my_client = docker.from_env()

        for i, c_id in enumerate(c_ids):
            try:
                c = my_client.containers.get(c_id)
                exec_results[docker_list[i]] = {}
                try:
                    if block == 'true':
                        result = c.exec_run(cmd)
                    else:
                        with eventlet.Timeout(timeout_s):
                            result = c.exec_run(cmd)
                except eventlet.timeout.Timeout:
                    raise TimeoutError("This cmd has been executed in the background and "
                        f"its exit_code and output were not available within TIMEOUT={timeout_s}s.")

                exec_results[docker_list[i]]["exit_code"] = result.exit_code
                if isinstance(result.output, bytes):
                    exec_results[docker_list[i]]["output"] = result.output.decode(
                        "utf-8")
                else:
                    exec_results[docker_list[i]]["output"] = result.output

            except Exception as e:
                exec_results[docker_list[i]]["exit_code"] = None
                exec_results[docker_list[i]]["output"] = f"{repr(e)}"

    # exec_results = {"":""}
    return exec_results
