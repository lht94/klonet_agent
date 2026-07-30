import os
from ..Implement_layer.LinkManager.link_operate import get_pid
from ..Service_layer.deploy_error import NodeIpv4UrpfConfigPathError
from .redisAPI import UserMapRedis
from nsenter import Namespace


def urpf_config(user, topo, nes, urpf_mode:int):
    """URPF配置方法

    Args:
        user : 用户名_
        topo : 拓扑名
        nes : 节点列表
        urpf_mode : URPF模式，1(启动)，0(停止)
        
    Raises:
        NodeIpv4UrpfConfigPathError: 配置错误异常
    """
    try:
        user_map_redis = UserMapRedis()
        user_db_cli = user_map_redis.get_user_db(user)
        user_map_redis.close()
        for ne in nes:
            table_name = f'{topo}_{ne}'
            container_id  = user_db_cli.get_all_values(table_name)['NEid']
            pid = get_pid(container_id)
            _ne_urpf_all_interface_config(pid, urpf_mode)
    except:
        raise 
    finally:
        user_db_cli.close()

def _ne_urpf_all_interface_config(pid, urpf_mode:int):
    """对lo本地回环除外的全部网卡的urpf服务进行配置 
    
    通过对内核文件rp_filter进行读写实现URPF启停，详细可参考Ubuntu URPF配置的相关资料

    Args:
        pid : 容器PID
        urpf_mode : URPF模式

    Raises:
        NodeIpv4UrpfConfigPathError: 配置错误异常
    """
    with Namespace(pid, 'net'):
        path = '/proc/sys/net/ipv4/conf/'
        Filelist = []
        if os.path.exists(path):
            Filelist = os.listdir(path)
            # print(Filelist)
        else:
            raise NodeIpv4UrpfConfigPathError('this path not exist')
        for file in Filelist:
            if not file == 'lo':
                cmd = 'echo '+ str(urpf_mode) +' > '+path + file +'/rp_filter'
                # print(cmd)
                os.system(cmd)  
