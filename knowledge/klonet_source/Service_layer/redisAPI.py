import json
import random
import redis
import weakref
import socket

from .redis_error import *
from ..vemu_config.config import PROJ_CONFIG


# 生产环境下， 需要更改redis的配置
# timeout 3600
# tcp-keepalive 60
USER_DB_COUNT = PROJ_CONFIG.redis_db_count

CONFIG_INFO = {
    'USER': PROJ_CONFIG.redis_user,
    'HOST': PROJ_CONFIG.master_ip,
    'PORT': PROJ_CONFIG.redis_port,
    'PASSWD': PROJ_CONFIG.redis_password
}

# hostname = socket.gethostname()
# if 'vemu4' in hostname:
#     CONFIG_INFO = VEMU_CONFIG_INFO
# else:
#     CONFIG_INFO = DEV_CONFIG_INFO

DB_BASE_URL = "redis://{USER}:[REDACTED]@{HOST}:{PORT}/".format(**CONFIG_INFO)


class CacheUserDBConnPoolManager:
    """
    数据库连接池实例缓存管理器， 保证连接到同一个数据库的连接池全局唯一
    """
    def __init__(self):
        self._cache = weakref.WeakValueDictionary()

    def get_conn_pool(self, num):
        """
        新创建连接池实例并缓存，或者从缓存中返回已经创建的连接池实例
        Args:
            num: 数据库的编号 int
        
        Returns:
            返回特定编号的数据库连接池对象
            UserDBConnPool()
        """
        if num not in self._cache:
            temp = UserDBConnPool.from_url(num, encoding='utf-8', decode_responses=True)
            self._cache[num] = temp
        else:
            temp = self._cache[num]
        return temp


class UserDBConnPool(redis.BlockingConnectionPool):
    """
    连接池对象
    """
    manager = CacheUserDBConnPoolManager()

    @classmethod
    def from_url(cls, num, *args, **kwargs):
        """
        通过给定的数据库编号生成对应的url, 并通过url返回连接池对象
        Args:
            num: 数据库编号 int

        Returns:
            返回特定编号的数据库连接池对象
            UserDBConnPool()
        """
        db_url = DB_BASE_URL + str(num)
        return super().from_url(db_url, *args, **kwargs)


def get_user_db_conn_pool(num):
    """
    返回连接池对象的工厂函数， 用户应该只通过这个函数得到连接池实例
    Args:
        num: 数据库编号 int

    Returns:
        返回特定编号的数据库连接池对象
        UserDBConnPool()
    """
    return UserDBConnPool.manager.get_conn_pool(num)


class DB0Redis:
    """
    DB0 的连接管理对象
    """
    conn_pool = get_user_db_conn_pool(0)
    table_name = ''

    def __init__(self):
        # https://github.com/redis/redis-py/issues/1186
        self._db_conn = redis.StrictRedis(connection_pool=self.conn_pool, 
            health_check_interval=30)

    def del_table(self):
        """
        删除Redis数据表
        """
        return self._db_conn.delete(self.table_name)

    def close(self):
        """
        关闭数据库连接
        """
        self._db_conn.close()


class PubSubRedis(DB0Redis):
    """
    管理事件发布/订阅的管理对象
    """
    def publish(self, channel, msg):
        """
        发布消息到指定频道
        """
        self._db_conn.publish(channel, msg)

    def subscribe(self, channel):
        """
        订阅指定频道的消息
        """
        pubsub = self._db_conn.pubsub()
        pubsub.subscribe(channel)
        return pubsub

    def get_msgs(self, pubsub):
        """
        从订阅频道接收消息
        """
        for message in pubsub.listen():
            if message['type'] == 'message':
                yield json.loads(message['data'])


class WorkerRedis(DB0Redis):
    """
    管理worker_list表的管理对象
    """
    table_name = PROJ_CONFIG.worker_list
    def set_worker(self, ip):
        return self._db_conn.sadd(self.table_name, ip)

    def set_all_workers(self, workers):
        for worker in workers:
            self._db_conn.sadd(self.table_name, worker)

    def del_worker(self, ip):
        return self._db_conn.srem(self.table_name, ip)

    def get_worker(self):
        pass

    def get_all_workers(self):
        return list(self._db_conn.smembers(self.table_name))

    def del_all_workers(self):
        return self._db_conn.delete(self.table_name)
    
class HardwareRedis(DB0Redis):
    """
    管理switch_list以及hardware_list表的管理对象
    """
    switch_list = PROJ_CONFIG.switch_list
    hardware_list = PROJ_CONFIG.hardware_list

    def get_switch_in_id(self, id):
        hash_dict = self._db_conn.hgetall(self.switch_list)
        print(hash_dict)
        switch = []
        for key, value_dict in hash_dict.items():
            value_dict_info = json.loads(value_dict)
            for inner_key, inner_dict in value_dict_info.items():
                if inner_dict.get("id") == id:
                    vlan = inner_dict['vlan']
                    switch_dict = {'switch':key, 'vlan':vlan}
                    switch.append(switch_dict)
                    break
        return switch
        
    def update_ne_state(self, id, state:bool):
        type_dict = self._db_conn.hgetall(self.hardware_list)
        for type, device in type_dict.items():
            device_dict = json.loads(device)
            if id in device_dict.keys():
                states = device_dict[id]["state"].split('_')
                if state:
                    status = 1
                else:
                    status = 0
                state_new = f"{states[0]}_{status}"
                device_dict[id]['state'] = state_new
                self.add_hardware_to_type(type, device_dict)


    def get_hardware_in_switch(self, switch):
        raw_info = self._db_conn.hget(self.switch_list, switch)
        try:
            info_dict = json.loads(raw_info)
        except TypeError:
            info_dict = {}
        return info_dict
    
    def get_hardware_in_type(self, type):
        raw_info = self._db_conn.hget(self.hardware_list, type)
        try:
            info_dict = json.loads(raw_info)
        except TypeError:
            info_dict = {}
        return info_dict
    
    def add_hardware_to_switch(self, switch, hardinfo:dict):
        return self._db_conn.hset(self.switch_list, switch, json.dumps(hardinfo))
    
    def add_hardware_to_type(self, type, hardinfo:dict):
        return self._db_conn.hset(self.hardware_list, type, json.dumps(hardinfo))


class ResourceRedis(DB0Redis):
    """
    管理设备资源表的管理对象
    """
    # resource_types = ["cpu", "gpu", "memory"]
    resource_types = ["cpu_time", "mem", "cpu_core"]

    def __init__(self, table_name):
        self.table_name = table_name
        super().__init__()

    def set_resource(self, resource_index, resource_info:dict):
        """
        写入资源的数据
        Args:
            resource_index: 资源的索引, 可以是worker_ip, 节点类型或者链路类型 string
            resource_info: 描述资源信息的字典 dict

        Returns:
            Bool: 操作成功为1， 失败则为0
        """
        return self._db_conn.hset(self.table_name, resource_index, json.dumps(resource_info))

    def get_resource(self, resource_index, resource_type=None):
        """
        得到资源数据
        Args:
            resource_index: 资源的索引, 可以是worker_ip, 节点类型或者链路类型 string
            resource_type: 资源的类型，例如cpu、gpu, 或者memory     string

        Returns:
            info_dict: 设备的资源信息 dict
        
        Raises:
            RedisError: 
        """
        raw_info = self._db_conn.hget(self.table_name, resource_index)
        try:
            info_dict = json.loads(raw_info)
        except TypeError:
            info_dict = {}
        if not resource_type:
            return info_dict
        elif resource_type not in self.resource_types:
            raise SourceTypeError("no such resource type only in [cpu_time, mem, cpu_core]")
        else:
            return info_dict.get(resource_type, None)

    def get_all_resources(self):
        """
        得到所有的资源信息
        """
        return self._db_conn.hgetall(self.table_name)

    def del_resource(self, resource_index: str):
        """
        Args:
            resource_index (str): 资源的名称索引
        """
        return self._db_conn.hdel(self.table_name, resource_index)

    def del_all_resources(self):
        """
        删除资源列表
        """
        return self._db_conn.delete(self.table_name)
    
    
class UserCPUResourceRedis(DB0Redis):
    """wudx
    管理所有用户的cpu配额的管理对象
    
    Attributes:
        table_name: user_cpu_resource
        _db_conn: 连接DB0的客户端
    """
    table_name = "user_cpu_resource"
    
    def set_default_resource(self, user_list: list):
        """
        初始化用户CPU配额表(不存在时才创建，存在时略过)
        
        Args:
            user_list(list): 所有用户名的list
        """
        try:
            if not self._db_conn.exists(self.table_name):
                default_cpu_num = PROJ_CONFIG.single_user_CpuNum
                for key in user_list:
                    self._db_conn.hset(self.table_name, key, default_cpu_num)
            else:
                # 如果表存在则检查是否存在没有加入资源表的新用户，并将其初始化
                for key in user_list:
                    if not self._db_conn.hexists(self.table_name, key):
                        default_cpu_num = PROJ_CONFIG.single_user_CpuNum
                        self._db_conn.hset(self.table_name, key, default_cpu_num)
        except:
            raise ValueError("初始化用户表失败，请联系管理员处理")
        return True
    
    def set_resource(self, user, core_num):
        """
        设置用户的CPU额度
        
        Args:
            user(str): 用户名
            core_num(int): CPU配额
        """
        return self._db_conn.hset(self.table_name, user, core_num)
    
    def get_resource(self, user):
        """
        获取用户的当前剩余CPU资源
        
        Args:
            user(str): 用户名
            
        Returns:
            available_cpu(int): 当前用户可用的CPU额度
        """
        return self._db_conn.hget(self.table_name, user)
        

class WorkerResourceRedis(DB0Redis):
    """
    管理worker剩余资源表的管理对象
    
    Attributes:
        table_name: worker_resource
        _db_conn: 连接数据库0的客户端
    """
    table_name = "worker_resource"
    def set_resource(self, worker_ip, resource_info:dict):
        """
        写入资源的数据
        Args:
            worker_ip:
            resource_info: 描述资源信息的字典，包含CPU和内存的剩余信息 dict

        Returns:
            Bool: 操作成功为1， 失败则为0
        """
        res = json.dumps(resource_info)
        print(type(res), res)
        return self._db_conn.hset(self.table_name, worker_ip, res)
    
    def set_all_resource(self, resource_info):
        for key, value in resource_info.items():
            self._db_conn.hset(self.table_name, key, json.dumps(value))
        return True
    
    def get_resource(self, worker_ip, resource_type=None):
        '''
        得到资源数据
        Args:
            worker_ip: 
            resource_type: 查询资源的类型，如:cpu、memory     string

        Returns:
            info_dict: 设备的资源信息 dict
        
        Raises:
            RedisError: 
        '''
        raw_info = self._db_conn.hget(self.table_name, worker_ip)
        try:
            info_dict = json.loads(raw_info)
        except TypeError:
            info_dict = {}
        if not resource_type:
            return info_dict
        elif resource_type not in self.resource_types:
            raise SourceTypeError("no such resource type only in [cpu, memory]")
        else:
            return info_dict.get(resource_type, None)
    
    def get_all_resources(self):
        res = {}
        raw_dict = self._db_conn.hgetall(self.table_name)
        for key, value in raw_dict.items():
            res[key] = json.loads(value)
        return res

    def del_resource(self, worker_ip):
        return self._db_conn.hdel(self.table_name, worker_ip)

    def del_all_resources(self):
        return self._db_conn.delete(self.table_name)


class HostPortsAvailableRedis(DB0Redis):
    """
    可用的宿主机端口数据库，使用了set数据结构
    """
    table_name = "host_ports"
    def set_port_default(self):
        """
        将数据库中的可用端口复位
        """
        self.del_table()
        for port in PROJ_CONFIG.host_ports:
            self._db_conn.sadd(self.table_name, port)
    
    def get_port(self):
        """
        弹出一个可用端口
        """
        # 若获取端口时，数据库里的表不存在，则进行复位
        if not self._db_conn.exists(self.table_name):
            self.set_port_default()
        return int(self._db_conn.spop(self.table_name))

    def return_port(self, port):
        """
        将一个端口交还回去
        """
        return self._db_conn.sadd(self.table_name, port)

    def is_available_port(self, port, remove=True):
        """
        检测一个端口是否可用，或一个list里第一个端口是否可用
        
        Args:
            port:   被检测的端口（或list）
            remove: 是否从数据库删除该端口，默认未删除

        Returns:
            Bool:   可用返回True，存在不可用为False
        """
        if isinstance(port, list):
            port = port[0]

        # 若获取端口时，数据库里的表不存在，则进行复位
        if not self._db_conn.exists(self.table_name):
            self.set_port_default()

        if self._db_conn.sismember(self.table_name, port):
            if remove:
                self._db_conn.srem(self.table_name, port)
            return True
        else:
            return False


# 管理用户数据库映射的表操作
class UserMapRedis(DB0Redis):
    """
    用户数据库映射的管理对象
    """
    table_name = 'user2DB'

    def set_user_db(self, username):
        """
        设置用户的数据库, 用户数据库的编号范围为 1~99
        
        Args:
            username: 用户名
        
        Returns:
            UserDB(): 用户数据库管理实例
        
        Raises:
            RedisError: 
        """
        # 检查数据库里的用户的数量, 如果小于USER_DB_COUNT说明一定存在空余的数据库
        if self._db_conn.hlen(self.table_name) >= USER_DB_COUNT:
            raise NoFreeDbForUserError(f"user db > {USER_DB_COUNT}")
        # 检查用户是否已经有数据库了
        if username in self._db_conn.hkeys(self.table_name):
            raise DbAlreadyExistError("the user db has exists, please do not create db again, \
                                        to get UserDB() use get_user_db method")
        # 现存的用户表
        tables = self._db_conn.hvals(self.table_name)
        # 尝试使用的db数
        db_num = len(tables) + 1
        db_name = 'DB' + str(db_num)
        # 如果这个表名被占用，随机取值，直到得到未在表里的名字
        if db_name in tables:
            while True:
                db_num = random.randint(1, USER_DB_COUNT)
                db_name = 'DB' + str(db_num)
                if db_name not in tables:
                    break
        # db和用户绑定
        if self._db_conn.hset(self.table_name, username, db_name):
            conn_pool = get_user_db_conn_pool(db_num)
            return UserDB(conn_pool)
        else:
            raise DbCreateFailedError(f"failed to create user db {db_name}")

    def get_user_db(self, username):
        """
        返回用户数据库管理对象

        Args:
            username: 用户名

        Returns:
            UserDB: 用户数据库管理实例

        Raise:
            RedisError: 
        """
        db_info = self._db_conn.hget(self.table_name, username)
        if db_info:
            db_num = int(db_info[2:])
            conn_pool = get_user_db_conn_pool(db_num)
            return UserDB(conn_pool)
        else:
            raise DbNotExistError(f'user [{username}] has no db please create before '
                'using')
        
    def get_all_user_dbs(self):
        """
        返回所有的用户与数据库的映射关系
        """
        return self._db_conn.hgetall(self.table_name)
    
    def get_user_list(self):
        """wudx
        返回一个用户列表
        """
        # user_list = []
        # for user in self._db_conn.hkeys(self.table_name):
        #     user_list.append(user.decode())
        # return user_list
        return self._db_conn.hkeys(self.table_name)

    def del_user_db(self, username):
        """
        删除用户数据库

        Args:
            username: 用户名

        Returns:
            Bool: 操作成功返回1， 失败返回0
        """
        db_conn = self.get_user_db(username)._db_conn
        db_conn.flushdb()
        return self._db_conn.hdel(self.table_name, username)
    
    def del_all_user_dbs(self):
        return self._db_conn.delete(self.table_name)


class UserDB:
    """
    用户数据库的管理对象
    """
    _topo_common_info_table = ['topo_list', 'plane_topo_list',
                            'topo_service', 'plane_subtopo_list',
                            'subtopo2worker', 'subtopo_service', 'topo2subtopo']

    def __init__(self, conn_pool):
        """
        conn_pool (UserDBConnPool): Redis连接池对象
        """
        # https://github.com/redis/redis-py/issues/1186
        self._db_conn = redis.StrictRedis(connection_pool=conn_pool,
            health_check_interval=30)

    def check_table_exist(self, table_name: str):
        """
        检查数据表是否存在
        Args:
            table_name (str): 数据表名
        """
        if not self._db_conn.exists(table_name):
            raise TableNotExistError(f"table [{table_name}] doesn't exist")

    def set_value(self, table_name, key, value):
        """
        设置数据表的键和值
        Args:
            table_name (str): 数据表名
            key (str):   键
            value (str): 值

        Returns:
            Bool: 操作成功为1， 失败则为0
        """
        return self._db_conn.hset(table_name, key, json.dumps(value))

    def set_all_values(self, table_name, info_dict):
        """
        用于设置虚拟网络节点、链路实体信息表项的值
        Args:
            table_name: 实体信息表名 string
            info_dict: 数据字典  dict
        
        Returns:
            Bool: 操作成功为1， 失败则为0
        """
        for key, value in info_dict.items():
            self._db_conn.hset(table_name, key, json.dumps(value))
        return True

    def check_exist(self, table_name: str, key: str):
        """
        检查Redis hash表中是否存在该 key
        Args:
            table_name (str): 实体信息表名
            key (key):        查询的key
        """
        return self._db_conn.hexists(table_name, key)

    def get_value(self, table_name: str, key: str):
        """
        得到查询的key对应的value
        Args:
            table_name: 数据库表名 string
            key: 键 string
        
        Returns:
            resp: key对应的value 可以是列表、字符串、 字典
        
        Raise:
            RedisError: 
        """
        self.check_table_exist(table_name)
        encode_str = self._db_conn.hget(table_name, key)
        try:
            resp = json.loads(encode_str)    
        except:
            raise KeyNotExistError(f'invalid key name {key}')
        return resp

    def get_all_values(self, table_name: str):
        """
        返回查询数据表里的所有的表项
        Args:
            table_name: 数据表名
        
        Returns：
            数据中的所有的键值对应的dict
        """
        temp_dict = {}
        raw_dict = self._db_conn.hgetall(table_name)
        for key, value in raw_dict.items():
            temp_dict[key] = json.loads(value)
        return temp_dict

    def get_hash_table(self, table_name: str):
        """
        查询hash_table里的所有内容
        
        Args:
            table_name: 数据表名
        
        Returns：
            {
                <key1>: <value1>,
                ...
            }
        """
        return self._db_conn.hgetall(table_name)

    def get_all_keys(self, table_name):
        """
        返回查询数据表中的所有key值
        Args:
            table_name: 数据表名
        
        Returns：
            数据中的所有的key值组成的list
        """
        temp_list = []
        temp_list = self._db_conn.hkeys(table_name)
        return temp_list

    def get_elements_in_set(self, set_name):
        """
        查询一个Set中所有的值，以列表的形式返回

        Args:
            set_name: Set名
        
        Returns:
            Set中所有的元素组成的list
        """
        return list(self._db_conn.smembers(set_name))

    def del_value(self, table_name: str, key: str):
        """
        删除hash表中的键
        Args:
            table_name (str):数据库表名
            key (str)        操作的键
        """
        self.check_table_exist(table_name)
        return self._db_conn.hdel(table_name, key)

    def del_all_values(self, table_name):
        """
        清空hash表
        Args:
            table_name (str):数据库表名
        """
        self.check_table_exist(table_name)
        return self._db_conn.delete(table_name)

    def del_table(self, table_name):
        """
        删除hash表
        Args:
            table_name (str):数据库表名
        """
        return self._db_conn.delete(table_name)

    def close(self):
        """
        关闭数据库连接
        """
        self._db_conn.close()

    def delete_topo_entry(self, topo_name):
        """
        删除拓扑相关的所有Redis数据表，删除拓扑的时候使用
        Args:
            topo_name (str): 拓扑名
        """
        topo_common_info_table = ['topo_list', 'plane_topo_list',
                            'topo_service', 'topo2subtopo', 'topo_resource']
        subtopo_info_table = ['plane_subtopo_list', 'subtopo2worker',
            'subtopo_service']
        subtopo_list = self.get_value('topo2subtopo', topo_name)
        for table in subtopo_info_table:
            for subtopo in subtopo_list:
                self.del_value(table, subtopo)
        for table in topo_common_info_table:
            try:
                self.del_value(table, topo_name)
            except Exception as e:
                if table == 'topo_resource':
                    pass
                else:
                    print(e)
        # 删除所有以 <topoName>_xxx开头的表项
        for table_name in self._db_conn.scan_iter(match='{}_*'.format(topo_name)):
            self.del_all_values(table_name)

        # 删除心跳健康检查的项目损坏表中的对应条目
        try:
            self.del_value(PROJ_CONFIG.broken_projects_table_name, topo_name)
        except:
            pass

    def get_worker_ip_by_ne_name(self, topo_name, ne_name):
        """
        通过节点名获取worker ip
        Args:
            topo_name (str): 拓扑名
            ne_name   (str): 节点名
        """
        table = '{}_{}'.format(topo_name, ne_name)
        subtopo = self.get_value(table, 'NEloc')
        return self.get_value('subtopo2worker', subtopo)

    def get_nic_by_ne_name_and_ip(self, topo, ne_name, nic_ip):
        """
        根据节点和网卡ip 查询网卡名
        Args:
            topo_name (str): 拓扑名
            ne_name   (str): 节点名
            nic_ip    (str): 网卡IP
        """
        table = '{}_{}'.format(topo, ne_name)
        result = self.get_all_values(table)
        for value in result.values():
            try:
                ip = value['ip']
                if nic_ip == ip:
                    return value['nic']
            except:
                pass
        raise KeyError('节点{}没有地址为{}的网卡'.format(ne_name, nic_ip))

    def get_ne_vxlan_info(self, topo, ne):
        """
        得到节点的 vxlan 信息
        Args:
            topo      (str): 拓扑名
            ne   (str): 节点名

        Returns:
            ip_ovs_map (dict): 得到worker_ip和vxlan对应网桥
        """
        ip_ovs_map = {}
        ne_info = self.get_all_values(f'{topo}_{ne}')
        for k, v in ne_info.items():
            if not k.startswith('link_'):
                continue
            link = k[5:]
            link_info = self.get_all_values(f'{topo}_{link}')
            vxlans = link_info.get('vxlan')
            if vxlans:
                # 这里的一对vxlan的信息是相互交叉的  IPA OVSB/ IPB OVSA
                # 组合成规格化的字符串
                # 对vxlan的信息进行解包
                src, tgt = vxlans
                tgt_ip = self.get_value(f'{topo}_{src}', 'remoteIP')
                src_ip = self.get_value(f'{topo}_{tgt}', 'remoteIP')
                # 这里是写入了源和目的的ovs名称， 这里是反过来的            
                # lzl 看到该函数只有一次调用， 因此直接修改redisAPI.py
                # 修改：需要存放ovs相关信息 不能只存放ovs名称了
                src_ovs_info = {'target': self.get_value(f'{topo}_{src}', 'target'), \
                                'src_veth': self.get_value(f'{topo}_{src}', 'sourceveth'),\
                                'service': link_info['sourceservice'],\
                                'id': link_info['sourceID'],\
                                'link': src,
                                }
                tgt_ovs_info = {'target': self.get_value(f'{topo}_{tgt}', 'target'), \
                                'src_veth': self.get_value(f'{topo}_{tgt}', 'sourceveth'),\
                                'service': link_info['targetservice'],\
                                'id': link_info['targetID'],\
                                'link': tgt,
                                        }
                tgt_lst = ip_ovs_map.setdefault(tgt_ip, [])
                tgt_lst.append(tgt_ovs_info)
                src_lst = ip_ovs_map.setdefault(src_ip, [])
                src_lst.append(src_ovs_info)
        return ip_ovs_map

    def get_worker_link_map(self, topo, link):
        """
        得到链路对应的worker_ip等信息
        Args:
            topo (str): 拓扑名
            link (str): 节点名

        Returns:
            ip_ovs_map (dict): 得到worker_ip和vxlan对应网桥
        """
        link_worker_map = {'veth': {}, 'vxlan': {}}
        link_info = self.get_all_values(f'{topo}_{link}')
        src, tgt = link_info['sourceNE'], link_info['targetNE']
        vxlans =link_info.get('vxlan')
        if not vxlans:
            ip = self.get_worker_ip_by_ne_name(topo, src)
            link_worker_map['veth'][ip] = link
        else:
            src, tgt = vxlans
            tgt_ip = self.get_value(f'{topo}_{src}', 'remoteIP')
            src_ip = self.get_value(f'{topo}_{tgt}', 'remoteIP')
            # 这里是写入了源和目的的ovs名称， 这里是反过来的            
            # mwl 看到该函数只有一次调用， 因此直接修改redisAPI.py
            # 修改：需要存放ovs相关信息 不能只存放ovs名称了
            src_ovs_info = {'target': self.get_value(f'{topo}_{src}', 'target'), \
                            'src_veth': self.get_value(f'{topo}_{src}', 'sourceveth'),\
                            'service': link_info['sourceservice'],\
                            'id': link_info['sourceID'],\
                            'link': src,
                            }
            tgt_ovs_info = {'target': self.get_value(f'{topo}_{tgt}', 'target'), \
                            'src_veth': self.get_value(f'{topo}_{tgt}', 'sourceveth'),\
                            'service': link_info['targetservice'],\
                            'id': link_info['targetID'],\
                            'link': tgt,
                                    }
            link_worker_map['vxlan'][tgt_ip] = tgt_ovs_info
            link_worker_map['vxlan'][src_ip] = src_ovs_info
        return link_worker_map

    def get_parallel_by_nes(self, topo, ne1, ne2):
        """
        通过两个节点名得到并行链路的名字
        Args:
            topo (str): 拓扑名
            ne1 (str): 节点名
            ne2 (str): 节点名

        Returns:
            count (int): 并行链路的数量
        """
        count = 0
        links = self.get_value('plane_topo_list', topo)['links']
        for link in links:
            link_info = self.get_all_values(f'{topo}_{link}')
            if link_info['sourceNE'] == ne1 and link_info['targetNE'] == ne2:
                count = max(count,link_info['parallel'])
            elif link_info['sourceNE'] == ne2 and link_info['targetNE'] == ne1:
                count = max(count,link_info['parallel'])
        return count
        # for k, v in ne1_info.items():
        #     if k.startswith('link_l'):
        #         if v['name'] == :
        #             return k[6:]
        # raise KeyError(f"节点{ne1}和节点{ne2}没有并行链路")
    

    def get_value_by_pipeline(self, query_lst):
        """
        使用pipeline实现多key值查询
        [(table, key), (table, key), (table, key), (table, key)]
        查询不存在的值会返回None, 如果存在则为字符串
        Args:
            query_lst (list[tuple]): 查询列表

        Returns:
            list : 结果集的列表
        """
        with self._db_conn.pipeline() as pipe:
            for table, key in query_lst:
                    pipe.hget(table, key)
            raw_resp = pipe.execute()
        try:
            return [json.loads(resp) for resp in raw_resp]
        except TypeError:
            raise RuntimeError(f"表名或者键错误{query_lst}")

    def get_nic_by_interface(self, topo: str, ne: str, interface: str):
        """
        通过网卡接口查询实际物理网卡
        Args:
            topo      (str): 拓扑名
            ne        (str): 节点名
            interface (str): 接口名
        """
        ne_info = self.get_all_values(f'{topo}_{ne}')
        for k, v in ne_info.items():
            if k.startswith('link_l') and v['name'] == interface:
                return v['nic']
        raise KeyError(f"该节点没有接口{interface}")


class DB0(UserDB):
    def __init__(self):
        """
        操作DB0所用的类，使用方法同UserDB
        """
        self._db_conn = redis.StrictRedis(
            connection_pool=get_user_db_conn_pool(0))
