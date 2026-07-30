from abc import ABCMeta, abstractmethod

import json, re
import subprocess
import traceback
import docker
import docker.errors
import requests
from nsenter import Namespace
from fnmatch import fnmatch

from ..Implement_layer import LinkManager as link_manager
from ..Implement_layer.LinkManager.link_operate import delete_dpdk_br, \
    delete_ovs_port, shell_execute
from ..vemu_config.config import PROJ_CONFIG
from ..Service_layer.redisAPI import HostPortsAvailableRedis
from ..Service_layer.redis_error import TableNotExistError
from .deploy_error import LinkOvsBridgePortDeleteError
from ..tools.log_tools import FLASK_LOGGER
from ..tools.tools import chinese_to_pinyin
from ..tools.file_tool import check_directory
from ..Service_layer.vm_cmd_execer import vm_cmd_execer
import os

docker_cli = docker.from_env()
SUCCESS_RESULT_MSG = {'code': 0, 'msg': 'success'}
NONE_NET = docker_cli.networks.get('none')

KVM_IMAGE_DIR = PROJ_CONFIG.kvm_image_registry_dir
DEFAULT_HOST = PROJ_CONFIG.default_host_image
DEFAULT_ROUTER = PROJ_CONFIG.default_router_image
DEFAULT_SWITCH = PROJ_CONFIG.default_switch_image

def get_kvm_init_para(user, topo, ne, user_db_cli):
    """
    得到容器创建的初始化参数
    Args:
        ne (str): 节点名

    Returns:
        None
    """
    table_name = f'{topo}_{ne}'
    ne_id = user_db_cli.get_value(table_name, 'NEid')
    ne_image = user_db_cli.get_value(table_name, 'NEimage')
    ne_resource = user_db_cli.get_value(table_name, 'NEresource')
    ne_cpu = ne_resource['cpu']
    ne_mem = ne_resource['mem']
    # 总配置
    ne_service = user_db_cli.get_value(table_name, 'NEservice')
    ne_vmconfig = user_db_cli.get_value(table_name, 'NEvmconfig')
    # 端口从1开始
    ne_br = user_db_cli.get_value(table_name, 'NEnic')
    ne_type = ne_vmconfig['type']
    # (Wudx)镜像路径配置
    image_path = ne_vmconfig['kvm_image']['image_path']
    qcow2_size = ne_vmconfig['kvm_image']['qcow2_size']  # 仅image是iso文件时生效，默认为-1
    image_name = ne_vmconfig['image_name']
    init_conf = {'topo': topo, 'user':user,'name': ne_id, 
                    'type': ne_type, 'br': ne_br, 'image_path':image_path, 
                    'qcow2_size':qcow2_size, 'cpu': ne_cpu, 'mem': ne_mem,
                    'image_name': image_name}
    return init_conf


def get_image_init_para(**kwargs):
    """
    得到容器初始化的运行参数
    Args:
        Any 键值对

    Returns:
        dict

    """
    default_para = {'privileged': True, 'oom_kill_disable': True, 'detach': True,
                    'network_mode': 'bridge', 'stdin_open': True, 'tty': True}
    if PROJ_CONFIG.mount_host_clock_enabled:
        default_para['volumes'] = ['/etc/localtime:/etc/localtime:ro']
    for k, v in kwargs.items():
        default_para[k] = v
    return default_para


def get_container_exec_para(**kwargs):
    """
    得到运行容器的运行参数
    Args:
        Any 键值对

    Returns:
        dict

    """
    return {'privileged': True, 'detach': True}


def get_overlay_net(net_name):
    """
        通过network的名字得到docker.Network对象

        Args:
            net_name (str): The ID of the network.

        Returns:
            (:py:class:`Network`) The network.

        Raises:
            :py:class:`docker.errors.APIError`
                If the docker server returns an error.

    """
    try:
        overlay = docker_cli.networks.get(net_name)
    except docker.errors.NotFound:
        net_para = {'name': net_name, 'driver': 'overlay', 'attachable': True}
        overlay = docker_cli.networks.create(**net_para)
    return overlay


def delete_overlay_net(name):
    """
    删除overlay网络
    Args:
        name (str): The ID of the network.

    Returns:
        None

    Raises:
        :py:class:`docker.errors.APIError`
            If the docker server returns an error.

    """
    try:
        net = docker_cli.networks.get(name)
        net.remove()
    except docker.errors.NotFound:
        pass
    except docker.errors.APIError as e:
        FLASK_LOGGER.error(e.args)
        if e.status_code == 403:
            pass


def is_netstat_free(port):
    """
    检查端口是否被占用
    """
    ports_in_use = [int(port) for port in re.findall(r'(?:\d{5})', \
        shell_execute('netstat -nlt'))]
    return port not in ports_in_use


def find_available_port(re_cli, topo, hostname, container_port):
    """
    给一个网元上一个端口，匹配一个宿主机端口
    """
    db0 = HostPortsAvailableRedis()
    while True:
        # 获得一个可用端口
        port = db0.get_port()
        if is_netstat_free(port):
            break
        else:
            db0.return_port(port)
    db0.close()

    # 写入数据库，在字典中写入(容器内端口:宿主机端口)
    table = f'{topo}_port_mapping'
    ports = re_cli.get_value(table, hostname) \
        if re_cli.check_exist(table, hostname) else {}  # 网元的所有已存在的端口映射
    ports[container_port] = [port]                      # 因为端口上未配置端口映射，赋值一个list即可
    re_cli.set_value(table, hostname, ports)            # 保存已有配置到数据库

    return port


def get_port_mapping_config(ne_name, topo, re_cli):
    """
    在创建容器之前，更新端口映射参数

    Args:
        ne_name: 节点名
        topo:    拓扑名
        re_cli:  用户数据库对象

    Returns:
        conf_port_dict:   配置的端口字典
    """
    conf_port_dict = {}  # 配置端口映射字典
    db_port_dict  = {}  # 数据库端口映射字典
    # 获得数据库中原有的端口映射，是部署之前设置的端口映射，加入conf_port_dict
    try:
        table = f'{topo}_port_mapping'  # 端口映射表名
        re_cli.check_table_exist(table) # 若拓扑未部署，而端口映射已经设置
        if re_cli.check_exist(table, ne_name):  # 若给定容器有被映射的端口
            # 从数据库获得端口映射字典
            db_port_dict = re_cli.get_value(table, ne_name)
            # 检测宿主机端口是否可用
            db0 = HostPortsAvailableRedis()
            for host_port in db_port_dict.values():
                if db0.is_available_port(host_port) == False \
                    or is_netstat_free(host_port) == False:
                    FLASK_LOGGER.info(f'用户自定义端口存在不可用: {host_port}')
                    raise ValueError('用户自定义端口存在不可用！')
            # 将数据库中内容加入conf_port_dict
            for container_port, host_port in db_port_dict.items():
                conf_port_dict[f'{container_port}/tcp'] = host_port[0]

    except TableNotExistError:
        pass

    # 对默认配置里的端口，用户没有自定义，则进行自动分配
    auto_mapping_ports = []
    for port in PROJ_CONFIG.container_port_default:
        if port not in db_port_dict.keys():
            auto_mapping_ports.append(port)

    # 对需自动分配的端口，找到宿主机的可用端口
    for port in auto_mapping_ports:
        conf_port_dict[f'{port}/tcp'] = find_available_port(re_cli, \
            topo, ne_name, port)

    return conf_port_dict


class NECreator(metaclass=ABCMeta):
    """
    节点容器创建的抽象基类，必须定义 create_and_run()接口
    """

    @abstractmethod
    def create_and_run(self):
        raise NotImplementedError


class NERunner(metaclass=ABCMeta):
    """
    节点容器运行起服务的抽象基类，必须定义start_service()接口
    """

    @abstractmethod
    def start_service(self):
        raise NotImplementedError


class NEEditor(metaclass=ABCMeta):
    """
    编辑节点容器的抽象基类，必须定义modify()接口
    """
    @abstractmethod
    def modify(self):
        raise NotImplementedError


class LinkCreator(metaclass=ABCMeta):
    """
    链路创建的抽象基类，必须定义create_link() write_info()接口
    """

    @abstractmethod
    def create_link(self):
        raise NotImplementedError

    # 写入生成的信息
    @abstractmethod
    def write_info(self):
        raise NotImplementedError

    @staticmethod
    def _add_nic_to_ovs_ctr(ctr_id: str, nic: str) -> None:
        """
        将虚拟网卡添加到OVS网桥中
        Args:
            ctr_id (str): 容器ID.
            nic (str): 网桥ID

        Returns:
            None

        """
        cmd = f'sudo docker exec {ctr_id} ovs-vsctl add-port init-br0 {nic}'
        link_manager.shell_execute(cmd)


class DynamicNeCreator:
    """
    已创建拓扑节点容器动态创建类
    
    Attributes:
        user (str): 用户名
        topo (str): 拓扑名
        name (str): 节点名
        table (str): 节点的redis表名
        re_cli (UserDB): redis连接类
        info (dict): 节点信息
        init_para (dict): 用于创建节点的基础信息
        _cal_resource() : 计算节点资源限制的私有方法
        
    """

    def __init__(self, user: str, topo: str, name: str, re_cli):
        """
        将虚拟网卡添加到OVS网桥中
        Args:
            user (str): 用户名
            topo (str): 拓扑名
            name (str): 节点名
            re_cli (UserDB): redis连接类

        Returns:
            None

        """
        self.user = user
        self.topo = topo
        self.name = name
        self.table = f'{topo}_{name}'
        self.re_cli = re_cli
        self.info = self.re_cli.get_all_values(self.table)
        if self.info['NEservice'] == 'kvm':
            self.init_para = get_kvm_init_para(self.user, self.topo, self.name, self.re_cli)
        else:
            self.init_para = {'image': self.info['NEimage'], 'name': self.info['NEid'], 'hostname': name}
            self._cal_resource()
            # =================== Nvidia GPU =====================
            if self.info['NEimage'] in PROJ_CONFIG.nvidia_on_list:
                print('--------------GPU ON-------------------')
                self.init_para.update(get_image_init_para(**{'device_requests': [{'driver': 'nvidia', 'count': -1, 'capabilities': [['gpu']]}]}))
            else:
                print('---------------GPU OFF--------------------')
                self.init_para.update(get_image_init_para())
            self.init_para.update({"ports": \
                get_port_mapping_config(self.init_para['hostname'], self.topo, self.re_cli)})

    def _cal_resource(self):
        '''
        计算资源限制,更新配置文件
        '''
        config = {}
        ne_resource = self.info['NEresource']
        if (ne_resource) and PROJ_CONFIG.resource_limit_enable:
            if ne_resource['cpu']:
                cpu_period = 50000
                fraction = int(ne_resource['cpu']) * 0.01
                cpu_quota = int(cpu_period * fraction)
                config.update({"cpu_period": cpu_period, "cpu_quota": cpu_quota})
            if ne_resource['mem']:
                config.update({"mem_limit": int(ne_resource['mem']) * (10 ** 6)})
            self.init_para.update(config)

    def create_and_run(self):
        """
        启动容器节点的基本服务进程
        """
        ne_type = self.info['NEtype']
        ne_service = self.info['NEservice']
        if ne_service == 'kvm':
            self._ne_creator = DefaultNECreator(self.init_para)
            self._ne_creator.create_kvm()
        else:
            if ne_type in ["host", "router"]:
                self._ne_creator = DefaultNECreator(self.init_para)
                self._ne_creator.create_and_run()
            elif ne_type == 'switch':
                self._ne_creator = OvsCreator(self.init_para, info=self.info,
                    re_cli=self.re_cli, user=self.user, topo=self.topo)
                self._ne_creator.create_and_run()
            elif ne_type == 'controller':
                self._ne_creator = ControllerCreator(self.init_para, self.re_cli)
                topo = chinese_to_pinyin(self.topo)
                net = f'{self.user}-{topo}-sdn'
                self._ne_creator.create_and_run(net, self.table)
            elif ne_type == 'dpdk':
                dpdk_init_para = self.init_para
                dpdk_init_para['volumes'].append('/mnt/huge:/mnt/huge')
                dpdk_init_para['volumes'].append('/usr/local/var/run/openvswitch:/var/run/openvswitch')
                self._ne_creator = DpdkCreator(self.init_para, self.re_cli, self.table)
                self._ne_creator.create_and_run()

                ne_id = self.re_cli.get_value(self.table, 'NEid')
                dpdk_nums = self.re_cli.get_value(self.table, 'dpdk_nums')
                container = docker_cli.containers.get(ne_id)
                dpdk_runner = DpdkRunner(dpdk_nums, container)
                dpdk_runner.start_service()

    def close(self):
        """
        关闭Redis连接
        """
        self.re_cli.close()


class DefaultNECreator(NECreator):
    """
    默认的节点创建类
    
    Attributes:
        conf (dict): 容器节点创建的参数字典
    """

    def __init__(self, conf):
        """
        Args:
            conf (dict): 容器创建的初始化配置

        Returns:
            None

        """
        self.conf = conf

    def create_and_run(self):
        print("-----------------when create-----------------")
        print("conf: ", self.conf)
        ctn = docker_cli.containers.run(**self.conf)
        #获取容器pid
        pid = docker_cli.containers.get(ctn.id).attrs['State']['Pid']
        # 由于某些容器中没有权限执行ip link命令，需要nsenter进入网络命名空间
        with Namespace(pid, 'net'):
            # 以桥接模式启动后down掉网卡，需要时在开启
            code = shell_execute("sudo ip link set eth0 down")
        if code:
            raise RuntimeError('网元启动失败')
        
    def create_kvm(self):
        print("-----------------when create-----------------")
        print("conf: ", self.conf)
        # 声明全局变量避免报错
        global network
        if self.conf['image_path'] == 'default_image':
            origin_path = KVM_IMAGE_DIR + '/kvm_default/'
        elif self.conf['image_path'].startswith('self_upload_image'):
            image_name = self.conf['image_path'].split(':')[-1]
            origin_path = f"{KVM_IMAGE_DIR}/{self.conf['user']}/{image_name}"
            print("self_upload_image: ", origin_path)
        else:
            origin_path = self.conf['image_path']
        if self.conf['type'] == 'host':
            # 生成网桥
            for i in self.conf['br']:
                cmd_br = "sudo brctl addbr " + i
                cmd_br_up = "sudo ip link set " + i + " up"
                shell_execute(cmd_br)
                shell_execute(cmd_br_up)
            a = len(self.conf["br"])
            list_br = self.conf["br"]
            list_nic_name = [',target=' + item + '1' for item in self.conf["br"]]
            list_model = [',model=virtio']*a
            # 需要考虑多机的镜像文件存储！！！
            # 默认第一张网卡为NAT模式网卡
            network = '--network network=default,model=virtio '
            for item1, item2, item3 in zip(list_br, list_nic_name, list_model):
                network += f'--network bridge={item1}{item2}{item3} '
            if self.conf['image_path'] == 'default_image':
                # (Wudx)采用默认镜像路径时，从全局配置的镜像路径下按相应节点类型的获取镜像路径
                # --import方式创建host虚机,暂时只提供centos7镜像
                image_host = self.conf['image_name']
                origin_path = origin_path + self.conf['type'] + '/' + image_host
                image_path = f"{KVM_IMAGE_DIR}/{self.conf['user']}/kvm_image/{self.conf['topo']}/{self.conf['name']}.qcow2"
                check_directory(image_path)
                cmd3 = f"sudo cp {origin_path} {image_path}"
                cmd2 = f"sudo VIRTINSTALL_OSINFO_DISABLE_REQUIRE=1 virt-install --virt-type=kvm --name=" + self.conf["name"] + " --memory=" + self.conf["mem"] + " --vcpus=" + self.conf["cpu"] + \
                    f" --nographics --noautoconsole --import --disk path={image_path},format=qcow2 " + network
                # cmd3 = "sudo cp /home/adminis/vm_image/centos.qcow2 /home/adminis/vm_image/" + self.conf['name'] + ".qcow2"
                # cmd2 = "virt-install --virt-type=kvm --name=" + self.conf["name"] + " --memory=" + self.conf["mem"] + " --vcpus=" + self.conf["cpu"] + \
                #     " --nographics --noautoconsole --import --disk path=/home/adminis/vm_image/" + \
                #         self.conf["name"] + ".qcow2,format=qcow2 " + network
                print(cmd2)
                shell_execute(cmd3)
                shell_execute(cmd2)
            else:
                image_path = f"{KVM_IMAGE_DIR}/{self.conf['user']}/kvm_image/{self.conf['topo']}/{self.conf['name']}.qcow2"
                check_directory(image_path)
                if origin_path.endswith(".qcow2"):
                    # 自定义qcow2镜像，也需要复制
                    cmd3 = f"sudo cp {origin_path} {image_path}"
                    cmd2 = f"sudo VIRTINSTALL_OSINFO_DISABLE_REQUIRE=1 virt-install --virt-type=kvm --name=" + self.conf["name"] + " --memory=" + self.conf["mem"] + " --vcpus=" + self.conf["cpu"] + \
                        f" --nographics --noautoconsole --import --disk path={image_path},format=qcow2 " + network
                    print(cmd2)
                    shell_execute(cmd3)
                    shell_execute(cmd2)
                elif origin_path.endswith(".iso"):
                    # 不建议这种模式创建虚机，后续需要用户自己连进虚机进行系统安装
                    # 此模式需要指定console pty,target_type=serial和extra-args=console=ttyS0，才可以正常显示系统安装界面
                    # 自定义iso镜像
                    cmd2 = f"sudo VIRTINSTALL_OSINFO_DISABLE_REQUIRE=1 virt-install --virt-type=kvm --name=" + self.conf["name"] + " --memory=" + self.conf["mem"] + " --vcpus=" + self.conf["cpu"] + \
                        f" --console=pty,target_type=serial --extra-args=console=ttyS0 --nographics --noautoconsole --location={origin_path} --disk path={image_path},size={self.conf['qcow2_size']},format=qcow2 " + network
                    print(cmd2)
                    shell_execute(cmd2)
                    
        elif self.conf['type'] == 'router':
            # 生成网络不能复用？
            for i in self.conf['br']:
                cmd_br = "sudo brctl addbr " + i
                cmd_br_up = "sudo ip link set " + i + " up"
                shell_execute(cmd_br)
                shell_execute(cmd_br_up)
            a = len(self.conf["br"])
            list_br = self.conf["br"]
            list_nic_name = [',target=' + item + '1' for item in self.conf["br"]]
            # 呃呃
            list_model = [',model=virtio']*a
            # (Wudx)路由器也默认采用预留第一个端口启用NAT模式，后续根据需求可能需要更改！！！
            network = '--network network=default,model=virtio '
            for item1, item2, item3 in zip(list_br, list_nic_name, list_model):
                network += f'--network bridge={item1}{item2}{item3} '
            # network = ''
            # for item1, item2 in zip(list_br, list_model):
            #     network += f'--network bridge={item1}{item2} '
            # cmd2 = "echo 'virt-install --name=" + self.conf["name"] + " --machine=pc-1.0 --cpu=host --memory=" + self.conf["mem"] + " --vcpus=" + self.conf["cpu"] + \
            #     " --console=pty,target_type=serial --accelerate --nographics --import --disk path=/home/adminis/vm_image/ne40e.qcow2,format=qcow2" + \
            #         network + " --extra-args='console=ttyS0' ' >> /home/vemu4/gjh/" + self.conf["name"] + ".txt"
            # shell_execute(cmd2)
            
            # default基于import模式下创建虚机
            if self.conf['image_path'] == 'default_image':
                image_router = self.conf['image_name']
                # ne40e拥有两个控制口
                if image_router == 'ne40e.qcow2':
                    network = '--network network=default,model=virtio ' + network
                    network = '--network network=default,model=virtio ' + network
                origin_path = origin_path + self.conf['type'] + '/' + image_router
                image_path = f"{KVM_IMAGE_DIR}/{self.conf['user']}/kvm_image/{self.conf['topo']}/{self.conf['name']}.qcow2"
                check_directory(image_path)
                cmd3 = f"sudo cp {origin_path} {image_path}"
                cmd2 = f"sudo VIRTINSTALL_OSINFO_DISABLE_REQUIRE=1 virt-install --machine=pc-1.0 --cpu=host --name=" + self.conf["name"] + " --memory=" + self.conf["mem"] + " --vcpus=" + self.conf["cpu"] + \
                    f" --nographics --console pty,target_type=serial --accelerate --noautoconsole --import --disk path={image_path},format=qcow2 " + network
                print(cmd2)
                shell_execute(cmd3)
                shell_execute(cmd2)
                
            # (Wudx)使用自定义上传路由器镜像，由于华为大概率不会提供iso路由器镜像，此处暂时仅考虑qcow2格式镜像文件
            else:
                image_path = f"{KVM_IMAGE_DIR}/{self.conf['user']}/kvm_image/{self.conf['topo']}/{self.conf['name']}.qcow2"
                check_directory(image_path)
                
                # 自定义qcow2镜像，也需要复制
                cmd3 = f"sudo cp {origin_path} {image_path}"
                cmd2 = f"sudo VIRTINSTALL_OSINFO_DISABLE_REQUIRE=1 virt-install --machine=pc-1.0 --cpu=host --name=" + self.conf["name"] + " --memory=" + self.conf["mem"] + " --vcpus=" + self.conf["cpu"] + \
                    f" --nographics --console pty,target_type=serial --accelerate --noautoconsole --import --disk path={image_path},format=qcow2 " + network
                print(cmd2)
                shell_execute(cmd3)
                shell_execute(cmd2)
                
        # 交换机镜像存在问题，以下代码仅是为了代码的完整性
        elif self.conf['type'] == 'switch':
            # cmd1 = "touch /home/vemu4/gjh/" + self.conf["name"] + ".txt"
            # shell_execute(cmd1)
            # for i in self.conf['br']:
            #     cmd_br = "echo 'sudo brctl addbr br_" + self.conf["name"] + "_" + i + "' >> /home/vemu4/gjh/" + self.conf["name"] + ".txt"
            #     cmd_br_up = "echo 'sudo ip link set br_" + self.conf["name"] + "_" + i + " up' >> /home/vemu4/gjh/" + self.conf["name"] + ".txt"
            #     shell_execute(cmd_br)
            #     shell_execute(cmd_br_up)
            # a = len(self.conf["br"])
            # list_br =  ['br_' + self.conf["name"] + '_' + item for item in self.conf["br"]]
            # list_model = [',model=e1000']*a
            # network = ''
            # for item1, item2 in zip(list_br, list_model):
            #     network += f'--network bridge={item1}{item2} '
            # cmd2 = "echo 'virt-install --name=" + self.conf["name"] + " --machine=pc-1.0 --cpu=host --memory=" + self.conf["mem"] + " --vcpus=" + self.conf["cpu"] + \
            #     " --console=pty,target_type=serial --accelerate --nographics --import --disk path=/home/adminis/vm_image/ce12800.qcow2,format=qcow2" + \
            #         network + " --extra-args='console=ttyS0' ' >> /home/vemu4/gjh/" + self.conf["name"] + ".txt"
            # print(cmd2)
            # shell_execute(cmd2)
            for i in self.conf['br']:
                cmd_br = "sudo brctl addbr " + i
                cmd_br_up = "sudo ip link set " + i + " up"
                shell_execute(cmd_br)
                shell_execute(cmd_br_up)
            a = len(self.conf["br"])
            list_br = self.conf["br"]
            list_nic_name = [',target=' + item + '1' for item in self.conf["br"]]
            list_model = [',model=virtio']*a
            # 初始化两个NAT模式下的控制口
            network = '--network network=default,model=virtio '
            network +=network
            for item1, item2, item3 in zip(list_br, list_nic_name, list_model):
                network += f'--network bridge={item1}{item2}{item3} '
            if self.conf['image_path'] == 'default_image':
                image_swtich = self.conf['image_name']
                origin_path = origin_path + self.conf['type'] + '/' + image_swtich
                image_path = f"{KVM_IMAGE_DIR}/{self.conf['user']}/kvm_image/{self.conf['topo']}/{self.conf['name']}.qcow2"
                check_directory(image_path)
                cmd3 = f"sudo cp {origin_path} {image_path}"
                cmd2 = f"sudo VIRTINSTALL_OSINFO_DISABLE_REQUIRE=1 virt-install --name=" + self.conf["name"] + " --machine=pc-1.0 --cpu=host --memory=" + self.conf["mem"] + " --vcpus=" + self.conf["cpu"] + \
                    f" --console=pty,target_type=serial --noautoconsole --accelerate --nographics --import --disk path={image_path},format=qcow2 " + network
                print(cmd2)
                shell_execute(cmd3)
                shell_execute(cmd2)
            # (Wudx)使用自定义上传交换机镜像，由于华为大概率不会提供iso交换机镜像，此处暂时仅考虑qcow2格式镜像文件
            else:
                image_path = f"{KVM_IMAGE_DIR}/{self.conf['user']}/kvm_image/{self.conf['topo']}/{self.conf['name']}.qcow2"
                check_directory(image_path)
                
                # 自定义qcow2镜像，也需要复制
                cmd3 = f"sudo cp {origin_path} {image_path}"
                cmd2 = f"sudo VIRTINSTALL_OSINFO_DISABLE_REQUIRE=1 virt-install --machine=pc-1.0 --cpu=host --name=" + self.conf["name"] + " --memory=" + self.conf["mem"] + " --vcpus=" + self.conf["cpu"] + \
                    f" --nographics --console pty,target_type=serial --accelerate --noautoconsole --import --disk path={image_path},format=qcow2 " + network
                print(cmd2)
                shell_execute(cmd3)
                shell_execute(cmd2)
        else:
            raise ValueError("当前不支持该类型的镜像，请选择\"host\", \"router\", \"switch\"类型的镜像创建虚机")

class DpdkCreator(NECreator):
    """
    DPDK节点的创建类，包含一个容器和两个网桥
    
    Args:
        conf (dict): 容器创建初始化参数
        re_cli (UserDB): redis连接类
        nums (list): 网桥列表
        tap_num (int): 网桥上tap网卡数目
    """

    def __init__(self, conf, re_cli=None, table_name=None):
        """
        Args:
            conf (dict): 容器创建初始化参数
            re_cli (UserDB): redis连接类
            table_name (str): 对应redis中的表名

        Returns:
            None

        """
        self.conf = conf
        self.re_cli = re_cli
        self.nums = re_cli.get_value(table_name, 'dpdk_nums')
        self.tap_num = self.nums[2]

    def create_and_run(self):
        """
        创建并配置DPDK容器
        """
        # 启动dpdk容器，docker run -itd --privileged --name ${ctn_name} -v /mnt/huge:/mnt/huge -v
        # /usr/local/var/run/openvswitch:/var/run/openvswitch -v /etc/localtime:/etc/localtime:ro $ctn_image /bin/bash
        container = docker_cli.containers.run(**self.conf)
        pid = docker_cli.containers.get(container.id).attrs['State']['Pid']
        with Namespace(pid, 'net'):
            # 以桥接模式启动后down掉网卡，需要时在开启
            code = shell_execute("sudo ip link set eth0 down")
        if code:
            raise RuntimeError('网元启动失败')
        # 启动ovs-dpdk网桥和ovs-standard网桥
        #self.nums = (str(link_operate.generate_uuid_len_10()), str(link_operate.generate_uuid_len_10()))
        br_d_name = "br_d" + self.nums[0]
        br_s_name = "br_s" + self.nums[0]
        cmd_create_b1 = "sudo ovs-vsctl add-br " + br_d_name + " -- set bridge " + br_d_name + " datapath_type=netdev"
        cmd_create_b2 = "sudo ovs-vsctl add-br " + br_s_name
        FLASK_LOGGER.debug("--------create cmd:-----")
        FLASK_LOGGER.debug(cmd_create_b1)
        FLASK_LOGGER.debug(cmd_create_b2)
        FLASK_LOGGER.debug("----------")
        FLASK_LOGGER.debug(link_manager.shell_execute(cmd_create_b1))
        FLASK_LOGGER.debug("-----------------")
        FLASK_LOGGER.debug(link_manager.shell_execute(cmd_create_b2))
        
        tap_num = self.tap_num
        for num in self.nums[:-1]:
            cmd1 = "sudo ovs-vsctl add-port " + br_d_name + " vhostuser" + num + " -- set Interface vhostuser" + num + " type=dpdkvhostuser"
            cmd2 = "sudo ovs-vsctl add-port " + br_d_name + " virtiouser" + num + " -- set Interface virtiouser" + num + " type=dpdk " + \
                "options:dpdk-devargs=virtio_user" + num + ",path=/dev/vhost-net"
            cmd3 = "sudo ovs-vsctl add-port " + br_s_name + " tap" + str(tap_num)
            cmd4 = "sudo ip l s tap" + str(tap_num) + " up"
            link_manager.shell_execute(cmd1)
            link_manager.shell_execute(cmd2)
            link_manager.shell_execute(cmd3)
            link_manager.shell_execute(cmd4)
            tap_num = tap_num + 1
        br_d_info = link_manager.shell_execute("sudo ovs-ofctl show " + br_d_name)
        br_s_info = link_manager.shell_execute("sudo ovs-ofctl show " + br_s_name)
        FLASK_LOGGER.debug(f"info of dpdk-br:\n {br_d_info}")
        FLASK_LOGGER.debug(f"info of standard-br:\n {br_s_info}")

        FLASK_LOGGER.debug("-------------------show bridges--------------------")
        link_manager.shell_execute("sudo ovs-vsctl show")

        # #将信息写入redis数据库
        # ne_config = self.re_cli.get_value(table_name, 'NEconfig')
        # ne_config.update({'dpdk_ctn_name': })

        # 写将dpdk容器和ovs网桥的对应关系存入数据库，成为一个整体存储
        self.flow_set()

    def get_nums(self) -> tuple:
        return self.nums

    def flow_set(self):
        """
        配置流表
        """
        br_d_name = "br_d" + self.nums[0]
        br_s_name = "br_s" + self.nums[0]
        cmd1 = "sudo ovs-ofctl add-flow " + br_d_name + " in_port=virtiouser" + self.nums[0][0:5] + ",actions=output:vhostuser" + self.nums[0][0:6]
        cmd2 = "sudo ovs-ofctl add-flow " + br_d_name + " in_port=vhostuser" + self.nums[0][0:6] + ",actions=output:virtiouser" + self.nums[0][0:5]
        cmd3 = "sudo ovs-ofctl add-flow " + br_d_name + " in_port=virtiouser" + self.nums[1][0:5] + ",actions=output:vhostuser" + self.nums[1][0:6]
        cmd4 = "sudo ovs-ofctl add-flow " + br_d_name + " in_port=vhostuser" + self.nums[1][0:6] + ",actions=output:virtiouser" + self.nums[1][0:5]

        cmd5 = "sudo ovs-ofctl add-flow " + br_s_name + " in_port=1,actions=output:3"
        cmd6 = "sudo ovs-ofctl add-flow " + br_s_name + " in_port=3,actions=output:1"
        cmd7 = "sudo ovs-ofctl add-flow " + br_s_name + " in_port=2,actions=output:4"
        cmd8 = "sudo ovs-ofctl add-flow " + br_s_name + " in_port=4,actions=output:2"


        link_manager.shell_execute(cmd1)
        link_manager.shell_execute(cmd2)
        link_manager.shell_execute(cmd3)
        link_manager.shell_execute(cmd4)
        link_manager.shell_execute(cmd5)
        link_manager.shell_execute(cmd6)
        link_manager.shell_execute(cmd7)
        link_manager.shell_execute(cmd8)


class ControllerCreator(NECreator):
    """
    控制器创建类
    
    Attributes:
        conf (dict): 容器创建初始化参数
        re_cli (UserDB): redis连接类
    
    """

    def __init__(self, conf, re_cli=None):
        """
        Args:
            conf (dict): 容器创建初始化参数
            re_cli (UserDB): redis连接类

        Returns:
            None

        """
        self.conf = conf
        self.re_cli = re_cli

    def create_and_run(self, net_name=None, table_name=None):
        """
        创建并配置控制器容器

        Args:
            net_name (str): overlay网络名称
            table_name (str): redis数据表对应信息

        Returns:
            None

        """
        ctr = docker_cli.containers.run(**self.conf)
        # pid = docker_cli.containers.get(ctr.id).attrs['State']['Pid']
        # with Namespace(pid, 'net'):
        #     # 以桥接模式启动后down掉网卡，需要时再开启，控制器中没有ip link命令
        #     code = shell_execute("sudo ifconfig eth0 down")
        # if code:
        #     raise RuntimeError('网元启动失败')
        overlay = get_overlay_net(net_name)
        none_net = docker_cli.networks.get('none')
        catch_none_net_remove_exception(none_net.disconnect, ctr)
        overlay.connect(ctr)
        ctr_net_json = link_manager.shell_execute(
            "sudo docker inspect -f '{{json .NetworkSettings.Networks}}' "
            + ctr.id
        )
        ctr_net_dict = json.loads(ctr_net_json)
        ctr_ip = ctr_net_dict[net_name]["IPAddress"]
        ne_config = self.re_cli.get_value(table_name, 'NEconfig')
        ne_config.update({'overlay': net_name, 'ip': ctr_ip})
        self.re_cli.set_value(table_name, 'NEconfig', ne_config)

class OvsCreator(NECreator):
    """
    Ovs创建类
    
    Attributes:
        conf (dict): 容器创建初始化参数
        re_cli (UserDB): redis连接类
        info (dict): 前端传来的信息（由master存储进了数据库中，并由worker读取）
        user (str): 用户名
        topo (str): 拓扑名
    """
    def __init__(self, conf, info={}, re_cli=None, user=None, topo=None):
        """
        Args:
            conf (dict): 容器创建初始化参数
            info (dict): 前端传来的信息（由master存储进了数据库中，并由worker读取）
            re_cli (UserDB): redis连接类

        Returns:
            None

        """
        self.conf = conf
        self.re_cli = re_cli
        self.info = info
        self.user = user
        self.topo = topo

    def create_and_run(self):
        """
        创建并配置OVS容器

        Args:
            None

        Returns:
            None

        """
        container = docker_cli.containers.run(**self.conf)
        code = container.exec_run('ip link set eth0 down').exit_code
        if code:
            raise RuntimeError('网元启动失败')
        code = container.exec_run('service openvswitch-switch start').exit_code
        if code:
            raise RuntimeError('OVS启动失败')
        result = container.exec_run('ovs-vsctl add-br init-br0')
        output = result.output.decode()
        code = result.exit_code
        # 实验/镜像仓库需要，报错屏蔽
        # 如果由于用户使用自定镜像创建容器时已经启动了ovs
        # 则pass这种报错
        if re.match(('ovs-vsctl: cannot create a bridge named init-br0'
                    ' because a bridge named init-br0 already exists'), output):
            pass
        elif code:
            raise RuntimeError('OVS创建网桥失败')
        FLASK_LOGGER.debug(f"info: {self.info}")
        # TODO（wudx）：代码阅读提示
        # 以下代码主要是配合前端已部署的节点，在修改配置时进行使用
        # 相当于把服务启动那一块代码复用了过来
        if self.info:
            # 若传入了info，则info中就应有["NEconfig"]["config"]["stp"]等key，
            # 若没有这些key，抛出key error也是正常的
            self._set_stp(container, self.info["NEconfig"]["config"]["stp"])
            self._add_controller(container)
        
        if self.re_cli:
            # 没有传re_cli则是初次创建，此时不保存dpids
            self._save_dpid_to_db(container)

    def _set_stp(self, container, stp_flag):
        """
        设置STP
        
        Args:
            container: container对象
            stp_flag (bool): 是否开启STP

        Returns:
            None
        """
        cmd = f'ovs-vsctl set bridge init-br0 rstp_enable={str(stp_flag).lower()}'
        code = container.exec_run(cmd).exit_code
        if code:
            raise RuntimeError(f"设置stp失败，所执行命令为{cmd}")

    def _add_controller(self, container):
        """
        如果该OVS有配置控制器, 就配置控制器

        Args:
            container对象
        """
        ctrs = self.info['NEconfig']['config']['controllers']
        if not ctrs:
            return
        cmd = 'ovs-vsctl set-controller init-br0 '
        ctr = ctrs[0]
        ctr_db_name = f'{self.topo}_{ctr}'
        ctr_info = self.re_cli.get_value(ctr_db_name, 'NEconfig')
        # 得到该拓扑的 overlay net
        net = docker_cli.networks.get(ctr_info['overlay'])
        # 这里需要注意后面留出一个空格V
        FLASK_LOGGER.debug(ctr_info)
        for ctr in ctrs:
            ctr_db_name = f'{self.topo}_{ctr}'
            ctr_info = self.re_cli.get_value(ctr_db_name, 'NEconfig')
            # 这里需要注意后面留出一个空格
            # 同时需要读出来port的信息，port 是在config里面的信息
            port = ctr_info.get("config", {}).get("port", 6633)
            cmd += f"tcp:{ctr_info['ip']}:{port} "
        FLASK_LOGGER.debug(f'ctr cmd: {cmd}')
        catch_none_net_remove_exception(NONE_NET.disconnect, container)
        net.connect(container)
        code= container.exec_run(cmd).exit_code
        if code:
            raise RuntimeError(f'为OVS{self.name}添加控制器失败')

    def _save_dpid_to_db(self, container):
        """
        存储OVS的dpid至redis数据库

        Args:
            container: docker SDK container对象
        """
        cmd = "sh -c 'ovs-ofctl show init-br0 | grep dpid' "
        result = container.exec_run(cmd)
        dpid = result.output.decode().strip().split(':')[-1]
        
        sw_table = f'{self.topo}_{self.conf["hostname"]}'
        ne_conf = self.re_cli.get_value(sw_table, "NEconfig")
        ne_conf['config'].update({'dpid': dpid})
        self.re_cli.set_value(sw_table, 'NEconfig', ne_conf)


class DefaultRunner(NERunner):
    """
    起服务默认类
    
    Attributes:
        name (str): 容器名
        ne_conf (dict): 容器配置
        container (docker.Container) 容器代理类
    """
    def __init__(self, name: str, ne_conf: dict, container):
        """
        Args:
            name (str): 容器名
            ne_conf (dict): 容器配置
            container (docker.Container) 容器代理类

        Returns:
            None

        """
        self.container = container
        self.ne_conf = ne_conf
        self.name = name

    def start_service(self):
        """
        默认一定成功
        """
        return


class HostRunner(DefaultRunner):
    """
    主机类型节点启动服务代理类
    """
    def start_service(self):
        """
        启动服务
        """
        # 这里为0就是正常运行
        code = 0
        gw = self.ne_conf['NEgateway']
        if gw:
            FLASK_LOGGER.debug(f'add gateway info of host:{self.name}')
            code = self.container.exec_run(f'route add default gw {gw}', user = 'root').exit_code
        if code:
            raise RuntimeError(f'主机{self.ne_conf["NEid"]}添加网关失败')


class DpdkRunner(DefaultRunner):
    """
    DPDK容器启动服务代理类
    
    Attributes:
        nums (list): 网桥列表
        br_d_name (str): 目标网桥名称
        container (docker.Container) 容器代理类
    """
    def __init__(self, nums, container) -> None:
        self.nums = nums
        self.br_d_name = "br_d" + nums[0]
        self.container = container

    def start_service(self):
        mac1 = link_manager.get_interface_mac(self.br_d_name, "virtiouser"+self.nums[0][0:5]) 
        # split top 15 characters beacause only in this way can "ovs-ofctl" sift the mac of the interface
        mac2 = link_manager.get_interface_mac(self.br_d_name, "virtiouser"+self.nums[1][0:5])
        cmd_start_service = "./l2fwd -c 0x3 -n 1 --socket-mem 1024 --file-prefix l2fwd --no-pci \
            --vdev 'net_virtio_user" + self.nums[0] + ",mac=" + mac1 + ",path=/var/run/openvswitch/vhostuser" + self.nums[0] + \
            "' --vdev 'net_virtio_user" + self.nums[1] + ",mac=" + mac2 + ",path=/var/run/openvswitch/vhostuser" + self.nums[1] + \
            "' -- -p 0x3 --no-mac-updating"
        work_dir = "/root/dpdk/examples/l2fwd/build/"
        FLASK_LOGGER.debug('--------------cmd_start_service--------------------')
        FLASK_LOGGER.debug(cmd_start_service)
        service_start_code = self.container.exec_run(cmd_start_service, workdir=work_dir, detach=True).exit_code
        if service_start_code:
            raise RuntimeError('dpdk二层转发服务启动失败')


class OvsRunner(DefaultRunner):
    """
    OVS容器启动服务代理类
    
    Attributes:
        topo (str): 所属topo
        re_cli (UserDB): Redis数据库连接类
    """

    def __init__(self, name: str, ne_conf: dict, container, topo=None, re_cli=None):
        """
        Args:
            name (str): 容器名
            ne_conf (dict): 容器配置
            container (docker.Container) 容器代理类
            topo (str): 所属topo
            re_cli (UserDB): Redis数据库连接类

        Returns:
            None

        """
        super().__init__(name, ne_conf, container)
        self.topo = topo
        self.re_cli = re_cli

    def start_service(self):
        """
        启动服务
        """
        # 这里的异常捕捉留到外面
        # self._start_ovs_service()
        self._config_stp()
        self._add_link()
        self._add_controller()
        self._config_dpid()
        self._get_link_port()

    def _config_dpid(self):
        """
        修改OVS dpid
        """
        # dpid = self.ne_conf['NEconfig']['config'].get('dpid', None)
        # if not dpid:
        #     cmd = "sh -c 'ovs-ofctl show init-br0 | grep dpid' "
        #     result = self.container.exec_run(cmd)
        #     dpid = result.output.decode().strip().split(':')[-1]
        # else:
        #     pass
        
        # 此处不应该直接从NEconfig里读取dpid
        # 部署新拓扑时，每一个dpid都是重新生成的
        # 因此该无条件查看每个sw的dpid并向redis里赋值
        
        cmd = "sh -c 'ovs-ofctl show init-br0 | grep dpid' "
        result = self.container.exec_run(cmd)
        dpid = result.output.decode().strip().split(':')[-1]
        sw_table = f'{self.topo}_{self.name}'
        self.ne_conf['NEconfig']['config'].update({'dpid': dpid})
        self.re_cli.set_value(sw_table, 'NEconfig', self.ne_conf['NEconfig'])
    
    def _get_link_port(self):
        """
        得到链路的虚拟网卡名
        """
        cmd = "bash -c 'ovs-ofctl show init-br0 | grep addr'"
        result = self.container.exec_run(cmd).output.decode('utf-8')
        port_list = [port.strip().split(':')[0] for port in result.split("\n")]
        nic2port = {}
        for port in port_list:
            # 切割后最后一个字符串为空
            if port and "LOCAL" not in port:
                port_num, nic = port.split('(')[0], port.split('(')[1].split(')')[0]
                nic2port[nic] = port_num
        sw_table = f'{self.topo}_{self.name}'
        for key in self.ne_conf:
            # 更新数据库
            if key.startswith('link'):
                port = nic2port[self.ne_conf[key]['nic']]
                self.ne_conf[key].update({"port": port})
                self.re_cli.set_value(sw_table, key, self.ne_conf[key])

    def _config_stp(self):
        """
        是否开启STP协议
        """
        FLASK_LOGGER.debug( self.ne_conf['NEconfig'])
        if_stp = self.ne_conf['NEconfig']['config']['stp']
        # 这里stp不可能为空， 默认值是true, 还有是false
        cmd = f'ovs-vsctl set bridge init-br0 stp_enable={str(if_stp).lower()}'
        FLASK_LOGGER.debug(cmd)
        code = self.container.exec_run(cmd).exit_code
        if code:
            raise RuntimeError(f'配置OVS:{self.name} stp失败')

    def _add_link(self):
        """
        给新的OVS容器添加网卡
        """
        for key, value in self.ne_conf.items():
            if key.startswith('link'):
                nic = value.get('nic')
                if nic:
                    FLASK_LOGGER.debug(f'now add {nic} in {self.name}')
                    # code = self.container.exec_run(f'ovs-vsctl add-port init-br0 {nic}').exit_code
                    res = self.container.exec_run(f'ovs-vsctl add-port init-br0 {nic}')
                    output = res.output.decode()
                    code = res.exit_code
                    # 实验/镜像仓库需要，报错屏蔽
                    # 基于commit下来的ovs镜像再启动容器，其网桥和网卡都是依然启动的
                    if re.match((f'ovs-vsctl: cannot create a port named {nic} '
                                 f'because a port named {nic} already exists on bridge init-br0'), output):
                        pass
                    elif code:
                        raise RuntimeError(f'为OVS{self.name}添加网卡{nic}失败')

    def _add_controller(self):
        """
        如果该OVS有配置控制器, 就配置控制器
        """
        ctrs = self.ne_conf['NEconfig']['config']['controllers']
        print("ctrs:", ctrs)
        if not ctrs:
            return
        cmd = 'ovs-vsctl set-controller init-br0 '
        ctr = ctrs[0]
        ctr_db_name = f'{self.topo}_{ctr}'
        ctr_info = self.re_cli.get_value(ctr_db_name, 'NEconfig')
        # 得到该拓扑的 overlay net
        net = docker_cli.networks.get(ctr_info['overlay'])
        # 这里需要注意后面留出一个空格V
        FLASK_LOGGER.debug(ctr_info)
        for ctr in ctrs:
            ctr_db_name = f'{self.topo}_{ctr}'
            ctr_info = self.re_cli.get_value(ctr_db_name, 'NEconfig')
            # 这里需要注意后面留出一个空格
            # 同时需要读出来port的信息，port 是在config里面的信息
            port = ctr_info.get("config", {}).get("port", 6633)
            cmd += f"tcp:{ctr_info['ip']}:{port} "
        FLASK_LOGGER.debug(f'ctr cmd {self.name} {cmd}')
        catch_none_net_remove_exception(NONE_NET.disconnect, self.container)
        net.connect(self.container)
        code= self.container.exec_run(cmd).exit_code
        if code:
            raise RuntimeError(f'为OVS{self.name}添加控制器失败')


# RIP 协议配置
RIP_CONF_COMMON = '''
hostname rip
password zebra
debug rip events
debug rip packet
router rip
'''

# OSPF默认协议配置
OSPF_COMMON_CONF = """
hostname ospfd
password zebra
debug ospf event
debug ospf packet all
router ospf
"""

# BGP协议默认配置
BGP_COMMON_CONF = """
hostname bgpd
password zebra
log stdout
debug bgp events
debug bgp zebra
"""


class QuaggaRunner(DefaultRunner):
    """
    quagga容器启动代理类
    
    Attributes:
        ripd (dict): rip配置
        ospfd (dict): ospf配置
        bgpd (dict): bgp配置
    """
    def __init__(self, name: str, ne_conf: dict, container):
        """
        Args:
            name (str): 容器名
            ne_conf (dict): 容器配置
            container (docker.Container) 容器代理类

        Returns:
            None

        """
        super().__init__(name, ne_conf, container)
        self.ripd = ne_conf['NEconfig']['config']['rip']
        self.ospfd = ne_conf['NEconfig']['config']['ospf']
        self.bgpd = ne_conf['NEconfig']['config']['bgp']

    def start_service(self):
        """
        启动服务
        """
        # 首先启动zebra守护进程
        self._zebra_conf()
        if not (self.ripd or self.ospfd or self.bgpd):
            return
        if self.ripd['enable']:
            self._rip_conf()
        if self.ospfd['enable']:
            self._ospf_conf()
        if self.bgpd['enable']:
            self._bgp_conf()

    def _zebra_conf(self):
        """
        配置 zebra
        """
        zebra_conf = f'''
        hostname {self.name}
        password zebra
        debug zebra events
        debug zebra packet
        debug zebra rib'''
        FLASK_LOGGER.debug('zebra_conf...')
        FLASK_LOGGER.debug(zebra_conf)
        code = self.container.exec_run(f"echo '{zebra_conf}' > /etc/quagga/zebra.conf").exit_code
        if code:
            raise RuntimeError('写入zebra.conf 失败')
        code = self.container.exec_run('zebra -d').exit_code
        FLASK_LOGGER.debug(f'start zebra code: {code}')
        if code:
            raise RuntimeError('启动zebra服务失败')

    def _rip_conf(self):
        """
        配置 rip协议
        """
        version = self.ripd['version'] if self.ripd['version'] else 2
        conf = RIP_CONF_COMMON + f'version {version}\n'
        for net in self.ripd['networks']:
            conf += f'network {net}\n'
        for neighbor in self.ripd['neighbors']:
            conf += f'neighbor {neighbor}\n'
        FLASK_LOGGER.debug('rip_conf...')
        FLASK_LOGGER.debug(conf)
        # 将文件内容重定向到 ripd.conf
        code = self.container.exec_run(
            'sh -c "echo \'{}\' > /etc/quagga/ripd.conf"'.format(conf)).exit_code
        if code:
            raise RuntimeError('写入ripd.conf 失败')
        code = self.container.exec_run('ripd -d').exit_code
        if code:
            raise RuntimeError('启动RIP路由协议失败')

    def _ospf_conf(self):
        """
        配置OSPF协议
        """
        conf = OSPF_COMMON_CONF
        FLASK_LOGGER.debug('ospf_conf...')
        if self.ospfd['router_id']:
            conf += f"ospf router-id {self.ospfd['router_id']}\n"
        for net, area in self.ospfd['networks']:
            conf += f"network {net} area {area}\n"
        try:
            for area, ranges in self.ospfd['areas'].items():
                for range in ranges:
                    conf += f"area {area} range {range}\n"
        except KeyError:
            pass
        FLASK_LOGGER.debug('ospf_conf...')
        FLASK_LOGGER.debug(conf)
        # 将文件内容重定向到 ospfd.conf
        code = self.container.exec_run(
            'sh -c "echo \'{}\' > /etc/quagga/ospfd.conf"'.format(conf)).exit_code
        if code:
            raise RuntimeError('写入ospfd.conf 失败')
        code = self.container.exec_run('ospfd -d').exit_code
        if code:
            raise RuntimeError('启动OSPF路由协议失败')

    def _bgp_conf(self):
        """
        配置 BGP协议
        """
        asn = self.bgpd['asn']
        conf = BGP_COMMON_CONF + f'router bgp {asn}\n'
        if self.bgpd['router_id']:
            conf += f"bgp router-id {self.bgpd['router_id']}\n"
        for net in self.bgpd['networks']:
            conf += f'network {net}\n'
        for neighbor, remote_as in self.bgpd['neighbors']:
            conf += f'neighbor {neighbor} remote-as {remote_as}\n'
        FLASK_LOGGER.debug('bgp_conf...')
        FLASK_LOGGER.debug(conf)
        code = self.container.exec_run(
            'sh -c "echo \'{}\' > /etc/quagga/bgpd.conf"'.format(conf)).exit_code
        if code:
            raise RuntimeError('写入bgpd.conf 失败')
        code = self.container.exec_run('bgpd -d').exit_code
        if code:
            raise RuntimeError('启动BGP路由协议失败')

def _create_link(topo, link_name, redis_cli):
    """对VethLink类wait_task_done的改写"""
    table_name = f'{topo}_{link_name}'
    info = redis_cli.get_all_values(table_name)
    src_id, tgt_id = info['sourceID'], info['targetID']
    src, tgt = info['sourceNE'], info['targetNE']
    src_type, tgt_type = info['sourceType'], info['targetType']

    # 创建
    if src_type == "dpdk/l2fwd" or src_type == 'dpdk':
        table_name = f'{topo}_{src}'
        dpdk_nums = redis_cli.get_value(table_name, 'dpdk_nums')
        result = link_manager.create_link("br_s" + dpdk_nums[0], tgt_id, src, tgt,
                                          info['targetIP'], src_type='bridge')
    elif tgt_type == "dpdk/l2fwd" or tgt_type == 'dpdk':
        table_name = f'{topo}_{tgt}'
        dpdk_nums = redis_cli.get_value(table_name, 'dpdk_nums')
        result = link_manager.create_link(src_id, "br_s" + dpdk_nums[0], src, tgt,
                                          info['sourceIP'], dst_type='bridge')
    else:
        result = link_manager.create_link(src_id, tgt_id, src, tgt,
                                          info['sourceIP'], info['targetIP'])
    if result.get('error_msg'):
        raise RuntimeError(f'创建veth链路{link_name}失败')

    # 原本的write_info阶段
    if src_type == "dpdk/l2fwd" or src_type == 'dpdk':
        src_port, src_mac = result['bridge']['nic'], result['bridge']['mac']
        tgt_port, tgt_mac = result['ctn']['nic'], result['ctn']['mac']
    elif tgt_type == "dpdk/l2fwd" or tgt_type == 'dpdk':
        src_port, src_mac = result['ctn']['nic'], result['ctn']['mac']
        tgt_port, tgt_mac = result['bridge']['nic'], result['bridge']['mac']
    else:
        src_port, src_mac = result[src_id]['nic'], result[src_id]['mac']
        tgt_port, tgt_mac = result[tgt_id]['nic'], result[tgt_id]['mac']
    redis_cli.set_value(table_name, 'sourcePort', src_port)
    redis_cli.set_value(table_name, 'targetPort', tgt_port)
    src_table = f'{topo}_{src}'
    link_key = f'link_{link_name}'
    FLASK_LOGGER.debug(f'current link_key is {link_key}')
    tgt_table = f'{topo}_{tgt}'
    src_link_info = redis_cli.get_value(src_table, link_key)
    src_link_info.update({'nic': src_port, 'mac': src_mac, 
        'name': f"{src}{tgt}"})
    tgt_link_info = redis_cli.get_value(tgt_table, link_key)
    tgt_link_info.update({'nic': tgt_port, 'mac': tgt_mac,
        'name': f"{tgt}{src}"})
    redis_cli.set_value(src_table, link_key, src_link_info)
    redis_cli.set_value(tgt_table, link_key, tgt_link_info)
    return 1

class VethLink(LinkCreator):
    """
    动态创建 veth-pair 创建代理类
    
    Attributes:
        topo (str): 拓扑名
        name (str): 链路名
        table (str): 链路信息表名
        re_cli (UserDB): Redis数据库连接
        info (dict): 链路信息
        src_id (str): 源端节点ID
        tgt_id (str): 目的端节点ID
        src (str): 源端节点名称
        tgt (str): 目的端节点名称
        src_type (str): 源端节点类型
        tgt_type (str): 目的端节点类型
        
    """
    def __init__(self, topo: str, name: str, re_cli):
        """
        Args:
            topo (str): 拓扑名
            name (dict): 链路名
            re_cli (UserDB): Redis数据库连接

        Returns:
            None

        """
        self.topo = topo
        self.name = name
        self.table = f'{topo}_{name}'
        self.re_cli = re_cli
        self.info = re_cli.get_all_values(self.table)
        self.cn = self.info['parallel']
        self.src_id, self.tgt_id = self.info['sourceID'], self.info['targetID']
        self.src, self.tgt = self.info['sourceNE'], self.info['targetNE']
        self.src_table = f'{topo}_{self.src}'
        self.tgt_table = f'{topo}_{self.tgt}'
        self.src_info = re_cli.get_all_values(self.src_table)
        self.tgt_info = re_cli.get_all_values(self.tgt_table)
        self.src_type, self.tgt_type = self.info['sourceType'], self.info['targetType']
        self.src_service, self.tgt_service = self.info['sourceservice'], self.info['targetservice']
        self.src_veth, self.tgt_veth = self.info['sourceveth'], self.info['targetveth']
        self.vm_src_port, self.vm_tgt_port = self.info.get('VMsourcePort', ''), self.info.get('VMtargetPort', '')
        # 确保tcConfig的存在，并为其提供默认值
        if 'tcConfig' not in self.info:
            self.info['tcConfig'] = {"flag": False, "src_con_flag": False, "trg_con_flag": False}

    def create_link(self):
        """
        创建链路
        Args:
            None

        Returns:
            result (dict): 执行结果

        Raise:
            RuntimeError: 执行失败的返回结果
        """
        if self.src_service == 'docker' and self.tgt_service == 'docker':
            if self.src_type == "dpdk/l2fwd" or self.src_type == 'dpdk':
                table_name = f'{self.topo}_{self.src}'
                dpdk_nums = self.re_cli.get_value(table_name, 'dpdk_nums')
                result = link_manager.create_link("br_s" + dpdk_nums[0], self.tgt_id, self.src, self.tgt,
                                                    1, self.info['targetIP'], src_type='bridge')
            elif self.tgt_type == "dpdk/l2fwd" or self.tgt_type == 'dpdk':
                table_name = f'{self.topo}_{self.tgt}'
                dpdk_nums = self.re_cli.get_value(table_name, 'dpdk_nums')
                result = link_manager.create_link(self.src_id, "br_s" + dpdk_nums[0], self.src, self.tgt,
                                                    1, self.info['sourceIP'], dst_type='bridge')
            else:
                result = link_manager.create_link(self.src_id, self.tgt_id,self.src, self.tgt,
                                            self.cn, self.info['sourceIP'], self.info['targetIP'])
                
        elif self.src_service == 'kvm' and self.tgt_service == 'kvm':
            src_int, tgt_int = self.src_info['NEnic'][self.vm_src_port - 1], self.tgt_info['NEnic'][self.vm_tgt_port - 1]
            result = link_manager.create_kvm_link(self.src_id, self.tgt_id, self.src_veth, self.tgt_veth, src_int, tgt_int)
        elif self.src_service =='kvm' and self.tgt_service == 'docker':
            # 链路中docker的一侧所指定的端口index不生效，而IP仅对docker一侧生效
            src_int = self.src_info['NEnic'][self.vm_src_port - 1]
            result = link_manager.create_kd_link(self.src_id, self.tgt_id, self.src, self.tgt, self.src_veth, src_int, self.info['targetIP'], self.cn)
        elif self.src_service =='docker' and self.tgt_service == 'kvm':
            tgt_int = self.tgt_info['NEnic'][self.vm_tgt_port - 1]
            result = link_manager.create_dk_link(self.src_id, self.tgt_id, self.src, self.tgt, self.tgt_veth, tgt_int, self.info['sourceIP'], self.cn)
        # print(result)
        if result.get('error_msg'):
            raise RuntimeError(f'创建veth链路{self.name}失败')
        return result

    # 检查链路两端节点是否为OVS， 若为OVS， 网卡信息加入OVS的 init-br0
    # 但是在拓扑创建的时候， 是不需要调用该方法的，OVS自己会有add_link的操作
    def add_nic_to_ovs_ctr(self, result: dict):
        """
        将veth pair 虚拟网卡加入到OVS容器中
        
        Args:
            result (dict): veth pair创建信息及结果
        """
        if self.src_type == 'switch':
            src_port = result[self.src_id]['nic']
            self._add_nic_to_ovs_ctr(self.src_id, src_port)
        if self.tgt_type == 'switch':
            tgt_port = result[self.tgt_id]['nic']
            self._add_nic_to_ovs_ctr(self.tgt_id, tgt_port)

    def write_info(self, result=None):
        """
        向Redis中写入链路信息
        进一步更新链路表中的mac, nic, src_port, tgt_port等信息
        
        Args:
            result (dict): veth pair创建信息及结果
        """
        #if one of the node's type is dpdk/l2fwd, then only need to store the container's info, 
        # don't need the info of the bridge of dpdk
        if self.src_type == "dpdk/l2fwd" or self.src_type == 'dpdk':
            # src_port, src_mac = result['ctn']['nic'], result['ctn']['mac']
            # tgt_port, tgt_mac = "",""
            src_port, src_mac = result['bridge']['nic'], result['bridge']['mac']
            tgt_port, tgt_mac = result['ctn']['nic'], result['ctn']['mac']
        elif self.tgt_type == "dpdk/l2fwd" or self.tgt_type == 'dpdk':
            # src_port, src_mac = "", ""
            # tgt_port, tgt_mac = result['ctn']['nic'], result['ctn']['mac']
            src_port, src_mac = result['ctn']['nic'], result['ctn']['mac']
            tgt_port, tgt_mac = result['bridge']['nic'], result['bridge']['mac']
        else:
            src_port, src_mac = result[self.src_id]['nic'], result[self.src_id]['mac']
            tgt_port, tgt_mac = result[self.tgt_id]['nic'], result[self.tgt_id]['mac']
        # 写入link_table
        self.re_cli.set_value(self.table, 'sourcePort', src_port)
        self.re_cli.set_value(self.table, 'targetPort', tgt_port)
        # 写入 <toponame>_<nename>
        src_table = f'{self.topo}_{self.src}'
        link_key = f'link_{self.name}'
        FLASK_LOGGER.debug(f'current link_key is {link_key}')
        tgt_table = f'{self.topo}_{self.tgt}'
        src_link_info = self.re_cli.get_value(src_table, link_key)
        src_link_info.update({'nic': src_port, 'mac': src_mac})
        tgt_link_info = self.re_cli.get_value(tgt_table, link_key)
        tgt_link_info.update({'nic': tgt_port, 'mac': tgt_mac})
        self.re_cli.set_value(src_table, link_key, src_link_info)
        self.re_cli.set_value(tgt_table, link_key, tgt_link_info)
        # 如果链路配置未设置，确保tcConfig字段在数据库中
        if self.info['tcConfig']['flag'] == False:
            self.re_cli.set_value(self.table, 'tcConfig', self.info['tcConfig'])
        
    def create_link_and_write_info(self):
        """
        wudx
        该函数为合并华为代码时，考虑继续使用多进程创建veth_pair的封装
        """
        res = self.create_link()
        self.write_info(res)


class VxLANLink(LinkCreator):
    """
    动态创建 vxlan链路 创建代理类


    vxlan的创建逻辑
    1. 创建并生成网卡信息
    2. 写表
    3. 将两端网卡加入init-br0
    
    Attributes:
        topo (str): 拓扑名
        name (dict): 链路名
        re_cli (UserDB): Redis数据库连接
        table (str): vxlan的信息表
        info (dict): vxlan的信息
        src_id (str): 源节点ID
        src_type (str): 源节点类型
        source (str): 源节点名称
        target (str): 目的节点名称
    """
    def __init__(self, topo: str, name: str, re_cli):
        """
        Args:
            topo (str): 拓扑名
            name (dict): 链路名
            re_cli (UserDB): Redis数据库连接

        Returns:
            None

        """
        self.topo = topo
        self.name = name
        self.re_cli = re_cli
        # 这里的表应该是vxlan的表
        self.table = f'{topo}_{name}'
        info = re_cli.get_all_values(self.table)
        self.info = info
        self.src_id = re_cli.get_value(f'{self.topo}_{info["source"]}', 'NEid')
        self.src_type = re_cli.get_value(f'{self.topo}_{info["source"]}', 'NEtype')
        self.src_service = re_cli.get_value(f'{self.topo}_{info["source"]}', 'NEservice')
        self.vlan = ''
        if self.src_service == 'hardware':
            ne_config = re_cli.get_value(f'{self.topo}_{info["source"]}', 'NEconfig')
            self.vlan = ne_config['config']['vlan']
        self.source = info['source']
        self.sourceveth = info['sourceveth']
        self.targetveth = info['targetveth']
        targetNE = re_cli.get_value(f'{self.topo}_{info["partof"]}', 'targetNE')
        sourceNE = re_cli.get_value(f'{self.topo}_{info["partof"]}', 'sourceNE')
        self.parallel = re_cli.get_value(f'{self.topo}_{info["partof"]}', 'parallel')
        source_service = re_cli.get_value(f'{self.topo}_{sourceNE}', 'NEservice')
        target_service = re_cli.get_value(f'{self.topo}_{targetNE}', 'NEservice')
        if source_service == 'hardware' or target_service == 'hardware':
            self.hardware = True
        else:
            self.hardware = False
        self.src_port = ""
        if self.source == targetNE :
            self.target = sourceNE
            if self.src_service ==  'kvm':
                self.src_port = re_cli.get_value(f'{self.topo}_{info["partof"]}', 'VMtargetPort')
        if self.source == sourceNE:
            self.target = targetNE
            if self.src_service ==  'kvm':
                self.src_port = re_cli.get_value(f'{self.topo}_{info["partof"]}', 'VMsourcePort')
        self.nics = re_cli.get_value(f'{self.topo}_{info["source"]}', 'NEnic')
        

    def create_link(self):
        """
        创建链路
        Args:
            None

        Returns:
            result (dict): 执行结果

        Raise:
            RuntimeError: 执行失败的返回结果

        """
        if isinstance(self.src_port, int):
            src_nic = self.nics[self.src_port - 1]
        else:
            src_nic = 'default'
        if self.src_type == 'dpdk':
            dpdk_nums = self.re_cli.get_value(f'{self.topo}_{self.info["source"]}', 'dpdk_nums')
            result = link_manager.create_vxlan("br_s" + dpdk_nums[0], self.info['target'], 
                                                self.info['remoteIP'], self.info['VNI'])
        if self.hardware:
            result = link_manager.create_hardware_vxlan(self.src_id, self.info['sourceIP'], self.info['target'],
                                           self.info['remoteIP'], self.info['VNI'], self.target, self.src_service, src_nic, self.sourceveth, self.targetveth, self.parallel, self.vlan)
        else:
            result = link_manager.create_vxlan(self.src_id, self.info['sourceIP'], self.info['target'],
                                           self.info['remoteIP'], self.info['VNI'], self.target, self.src_service, src_nic, self.sourceveth, self.targetveth, self.parallel)
        if result.get('error_msg'):
            raise RuntimeError(f'创建vxlan链路{self.name}失败')
        return result

    def add_nic_to_ovs_ctr(self, result):
        """
        如果相连节点为交换机OVS类型， 需要将网卡加入到网桥上
        """
        if self.src_type == 'switch':
            src_port = result[self.src_id]
            self._add_nic_to_ovs_ctr(self.src_id, src_port)

    def write_info(self, result=None):
        """
        向Redis中写入链路信息
        Args:
            result (dict): vxlan-link创建信息及结果
        """
        FLASK_LOGGER.debug(result)
        src_port = result[self.src_id]
        # 写入toponame_vxlanlinkname toponame_linkname topoName_NEname
        self.re_cli.set_value(self.table, 'sourcePort', src_port)
        ori_link_table = '{}_{}'.format(self.topo, self.info['partof'])
        # 通过source_id 来反查表项的前缀
        temp = self.re_cli.get_value(ori_link_table, 'sourceID')
        # 因为这里的source和targe在节点处没有区分
        key_prefix = 'source' if temp == self.src_id else 'target'
        self.re_cli.set_value(ori_link_table, '{}Port'.format(key_prefix), src_port)
        topo_ne_table = f"{self.topo}_{self.info['source']}"
        ne_link_key = f'link_{self.info["partof"]}'

        FLASK_LOGGER.debug("----------table_name--------------")
        FLASK_LOGGER.debug(topo_ne_table)
        FLASK_LOGGER.debug(ne_link_key)

        ne_detail = self.re_cli.get_value(topo_ne_table, ne_link_key)
        ne_detail['nic'] = src_port
        ne_detail['mac'] = result['src_mac']

        # 从数据库获得目的节点名称
        link_table = f"{self.topo}_{self.info['partof']}"
        src = self.re_cli.get_value(link_table, 'sourceNE')
        tgt = self.re_cli.get_value(link_table, 'targetNE')
        fromNE = self.info['source']
        toNE = src if fromNE != src else tgt
        # ne_detail['name'] = f"{fromNE}{toNE}1111"

        self.re_cli.set_value(topo_ne_table, ne_link_key, ne_detail)


class DefaultNEDeleter(object):
    """
    默认节点节点删除代理类
    
    Attributes:
        ne_info (dict): 节点的数据库信息
    """
    def __init__(self, ne_info):
        """
        Args:
            ne_id (str): 节点ID

        Returns:
            None
        """
        self.ne_info = ne_info
        self.topo = ne_info.get('topo', None)
        self.user = ne_info.get('user', None)
        self.ne_name = ne_info.get('ne_name', None)
        self.ne_info['NEservice'] = ne_info.get('NEservice', 'docker')
        

    # 直接删除与动态删除已做区分，stop_and_delete 和 stop_and_delete_dynamic。
    # 区分开来是因为动态删除需要考虑的更多，直接删除拓扑更为简洁。
    # 任何涉及到删除操作的修改，都需要考虑二者，在两个地方均需修改，否则可能出错。
    # 如果要合并，需要解决二者参数的BUG问题 。    -- wtx
    def stop_and_delete(self):

        """
        停止并删除节点
        
        Returns:
            None

        """
        if self.ne_name != 'default':
            service = self.ne_info['NEservice']
            if service == 'docker':
                try:
                    ne = docker_cli.containers.get(self.ne_info['NEid'])
                    ne.stop()
                    ne.remove(force=True)
                    # 移除与dpdk相关的网桥
                    if 'dpdk_nums' in self.ne_info:
                        dpdk_nums = self.ne_info['dpdk_nums']
                        result1 = delete_dpdk_br(f'br_d{dpdk_nums[0]}')
                        result2 = delete_dpdk_br(f'br_s{dpdk_nums[0]}')
                        if result1['code'] == 0 or result2['code'] == 0:
                            FLASK_LOGGER.error(f"{result1['error_msg']} { result2['error_msg']}")

                except docker.errors.NotFound:
                    pass
                except docker.errors.APIError as e:
                    FLASK_LOGGER.error(e)
                except requests.exceptions.HTTPError as e:
                    pass
                except LinkOvsBridgePortDeleteError:
                    traceback.print_exc()
                #Kc
                #如果容器之前就已经被删除，会导致docker_cli请求超时
                #增加一个健壮性的判断
                except requests.exceptions.ReadTimeout:  # 捕获ReadTimeout异常
                    FLASK_LOGGER.error("Docker API call read timeout.")
                except Exception as e:  # 捕获所有其他异常
                    FLASK_LOGGER.error(f"Unexpected error: {e}")
                    traceback.print_exc()
            elif service == 'kvm':
                try:
                    cmd1 = "virsh destroy " + self.ne_info['NEid']
                    cmd2 = "virsh undefine " + self.ne_info['NEid']
                    # (Wudx)更改镜像路径
                    # cmd3 = "sudo rm -rf /home/adminis/vm_image/" + self.ne_info['NEid'] + ".qcow2"
                    # 仅删除相应节点镜像，而不删除用户的上传的原始镜像
                    cmd3 = f"sudo rm -rf {KVM_IMAGE_DIR}/{self.user}/kvm_image/{self.topo}/{self.ne_info['NEid']}.qcow2"
                    shell_execute(cmd1)
                    shell_execute(cmd2)
                    shell_execute(cmd3)
                except Exception as e:
                    FLASK_LOGGER.error(e)
                # 更改删网桥的逻辑
                # ne_interface = self.ne_info['NEinterface']
                # ne_br = [d['name'] for d in ne_interface]
                ne_br = self.ne_info['NEnic']
                for i in ne_br:
                    try:
                        cmd_down = "sudo ip link set " + i + " down"
                        cmd_del = "sudo brctl delbr " + i
                        shell_execute(cmd_down)
                        shell_execute(cmd_del)
                    except subprocess.CalledProcessError:
                        pass
        else:
            veth = self.ne_info['veth']
            for i in veth:
                cmd = "sudo ip link show type veth"
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                output = result.stdout
                if i in output:
                    cmd1 = "sudo ip link delete " + i
                    shell_execute(cmd1)
                else:
                    pass



    # 直接删除与动态删除已做区分，stop_and_delete 和 stop_and_delete_dynamic。
    # 区分开来是因为动态删除需要操作的更多，直接删除拓扑更为简洁(直接停掉容器)。
    # 任何涉及到网元删除操作的修改，都需要考虑二者，在两个地方均需修改，否则可能出错。
    # 如果要合并，需要解决二者参数的BUG问题 。    -- wtx
    def stop_and_delete_dynamic(self, topo, ne_name, redis_cli):
        """
        动态停止并删除节点

        Args:
            topo: 拓扑名
            ne_name： 网元名
            redis_cli: redis数据库用户连接实例

        Returns:
            None
        """
        if self.ne_info['NEservice'] == 'docker':
            try:
                ne = docker_cli.containers.get(self.ne_info['NEid'])
                ne.stop()
                ne.remove(force=True)
                # 移除与dpdk相关的网桥
                if 'dpdk_nums' in self.ne_info:
                    dpdk_nums = self.ne_info['dpdk_nums']
                    result1 = delete_dpdk_br(f'br_d{dpdk_nums[0]}')
                    result2 = delete_dpdk_br(f'br_s{dpdk_nums[0]}')
                    if result1['code'] == 0 or result2['code'] == 0:
                        FLASK_LOGGER.error(f"{result1['error_msg']} { result2['error_msg']}")
                # 移除ovs网桥的端口
                # 只需要考虑相连接的节点是否是 ovs，如果是则需要删除
                # 需要借用 nic名，随着nic命名规则更改而失效，暂时也没有其它办法
                for key, value in self.ne_info.items():
                    # 选择链路
                    if key.startswith('link'):
                        # 选择相连的 ovs 节点
                        if value['nic'].startswith('tos'):
                            # nic 名为to{ne}，通过 nic 获取网元名字
                            target_ne = value['nic'][2:]
                            table_name = f'{topo}_{target_ne}' 
                            ne_id = redis_cli.get_value(table_name, 'NEid')
                            ovs_port = f'to{ne_name}'
                            delete_ovs_port(ne_id, ovs_port, f'init-br0')

            except docker.errors.NotFound:
                pass
            except docker.errors.APIError as e:
                FLASK_LOGGER.error(e)
            except requests.exceptions.HTTPError as e:
                pass
            except LinkOvsBridgePortDeleteError:
                traceback.print_exc()
        elif self.ne_info['NEservice'] == 'kvm':
            cmd1 = "virsh destroy " + self.ne_info['NEid']
            cmd2 = "virsh undefine " + self.ne_info['NEid']
            cmd3 = f"sudo rm -rf {KVM_IMAGE_DIR}/{self.user}/kvm_image/{self.topo}/{self.ne_info['NEid']}.qcow2"
            shell_execute(cmd1)
            shell_execute(cmd2)
            shell_execute(cmd3)
            # 删除后如果文件夹为空就删除文件夹
            if not os.listdir(f"{KVM_IMAGE_DIR}/{self.user}/kvm_image/{self.topo}"):
                os.rmdir(f"{KVM_IMAGE_DIR}/{self.user}/kvm_image/{self.topo}")

            # 更改删网桥的逻辑
            ne_br = self.ne_info['NEnic']
            for i in ne_br:
                try:
                    # 获取br上的接口名称，并删除veth对
                    cmd_show = "ip link show master " + i
                    info = shell_execute(cmd_show)
                    lines = info.splitlines() # 直接将info字符串按行分割
                    # 提取接口名称
                    interfaces = []
                    for line in lines:
                        # 使用正则表达式匹配接口名称
                        match = re.search(r'^\d+: ([^:@]+)', line)
                        if match:
                            interfaces.append(match.group(1))
                    for interface in interfaces:
                        cmd_del_veth = f"sudo ip link delete {interface}"
                        shell_execute(cmd_del_veth)
                    cmd_down = "sudo ip link set " + i + " down"
                    cmd_del = "sudo brctl delbr " + i
                    shell_execute(cmd_down)
                    shell_execute(cmd_del)
                except subprocess.CalledProcessError:
                    pass


class KvmEditor(NEEditor):
    """
    虚拟机节点动态编辑代理类
    """
    def __init__(self, topo: str, name: str, changed: dict, info: dict, re_cli=None):
        """
        Args:
            topo (str): 拓扑名
            name (str): 节点名
            changed (dict): 维护的改变的信息hash表
            info (dict): 上传的信息

        Returns:
            None
        """
        self.changed = changed #{'interface': {'link_l2': {'ip': '192.168.134.146', 'mask': '255.255.255.0'}}, 'NEgateway': '192.168.134.101'}
        self.topo = topo
        self.name = name
        self.ne_id = info['NEid']
        self.type = info['NEtype']
        self.info = info
        self.re_cli = re_cli

    def modify(self):
        """
        根据修改信息修改节点配置信息
        """
        intf_conf = self.changed.get('interface', None)
        if intf_conf:
            self._modify_intf(intf_conf)
        gateway = self.changed.get('NEgateway', None)
        if gateway:
            self._modify_gateway(gateway)

    def _modify_intf(self, intf_conf):
        """
        修改网卡信息
        """
        commands = []
        if self.type == 'host':
            nodetointerface = self.re_cli.get_all_values(f'{self.topo}_{self.name}_nodetointerface')
            interfacetonode = {value: key for key, value in nodetointerface.items()}
            for link in intf_conf.keys():
                nic = self.info[link]['nic']
                intf = interfacetonode[nic]
                # 掩码转换255.255.255.0转换成24类似
                mask = intf_conf[link]['mask']
                mask_len = sum([bin(int(x)).count('1') for x in mask.split('.')])
                commands.append(f'sudo ip link set {intf} up')
                commands.append(f'sudo ip addr flush dev {intf}')
                commands.append(f"sudo ip addr add {intf_conf[link]['ip']}/{mask_len} dev {intf}")
        elif self.type == 'router':
            nodetointerface = self.re_cli.get_all_values(f'{self.topo}_{self.name}_nodetointerface')
            interfacetonode = {value: key for key, value in nodetointerface.items()}
            commands.append(f'return')
            commands.append(f'sys')
            for link in intf_conf.keys():
                nic = self.info[link]['nic']
                intf = interfacetonode[nic]
                # 掩码转换255.255.255.0转换成24类似
                mask = intf_conf[link]['mask']
                mask_len = sum([bin(int(x)).count('1') for x in mask.split('.')])
                commands.append(f'interface {intf}')
                commands.append(f'undo shutdown')
                commands.append(f"ip address {intf_conf[link]['ip']} {mask}")
                commands.append(f'quit')
            commands.append(f'commit')
            commands.append(f'return')
            
        time_gap_list = [0]*len(commands)
        timeouts_list = [1]*len(commands)
        mode = 0
        commands_execer = vm_cmd_execer(vm_id=self.ne_id,
                                        vm_NEtype=self.type,
                                        cmd_list=commands,
                                        time_gap_list=time_gap_list,
                                        timeout_s_list=timeouts_list,
                                        mode_list=mode)
        commands_execer.exe_command()
        if commands_execer.code_list[-1] != 0:
            raise RuntimeError("修改网卡信息失败")
                
    def _modify_gateway(self, gateway):
        commands = []
        if self.type == 'host':
            commands.append(f'sudo ip route del default')
            commands.append(f'sudo ip route add default via {gateway}')

        time_gap_list = [0]*len(commands)
        timeouts_list = [1]*len(commands)
        mode = 0
        commands_execer = vm_cmd_execer(vm_id=self.ne_id,
                                        vm_NEtype=self.type,
                                        cmd_list=commands,
                                        time_gap_list=time_gap_list,
                                        timeout_s_list=timeouts_list,
                                        mode_list=mode)
        commands_execer.exe_command()
        if commands_execer.code_list[-1] != 0:
            raise RuntimeError("修改网卡信息失败")


class HostEditor(NEEditor):
    """
    主机类型节点编辑类
    
    Attributes:
        changed (dict): 维护的改变的信息hash表
        topo (str): 拓扑名
        name (str): 节点名
        ne_id (str): 节点ID
        info (dict): 上传的信息
        re_cli (UserDB): Redis数据库连接类
    """

    def __init__(self, topo: str, name: str, changed: dict, info: dict, re_cli=None):
        """
        Args:
            topo (str): 拓扑名
            name (str): 节点名
            changed (dict): 维护的改变的信息hash表
            info (dict): 上传的信息

        Returns:
            None
        """
        self.changed = changed #{'interface': {'link_l2': {'ip': '192.168.134.146', 'mask': '255.255.255.0'}}, 'NEgateway': '192.168.134.101'}
        self.topo = topo
        self.name = name
        self.ne_id = info['NEid']
        self.info = info
        self.re_cli = re_cli

    def modify(self):
        """
        根据修改信息修改节点配置信息
        """
        intf_conf = self.changed.get('interface', None)
        if intf_conf:
            self._modify_intf(intf_conf)
        gateway = self.changed.get('NEgateway', None)
        if gateway:
            self._modify_gateway(gateway)

    def _modify_intf(self, intf_conf):
        """
        修改网卡信息
        """
        # 这里直接找key 用changed的key, 在info里面进行索引
        for link in intf_conf.keys():
            info = self.info[link]
            link_manager.modify_intf(self.ne_id, info['nic'], info['ip'], info['mask'])

    def _modify_gateway(self, gateway):
        link_manager.modify_gateway(self.ne_id, gateway)


class UbuntuEditor(HostEditor):
    """
    Ubuntu节点动态编辑代理类
    """
    pass


class SwitchEditor(NEEditor):
    """
    OVS交换机 节点动态编辑代理类
    
    Attributes:
        topo (str): 拓扑名
        name (str): 节点名
        changed (dict): 维护的改变的信息hash表
        container (str): 节点ID
        info (dict): 上传的信息
        re_cli (UserDB): Redis数据库连接
    """

    def __init__(self, topo: str, name: str, changed: dict, info: dict, re_cli=None):
        """
        Args:
            topo (str): 拓扑名
            name (str): 节点名
            changed (dict): 维护的改变的信息hash表
            info (dict): 上传的信息
            re_cli (UserDB): Redis数据库连接

        Returns:
            None
        """
        self.topo = topo
        self.name = name
        self.changed = changed
        self.container = docker_cli.containers.get(info['NEid'])
        self.info = info
        self.re_cli = re_cli

    def modify(self):
        """
        根据修改信息修改OVS节点配置
        """
        for key in self.changed.keys():
            modify_func = getattr(self, f'_modify_{key}')
            modify_func(self.info['NEconfig']['config'][key])

    def _modify_stp(self, stp_flag):
        """
        重新设置STP
        Args:
            stp_flag (bool): 是否开启STP

        Returns:
            None
        """
        cmd = f'ovs-vsctl set bridge init-br0 rstp_enable={str(stp_flag).lower()}'
        code = self.container.exec_run(cmd).exit_code
        if code:
            raise RuntimeError("设置stp失败")

    def _modify_controllers(self, ctrs: list):
        """
        设置 OVS交换机的控制器
        Args:
            ctrs (list): 控制器列表

        Returns:
            None
        """
        code = self.container.exec_run('ovs-vsctl del-controller init-br0').exit_code
        if code:
            raise RuntimeError('删除网桥上的控制器失败')
        if not ctrs:
            return
        cmd = 'ovs-vsctl set-controller init-br0 '
        for ctr in ctrs:
            ctr_db = f'{self.topo}_{ctr}'
            ctr_info = self.re_cli.get_value(ctr_db, 'NEconfig')
            port = ctr_info['config'].get('port')
            if port:
                cmd += f"tcp:{ctr_info['ip']}:{port} "
            else:
                cmd += f"tcp:{ctr_info['ip']}:6653 "
            overlay = ctr_info['overlay']
        FLASK_LOGGER.debug(f'ctr cmd {self.name} {cmd}')
        # 我这里没有controllers的话， controller字段里面是不应该有值的
        overlay_net = get_overlay_net(overlay)
        # edit的时候， 如果之前没有， 动态编辑的时候也无法确定
        # 是否已经创建了overlay网络，也得检查是第一次加入还是第二次加入
        # 这里需要检查是否已经将container添加到了net中
        catch_none_net_remove_exception(NONE_NET.disconnect, self.container)
        try:
            overlay_net.connect(self.container)
        except docker.errors.APIError as e:
            FLASK_LOGGER.error(e.args)
            if e.status_code == 409:
                pass
        code = self.container.exec_run(cmd).exit_code
        if code:
            raise RuntimeError(f'为OVS {self.name}添加控制器失败')


class OvsEditor(SwitchEditor):
    """
    OVS节点节点 动态编辑代理类
    """
    pass


class QuaggaEditor(NEEditor):
    """
    Quagga节点编辑代理类
    
    Attributes:
        info (dict):上传的信息
        re_cli (UserDB): Redis数据库连接
        topo (str): 拓扑名
        name (str): 节点名
        container (str): 节点ID
        changed (dict): 维护的改变的信息hash表
    """

    def __init__(self, topo: str, name: str, changed: dict, info: dict, re_cli=None):
        """
        Args:
            topo (str): 拓扑名
            name (str): 节点名
            changed (dict): 维护的改变的信息hash表
            info (dict):上传的信息
            re_cli (UserDB): Redis数据库连接

        Returns:
            None
        """
        # 这里的info 最好就是使用最外层的info
        self.info = info
        self.re_cli = re_cli
        self.topo = topo
        self.name = name
        self.container = docker_cli.containers.get(info['NEid'])
        self.changed = changed

    def modify(self):
        """
        修改节点信息
        """
        intf_conf = self.changed.pop('interface', None)
        if intf_conf:
            self._modify_intf(intf_conf)
        for protocol in self.changed.keys():
            self._modify_protocol(protocol)

    def _modify_intf(self, intf_conf):
        """
        修改与该节点相连的链路信息
        """
        for link in intf_conf.keys():
            info = self.info[link]
            link_manager.modify_intf(self.info['NEid'], info['nic'], info['ip'], info['mask'])

    def _modify_protocol(self, protocol):
        """
        修改quagga节点的配置信息
        """
        quagga_runner = QuaggaRunner(self.name, self.info, self.container)
        # 这里有可能是运行也有可能是关闭某个运行的协议
        self.container.exec_run(f"sh -c 'kill $(cat /var/run/quagga/{protocol}d.pid)' ")
        if self.info['NEconfig']['config'][protocol]['enable']:
            protocol_func = getattr(quagga_runner, f'_{protocol}_conf')
            protocol_func()


class ControllerEditor(NEEditor):
    """
    控制器动态创建代理类
    控制器动态创建后，不能修改任何配置信息
    """
    def __init__(self, topo, name, changed, info, re_cli=None):
        self.topo = topo
        self.name = name
        self.changed = changed
        self.info = info
        self.re_cli = re_cli

    def modify(self):
        pass

    def _modify_dpid(self):
        pass

    def _modify_mac(self):
        pass

def catch_none_net_remove_exception(disconnect_func, node):
    '''
    捕获并屏蔽在none网络中尝试移除节点而报错的异常

    Args:
        disconnect_func：移除节点函数，如NONE_NET.disconnect
        node: docker SDK得到的节点对象
    '''
    try:
        disconnect_func(node)
    except Exception as e:
        # 若为非None网络，在尝试从none网络中移除时会报错，因此屏蔽此异常
        if e.explanation:
            if e.explanation.endswith("connected to network none"):
                FLASK_LOGGER.debug(f"Try to remove {node.id} from none_net, but it is not"
                    " in the none_net!")
                pass
            else:
                raise e
        else:
            raise e        

