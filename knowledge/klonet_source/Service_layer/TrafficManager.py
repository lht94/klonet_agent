from .topo_deploy_errors import TrafficGenError, PackageGenError, RedisTrafficError
from .redisAPI import UserMapRedis
from .redis_error import TableNotExistError
from ..Function_layer import master_business_division
from ..Function_layer.master_business_division import BusiDivison
import traceback

TRAFFIC_ROLES = ['traffic_server', 'traffic_client', 'pkt_gen2', 'pkt_gen1']

class MasterTrafficManager:
    """
    处理流量信息,并存储/删除流量信息

    Attributes:
        traffic_info: 从redis中获取的流量app信息,用于流量信息的划分
        division_manager: 流量服务切分实例
    """

    def __init__(self, traffic_index_info):
        # 从redis数据库中读取流量服务的某个app信息
        user = traffic_index_info["user"]
        topo = traffic_index_info['topo']
        app = traffic_index_info['app_name']
        table_name = f"{topo}_traffic"
        user_map_redis = UserMapRedis()
        self.user_db_cli = user_map_redis.get_user_db(user)
        user_map_redis.close()
        self.traffic_info = self.user_db_cli.get_value(table_name, app)
        self.traffic_info["topo"] = topo
        self.traffic_info["app_seq"] = app
        self.traffic_info["user"] = user
        self.divison_manager = BusiDivison(self.traffic_info)

        
    def set_value_to_db(self):
        """
        根据流量服务类型的不同,分别存储流量服务信息到数据库,并返回服务所在的worker信息
        Returns:
            worker_map: 不同服务client、server端所在的worker映射信息
        """
        topo = self.traffic_info['topo']
        app_seq = self.traffic_info['app_seq']
        traffic_s_worker_list, traffic_c_worker_list = self._handle_traffic_gen_info(topo, app_seq)
        pkt2_worker_list = self._handle_pkt_gen_total_to_sub(topo, app_seq, "pkt_gen2")
        pkt1_worker_list = self._handle_pkt_gen_total_to_sub(topo, app_seq, "pkt_gen1")
        # TODO(sw):若还要扩展需要在此处添加  
        worker_map = {
            'traffic_server_workers': list(traffic_s_worker_list),
            'traffic_client_workers': list(traffic_c_worker_list),
            'pkt_gen2_workers': list(pkt2_worker_list),
            'pkt_gen1_workers': list(pkt1_worker_list)
        }
        # 创建一个table保存流量服务与worker的对应关系
        table_name = f"{topo}_{app_seq}_to_worker"
        self.user_db_cli.set_value(table_name, "worker_map", worker_map)
        print("set_value_to_db")
        return worker_map
    
    def get_value_from_db(self):
        """
        从数据库中获取流量服务的worker映射信息
        Returns:
            worker_map: 不同服务client、server端所在的worker映射信息    
        """
        topo = self.traffic_info['topo']
        app_seq = self.traffic_info['app_seq']
        table_name = f"{topo}_{app_seq}_to_worker"
        worker_map = self.user_db_cli.get_value(table_name, "worker_map")
        return worker_map
    
    def del_value_from_db(self, server_worker_map):
        """
        删除数据库中的流量服务表项
        """
        topo = self.traffic_info['topo']
        app_seq = self.traffic_info['app_seq']
        table_name = f"{topo}_{app_seq}_to_worker"
        self.user_db_cli.del_table(table_name)
        for role in TRAFFIC_ROLES:
            workers_list_key = f'{role}_workers'
            if "server" in workers_list_key:
                for ip in server_worker_map[workers_list_key]:
                    table_name = '{}_{}_{}_s'.format(topo, app_seq, ip)
                    self.user_db_cli.del_table(table_name)
            else:
                for ip in server_worker_map[workers_list_key]:
                    table_name = '{}_{}_{}_c'.format(topo, app_seq, ip)
                    if "client" in workers_list_key:
                        self.user_db_cli.del_value(table_name, "traffic_gen")
                    else:
                        self.user_db_cli.del_value(table_name, role)
            # elif "client" in workers_list_key:
            #     for ip in server_worker_map[workers_list_key]:
            #         table_name = '{}_{}_{}_c'.format(topo, app_seq, ip)
            #         self.user_db_cli.del_value(table_name, "traffic_gen")
            # elif "pkt_gen2" in workers_list_key:
            #     for ip in server_worker_map[workers_list_key]:
            #         table_name = '{}_{}_{}_c'.format(topo, app_seq, ip)
            #         self.user_db_cli.del_value(table_name, "pkt_gen2")
            # elif "pkt_gen2" in workers_list_key:
            #     for ip in server_worker_map[workers_list_key]:
            #         table_name = '{}_{}_{}_c'.format(topo, app_seq, ip)
            #         self.user_db_cli.del_value(table_name, "pkt_gen1")
            
        
    def _handle_traffic_gen_info(self, topo, app_seq):
        """
        处理traffic_gen流量发生器的信息,将不同宿主机的流量服务切分到对应worker
        Args:
            topo: 拓扑名
            app_seq: 流量服务的ID
        Returns:
            traffic_server_set: 流量服务server端对应的worker ip
            traffic_client_set: 流量服务client端对应的worker ip
        """

        traffic_server_set = set()
        traffic_client_set = set()
        try:
            tra_gen_s, tra_gen_c = self.divison_manager.traffic_gen_total_to_sub()
            # tra_gen_s, tra_gen_c = master_traffic_manager.traffic_gen_total_to_sub(self.traffic_info)
            for ip, tra_s_lst in tra_gen_s.items():
                traffic_server_set.add(ip)
                table_name = '{}_{}_{}_s'.format(topo, app_seq, ip)
                self.user_db_cli.set_value(table_name, 'traffic_gen', tra_s_lst)
            for ip, tra_c_lst in tra_gen_c.items():
                traffic_client_set.add(ip)
                table_name = '{}_{}_{}_c'.format(topo, app_seq, ip)
                self.user_db_cli.set_value(table_name, 'traffic_gen', tra_c_lst)
            return traffic_server_set, traffic_client_set
        except KeyError as e:
            raise TrafficGenError('KeyError in traffic gen {}'.format(e.args[0]))

    
    def _handle_pkt_gen_total_to_sub(self, topo, app_seq, pkt_gen_type="pkt_gen2"):
        """
        处理pkt_gen1、pkt_gen2流量发生器的信息,将不同宿主机的流量服务切分到对应worker
        Args:
            topo: 拓扑名
            app_seq: 流量服务的ID
            pkt_gen_type: 流量服务的类型, pkt_gen1 or pkt_gen2
        Returns:
            pkt_gen_set: 流量服务client端对应的worker ip
        """
        pkt_gen_set = set()
        try:
            src_in_worker = self.divison_manager.pkt_gen2_total_to_sub(pkt_gen_type)
            # src_in_worker = master_traffic_manager.pkt_gen_total_to_sub(
            #     self.traffic_info, pkt_gen_type)
            for ip, pkt_list in src_in_worker.items():
                pkt_gen_set.add(ip)
                table_name = '{}_{}_{}_c'.format(topo, app_seq, ip)
                self.user_db_cli.set_value(table_name, pkt_gen_type, pkt_list)
            return pkt_gen_set
        except KeyError as e:
            traceback.print_exc()
            raise PackageGenError('KeyError in packet gen {}'.format(e.args[0]))


    def close(self):
        self.user_db_cli.close()


# worker 读取数据库信息，启动服务
# 这个好像是不需要的，因为创建的时候，直接就是在视图函数里面写的了
# 这里其实不必要再封装一层的
# 先空缺一下吧
class WorkerTrafficManager:
    pass




def template2traffic(traffic_index_info):
    user = traffic_index_info["user"]
    topo = traffic_index_info['topo']
    app = traffic_index_info['app_name']
    tra = traffic_index_info['tra_name']
    src_node = traffic_index_info['src_node']
    dst_node = traffic_index_info['dst_node']
    src_ip = traffic_index_info['src_ip']
    dst_ip = traffic_index_info['dst_ip']
    table_name = f"{topo}_template"
    traffic_table = f"{topo}_traffic"
    user_map_redis = UserMapRedis()
    user_db_cli = user_map_redis.get_user_db(user)
    user_map_redis.close()
    traffic_info = user_db_cli.get_value(table_name, app)
    traffic_info["pkt_gen1"][0]["src"] = src_node
    traffic_info["pkt_gen1"][0]["dst"] = dst_node
    traffic_info["pkt_gen1"][0]["src_ip"] = src_ip
    traffic_info["pkt_gen1"][0]["dst_ip"] = dst_ip
    user_db_cli.set_value(traffic_table, tra, traffic_info)
    
    


   
