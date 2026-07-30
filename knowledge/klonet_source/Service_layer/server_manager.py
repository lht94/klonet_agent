import traceback
import docker
from .redisAPI import *
from .topo_deploy_errors import *

from ..tools import get_host_ip
from ..tools.log_tools import FLASK_LOGGER
from ..Implement_layer import LinkManager as link_manager
from ..Implement_layer import NE_Management as container_manager
from ..Function_layer import master_business_division, master_expr_monitor
from . import expr_monitor_worker as worker_expr_monitor



# 不同子拓扑肯定在不同的worker上
def request_user_info():
    return {'user': 'xc', 'topo': 'test_topo1', 'subtopo': 'test_topo1sub1'}


def get_image_init_para(image):
    return {'privileged': True, 'oom_kill_disable': True, 'detach': True, 
            'network_mode': 'none', 'stdin_open': True, 'tty': True}

def get_container_exec_para():
    return {'privileged': True, 'detach': True}

# 关于节点的相关配置
# 还需要镜像列表
# 功能层的模块调用
# 服务的层级是我们自己 人为划分的嘛？  l2 l3
NE_SERVICE = ['hosts', 'switches', 'routers']
l2_service = ['switches', ]
l3_service = ['routers']
SERVICE_HIERARCHURE = ['l3', 'l2', 'other']


docker_cli = docker.from_env()
user_map_redis = UserMapRedis()
SUCCESS_RESULT_MSG = {'code': 1, 'msg': 'success'}


# 这里是假定topo的切分、数据库的写入都写好了才进行的topo的创建
class TopoManager:

    # 写入就不会改变的数据表， 从数据库读取后可以缓存为实例属性
    # 这里服务创建请求发来的时候，是不是又要重新实例化一个，那这个时候
    _subtopo_common_table = ['plane_subtopo_list', 'subtopo_service']

    def __init__(self, user, topo, subtopo):
        self.user = user
        self.topo = topo
        self.subtopo = subtopo
        # 缓存 subtopo_service 的 properties： switches, hosts, routers
        # 缓存 plane_subtopo_list 的properties: NEs,  links, vxlanlinks
        # 这里初始化的时候是不需要缓存的， 因为服务创建的时候，用的也是这个类
        self.user_db_cli = user_map_redis.get_user_db(user)
        for common_table in self._subtopo_common_table:
            common_info = self.user_db_cli.get_value(common_table, subtopo)
            self.__dict__.update(common_info)

    # 这里应该多线程进行创建吧，不然应该会很慢的
    # 先写的是单线程，之后进行更改
    def deploy_topo(self):
        topo_element = ['NEs', 'links', 'vxlanlinks']
        FLASK_LOGGER.debug(f'deploy subtopo {self.subtopo} of {self.user}...')
        error_msg = {}
        try:
            for ele in topo_element:
                # 这里getattr的时候，已经完成了方法与实例的绑定
                create_func = getattr(self, '_create_{}'.format(ele))
                for node in getattr(self, ele):
                    FLASK_LOGGER.debug(f'create {ele}: {node}...')
                    create_func(node)
        except NEContainerCreateError as e:
            error_msg['error_msg'] = e.args[0]
        except LinkCreateError as e:
            error_msg['error_msg'] = e.args[0]
        except VXLinkCreateError as e:
            error_msg['error_msg'] = e.args[0]
        except Exception:
            pass
        if error_msg:
            error_msg['code'] = 0
            return error_msg
        return SUCCESS_RESULT_MSG

    # 创建节点
    # 这里有错误还不如直接抛出异常
    # 还更方便处理一点
    def _create_NEs(self, ne):
        table_name = '{}_{}'.format(self.topo, ne)
        # 读取节点ID
        ne_id = self.user_db_cli.get_value(table_name, 'NEid')
        ne_config = self.user_db_cli.get_value(table_name, 'NEconfig')
        con_init_config = {}
        image_name = ne_config['image']
        con_init_config['image'] = image_name
        con_init_config['name'] = ne_id
        # 这里预留了初始化容器的时候可能用到的特定的配置的接口比如 cpu 等信息
        # 和特定的镜像是绑定的
        # 这里如果不写命令的话， 默认是bash
        con_init_config.update(get_image_init_para(image_name))
        try:
            # container_manager.create_container(**con_init_config)
            # 用原始接口的时候， 需要注意起容器的必要的参数
            '''
            通用属性的字典, 用户上传的参数在config里面, 难道config里面用户需要输入这么多的参数嘛？
            如上 con_init_config
            '''
            docker_cli.containers.run(**con_init_config)
        except:
            FLASK_LOGGER.error('an error occurred when create ne {}'.format(ne))
            raise NEContainerCreateError(
                'an error occurred when create ne {}'.format(ne))

    # 那这里每一个都要返回？
    # 直接抛出异常给上层处理
    # 创建链路
    def _create_links(self, link):
        table_name = '{}_{}'.format(self.topo, link)
        info = self.user_db_cli.get_all_values(table_name)
        result = link_manager.create_link(info['sourceID'], info['targetID'],
                                          info['sourceIP'], info['targetIP'])
        FLASK_LOGGER.debug(result)
        if result.get('error_msg'):
            raise LinkCreateError(
                'an error occurred when create link {}'.format(link))
        else:
            # 若设置正确， 进行数据的写入
            source_port = result[info['sourceID']]
            target_port = result[info['targetID']]
            self.user_db_cli.set_value(
                table_name, 'sourcePort', source_port)
            self.user_db_cli.set_value(
                table_name, 'targetPort', target_port)
            # 这里还需要写入到<toponame>_<nename>中的link对应的网卡名中去
            src_ne, tgt_ne = info['sourceNE'], info['targetNE']
            # 先读， 再写
            # 这个时候已经写了IP了吗？肯定有的， 只是没有生成网卡名
            src_link_info = self.user_db_cli.get_value('{}_{}'.format(self.topo, src_ne), link)
            src_link_info['nic'] = source_port
            tgt_link_info = self.user_db_cli.get_value('{}_{}'.format(self.topo, tgt_ne), link)
            tgt_link_info['nic'] = target_port
            self.user_db_cli.set_value('{}_{}'.format(self.topo, src_ne), link, src_link_info)
            self.user_db_cli.set_value('{}_{}'.format(self.topo, tgt_ne), link, tgt_link_info)

    # 创建vxlanlink
    # 有问题就抛出异常给上级处理
    def _create_vxlanlinks(self, vxlink):
        table_name = '{}_{}'.format(self.topo, vxlink)
        vx_info = self.user_db_cli.get_all_values(table_name)
        # 数据库里包含了源端container的IP
        # 得到节点的NEid
        source_id = self.user_db_cli.get_value(
            '{}_{}'.format(self.topo, vx_info['source']), 'NEid')
        result = link_manager.create_vxlan(source_id, vx_info['sourceIP'], vx_info['target'],
                                           vx_info['remoteIP'], vx_info['VNI'])
        if result.get('error_msg'):
            raise VXLinkCreateError(
                'an error occurred when create vxlanlink {}'.format(vxlink))
        else:
            source_intf = result[source_id]
            # 写入toponame_vxlanlinkname toponame_linkname topoName_NEname
            self.user_db_cli.set_value(
                table_name, 'sourcePort', source_intf)
            ori_link_table = '{}_{}'.format(self.topo, vx_info['partof'])
            # 通过source_id 来反查表项的前缀
            temp = self.user_db_cli.get_value(ori_link_table, 'sourceID')
            key_preifx = 'source' if temp == source_id else 'target'
            self.user_db_cli.set_value(
                ori_link_table, '{}Port'.format(key_preifx), source_intf)
            topo_ne_table = '{}_{}'.format(self.topo, vx_info['source'])
            ne_detail = self.user_db_cli.get_value(
                topo_ne_table, vx_info['partof'])
            ne_detail['nic'] = source_intf
            self.user_db_cli.set_value(
                topo_ne_table, vx_info['partof'], ne_detail)

    # 区分层级的服务
    # 暂时这样写，因为具体的启动流程还有不确定的地方
    # 这里应该还是要像之前那样分开
    # 就和启动服务一样的代码结构
    def service_deploy(self):
        error_msg = {}
        for layer in SERVICE_HIERARCHURE:
            # 调用对应的层级创建服务
            # ！！！！！ 如果起l2出了问题但是起l3没有问题， 则报错信息会被覆盖
            result = getattr(self, '_start_{}_service'.format(layer))()
            error_msg.update(result)
        return error_msg

    # 传参是什么呢?
    def _start_l2_service(self):
        # 这里在启动之后， 需要启动ovs的服务
        # 之后可能还要考虑多种sw的时候
        # 进入对应的ovs容器， 运行启动命令
        FLASK_LOGGER.info('start l2 service...')
        error_msg = {}
        for sw in getattr(self, 'switches'):
            sw_info = self.user_db_cli.get_all_values('{}_{}'.format(self.topo, sw))
            sw_id = sw_info['NEid']
            sw_id = self.user_db_cli.get_value(
                '{}_{}'.format(self.topo, sw), 'NEid')
            # 这里需要docker container 对象来运行exec_run 命令
            # id 就是容器运行时的名字
            try:
                ovs_con = docker_cli.containers.get(sw_id)
                FLASK_LOGGER.debug(f'start service in {sw}: {sw_id}... ')
                # # 这里不需要检查退出的状况，这里应该是监控程序来做负责的
                # # 检查退出了，就应该由监控程序报警
                # 这里需要检查容器的命令是否正常执行了
                # 对于之后的每一种不同类型的镜像启动的容器，把对应的必要的启动脚本先存在镜像里面？
                # 对应于之后的每种交换机， 都应该在启动的时候配置好自动化启动的脚本，不然用户暂停了容器之后
                # 或者说，对于之后其他种类的交换机，应该不同的启动命令封装成不同的启动脚本
                result = []
                result.append(ovs_con.exec_run('service openvswitch-switch start').exit_code)
                # 容器可能出问题？？？？
                result.append(ovs_con.exec_run('ovs-vsctl add-br init-br0').exit_code)
                # 这里还要将相关的网卡添加到br init-br0 上
                FLASK_LOGGER.debug(f'result is {result}')
                for value in sw_info.values():
                    if isinstance(value, dict):
                        nic = value.get('nic')
                        if nic:
                            FLASK_LOGGER.debug(f'now add {nic} in {sw}')
                            result.append(ovs_con.exec_run(f'ovs-vsctl add-port init-br0 {nic}').exit_code)
                if any(result):
                    FLASK_LOGGER.info(f'ovs {sw} 非正常启动')
                    raise OVSStartError(f'ovs {sw} 非正常启动')
            except:
                error_msg.setdefault('code', 0)
                error_msg.setdefault(
                    'error_msg', 'something wrong during exec switches')
                FLASK_LOGGER.error(error_msg)
                break
        return error_msg if error_msg else SUCCESS_RESULT_MSG

    # 目前来说，现在都归为一类
    # 也没有什么需要特殊运行指令的节点容器
    def _start_other_service(self):
        for host in getattr(self, 'hosts'):
            pass
        return {}
        # return SUCCESS_RESULT_MSG

    def _start_l3_service(self):
        for router in getattr(self, 'routers'):
            pass
        return {}
        # return SUCCESS_RESULT_MSG

    # 还得和监控程序进行交互，如果是指定了需要删除topo
    # 那监控程序就应该解除对topo的监控，让其正常退出和删除
    # 应该是有两个API 吧
    # master与worker一样的职能以及不一样的职能
    # 就直接是删除了
    def destory_topo(self):
        error_msg = {}
        try:
            for ne in getattr(self, 'NEs'):
                self._delete_nes(ne)
            self._delete_topo_entry()
        except:
            error_msg['code'] = 0
            error_msg['error_msg'] = 'failed to destroy topo'
        return error_msg if error_msg else SUCCESS_RESULT_MSG


    # 删除节点的同时，还需要删除Redis里面的数据表
    # 删除数据表可以使用正则匹配来匹配嘛
    # 向上抛出异常
    def _delete_nes(self, ne):
        table_name = '{}_{}'.format(self.topo, ne)
        # ne_id 就为节点容器的name
        con_name = self.user_db_cli.get_value(table_name, 'NEid')
        try:
            # container_manager.delete_container(con_name)
            ne = docker_cli.containers.get(con_name)
            ne.stop()
            ne.remove()
        except docker.errors.NotFound:
            pass

    # 删除redis数据库里公共数据表中包含该topo的条目
    def _delete_topo_entry(self):
        self.user_db_cli.delete_topo_entry(self.topo)

    # 服务删除和拓扑删除应该是两个概念上的东西吧
    # 这里需要用到pause的操作嘛？
    # 用户应该不需要手动的停止服务，不应该暴露给用户的


class MasterTrafficManager:

    def __init__(self, traffic_info):
        self.traffic_info = traffic_info
        self.user_db_cli = user_map_redis.get_user_db(traffic_info['user'])

    # 下层抛出异常, 得对视图函数返回明确的信息
    # 这里是对所有信息写入的操作
    # 要等所有的信息写完在进行最终的服务创建
    def set_value_to_db(self):
        topo = self.traffic_info['topo']
        app_seq = self.traffic_info['app_seq']
        self._handle_traffic_gen_info(topo, app_seq)
        self._handle_pkt_gen2_total_to_sub(topo, app_seq)
        
    def _handle_traffic_gen_info(self, topo, app_seq):
        try:
            tra_gen_s, tra_gen_c = master_business_division.traffic_gen_total_to_sub(self.traffic_info)
            for ip, tra_s_lst in tra_gen_s.items():
                table_name = '{}_{}_{}_s'.format(topo, app_seq, ip)
                self.user_db_cli.set_value(table_name, 'traffic_gen', tra_s_lst)
            for ip, tra_c_lst in tra_gen_c.items():
                table_name = '{}_{}_{}_c'.format(topo, app_seq, ip)
                self.user_db_cli.set_value(table_name, 'traffic_gen', tra_c_lst)
        except KeyError as e:
            raise TrafficGenError('KeyError in traffic gen {}'.format(e.args[0]))

    def _handle_pkt_gen2_total_to_sub(self, topo, app_seq):
        try:
            src_in_worker = master_business_division.pkt_gen2_total_to_sub(self.traffic_info)
            for ip, pkt_list in src_in_worker.items():
                table_name = '{}_{}_{}_c'.format(topo, app_seq, ip)
                self.user_db_cli.set_value(table_name, 'pkt_gen2', pkt_list)
        except KeyError as e:
            traceback.print_exc()
            raise PackageGenError('KeyError in package gen {}'.format(e.args[0]))

    def delete_traffic_info(self):
        return {'code': 1, 'msg': '删除成功'}


# worker 读取数据库信息，启动服务
# 这个好像是不需要的，因为创建的时候，直接就是在视图函数里面写的了
# 这里其实不必要再封装一层的
# 先空缺一下吧
class WorkerTrafficManager:

    def __init__(self):
        pass

    # API发停止信号，用户前端
    # 一开始就指定实验时长
    # 在程序里给API发终止信号


# 只用初始化到数据库的链接就好了
class MasterExprMonitor:

    def __init__(self, user=None, topo=None, expr=None) -> None:
        self.user = user
        self.topo = topo
        self.expr = expr
        self.user_db_cli = user_map_redis.get_user_db(user)

    def set_value_to_db(self, monitor_info):
        # 直接把前端传过来的json作为参数发给convert_event_to_pcap()
        # 这样改动的地方是最少的
        # 甚至这个类最后都可以是不需要的了
        master_expr_monitor.handle_monitor_info(monitor_info)
    
    def _save_pcaps_to_db(self, pcaps:dict):
        table = '{}_{}_monitor'.format(self.topo, self.expr)
        temp_value = {}
        for event_seq, nic_pcaps in pcaps.items():
            for nic_name, pcap in nic_pcaps.items():
                worker_ip = self.user_db_cli.get_worker_ip_by_ne_name(self.topo, pcap['ne_name'])
                temp_dict = temp_value.setdefault(worker_ip, {})
                worker_event_seq = temp_dict.setdefault(event_seq, {})
                nic_info = {'ne_name': pcap['ne_name'], 'filter': pcap['filter']}
                worker_event_seq[nic_name] = nic_info
        FLASK_LOGGER.debug(temp_value)
        self.user_db_cli.set_all_values(table, temp_value)

    # 是在这个函数里面做分发请求，还是就只是查询数据库然后返回一个列表？
    def terminate_monitor(self, signal):
        master_expr_monitor.handle_user_terminal_signal(signal)
        pass


# worker上的流量监控的程序需要做哪些工作呢？
#  这是在视图函数里面调用的， 请求里面自带用户名， 拓扑名， 实验名
# 为什么不用单例模式， 全局初始化一个呢
# 最好是分发的时候就把创建相关的信息发出去，这样可以节约一次通信的时间和资源开销

# 下面的完全可以当成是一个类方法
# 这里完全是需要删除的啊
class WorkerExprMonitor:

    def deploy_monitor(self, user, topo, expr):
        try:
            pcap_processing_list = worker_expr_monitor.deploy_monitor(user, topo, expr)
            return pcap_processing_list
        except:
            raise PcapDeployError('user: {} topo: {} expr:{} pcap deploy on worker{} failed'.format(
                                user, topo,  expr, get_host_ip()))

    # 这里就进行了指标计算， 还需要另外的查询的API （另外的查询API是不是说明这里的数据是不能删除的）
    # 同时，如果不删除， 用户用做其他实验的时候， 又起了一个叫做expr1的实验怎么办呢
    def terminate_monitor(self, user, topo, expr, pcap_processing_list):
        worker_expr_monitor.terminate_monitor(user, topo, expr, pcap_processing_list)


class MasterLinkManager:

    def __init__(self):
        pass

    def config_link(self):
        pass


# 读取链路配置，整理容器所在的worker进行相关信息的返回
class WorkerLinkManager:

    def config_link(self, link_config:dict):
        user, topo = link_config['user'], link_config['topo']
        user_db_cli = user_map_redis.get_user_db(user)
        # 需要传入container_id 和 
        for link, config in link_config.items():
            pass
