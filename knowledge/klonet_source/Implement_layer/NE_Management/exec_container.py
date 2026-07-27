# !/usr/bin/python
# coding:utf-8
import subprocess
from .create_container import run_shell


def exec_container(name, choice="detach", command="/bin/bash") -> dict:
    '''
        输入:
            name:容器名
            choice:容器命令在后台执行与否,"detach" or "notdetach"
            command:容器执行的命令
            param:其他输入参数
        输出：
            容器执行命令后的结果字典
        功能描述：
            使用subprocess.run()执行容器命令
    '''
    # 启动并执行命令
    result = {}
    # for i in range(120):
    try:
        out = run_shell("sudo docker start " + name)
        base_cmd = "sudo docker exec "
        # detach 需要加-d参数，其不会返回执行结果;notdetach则打印结果
        if choice == "detach":
            base_cmd += " -d "
        else:
            assert choice == "notdetach"
        cmd = base_cmd + " {name} {command}".format(name=name, command=command)
        print(cmd)
        out = run_shell(cmd)
    except subprocess.CalledProcessError as e:
        result['result'] = name + " execute command unsuccessfully!"
        result['error_msg'] = "EXECUTE " + name + " ERROR when execute command '" + e.cmd + "', exit code: " + \
            str(e.returncode)  # + ", stderr: " + e.stderr + ", stdout: " + e.stdout
        # 返回执行结果的标准输出和标准错误输出
        result["stdout"] = str(e.stdout, encoding="utf-8")
        result["stderr"] = str(e.stderr, encoding="utf-8")
        print("result: ", result)
        return result
    except AssertionError:
        result['result'] = name + " execute command unsuccessfully!"
        result['error_msg'] = "EXECUTE " + name + " ERROR because of unknown choice \"" + choice + "\""
        print("result: ", result)
        return result
    else:
        result['result'] = name + " execute successfully!"
        result["success_msg"] = str("EXECUTE " + name + " SUCCESS ")
        result["stdout"] = str(out, encoding="utf-8")
        # result["stderror"] = process.stderr
        print("result: ", result)
        # print("stdout: ", result["stdout"])
        return result
