from .redisAPI import UserDB, UserMapRedis
from ..Implement_layer import LinkManager as link_manager
from ..Implement_layer.LinkManager.link_operate import shell_execute,get_pid,clear_qdisc
from .deploy_error import LinkConfigError, LinkInterfaceDeleteError
import os
import psutil
import multiprocessing
from gevent import subprocess

class WorkerLinkManager:
    def __init__(self, data):
        self.data = data
        user_map_redis = UserMapRedis()
        self.user_db_cli = user_map_redis.get_user_db(data['user'])
        user_map_redis.close()

    def close(self):
        self.user_db_cli.close()

    def config_links(self, operate):
        """批量配置链路

        Args:
            operate : TC配置相关的命令字段

        Raises:
            LinkConfigError: 链路配置异常
        """
        topo, links = self.data['topo'], self.data['links']
        link = links[0]['link']
        for link_config in links:
            table = f"{topo}_{link_config['ne']}"
            # 直接得到节点所有的信息
            ne_info = self.user_db_cli.get_all_values(table)
            container_id =ne_info['NEid']
            nic_intf = self.user_db_cli.get_value(table, link_config['link'])['nic']
            # 这里返回的result = {}  如果执行成功的话？
            if not nic_intf:
                raise LinkConfigError(f"节点{link_config['ne']}没有网卡接口{nic_intf}")
            result = link_manager.config_link(
                container_id, nic_intf,
                link_config['bw_kbps'],
                queue_size_byte = link_config['queue_size_bytes'],
                delay_us = link_config['delay_us'],
                loss_rate = link_config['loss'], 
                jitter_us = link_config['jitter_us'], 
                correlation = link_config['correlation'], 
                delay_distribution = link_config['delay_distribution'], 
                operate = operate
                )
            if result:
                raise LinkConfigError(f"设置节点{link_config['ne']}的网卡接口{nic_intf}出错")

            

    def clear_qdisc(self):
        """批量重置链路"""
        topo, links = self.data['topo'], self.data['links']   
        for link_config in links:
            table = f"{topo}_{link_config['ne']}"
            container_id = self.user_db_cli.get_value(table, 'NEid')
            nic_intf = self.user_db_cli.get_value(table, link_config['link'])['nic']
            result = link_manager.clear_qdisc(container_id, nic_intf)
            if result:
                raise LinkInterfaceDeleteError(f"{result['error_msg']}")



PARENT_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
# 脚本位置
IMPLEMENT_LAYER_LINK_MM= PARENT_DIR + "/Implement_layer/LinkManager/mmwave"


def deploy_mmlink(user, topo, links, user_db_cli:UserDB):
    '''配置毫米波链路
    
    创建新进程执行毫米波链路脚本文件，最终返回一个进程列表供后续关闭进程。

    Args:
        user: 用户名
        topo: 拓扑名
        links: 毫米波链路参数信息
        user_db_cli: 数据库连接实例
        
    Returns:
        list: 毫米波链路进程号列表
    '''
    mmlink_processing_list = []
    try:
        for link_config in links:
            table = f"{topo}_{link_config['ne']}"
            container_id = user_db_cli.get_value(table, 'NEid')
            nic_intf = user_db_cli.get_value(table, link_config['link'])['nic']
            pid = get_pid(container_id)
            # 需要提供绝对路径 
            t = multiprocessing.Process(
                target=shell_execute, 
                args=("sudo nsenter -t "+ pid + " --net " +   
                    IMPLEMENT_LAYER_LINK_MM + "/tputvary.sh " + nic_intf + " " + 
                    link_config['link_scenario'] +" " + link_config['queue_type'] +
                    " " + IMPLEMENT_LAYER_LINK_MM + " " + str(link_config['loss']) + "% " 
                    +str(link_config['bandwidth_scaling']),
                     )
                )
            t.start()
            info_dict = {'container_id':container_id ,'nic_intf':nic_intf ,'process_id':t.pid}
            mmlink_processing_list.append(info_dict)
    except subprocess.CalledProcessError as e:
        raise e
    except:
        raise
    return mmlink_processing_list

def terminate_mmlink_processings(processing_list:list) -> bool:
    '''结束毫米波链路进程

    根据数据库中的信息停止毫米波链路进程，清除tc队列

    Args:
        processing_list: 毫米波链路脚本的进程号列表
        
    Returns:
        bool: 结束成功返回True，结束失败返回False
    '''
    # 目前来看直接杀掉即可
    dict = processing_list[0] 
    os.kill(dict['process_id'], 9)
    clear_qdisc(dict['container_id'], dict['nic_intf'])
    return True

