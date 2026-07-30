from builtins import print
import docker
from gevent import subprocess
import docker.errors
import requests
from fnmatch import fnmatch
import multiprocessing


import random

from .redisAPI import UserMapRedis
from ..Implement_layer import LinkManager as link_manager
from ..Function_layer.deploy_process_bar import ProcessBarDeploy, ProcessBarDelete
from .NEManager import (DefaultNECreator, OvsCreator, DpdkCreator, ControllerCreator, delete_overlay_net,
                        HostRunner, OvsRunner, DpdkRunner, QuaggaRunner, VethLink, VxLANLink, get_overlay_net)
from ..vemu_config.config import PROJ_CONFIG
from .AsyncTopoManager import VethCreateTasks, VxLANCreateTasks, NeDelTasks, VxlanDelTasks, NeCreateTasks
from ..Service_layer.redisAPI import HostPortsAvailableRedis, ResourceRedis, UserMapRedis
from ..Service_layer.NEManager import get_port_mapping_config
from ..Implement_layer.LinkManager.link_operate import shell_execute
from ..tools.log_tools import FLASK_LOGGER
from ..tools.tools import chinese_to_pinyin


# 节点类型
NE_SERVICE = ['hosts', 'switches', 'routers', 'dpdks']
l2_service = ['switches', ]
l3_service = ['routers']
# 服务层级划分
SERVICE_HIERARCHURE = ['l3', 'l2', 'other', 'dpdk_l2fwd', 'tc']

docker_cli = docker.from_env()
# 得到None_net
NONE_NET = docker_cli.networks.get('none')
user_map_redis = UserMapRedis()
SUCCESS_RESULT_MSG = {'code': 1, 'msg': 'success'}


def get_image_init_para(**kwargs):
    """
    Args:
        kwargs (dict): 用户传入的参数

    Returns:
        default_para (dict): 镜像初始化的默认参数
    """
    default_para = {'privileged': True, 'oom_kill_disable': True, 'detach': True,
                    'network_mode': 'bridge', 'stdin_open': True, 'tty': True}
    if PROJ_CONFIG.mount_host_clock_enabled:
        default_para['volumes'] = ['/etc/localtime:/etc/localtime:ro']
    for k, v in kwargs.items():
        default_para[k] = v
    return default_para


class TopoDeployManager(object):
    """
    拓扑创建代理类
    
    Attributes:
        user (str):    用户名
        topo (str):    拓扑名
        subtopo (str): 子拓扑名
        user_db_cli (UserDB): redis数据库连接
        __dict__ (dict): 包含有拓扑信息的类属性字典
    """
    # 写入就不会改变的数据表， 从数据库读取后可以缓存为实例属性
    _subtopo_common_table = ['plane_subtopo_list', 'subtopo_service']

    def __init__(self, user: str, topo: str, subtopo: str):
        """
        Args:
            user (str):    用户名
            topo (str):    拓扑名
            subtopo (str): 子拓扑名

        Returns:
            None
        """
        self.user = user
        self.topo = topo
        self.subtopo = subtopo
        user_map_redis = UserMapRedis()
        self.user_db_cli = user_map_redis.get_user_db(user)
        user_map_redis.close()
        sub_topo_service = self.user_db_cli.get_value('subtopo_service', subtopo)
        self.__dict__.update(sub_topo_service)
        self.__dict__.update({'ne_type': list(sub_topo_service.keys())})
        # 缓存 plane_subtopo_list 的properties: NEs,  links, vxlanlinks
        plane_subtopo_list = self.user_db_cli.get_value('plane_subtopo_list', subtopo)
        self.__dict__.update(plane_subtopo_list)

    def _before_deploy(self):
        """
        创建拓扑前的检查函数
        在创建之前，应该检查是不是有controller容器
        如果有的话，应该就先创建好overlay网络，避免之后并发创建的时候
        """
        if getattr(self, 'controllers'):
            topo = chinese_to_pinyin(self.topo)
            get_overlay_net(f'{self.user}-{topo}-sdn')

    def deploy_topo(self):
        """
        创建拓扑的主承担功能函数
        """
        FLASK_LOGGER.debug(f'deploy subtopo {self.subtopo} of {self.user}...')
        error_msg = {}
        try:
            self._before_deploy()
            ne_create_tasks = []
            for ne_type in getattr(self, 'ne_type'):
                for ne in getattr(self, ne_type):
                    FLASK_LOGGER.debug(f'create {ne_type}: {ne}...')
                    init_conf = self._get_ne_init_para(ne)
                    # 将创建所需要的函数和参数打包为列表
                    args = (init_conf, ne_type, ne)
                    ne_create_tasks.append((self._create_ne, args))
             
            # 1、并行创建容器
            nes_creator = NeCreateTasks(ne_create_tasks)
            nes_creator.wait_task_done(0, self.user_db_cli, self.topo)
            ProcessBarDeploy(1, self.user_db_cli, self.topo)

            # 2、并行创建veth pair
            # 进程池只能在__main__模块下使用
            # 利用Queue耗时又会回到之前
            # 进度条没有必要更新
            links = getattr(self, 'links')
            procs = []
            for link in links:
                veth = VethLink(self.topo, link, self.user_db_cli)
                proc = multiprocessing.Process(target=veth.create_link_and_write_info)
                procs.append(proc)
                proc.start()
            for proc in procs:
                proc.join()
            
            # 以前的方法
            # veth_creators = VethCreateTasks(links, self.topo, self.user_db_cli)
            # veth_creators.wait_task_done(1, self.user_db_cli, self.topo)
            ProcessBarDeploy(2, self.user_db_cli, self.topo)

            # 3、并行创建 vxlan
            vxlanlinks = getattr(self, 'vxlanlinks')
            vxlan_creators = VxLANCreateTasks(vxlanlinks, self.topo, self.user_db_cli)
            vxlan_creators.wait_task_done(2, self.user_db_cli, self.topo)
            ProcessBarDeploy(3, self.user_db_cli, self.topo)

        except RuntimeError as e:
            error_msg['msg'] = e.args[0]
        finally:
            self.user_db_cli.close()
        if error_msg:
            error_msg['code'] = 0
            return error_msg
        return SUCCESS_RESULT_MSG

    def _create_ne(self, init_conf, ne_type, ne=None):
        """
        根据节点类型的创建节点功能分发函数
        Args:
            init_conf (dict): 初始化信息
            ne_type    (str): 类型字符串
            ne         (Node): Node类型

        Returns:
            None
        """
        if "type" not in init_conf:
            init_conf.update({"ports": \
                get_port_mapping_config(init_conf['hostname'], self.topo, self.user_db_cli)})
            if ne_type in ['hosts', 'routers']:
                self._create_default_ne(init_conf)
            elif ne_type in ['switches', ]:
                self._create_ovs(init_conf)
            elif ne_type in ['controllers', ]:
                self._create_controllers(init_conf, ne)
            elif ne_type in ['dpdks', ]:
                self._create_dpdks(init_conf, ne)
            else:
                self._create_default_ne(init_conf)
        elif "type" in init_conf:
            if init_conf["type"] == "hardware":
                pass
            else:
                self._create_kvm(init_conf)

    def _create_ovs(self, init_conf):
        """
        创建  OVS 容器
        Args:
            init_conf (dict): 初始化参数
        """
        ne_creator = OvsCreator(init_conf)
        ne_creator.create_and_run()

    def _create_default_ne(self, init_conf):
        """
        默认的 容器 创建函数
        Args:
            init_conf (dict): 初始化参数
        """
        ne_creator = DefaultNECreator(init_conf)
        ne_creator.create_and_run()
        
    def _create_kvm(self, init_conf):
        ne_creator = DefaultNECreator(init_conf)
        ne_creator.create_kvm()

    def _create_dpdks(self, init_conf, ne):
        """
        创建 DPDK 容器
        Args:
            init_conf (dict): 初始化参数
            ne         (str): 节点名
        """
        #generate nums for dpdk-use and save it to db
        # for ne_dpdk in self.NEs['dpdks']:
        #     dpdk_table_name = f'{self.topo}_{ne_dpdk}'
        #     dpdk_nums = (str(link_operate.generate_uuid_len_10()), str(link_operate.generate_uuid_len_10()))
        #     self.user_db_cli.set_value(dpdk_table_name, 'dpdk_nums', dpdk_nums)
        #run it
        dpdk_table_name = f'{self.topo}_{ne}'
        FLASK_LOGGER.debug('-----------------dpdk_table-name-----------------')
        FLASK_LOGGER.debug(dpdk_table_name)
        init_conf['volumes'].append('/mnt/huge:/mnt/huge')
        init_conf['volumes'].append('/usr/local/var/run/openvswitch:/var/run/openvswitch')
        # init_conf.update({'volumes': ['/mnt/huge:/mnt/huge', '/usr/local/var/run/openvswitch:/var/run/openvswitch', '/etc/localtime:/etc/localtime:ro']})
        dpdk_creator = DpdkCreator(init_conf, self.user_db_cli, dpdk_table_name)
        dpdk_creator.create_and_run()

    def _create_controllers(self, init_conf, ne):
        """
        创建 控制器 容器，在创建的时候就需要将其加入到overlay网络中去，然后写入被分配到的IP地址
        Args:
            init_conf (dict): 初始化参数
            ne         (str): 节点名

        Returns:
            None
        """
        ctr_creator = ControllerCreator(init_conf, self.user_db_cli)
        topo = chinese_to_pinyin(self.topo)
        net = f'{self.user}-{topo}-sdn'
        table = f'{self.topo}_{ne}'
        ctr_creator.create_and_run(net, table)

    def _get_ne_init_para(self, ne):
        """
        得到容器创建的初始化参数
        Args:
            ne (str): 节点名

        Returns:
            None
        """
        table_name = f'{self.topo}_{ne}'
        ne_id = self.user_db_cli.get_value(table_name, 'NEid')
        ne_image = self.user_db_cli.get_value(table_name, 'NEimage')
        ne_resource = self.user_db_cli.get_value(table_name, 'NEresource')
        ne_cpu = ne_resource['cpu']
        ne_mem = ne_resource['mem']
        # 总配置
        ne_service = self.user_db_cli.get_value(table_name, 'NEservice')
        if ne_service == 'docker':
            init_conf = {'image': ne_image, 'name': ne_id, 'hostname': ne}
            # 获取资源限制信息
            config = {}
            print(ne_image)
            # GPU on
            if ne_image in PROJ_CONFIG.nvidia_on_list:
                print('------------------GPU ON-----------------')
                config.update({'device_requests': [{'driver': 'nvidia', 'count': -1, 'capabilities': [['gpu']]}]})
            if (ne_resource) and PROJ_CONFIG.resource_limit_enable:
                # cpu配置
                cpu_period = 50000
                # fraction = int(ne_resource['cpu'][:-1]) * 0.01 if ne_resource['cpu'].endswith("%") else 1
                # bug修复（wudx）
                # cpu并不支持百分号格式的数据，会在拓扑切分计算资源时报错
                # 更改为默认隐含百分号，将cpu的资源限制转换为小数
                fraction = int(ne_resource['cpu']) * 0.01
                cpu_quota = int(cpu_period * fraction)
                config.update({"cpu_period": cpu_period, "cpu_quota": cpu_quota})
                # mem配置
                config.update({"mem_limit": int(ne_resource['mem']) * (10 ** 6)})
                # 总配置
                # print("resource config:", config)
                init_conf.update(get_image_init_para(**config))
                
            # wdx
            # 采用CPU_SET方式进行资源隔离
            # 直接按照QUOTA的方式等价换算为绑定CPU
            elif (ne_resource) and (PROJ_CONFIG.node_iso_resource_limit_CpuSet 
                                    or PROJ_CONFIG.topo_iso_resource_limit_CpuSet):
                # 查表，获取CPU绑定信息
                user_manager = UserMapRedis()
                user_cli = user_manager.get_user_db(self.user)
                
                # config增加参数
                indices = list(user_cli.get_value(f"{self.topo}_{ne}", "NEcpuset").values())[0]
                core_set = "" 
                for index in indices:
                    core_set += str(index)
                    if index != indices[-1]:
                        core_set += ","
                print("绑定核心", core_set)
                config.update({"cpuset_cpus": core_set})
                config.update({"mem_limit": int(ne_resource['mem']) * (10 ** 6)})
                # 总配置
                init_conf.update(get_image_init_para(**config))
            else:
                print("resource config:", config)
                init_conf.update(get_image_init_para(**config))
                print("resource init_config:", init_conf)
        elif ne_service == 'kvm':
            ne_vmconfig = self.user_db_cli.get_value(table_name, 'NEvmconfig')
            # ne_interface = self.user_db_cli.get_value(table_name, 'NEinterface')
            # 端口从1开始
            ne_br = self.user_db_cli.get_value(table_name, 'NEnic')
            ne_type = ne_vmconfig['type']
            # (Wudx)镜像路径配置
            image_path = ne_vmconfig['kvm_image']['image_path']
            qcow2_size = ne_vmconfig['kvm_image']['qcow2_size']  # 仅image是iso文件时生效，默认为-1
            image_name = ne_vmconfig['image_name']
            init_conf = {'topo': self.topo, 'user':self.user,'name': ne_id, 
                         'type': ne_type, 'br': ne_br, 'image_path':image_path, 
                         'qcow2_size':qcow2_size, 'cpu': ne_cpu, 'mem': ne_mem, 
                         'image_name': image_name}
        else:
            init_conf = {'type': 'hardware'}
        return init_conf

    def _create_links(self, link):
        """
        创建veth-pair
        Args:
            link (str): 链路名

        Returns:
            None
        """
        link_creator = VethLink(self.topo, link, self.user_db_cli)
        result = link_creator.create_link()
        FLASK_LOGGER.debug(result)
        link_creator.write_info(result)

    def _create_vxlanlinks(self, link):
        """
        创建 vxlan
        Args:
            link (str): 链路名

        Returns:
            None
        """
        vxlan_creator = VxLANLink(self.topo, link, self.user_db_cli)
        result = vxlan_creator.create_link()
        FLASK_LOGGER.debug(f'result in create vxlanlink: {result}')
        vxlan_creator.write_info(result)


class TopoDeleteManager(object):
    """
    拓扑创建代理类
    """
    _subtopo_common_table = ['plane_subtopo_list', 'subtopo_service']

    def __init__(self, user: str, topo: str, subtopo: str):
        """
        Args:
            user (str):    用户名
            topo (str):    拓扑名
            subtopo (str): 子拓扑名
        Returns:
            None
        """
        self.user = user
        self.topo = topo
        self.subtopo = subtopo
        user_map_redis = UserMapRedis()
        self.user_db_cli = user_map_redis.get_user_db(user)
        user_map_redis.close()
        for common_table in self._subtopo_common_table:
            common_info = self.user_db_cli.get_value(common_table, subtopo)
            self.__dict__.update(common_info)

    def destroy_topo(self):
        """
        删除拓扑
        """
        # 1、删除ne与残留veth对
        nes = getattr(self, 'NEs')
        links = getattr(self, 'links')
        ne_infos = []
        veth_infos = []
        veth_all = {}
        for ne in nes:
            table = f'{self.topo}_{ne}'
            # ne_id 就为节点容器的name
            con_info = self.user_db_cli.get_all_values(table)
            # 如果不为空字典，再加入列表进行删除，防止删除报错
            if con_info != {}:
                con_info['topo'] = self.topo
                con_info['user'] = self.user
                con_info['ne_name'] = ne
                ne_infos.append(con_info)
        # （gjh虚机相关）收集残留veth装入列表，封装成字典压入节点删除任务队列尾部
        for link in links:
            table = f'{self.topo}_{link}'
            con_info = self.user_db_cli.get_all_values(table)
            if con_info != {}:
                src_veth = con_info.get('sourceveth','')
                tgt_veth = con_info.get('targetveth','')
                if src_veth != '':
                    veth_infos.append(src_veth)
                if tgt_veth != '':
                    veth_infos.append(tgt_veth)
        # 加入伪装信息，方便底层初始化
        veth_all['topo'] = self.topo
        veth_all['user'] = self.user
        veth_all['ne_name'] = 'default' #标志位，用于区分节点删除任务与veth删除任务
        veth_all['veth'] = veth_infos
        ne_infos.append(veth_all)
        ne_deleter = NeDelTasks(ne_infos)
        ne_deleter.wait_task_done(0, self.user_db_cli, self.topo)
        ProcessBarDelete(1, self.user_db_cli, self.topo)

        # 2、删除vxlan以及虚机带来的残留veth对
        ovs_targets = []
        for vxlan in getattr(self, 'vxlanlinks'):
            ovs_info = {}
            table_name = f'{self.topo}_{vxlan}'
            src_node = self.user_db_cli.get_value(table_name, 'source')
            link = self.user_db_cli.get_value(table_name, 'partof')
            src_table = f'{self.topo}_{src_node}'
            src_service = self.user_db_cli.get_value(src_table, 'NEservice')
            targetNE = self.user_db_cli.get_value(f'{self.topo}_{link}', 'targetNE')
            sourceNE = self.user_db_cli.get_value(f'{self.topo}_{link}', 'sourceNE')
            if src_node == targetNE:
                tgt_node = sourceNE
            else:
                tgt_node = targetNE
            tgt_table = f'{self.topo}_{tgt_node}'
            tgt_service = self.user_db_cli.get_value(tgt_table, 'NEservice')
            if src_service == 'hardware':
                ovs_info['ne_id'] = self.user_db_cli.get_value(f'{self.topo}_{src_node}', 'NEid')
                config = self.user_db_cli.get_value(f'{self.topo}_{src_node}', 'NEconfig')
                vlan = config['config']['vlan']
                ovs_info['vlan'] = vlan
            ovs_info['vni'] = self.user_db_cli.get_value(table_name, 'VNI')
            ovs_info['remote_ip'] = self.user_db_cli.get_value(table_name, 'remoteIP')
            ovs_info['src_service'] = src_service
            ovs_info['tgt_service'] = tgt_service
            ovs_info['target'] = self.user_db_cli.get_value(table_name, 'target')
            ovs_info['src_veth'] = self.user_db_cli.get_value(table_name, 'sourceveth')
            ovs_info['tgt_veth'] = self.user_db_cli.get_value(table_name, 'targetveth')
            ovs_targets.append(ovs_info)
        vxlan_deleter = VxlanDelTasks(ovs_targets)
        vxlan_deleter.wait_task_done(1, self.user_db_cli, self.topo)
        ProcessBarDelete(2, self.user_db_cli, self.topo)
        
        # 3、删除overlay网络
        self._delete_overlay_net()
        ProcessBarDelete(3, self.user_db_cli, self.topo)
        FLASK_LOGGER.debug('==> overlay del tasks done')

        # 4、归还并删除端口映射表
        ports_delete = []            # 待删除的端口列表
        for map in self.user_db_cli.get_all_values(f'{self.topo}_port_mapping').values():
            if isinstance(map, dict):
                for port in map.values():
                    ports_delete.extend(port)
        db0 = HostPortsAvailableRedis()
        for port in ports_delete:
            db0.return_port(port)    # 归还列表中的所有端口
        db0.close()
        self._del_port_iptables()  # 将nat表中的配置删除
        self.user_db_cli.del_table(f'{self.topo}_port_mapping')
        
        # except:
        #     error_msg['code'] = 0
        #     error_msg['msg'] = '删除拓扑失败'
        # finally:
        self.user_db_cli.close()
        # return error_msg if error_msg else SUCCESS_RESULT_MSG
        return SUCCESS_RESULT_MSG

    def _delete_nes(self, ne: str):
        """
        删除节点
        Args:
            ne  (str): 节点名称

        Returns:
            None
        """
        table = f'{self.topo}_{ne}'
        # ne_id 就为节点容器的name
        ne_info = self.user_db_cli.get_all_values(table)
        con_name = ne_info['NEid']
        if 'dpdk_nums' in ne_info:
            br_ds_name = []
            dpdk_nums = ne_info['dpdk_nums']
            br_ds_name.append(f'br_d{dpdk_nums[0]}')
            br_ds_name.append(f'br_s{dpdk_nums[0]}')
            self.delete_dpdk_l2fwd_bridge(br_ds_name)
        
        try:
            ne = docker_cli.containers.get(con_name)
            ne.stop()
            ne.remove()
        except docker.errors.NotFound:
            FLASK_LOGGER.error(f'找不到容器:{ne}')
        except docker.errors.APIerror as e:
            FLASK_LOGGER.error(e)
        except requests.exceptions.HTTPError as e:
            FLASK_LOGGER.error(e)

    def _delete_overlay_net(self):
        """
        删除overlay网络
        """
        topo = chinese_to_pinyin(self.topo)
        name = f'{self.user}-{topo}-sdn'
        delete_overlay_net(name)

    def delete_vxlan(self, vxlan):
        """
        删除 vxlan
        Args:
            vxlan (str): vxlan 链路名

        Returns:
            None
        """
        table_name = f'{self.topo}_{vxlan}'
        ovs_target = self.user_db_cli.get_value(table_name, 'target')
        link_manager.delete_vxlan(ovs_target)

    def delete_dpdk_l2fwd_bridge(self, br_names):
        """
        删除 DPDK 二层网桥
        Args:
            br_names (str): DPDK网桥名

        Returns:
            None
        """
        for br_name in br_names:
            result = link_manager.delete_dpdk_br(br_name)
            if result['code'] == 0:
                FLASK_LOGGER.error(f'error in del_dpdk_br \n {result}')

    def _delete_topo_entry(self):
        """
        删除Redis中数据库相关信息
        """
        FLASK_LOGGER.debug("delete topo entry in redis...")
        self.user_db_cli.delete_topo_entry(self.topo)

    def _del_port_iptables(self):
        """
        对于容器启动后配置的端口映射，在删除拓扑时也需删除iptables配置
        """
        table = f'{self.topo}_port_mapping'
        container_list = self.user_db_cli.get_value(table, "containers_modified_NAT") \
            if self.user_db_cli.check_exist(table, "containers_modified_NAT") else []
        iptables = ['filter', 'nat']
        
        for tb in iptables:
            exec_result = shell_execute(f'iptables -t {tb} --list-rules DOCKER')
            exec_lines = set(exec_result.splitlines())  # 避免重复，使用集合过滤
            for container_ip in container_list:
                if container_ip in exec_result:
                    for line in exec_lines:
                        if container_ip in line:
                            try:
                                shell_execute(f'sudo iptables -t {tb} -D' + line[2:])
                            except subprocess.CalledProcessError:
                                pass

    def __del__(self):
        self.user_db_cli.close()

class ServiceManager(object):
    """
    服务创建代理类
    
    Attributes:
        user (str): 用户名
        topo (str): 拓扑名
        subtopo (str): 子拓扑名
        user_db_cli (UserDB): redis数据库连接
        __dict__ (dict): 包含子拓扑信息的类属性字典
    """

    # 写入就不会改变的数据表， 从数据库读取后可以缓存为实例属性
    _subtopo_common_table = ['plane_subtopo_list', 'subtopo_service']

    def __init__(self, user: str, topo: str, subtopo: str):
        """
        Args:
            user (str):  用户名
            topo (str):  拓扑名
            nes (dict):  节点名称列表

        Returns:
            None
        """
        self.user = user
        self.topo = topo
        self.subtopo = subtopo
        # 缓存 subtopo_service 的 properties： switches, hosts, routers
        # 缓存 plane_subtopo_list 的properties: NEs,  links, vxlanlinks
        # 这里初始化的时候是不需要缓存的， 因为服务创建的时候，用的也是这个类
        user_map_redis = UserMapRedis()
        self.user_db_cli = user_map_redis.get_user_db(user)
        user_map_redis.close()
        for common_table in self._subtopo_common_table:
            common_info = self.user_db_cli.get_value(common_table, subtopo)
            self.__dict__.update(common_info)

    def service_deploy(self):
        """
        按照层级顺序串行依次启动不同节点的内置服务，
        若启动失败，返回用户提示
        """
        error_msg = {}
        for i, layer in enumerate(SERVICE_HIERARCHURE):
            # 调用对应的层级创建服务
            result = getattr(self, '_start_{}_service'.format(layer))()
            error_msg.update(result)
            # 每次创建服务完成，更新进度值
            ProcessBarDeploy(i+4, self.user_db_cli, self.topo)
        return error_msg

    def _start_l2_service(self):
        """
        启动二层服务
        """
        error_msg = {}
        try:
            for sw in getattr(self, 'switches'):
                sw_table = f'{self.topo}_{sw}'
                sw_info = self.user_db_cli.get_all_values(sw_table)
                if sw_info['NEservice'] == 'docker':
                    sw_id = sw_info['NEid']
                    sw_con = docker_cli.containers.get(sw_id)
                    FLASK_LOGGER.debug(f'start service in {sw}: {sw_id}... ')
                    ovs = OvsRunner(sw, sw_info, sw_con, self.topo, self.user_db_cli)
                    ovs.start_service()
                elif sw_info['NEservice'] == 'kvm' or sw_info['NEservice'] == 'hardware':
                    pass
        except RuntimeError as e:
            error_msg['msg'] = e.args[0]
            error_msg['code'] = 0
        FLASK_LOGGER.info('==> l2 service task done')
        return error_msg if error_msg else SUCCESS_RESULT_MSG

    def _start_dpdk_l2fwd_service(self):
        """
        启动DPDK服务
        """

        error_msg = {}
        try:
            for dpdk in getattr(self, 'dpdks'):
                dpdk_table = f'{self.topo}_{dpdk}'
                dpdk_info = self.user_db_cli.get_all_values(dpdk_table)
                dpdk_id = dpdk_info['NEid']
                dpdk_ctn = docker_cli.containers.get(dpdk_id)
                dpdk_ctn_name = dpdk_ctn.name
                FLASK_LOGGER.debug(f'------------dpdkctnname------------- \n {dpdk_ctn_name}')
                dpdk_nums = self.user_db_cli.get_value(dpdk_table, 'dpdk_nums')
                FLASK_LOGGER.debug(f'start service in {dpdk}: {dpdk_id}... ')
                l2fwd = DpdkRunner(dpdk_nums[0:2], dpdk_ctn)
                l2fwd.start_service()
        except RuntimeError as e:
            error_msg['msg'] = e.args[0]
            error_msg['code'] = 0
            FLASK_LOGGER.error(error_msg)
        FLASK_LOGGER.info('==> DPDK service done')
        return error_msg if error_msg else SUCCESS_RESULT_MSG

    def _start_other_service(self):
        """
        启动端节点的节点服务
        """
        error_msg = {}
        try:
            for host in getattr(self, 'hosts'):
                host_conf = self.user_db_cli.get_all_values(f'{self.topo}_{host}')
                if host_conf['NEservice'] == 'docker':
                    cntr = docker_cli.containers.get(host_conf['NEid'])
                    host = HostRunner(host, host_conf, cntr)
                    host.start_service()
                elif host_conf['NEservice'] == 'kvm' or host_conf['NEservice'] == 'hardware':
                    pass
        except RuntimeError as e:
            error_msg['msg'] = e.args[0]
            error_msg['code'] = 0
        FLASK_LOGGER.info('==> other service done')
        return error_msg if error_msg else SUCCESS_RESULT_MSG

    def _start_tc_service(self):
        """启动链路TC服务

        """
        # 跨宿主机，如何只请求一次？规定只有源端能够发起请求
        # 与老版本兼容
        error_msg = {}
        try:
            resp_result = []
            # 这里是没有跨宿主机的链路
            for link in getattr(self, 'links'):
                # 获取节点 id 与 配置字典
                config_dict = self.user_db_cli.get_value(f'topo_list', f'{self.topo}') \
                        ['networks']['links'][f'{link}']['config']
                # 读取配置标志位，标志位不存在为老版本链路不做配置，为false也不做配置
                if 'flag' not in config_dict or config_dict['flag'] == False:
                    self.user_db_cli.set_value(f'{self.topo}_{link}', 
                                               'tcConfig',  {"flag":False, "src_con_flag":False, "trg_con_flag":False})
                else:
                    # 由于在写链路配置的时候没有提出统一的约定，导致单独配置请求的字段
                    # 信息不够合理，但是功能是可以正常进行的，为了避免重构带来的工作量
                    # 在后端进行了链路配置参数格式的相互转换，如果后面有需要，可
                    # 以考虑以此处 tcConfig 为准，修改链路配置的请求参数
                    # 
                    # "tcConfig": {
                    #     "flag": True,
                    #     "source": {
                    #         "linkchoice":"static",
                    #         "link": "link_l1",
                    #         "ne":r1
                    #         "bw_kbps": "2000",
                    #         "queue_size_bytes": "10000000",
                    #         "delay_us": "15",
                    #         "loss": "20",
                    #         "jitter_us": "6",
                    #         "correlation": "15",
                    #         "delay_distribution": "uniform"
                    #     },
                    #     "target": {
                    #         "linkchoice":"static",
                    #         "link": "link_l1",
                    #         "ne":s1
                    #         "bw_kbps": "2000",
                    #         "queue_size_bytes": "10000000",
                    #         "delay_us": "15",
                    #         "loss": "10",
                    #         "jitter_us": "6",
                    #         "correlation": "15",
                    #         "delay_distribution": "uniform"
                    #      }
                    # }
                    self.user_db_cli.set_value(f'{self.topo}_{link}', 'tcConfig', {"flag":False, "src_con_flag":False, "trg_con_flag":False})
                    config_list = [config_dict['source'], config_dict['target']]
                    info_dict = {'user':self.user, 'topo':self.topo, 'links':config_list}
                    tc_req_url = f'http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/link/'
                    req_method = getattr(requests, 'post')
                    resp_result.append(req_method(tc_req_url, json=info_dict))
            # 这里是跨宿主机的链路
            for vxlanlink in getattr(self, 'vxlanlinks'):
                # 提取链路的名字，通过"_"，切割字符串，要求链路的命名格式不能包含"_"
                link = vxlanlink.split('_')[1]
                # 查看链路的源
                source_ne = self.user_db_cli.get_value(f'{self.topo}_{link}','sourceNE')
                nes_list = getattr(self,'NEs')
                # 查看源节点是否在此服务器的NEs中，如果是则发起请求，如果不是则跳过 
                if source_ne in nes_list:
                    # 其余逻辑与上面一样
                    config_dict = self.user_db_cli.get_value(f'topo_list', f'{self.topo}') \
                            ['networks']['links'][f'{link}']['config']
                    if 'flag' not in config_dict or config_dict['flag'] == False:
                        self.user_db_cli.set_value(f'{self.topo}_{link}', 'tcConfig', {"flag":False, "src_con_flag":False, "trg_con_flag":False})
                    else:
                        self.user_db_cli.set_value(f'{self.topo}_{link}', 'tcConfig', {"flag":False, "src_con_flag":False, "trg_con_flag":False})
                        config_list = [config_dict['source'], config_dict['target']]
                        info_dict = {'user':self.user, 'topo':self.topo, 'links':config_list}
                        tc_req_url = f'http://{PROJ_CONFIG.master_ip}:{PROJ_CONFIG.master_port}/master/link/'
                        req_method = getattr(requests, 'post')
                        resp_result.append(req_method(tc_req_url, json=info_dict))
            for i, resp in enumerate(resp_result, 1):
                if resp.json()["code"] != 1:
                    raise RuntimeError(f'发往master的第{i}个请求失败')   
        except RuntimeError as e:
            error_msg['msg'] = e.args[0]
            error_msg['code'] = 0
        FLASK_LOGGER.info('==> tc service done')
        return error_msg if error_msg else SUCCESS_RESULT_MSG

    def _start_l3_service(self):
        """
        启动三层服务
        """
        error_msg = {}
        try:
            for rt in getattr(self, 'routers'):
                FLASK_LOGGER.debug(f'start router {rt} service...')
                rt_info = self.user_db_cli.get_all_values(f'{self.topo}_{rt}')
                if rt_info['NEservice'] == 'docker':
                    rt_id = rt_info['NEid']
                    container = docker_cli.containers.get(rt_id)
                    quagga = QuaggaRunner(rt, rt_info, container)
                    quagga.start_service()
                elif rt_info['NEservice'] == 'kvm' or rt_info['NEservice'] == 'hardware':
                    pass
        except RuntimeError as e:
            error_msg['msg'] = f'启动路由器失败：{e.args[0]}'
            error_msg['code'] = 0
        FLASK_LOGGER.info('==> l3 service task done')
        return error_msg if error_msg else SUCCESS_RESULT_MSG

    def close(self):
        """
        关闭用户Redis数据库连接
        """
        self.user_db_cli.close()

    def __del__(self):
        self.user_db_cli.close()