# !/usr/bin/python
# coding:utf-8
import subprocess


def run_shell(cmd):
    process = subprocess.run(
        cmd, 
        shell=True,  # 执行shell命令
        # capture_output=True,  # 效果与设置stdout=PIPE, stderr=PIPE一样
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # text=True,  # 将stdin, stdout, stderr修改为string模式
        check=True,  # 开启检查，若出错则raise CalledProcessError
        )
    return process.stdout


def create_container(name, image, stdin="True", tty="True", net="none", command="", **param) -> dict:
    '''
        输入:
            name:容器名
            image:镜像名
            command:容器执行的命令
            param:其他输入参数
        输出：
            容器创建命令执行后的结果字典
        功能描述：
            使用subprocess.run()执行容器创建命令
    '''
    # TODO:--publish-all=true似乎没用,--user=root是否需要？
    base_cmd = "sudo docker run -d --privileged --oom-kill-disable=true "
    if stdin == "True":
        base_cmd += " -i "
    if tty == "True":
        base_cmd += " -t "
    base_cmd += " --net={net_type} ".format(net_type=net)
    # TODO:添加param中的可变参数到base_cmd中
    if param["volume"] != "":
        for host_dir, container_dir in param["volume"].items():
            base_cmd += " -v " + "{host_dir}:{container_dir}".format(host_dir=host_dir, container_dir=container_dir)
    if param["env"] != "":
        for key, value in param["env"].items():
            base_cmd += " --env " + "{key}={value}".format(key=key, value=value)
    if param["port"] != "":
        for host_port, container_port in param["port"].items():
            base_cmd += " --publish=" + "{host_port}:{container_port}".format(host_port=host_port, container_port=container_port)
    if param["memory"] != "":
        base_cmd += " -m " + param["memeory"]
    
    # CPU
    if param["cpuset_cpus"] != "":
        base_cmd += " --cpuset-cpus " + param["cpuset_cpus"]
    if param["cpu_shares"] != "":
        base_cmd += " --cpu-shares " + param["cpu_shares"]
    if param["cpu_period"] != "":
        base_cmd += " --cpu-period " + param["cpu_period"]
    if param["cpu_quota"] != "":
        base_cmd += " --cpu-quota " + param["cpu_quota"]
    # 其他
    if param["workdir"] != "":
        base_cmd += " --workdir " + param["workdir"]
    # TODO: extend配置，主要针对监控容器
    # 拓展配置（主要针对监控容器的需求）
    if param["extend"] != "":
        for key, value in param["extend"].items():
            command += " {key}={value}".format(key=key, value=value)
    cmd = base_cmd + " --name={name} {image} {command}".format(name=name, image=image, command=command)

    # result = {
    #     "result": "",
    #     "error_msg": "",
    #     "success_msg": ""
    # }
    result = {}
    try:
        print(cmd)
        out = run_shell(cmd)
        # print(cmd)
    except subprocess.CalledProcessError as e:
        # print(process)
        # close_fds(process)
        result['result'] = name + " create unsuccessfully!"
        result['error_msg'] = "CREATE " + name + " ERROR when execute command '" + e.cmd + "', exit code: " + \
            str(e.returncode) + ", stderr: " + str(e.stderr, encoding="utf-8") + ", stdout: " + str(e.stdout, encoding="utf-8")
        # print(e.stderr)
        print("result: ", result)
        return result
    else:
        result['result'] = name + " create successfully!"
        result['success_msg'] = "CREATE " + name + " SUCCESS " + ", stdout:" + str(out, encoding="utf-8")
        print("result: ", result)
        return result


# def server_config(self):
#     # 针对不同节点节点的服务开启？
#     pass
