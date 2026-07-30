import re
import os
import json
from flask import request
from flask.views import MethodView

from ....Implement_layer.LinkManager.link_operate import shell_execute
from ....tools.context import redis_context
from ....tools.log_tools import FLASK_LOGGER
from ....Service_layer.redis_error import TableNotExistError
from ....Service_layer.redisAPI import HostPortsAvailableRedis
from ....Service_layer.ssh_worker_manager import *
from ....tools.schema.schema import parameter_check
from ....tools.schema.ssh_service_schema import *
from ....vemu_config.config import PROJ_CONFIG
from ....Service_layer.vm_cmd_execer import vm_cmd_execer


class SSHServiceAPI(MethodView):
    """
    有关ssh服务的api
    """
    def post(self):
        """
        开启ssh服务
        """
        try:
            # json数据解析
            data = json.loads(request.get_data(as_text=True))
            # 检查参数
            result = parameter_check(data, schema_ssh_post)
            if result['code'] == 0:
                return {'code': 0, 'msg': result['msg']}
            # 信息提取
            user, topo, ne, ssh_status, passwd = data['user'], data['topo'], data['ne'], data['ssh'], data['passwd']

            # 获得网元容器对象
            with redis_context(user) as user_db_cli:
                table = f'{topo}_{ne}'
                user_db_cli.check_table_exist(table)
                ne_id = user_db_cli.get_value(table, 'NEid')
            container = docker_cli.containers.get(ne_id)

            # 开关ssh服务
            if ssh_status:    # 开启ssh
                
                # 下载方式标志位，默认使用dpkg离线安装，不使用apt在线安装
                apt_install = False
                
                # 包安装，不同版本进行不同处理，共四种情况
                try:
                    # 获得容器版本
                    version = container.exec_run('cat /etc/lsb-release')
                    if version.exit_code:
                        raise RuntimeError('获取容器版本失败')
                    version = str(version.output)

                    # 情况一：若容器的 ubuntu 版本为 18.04
                    if '18.04' in version:
                        FLASK_LOGGER.debug('ubuntu发行版本为18.04，即将进行离线安装...')
                        # 将包含所有deb包的tar包放到容器内
                        with open(os.path.dirname(__file__)+'/ssh_debs-18.04.tar', 'rb') as f:
                            container.put_archive('/root/', f)
                        # 在容器内执行命令，按序离线安装所有deb包
                        container_exec_cmds(container, 'dpkg -i /root/ssh_debs-18.04/', dpkg_debs_1804, '')
                    
                    # 情况二：若容器的 ubuntu 版本为 20.04
                    elif '20.04' in version:
                        FLASK_LOGGER.debug('ubuntu发行版本为20.04，即将进行离线安装...')
                        # 将包含所有deb包的tar包放到容器内
                        with open(os.path.dirname(__file__)+'/ssh_debs-20.04.tar', 'rb') as f:
                            container.put_archive('/root/', f)
                        # 在容器内执行命令，按序离线安装所有deb包
                        container_exec_cmds(container, 'dpkg -i /root/ssh_debs-20.04/', dpkg_debs_2004, '')
                    
                    # 情况三：版本的安装包在本地没有，使用apt在线安装
                    else:
                        FLASK_LOGGER.info('ubuntu发行版本对应安装包在本地不存在，即将进行在线安装...')
                        apt_install = True

                except RuntimeError:
                    # 情况四：离线安装发生错误，使用apt在线安装
                    FLASK_LOGGER.error('离线安装发生错误，即将进行在线安装...')
                    apt_install = True

                # 若需要进行apt在线安装，则按步骤安装即可
                if apt_install:
                    # 在线安装需要的包
                    apt_cmds = [
                        'chmod 777 /tmp',                                           # 修改权限
                        'apt-get update',                                           # apt 更新
                        'echo -e \'6/n31/n\' | apt-get install -y openssh-server',  # 下载 ssh
                    ]
                    # 在容器内执行命令，按序在线安装所有包
                    container_exec_cmds(container, 'bash -c \"', apt_cmds, '\"')
                
                # 进行ssh的其他配置
                ssh_cmds = [
                    'echo \'PermitRootLogin yes\' >> /etc/ssh/sshd_config',         # 修改ssh配置文件
                    'service ssh restart',                                          # 启动ssh服务
                    f'echo \'root:{passwd}\' | chpasswd',                           # 修改root用户的密码
                ]
                container_exec_cmds(container, 'bash -c \"', ssh_cmds, '\"')

            else:             # 关闭ssh
                if container.exec_run('service ssh stop').exit_code:
                    raise RuntimeError('ssh服务关闭失败')

            return {"code": 1, "msg": "ssh开关请求成功结束！"}

        except Exception as e:
            return {"code": 0, "msg": f"ssh开关请求发生错误：{str(e)}"}

    def get(self):
        """
        ssh服务连接信息的获取,
        连接信息包括worker的ip和节点的所有端口映射
        """
        try:
            # json数据解析
            data = json.loads(request.get_data(as_text=True))  
            # 检查参数
            result = parameter_check(data, schema_ssh_get)
            if result['code'] == 0:
                return {'code': 0, 'msg': result['msg']}
            # 信息提取
            user, topo, ne = data['user'], data['topo'], data['ne']
            
            # 获得节点所在的worker的ip，用于ssh连接
            worker_ip = get_worker_ip(user, topo, ne)
            
            # 读数据库中节点已有的端口映射
            with redis_context(user) as user_db_cli:
                try:
                    table = f'{topo}_port_mapping'
                    user_db_cli.check_table_exist(table)
                    ne_port = user_db_cli.get_value(table, ne) if user_db_cli.check_exist(table, ne) else {}
                except:
                    ne_port = {}

            return {"code": 1, "worker_ip": worker_ip, "ne_port": ne_port, "msg": "节点数据获取请求成功结束！"}

        except Exception as e:
            return {"code": 0, "msg": f"节点数据获取请求发生错误：{str(e)}"}

    def delete(self):
        return {'code': 0, 'msg': 'method not allowed', 'status': 405}


class ModifyNePortMapping(MethodView):
    def put(self):
        """
        编辑里修改端口映射，就到这里
        """
        try:
            # json数据解析
            data = json.loads(request.get_data(as_text=True))
            # 检查参数
            result = parameter_check(data, schema_port_modify)
            if result['code'] == 0:
                return {'code': 0, 'msg': result['msg']}
            # 信息提取
            user, topo, ne, port_mapping = data['user'], data['topo'], \
                data['ne'], data['port_mapping']

            # 端口映射数据处理，改为一个包含偶数个int元素的list
            port_mapping = {port_mapping[2*i]:[port_mapping[2*i+1]] \
                for i in range(int(len(port_mapping)/2))}

            with redis_context(user) as user_db_cli:
                table = f'{topo}_{ne}'
                try:                                      # 若表存在，说明拓扑已经部署
                    # 获取节点的容器ip
                    user_db_cli.check_table_exist(table)
                    ne_id = user_db_cli.get_value(table, 'NEid')
                    ne_service = user_db_cli.get_value(table,'NEservice')
                    ne_type = user_db_cli.get_value(table,'NEtype')
                    
                    # 数据库端口映射追加
                    table = f'{topo}_port_mapping'
                    # 获得表中原有的端口映射
                    ports = dict()
                    try:
                        user_db_cli.check_table_exist(table)
                        if user_db_cli.check_exist(table, ne):
                            ports = user_db_cli.get_value(table, ne)
                    except TableNotExistError:
                        pass

                    if ne_service == 'docker':
                        container = docker_cli.containers.get(ne_id)
                        ne_ip = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', \
                            shell_execute(f'docker inspect {container.name} | grep IPAddress'))[0]

                    elif ne_service == 'kvm':
                        if ne_type == 'host':
                            ne_ip = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', \
                            shell_execute(f'virsh domifaddr {ne_id} | grep ip'))[0]
                        elif ne_type == 'router':
                            commands = ['display interface | include 192.168.122']
                            time_gap_list = [0]*len(commands)
                            timeouts_list = [1]*len(commands)
                            mode = 1
                            commands_execer =vm_cmd_execer( vm_id=ne_id,
                                                            #vm_id ='lzl_ar1000_store',
                                                            vm_NEtype=ne_type,
                                                            #vm_NEtype='router', 
                                                            cmd_list=commands, 
                                                            time_gap_list=time_gap_list,
                                                            timeout_s_list=timeouts_list,
                                                            mode_list=mode)
                            commands_execer.exe_command()
                            ne_ip = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', \
                            commands_execer.result_list[0])[0]

                    # 将旧映射和新的映射结合起来
                    for ne_port, host_port in port_mapping.items():
                        if str(ne_port) not in ports:
                            ports[str(ne_port)] = [host_port[0]]
                        if host_port[0] not in ports[str(ne_port)]:
                            ports[str(ne_port)].append(host_port[0])   
                    
                    # 先检查是否超出可映射端口范围
                    for ne_port, host_port in port_mapping.items():
                        # 检测 host_port 是否超过默认配置
                        if host_port[0] not in PROJ_CONFIG.host_ports:
                            return {"code": 0, "msg": f"宿主机端口{host_port}超出默认范围，请填写{PROJ_CONFIG.port_from}~{PROJ_CONFIG.port_to}之间的值！"}

                    # 容器处理方法
                    if ne_service == "docker":
                        worker_chain = "DOCKER"
                        worker_br = "dokcer0"
                    # 虚机处理方法
                    elif ne_service == "kvm":
                        # 暂时不敢对系统的iptables链做出改动
                        worker_chain = "DOCKER"
                        worker_br = "virbr0"

                    # 在宿主机修改nat表，实现新的端口映射
                    for ne_port, host_port in port_mapping.items():
                        # 检测 host_port 是否在DB0规定的数据库中
                        db0 = HostPortsAvailableRedis()
                        if is_netstat_free(host_port) and db0.is_available_port(host_port):
                            host_port = host_port[0]
                            # assert ne_type in ['container','kvm']                            
                            FLASK_LOGGER.debug(f'配置网元{ne_ip}的端口映射: {host_port}->{ne_port}')
                            shell_execute(f'sudo iptables -t nat -A {worker_chain} ! -i {worker_br} -p tcp -m tcp --dport {host_port} -j DNAT --to-destination {ne_ip}:{ne_port}')
                            shell_execute(f'sudo iptables -t filter -A {worker_chain} -d {ne_ip}/32 ! -i {worker_br} -o {worker_br} -p tcp -m tcp --dport {ne_port} -j ACCEPT')
                            # 将配置过nat表的容器ip都记录入数据库，便于之后删除                        
                            container_list = user_db_cli.get_value(table, "containers_modified_NAT") if user_db_cli.check_exist(table, "containers_modified_NAT") else []
                            if ne_ip not in container_list:
                                container_list.append(ne_ip)
                            user_db_cli.set_value(table, "containers_modified_NAT", container_list)
                            user_db_cli.set_value(table, ne, ports)
                        else:
                            return {"code": 0, "msg": f"宿主机端口{host_port[0]}已被占用，请尝试填写{PROJ_CONFIG.port_from}~{PROJ_CONFIG.port_to}之间的其他值！"}

                except TableNotExistError:                # 表不存在，说明拓扑还没部署，等到创建容器时使用-p参数进行映射即可
                    user_db_cli.set_value(f'{topo}_port_mapping', ne, port_mapping)  # 加入json，最好不要写入数据库（以后再改）
        
                return {"code": 1, "msg": "修改节点端口映射请求成功结束！"}

        except Exception as e:
            return {"code": 0, "msg": f"修改节点端口映射请求发生错误：{str(e)}"}

    def get(self):
        return {'code': 0, 'msg': 'method not allowed', 'status': 405}

    def delete(self):
        """
        宿主机可用端口初始化
        """
        HostPortsAvailableRedis().set_port_default()
        return {'code': 1, 'msg': '宿主机可用端口初始化成功！'}
        