import re
import docker

from ..tools.upper_level_redis_API import get_workers_to_nes
from ..Implement_layer.LinkManager.link_operate import shell_execute


docker_cli = docker.from_env()

# 需要安装的deb包，按照列表顺序安装即可
dpkg_debs_1804 = [
    'ucf_3.0038_all.deb',
    'libwrap0_7.6.q-30_amd64.deb',
    'dialog_1.3-20171209-1_amd64.deb',
    'libkrb5support0_1.16-2build1_amd64.deb',
    'libk5crypto3_1.16-2build1_amd64.deb',
    'libkeyutils1_1.5.9-9.2ubuntu2_amd64.deb',
    'libk5crypto3_1.16-2build1_amd64.deb',
    'libkrb5-3_1.16-2build1_amd64.deb',
    'libbsd0_0.8.7-1_amd64.deb',
    'libedit2_3.1-20170329-1_amd64.deb',
    'libgssapi-krb5-2_1.16-2build1_amd64.deb',
    'libssl1.0.0_1.0.2n-1ubuntu5_amd64.deb',
    'openssh-client_7.6p1-4ubuntu0.7_amd64.deb',
    'openssh-sftp-server_7.6p1-4ubuntu0.7_amd64.deb',
    'openssh-server_7.6p1-4ubuntu0.7_amd64.deb',
    'ssh_7.6p1-4_all.deb',
]

dpkg_debs_2004 = [
    'ucf_3.0038_all.deb',
    'libwrap0_7.6.q-30_amd64.deb',
    'libkrb5support0_1.17-6ubuntu4_amd64.deb',
    'libkeyutils1_1.6-6ubuntu1.1_amd64.deb',
    'libk5crypto3_1.17-6ubuntu4_amd64.deb',
    'libedit2_3.1-20191231-1_amd64.deb',
    'libcbor0.6_0.6.0-0ubuntu1_amd64.deb',
    'libfido2-1_1.3.1-1ubuntu2_amd64.deb',
    'libkrb5-3_1.17-6ubuntu4_amd64.deb',
    'libgssapi-krb5-2_1.17-6ubuntu4_amd64.deb',
    'openssh-client_8.2p1-4ubuntu0.5_amd64.deb',
    'openssh-sftp-server_8.2p1-4ubuntu0.5_amd64.deb',
    'openssh-server_8.2p1-4ubuntu0.5_amd64.deb',
    'ssh_8.2p1-4_all.deb',
]


def get_worker_ip(user, topo, ne):
    """
    节点所在的worker的ip
    """
    worker_ne_dict = get_workers_to_nes(user, topo)
    for worker, nes in worker_ne_dict.items():
        for ne_list in nes.values():
            if ne in ne_list:
                return worker


def is_netstat_free(port):
    """
    检查端口是否被占用
    """
    ports_in_use = [int(port) for port in re.findall(r'(?:\d{5})', shell_execute('netstat -nlt'))]
    return port not in ports_in_use


def container_exec_cmds(container, cmd_start, cmds, cmd_end):
    """
    容器内执行若干命令
    cmd_start、cmds、cmd_end三部分构成命令集
    
    Args:
        container: 运行命令的容器
        cmd_start: 字符串，命令集的开头公共部分
        cmds:      字符串组成的列表，命令集中间差异的部分
        cmd_end:   字符串，命令集的结尾公共部分

    Return:
        None, 若运行失败会直接报错RuntimeError
    """
    for cmd in cmds:
        exec_cmd = cmd_start + cmd + cmd_end
        if container.exec_run(exec_cmd).exit_code:
            print(f'{exec_cmd}发生错误！')
            raise RuntimeError(f'{exec_cmd}发生错误！')
        print(f'{exec_cmd}运行成功！')

