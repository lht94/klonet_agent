# !/usr/bin/python
# coding:utf-8
import subprocess
from .create_container import run_shell

msg_words = {
    'unpause': ' unpause ',
    'pause': ' pause '
}


def delete_container(name) -> dict:
    '''
        输入:
            name:容器名
        输出：
            容器删除命令执行后的结果字典
        功能描述：
            使用subprocess.run()执行容器删除命令
    '''
    # 停止并删除容器
    # TODO:异常处理：Error response from daemon: No such container: xx？
    # result = {
    #     "result": "",
    #     "error_msg": "",
    #     "success_msg": ""
    # }
    result = {}
    try:
        out = run_shell("docker stop " + name)
        out = run_shell("docker rm " + name)
    except subprocess.CalledProcessError as e:
        result['result'] = name + " delete unsuccessfully!"
        result['error_msg'] = "DELETE " + name + " ERROR when execute command '" + e.cmd + "', exit code: " + \
            str(e.returncode) + ", stderr: " + str(e.stderr, encoding="utf-8") + ", stdout: " + str(e.stdout, encoding="utf-8")
        print("result: ", result)
        return result
    else:
        result['result'] = name + " delete successfully!"
        result['success_msg'] = "DELETE " + name + " SUCCESS" + ",stdout:" + str(out, encoding="utf-8")
        print("result: ", result)
        return result


def pause_unpause_container(name, choice) -> dict:
    word = msg_words[choice]
    '''
        输入:
            name:容器名
        输出：
            容器暂停命令执行后的结果字典
        功能描述：
            使用subprocess.run()执行容器暂停/启动命令
    '''
    result = {}
    try:
        out = run_shell("docker" + word + name)
    except subprocess.CalledProcessError as e:
        result['result'] = name + word + "unsuccessfully!"
        result['error_msg'] = word.upper().lstrip() + name + " ERROR when execute command '" + e.cmd + "', exit code: " + \
            str(e.returncode) + ", stderr: " + str(e.stderr, encoding="utf-8") + ", stdout: " + str(e.stdout, encoding="utf-8")
        print("result: ", result)
        return result
    else:
        result['result'] = name + word + "successfully!"
        result['success_msg'] = word.upper().lstrip() + name + " SUCCESS" + ",stdout:" + str(out, encoding="utf-8")
        print("result: ", result)
        return result
