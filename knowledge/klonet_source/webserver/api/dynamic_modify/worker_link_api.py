import json
import re

from flask import request
from flask.views import MethodView

from ....Implement_layer.LinkManager.link_operate import shell_execute,delete_ovs_port

from ....Service_layer.redisAPI import UserMapRedis
from ....Service_layer.NEManager import VethLink, VxLANLink
from ....Implement_layer import LinkManager as link_manager
from ....tools.log_tools import FLASK_LOGGER

user_db_map = UserMapRedis()

regex = re.compile('dpdk')

def get_modified_num_of_dpdkLink(table_name, re_cli):
    '''
    动态创建时删除端口后，网卡的编号发生改变，需要将其重新进行编号
    
    Args:
        table_name (str): 节点表名
        re_cli (UserDB): Redis数据库连接
        
    Returns:
        modified_num: 修改后的网卡编号
    '''
    dpdk_nums = re_cli.get_value(table_name, 'dpdk_nums')
    ports_in_bridge = link_manager.shell_execute(f"ovs-vsctl list-ports br_s{dpdk_nums[0]}").split("\n")
    FLASK_LOGGER.debug('ports:'+str(ports_in_bridge))
    ports_num = []
    for port in ports_in_bridge:
        port_num = int(link_manager.shell_execute(f"ovs-vsctl list interface {port} | grep ofport \
            | grep -v ofport_request | awk " + "'{print $3}'"))
            # 因为ovs-vsctl获取到的网卡列表可能有不存在的网卡（另一端被删了，但不del-port的话会error:
            # "could not open network device 69d4f934f50 (No such device)"），但获取到的编号是负数
        if port_num > 0:    
            ports_num.append(port_num)
    ports_num = sorted(ports_num)
    FLASK_LOGGER.debug("ports_num: "+str(ports_num))
    for i in range(len(ports_num)):
        if i != ports_num[i] - 1:
            modified_num = i + 1
        else:
            modified_num = ports_num[-1] + 1
    FLASK_LOGGER.debug("modified_num: "+str(modified_num))
    return modified_num

class DynamicVethLink(MethodView):
    """
    worker动态创建veth-pair链路的代理类
    """

    def post(self):
        """
        在worker上动态创建veth-pair链路
        
        POST /modification/vethlink/
        
        Args:
            data (dict): 拓扑名-用户名-链路名称的组合信息
            
        Returns:
            执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        try:
            db_cli = user_db_map.get_user_db(data['user'])
        except Exception as e:
            return {'code': 0, 'msg': e.args[0]}
        finally:
            user_db_map.close()

        try:
            topo, link = data['topo'], data['name']
            link_creator = VethLink(topo, link, db_cli)
            try:
                if link_creator.src_type == "dpdk/l2fwd" or link_creator.src_type == 'dpdk':
                    table_name = f'{topo}_{link_creator.src}'
                    modified_num = get_modified_num_of_dpdkLink(table_name, db_cli)
                    result = link_creator.create_link()
                    link_creator.write_info(result)
                    link_creator.add_nic_to_ovs_ctr(result)
                    shell_execute(f"ovs-vsctl -- set interface {result['bridge']['nic']} ofport_request={modified_num}")
                elif link_creator.tgt_type == "dpdk/l2fwd" or link_creator.tgt_type == 'dpdk':
                    table_name = f'{topo}_{link_creator.tgt}'
                    modified_num = get_modified_num_of_dpdkLink(table_name, db_cli)
                    result = link_creator.create_link()
                    link_creator.write_info(result)
                    link_creator.add_nic_to_ovs_ctr(result)
                    shell_execute(f"ovs-vsctl -- set interface {result['bridge']['nic']} ofport_request={modified_num}")
                else:
                    result = link_creator.create_link()
                    link_creator.write_info(result)
                    link_creator.add_nic_to_ovs_ctr(result)
            except RuntimeError as e:
                return {'code': 0, 'msg': {e.args[0]}}
            finally:
                db_cli.close()
            return {'code': 1, 'msg': '创建链路成功'}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': {e.args[0]}}

    def delete(self):
        """
        在worker上动态删除veth-pair链路
        
        DELETE /modification/vethlink/
        
        Args:
            data (dict): 拓扑名-用户名-链路名称的组合信息
            
        Returns:
            执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        try:
            db_cli = user_db_map.get_user_db(data['user'])
        except Exception as e:
            return {'code': 0, 'msg': e.args[0]}
        finally:
            user_db_map.close()
        try:
            topo, link = data['topo'], data['name']
            info = db_cli.get_all_values(f'{topo}_{link}')
            src_service = info['sourceservice']
            tgt_service = info['targetservice']            
            if re.search(regex, info['sourceType']):
                dpdk_nums = db_cli.get_value(f"{topo}_{info['sourceNE']}", "dpdk_nums")
                bridge_standard = f"br_s{dpdk_nums[0]}"
                # 删除链路前现删掉网桥的对应流表
                # 获取in_port为该条链路port的流表条目
                # 长这样：...table=0, n_packets=15008, n_bytes=1351228, idle_age=1537, in_port=1 actions=output:3

                # flow = shell_execute(f'ovs-ofctl dump-flows {bridge_standard} "in_port={info["sourcePort"]}"')
                # flow_output_num = flow.split(':')[-1] # 获取了in_port对应的output在openvswitch中的编号
                # flow_output_num = shell_execute(f"ovs-vsctl list interface {info['sourcePort']} \
                #     | grep ofport | grep -v ofport_request | awk " + "'{print $3}'")
                # print(f'ovs-ofctl del-flows {bridge_standard} "in_port={info["sourcePort"]}"',
                # f'ovs-ofctl del-flows {bridge_standard} "in_port={flow_output_num}"')
                # shell_execute(f'ovs-ofctl del-flows {bridge_standard} "in_port={info["sourcePort"]}"') # 删除in_port为该端口的流
                # shell_execute(f'ovs-ofctl del-flows {bridge_standard} "in_port={flow_output_num}"')

                # 删除容器中网卡及网桥上网卡
                result = link_manager.delete_link(info['targetID'], info['targetPort'])
                shell_execute(f"ovs-vsctl --if-exists del-port {bridge_standard} {info['sourcePort']}")
            elif re.search(regex, info['targetType']):
                dpdk_nums = db_cli.get_value(f"{topo}_{info['targetNE']}", "dpdk_nums")
                bridge_standard = f"br_s{dpdk_nums[0]}"
                result = link_manager.delete_link(info['sourceID'], info['sourcePort'])
                shell_execute(f"ovs-vsctl --if-exists del-port {bridge_standard} {info['targetPort']}")
            elif src_service == 'docker' and tgt_service == 'docker':
                result = link_manager.delete_link(info['sourceID'], info['sourcePort'])
                # 动态删除 ovs 网桥的端口
                # 网桥名是否固定为 init-br0，目前还未遇到反例，
                # 数据库中并未存储网桥名，一旦命名规则发生改变就会失效，需要保证命名规则的相似性
                # vxlan需不需要？似乎不需要。
                if info['sourceType'] == 'switch':
                    delete_ovs_port(info['sourceID'], info['sourcePort'], f'init-br0')
                if info['targetType'] == 'switch':
                    delete_ovs_port(info['targetID'], info['targetPort'], f'init-br0')
            elif src_service == 'kvm' and tgt_service == 'kvm':
                # 虚机删除链路不需要删除网卡，只需要将veth删除即可
                veth_infos = [info['sourceveth'], info['targetveth']]
                result = link_manager.delete_kvm_link(veth_infos)
            else:
                # 一端容器一端虚机 只需要删除虚机端 容器端就会自动删除了
                result = link_manager.delete_dkAkd_link([info['sourceveth'], info['targetveth']] if src_service=='kvm' \
                                                        else [info['targetveth'], info['sourceveth']])
                # 这两句还未测试 
                if src_service == 'docker' and info['sourceType'] == 'switch':
                    delete_ovs_port(info['sourceID'], info['sourcePort'], f'init-br0')
                if tgt_service == 'docker' and info['targetType'] == 'switch':
                    delete_ovs_port(info['targetID'], info['targetPort'], f'init-br0')
 
            # if re.search(regex, info['sourceType']) == False and \
            #     re.search(regex, info['targetType']) == False:
            #     result= link_manager.delete_link(info['sourceID'], info['sourcePort'])
            # else:

            #     result = link_manager.delete_link(info['sourceID'], info['targetPort'])
            db_cli.close()
            if result.get('error_msg'):
                return {'code': 0, 'msg': '删除链路失败'}
            return {'code': 1, 'msg': '删除链路成功'}
        except Exception as e:
            FLASK_LOGGER.error(e)
            return {'code': 0, 'msg': '删除链路失败'}


class DynamicVxlanLink(MethodView):
    """
    动态创建vxlan链路的代理类 由master分发每一个对worker的创建vxlan的请求
    """

    def post(self):
        """
        动态创建vxlan链路
        
        POST /modification/vxlanlink/
        
        Args:
            data (dict): 拓扑名-用户名-链路名称的组合信息
            
        Returns:
            执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        FLASK_LOGGER.debug(data)
        user, topo, name = data['user'], data['topo'], data['name']
        try:
            db_cli = user_db_map.get_user_db(user)
        except Exception as e:
            return {'code': 0, 'msg': e.args[0]}
        finally:
            user_db_map.close()
        try:
            vxlan_creator = VxLANLink(topo, name, db_cli)
            result = vxlan_creator.create_link()
            FLASK_LOGGER.debug(f'create result is {result}...')
            vxlan_creator.write_info(result)
            vxlan_creator.add_nic_to_ovs_ctr(result)
            db_cli.close()
            return {'code': 1, 'msg': '创建vxlan成功'}
        except Exception as e:
            FLASK_LOGGER.error(e.args[0])
            return {'code': 0, 'msg': '创建vxlan失败'}

    def delete(self):
        """
        动态删除vxlan链路
        
        DELETE /modification/vxlanlink/
        
        Args:
            data (dict): 拓扑名-用户名-链路名称-ovs名称的组合信息
            
        Returns:
            执行结果字典
        """
        data = json.loads(request.get_data(as_text=True))
        user, topo, ovs_lst = data['user'], data['topo'], data['ovs_lst']
        try:
            db_cli = user_db_map.get_user_db(user)
        except Exception as e:
            return {'code': 0, 'msg': e.args[0]}
        finally:
            user_db_map.close()
        result = {}

        try:
            for ovs_info in ovs_lst:
                link = ovs_info['link']
                info = db_cli.get_all_values(f'{topo}_{link}')
                ovs_info.update({'port': info['sourcePort']})
                result = link_manager.delete_dynative_vxlan(ovs_info)
            db_cli.close()
            if result.get('error_msg'):
                return {'code': 0, 'msg': '删除vxlan失败'}
            return {'code': 1, 'msg': '删除vxlan成功'}
        except Exception as e:
            FLASK_LOGGER.error(e.args[0])
            return {'code': 0, 'msg': '创建vxlan成功'}
