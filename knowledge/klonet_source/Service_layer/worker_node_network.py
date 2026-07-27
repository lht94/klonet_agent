import docker
from ..tools.log_tools import FLASK_LOGGER
docker_cli = docker.from_env()

# 镜像源文件配置
source_text = "\
deb http://mirrors.tuna.tsinghua.edu.cn/ubuntu/ bionic main restricted universe multiverse \n\
deb http://mirrors.tuna.tsinghua.edu.cn/ubuntu/ bionic-updates main restricted universe multiverse \n\
deb http://mirrors.tuna.tsinghua.edu.cn/ubuntu/ bionic-backports main restricted universe multiverse \n\
deb http://security.ubuntu.com/ubuntu/ bionic-security main restricted universe multiverse \n\
"
source_file_path = "/etc/apt/sources.list"
version = "Ubuntu 18.04"

def network_enable(ctn_id):
    """启动容器网络服务
    
    启用eth0网卡后，网络状态与宿主机一致，同时写入路由，配置镜像源（清华），

    Args:
        ctn_id : 容器ID

    Returns:
        bool: 成功或者失败
    """
    # 没有考虑镜像差异，版本差异
    # 网卡默认是eth0
    # 需要手动添加docker0网关
    # 网络状态与宿主机一致
    # shell命令这么写不是很好
    try:
        ctn = docker_cli.containers.get(ctn_id)
        # 先关在起，重复操作
        ctn.exec_run('ifconfig eth0 down')
        if ctn.exec_run('ifconfig eth0 up').exit_code:
            FLASK_LOGGER.warning('eth0网卡启动失败')
            return False
        if ctn.exec_run('route add -net default gw 172.17.0.1').exit_code:
            FLASK_LOGGER.warning('网关路由写入失败')
            return False
        command = "bash -c \"echo \'{}\' > {}\"".format(source_text, source_file_path)
        if ctn.exec_run(cmd=command, demux=True).exit_code:
            FLASK_LOGGER.warning("镜像源配置错误")
            return False
        if ctn.exec_run('chmod 1777 /tmp').exit_code:
            FLASK_LOGGER.warning('权限错误')
            return False
    except:
        FLASK_LOGGER.error('未知错误')
        return False
    return True

def network_disable(ctn_id):
    """停止容器网络服务

    Args:
        ctn_id : 容器ID

    Returns:
        bool: 成功或者失败
    """
    try:
        ctn = docker_cli.containers.get(ctn_id)
        ctn.exec_run('ifconfig eth0 down')
    except:
        FLASK_LOGGER.error('docker错误')
        return False 
    return True