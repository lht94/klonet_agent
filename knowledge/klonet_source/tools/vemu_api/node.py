from .common import Manager, Node, NodeNotExistsError
import copy
from .common.base_classes import Dict2Class

class NodeManager(Manager):
    """节点管理类

    Attributes:
        user(str): 用户名
        project(str): 项目名

    """
    def __init__(self, user_name, project_name,
                backend_ip=None, backend_port=None):
        super().__init__(backend_ip, backend_port)
        self.user = user_name
        self.project = project_name

    def dynamic_add_node(self, node_name, image=None, resource_limit=None,
            location={"x": 0, "y": 0}, worker_specified=None, service="docker", 
            vm_port_num=None, portname = None):
        '''动态添加节点。

        注意：该API仅对已创建项目生效！

        Args:
            node_name(str): 所添加节点的名称
            image(Image): 节点所用镜像的Image对象，需先通过镜像相关API获取
            resource_limit(dict): 资源限制。例子：
                {"cpu": "1000", # CPU利用率限制，单位：%
                "mem": "1000" # 内存限制，单位：Mbytes}
                默认为None，将采用镜像中的默认资源限制。
            location(dict): 在前端画布上的横纵坐标，x和y均应该大于等于0。例子：
                {"x": 0,
                "y": 0}
                默认值同样为{"x":0, "y":0}
            worker_specified(str): 指定动态添加节点时的宿主机，如172.17.0.16（平台
                使用宿主机的ip地址来表示宿主机）；若不进行指定，则将由平台自动进行指
                定。
                
            ----------------------分界线 for Huawei_KVM Paras------------------
            提醒：
            为虚拟机增加了service和vm_port_num参数，并修改了image和resource_limit参数
            
            image(Image): 节点所用镜像的Image对象，需先通过KVM镜像相关API获取
            service(str): 节点服务类型，"docker" or "kvm"(虚机必须指定为"kvm")，默认"docker"
            resource_limit(dict): 资源限制。（需要注意单位上跟容器有所区别！）
            例子：
            {
                "cpu": "2", # KVM虚拟cpu个数，建议不要超过4
                "mem": "1024" # KVM内存大小，以2的10次方倍数来设置，如1024，单位：Mbytes
            }
            vm_port_num(int): 8  # 虚机默认开启的端口数量，建议手动指定，默认值随不同镜像而改变
            port_name(list):["nic_1","nic_2",...,"nic_8"]   # 虚机的系统呈现端口名称，默认为nic_N
            

        Returns:
            所添加节点的Node对象，便于后续直接对其进行相关操作

        Raises:
            HttpStatusError: 当HTTP的返回状态码不为200时，触发此异常
            JsonDecodeError: 当返回体不包含json时，触发此异常
            VemuExecError: 当HTTP请求成功，但json中的返回码不为1时，触发此异常
        '''
        # node对象构建
        if service == "docker" or service == "kvm":
            node = copy.deepcopy(image) # 将image对象当作node对象使用
        else:
            raise ValueError("当前节点服务类型不支持，请选择\"docker\"或者\"kvm\"")

        if resource_limit:
            node.resource_limit = resource_limit
        node.name = node_name
        node.x = location["x"]
        node.y = location["y"]
        if worker_specified:
            node.config.update({"worker_specified": worker_specified})

        # huawei_KVM Paras
        if node.service == "kvm":
            if vm_port_num:
                node.vm_config["port_num"] = vm_port_num
            if portname:
                if len(portname) == node.vm_config["port_num"]:
                    node.portname = portname
                else:
                    raise ValueError("端口命名数量与节点端口数目不符合，请指定所有" + {node.vm_config["port_num"]} + "个端口的名称！")

            # 默认名称
            if node.portname == None or node.portname == [] or len(node.portname) != node.vm_config["port_num"]:
                node.portname = [f"nic_{i}" for i in range(1, node.vm_config["port_num"] + 1)]
            # 创建一个用于检查json中节点port的连接关系的检查列表
            node.vm_config["check_port"] = [0] * (node.vm_config["port_num"] + 1)            

        payload = {"user": self.user, "topo": self.project,
            "info": node.dictform()}
        if service == "docker":
            resp = self._post("/modification/container/", json=payload)
        elif service == "kvm":
            resp = self._post("/modification/kvm/", json=payload)

        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)

        print(f"节点{node_name}动态添加成功！")
        return Node(**node.dictform())

    def dynamic_delete_node(self, node_name, service="docker"):
        '''动态删除节点。

        注意：该API仅对已创建项目生效！

        Args:
            node_name: 要删除节点名

        Returns:
            None

        Raises:
            HttpStatusError: 当HTTP的返回状态码不为200时，触发此异常
            JsonDecodeError: 当返回体不包含json时，触发此异常
            VemuExecError: 当HTTP请求成功，但json中的返回码不为1时，触发此异常
        '''
        node = self.get_node(node_name)

        payload = {"user": self.user, "topo": self.project, 
            "info": node.__dict__}
        if service == "docker":
            resp = self._delete("/modification/container/", json=payload)
        elif service == "kvm":
            resp = self._delete("/modification/kvm/", json=payload)
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        print(f"节点{node_name}动态删除成功！")
        

    def dynamic_modify_kvminterface(self, node_name, interface, new_name):
        '''动态修改虚机端口名称。

        注意：该API仅对已创建项目生效！

        Args:
            node_name: 要修改虚机节点名


        Returns:
            None

        Raises:
            HttpStatusError: 当HTTP的返回状态码不为200时，触发此异常
            JsonDecodeError: 当返回体不包含json时，触发此异常
            VemuExecError: 当HTTP请求成功，但json中的返回码不为1时，触发此异常
        '''
        node = self.get_node(node_name)
        payload = {"user": self.user, "topo": self.project, 
            "info":{'ne':node_name,'interface':interface,'name':new_name}}
        resp = self._post("/modification/interface/", json=payload)
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        print(f"虚机{node_name}动态修改端口{interface}名称为{new_name}成功！")

    # def dynamic_modify_node(self, node):
    #     '''
    #     TODO: 灵活性太多、输入参数不确定？
    #     '''
    #     pass

    def get_nodes(self):
        '''获取该项目下所有的节点名及对应的Node对象

        Returns:
            一个字典，包含该项目所有的节点名及对应的Node对象。例子：
            
            {"h1": h1的Node对象,
            "h2": h2的Node对象,
            "...": ...}

        Raises:
            HttpStatusError: 当HTTP的返回状态码不为200时，触发此异常
            JsonDecodeError: 当返回体不包含json时，触发此异常
            VemuExecError: 当HTTP请求成功，但json中的返回码不为1时，触发此异常
        '''
        resp = self._get(f"/re/project/{self.project}/",
            params={"user": self.user})
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)

        nodes = {}
        for type in resp_json["project"]["topo"].keys():
            if type == "links":
                continue
            for node_name, node_dict in resp_json["project"]["topo"][
                type].items():
                nodes[node_name] = Node(**node_dict)

        return nodes

    def get_node(self, node_name):
        '''获取目标节点的Node对象。

        Args:
            node_name(str): 节点名

        Returns:
            目标节点的Node对象。

        Raises:
            HttpStatusError: 当HTTP的返回状态码不为200时，触发此异常
            JsonDecodeError: 当返回体不包含json时，触发此异常
            VemuExecError: 当HTTP请求成功，但json中的返回码不为1时，触发此异常
            NodeNotExistsError: 当目标节点不存在时，触发此异常
        '''
        nodes = self.get_nodes()
        try:
            node = nodes[node_name]
            return node
        except KeyError:
            raise NodeNotExistsError(f"Node [{node_name}] does not exist, "
                f"avaliable nodes are {list(nodes.keys())}")


    def ssh_service(self, node_name, start, passwd="[REDACTED]"):
        """对该项目下一个节点开启或关闭ssh服务

        Args:
            node_name(str): 节点名
            start(bool): True为开启服务，False为关闭服务
            passwd(str): 为容器的root用户配置密码，初始为123456

        Returns:
            返回True为成功更改服务状态，否则直接报错
        """
        print("Starting SSH service is time-consuming, please be patient...")
        resp = self._post(f"/master/ssh_service/", \
            json={"user": self.user, "topo": self.project, "ne": node_name, "ssh": start, "passwd": passwd})
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        return True

    def get_port_mapping(self, node_name):
        """获得该项目下一个节点的端口映射，另外包括了节点所在worker的ip地址

        Args:
            node_name(str): 节点名

        Returns:
            节点的端口映射和worker的ip地址
        """
        resp = self._get(f"/master/ssh_service/", \
            json={"user": self.user, "topo": self.project, "ne": node_name})
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        return {"worker_ip": resp_json["worker_ip"], "ne_port": resp_json["ne_port"]}

    def modify_port_mapping(self, node_name, port_mapping):
        """修改该项目下一个节点（容器）到宿主机的端口映射

        Args:
            node_name(str): 节点名
            port_mapping(list): 端口映射，为一个包含偶数个元素的列表，
                index为偶数为容器端口，奇数为宿主机端口

        Returns:
            返回True为成功更改端口映射状态，否则直接报错
        """
        resp = self._put(f"/master/modify_port_mapping/", \
            json={"user": self.user, "topo": self.project, "ne": node_name, "port_mapping": port_mapping})
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        return True

    def get_nic_nickname2realname(self, node_name):
        """获取某一节点上所有端口的昵称到真实名字的对应关系

        Args:
            node_name(str): 节点名

        Returns:
            包含节点上所有端口的昵称到真实名字的对应关系字典，否则直接报错
            字典格式为：{'s1h1': 'ea857d34e8', 's1h2': 'b0a7d30a46'} 
        """
        resp = self._get(f"/my/edit/", params={"username": self.user, "toponame": self.project})
        resp_json = self._parse_resp(resp)
        nic_data = resp_json["static"]  # 从相应里提取对应关系信息
        return nic_data[node_name]

    def get_nic_realname2nickname(self, node_name):
        """获取某一节点上所有端口的真实名字到昵称的对应关系

        Args:
            node_name(str): 节点名

        Returns:
            包含节点上所有端口到网卡的对应关系字典，否则直接报错
            字典格式为：{'ea857d34e8': 's1h1', 'b0a7d30a46': 's1h2'}
        """
        nickname2realname_dict = self.get_nic_nickname2realname(node_name)
        return dict([val, key] for key, val in nickname2realname_dict.items())

    def get_node_worker_ip(self, node_name=None):
        """获取某一节点所在的worker的IP地址
        
        若node_name = None，则返回所有节点所在的worker的ip地址

        Args:
            node_name(str): 节点名

        Returns:
            节点所在的worker的ip地址，如：
            {"h1":"192.168.1.1", "h2": "192.168.1.2"}    
        """
        
        resp = self._get(f"/re/project/{self.project}/worker_ip/",
            params={"user": self.user})
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        all_node_worker_ip_info = resp_json["worker_ip"]
        if node_name == None:
            return all_node_worker_ip_info
        else:
            try:
                one_node_worker_ip_info={}
                one_node_worker_ip_info.setdefault(node_name,
                                                all_node_worker_ip_info[node_name])
                return one_node_worker_ip_info
            except KeyError:
                raise NodeNotExistsError(f"Node [{node_name}] does not exist, "
                    f"avaliable nodes are {list(all_node_worker_ip_info.keys())}")
                
    def get_network(self, node_name):
        """获取某一节点的外网连通情况

        Args:
            node_name(str): 节点名
        """
        resp = self._get('/master/node/network/', params={"user": self.user, "topo": self.project, "ne": node_name})
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        return print(node_name + resp_json["msg"])
        
    def enable_network(self, node_name):
        """启动某一节点的外网连接

        Args:
            node_name(str): 节点名
        """
        payload = {"user": self.user, "topo": self.project, "ne": node_name}
        resp = self._post('/master/node/network/', json=payload)
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        return print(node_name + resp_json["msg"])
    
    def stop_network(self, node_name):
        """关闭某一节点外网连接

        Args:
            node_name(str): 节点名
        """
        payload = {"user": self.user, "topo": self.project, "ne": node_name}
        resp = self._delete('/master/node/network/', payload)
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        return print(node_name + resp_json["msg"])
    
    # ovs远程配置接口
    def ovs_vsctl_tools(self, node_name, action, *content):
        '''ovs-vsctl各项配置命令远程接口
        
        Args:
            node_name(str): 节点名
            action(str): ovs-vsctl执行动作，如show，add-br等
            content(str): 动作的具体内容，具体内容得一次性写完，所以content只能为0或1个字符串
        
        Returns:
            命令执行结果
        '''
        if len(content) > 1:
            return print("content请输入完整的动作内容，不能拆开每个参数单独传入")
        payload = {
            "user": self.user,
            "topo": self.project,
            "node_name": node_name,
            "option": "ovs-vsctl",
            "action": action,
            "content":content
        }
        resp = self._post('/master/ovs_cmd/', payload)
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        output = self._output_trans(resp_json["exec_results"])
        return print(output)

    
    def ovs_ofctl_tools(self, node_name, action, *content):
        '''ovs-ofctl流表配置接口
        
        Args:
            node_name(str): 节点名
            action(str): ovs-ofctl执行动作，如add-flow，del-flows等
            content(str): 动作具体内容，具体内容得一次性写完，所以content只能为0或1个字符串
            
        Returns:
            命令执行结果
        '''
        if len(content) > 1:
            return print("content请输入完整的动作内容，不能拆开每个参数单独传入")
        payload = {
            "user": self.user,
            "topo": self.project,
            "node_name": node_name,
            "option": "ovs-ofctl",
            "action": action,
            "content":content
        }
        resp = self._post('/master/ovs_cmd/', payload)
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        output = self._output_trans(resp_json["exec_results"])
        return print(output)
    
    def _output_trans(self, exec_results):
        '''内部函数用于格式化输出命令执行结果
        
        Args:
            "exec_results": 平台执行命令返回的嵌套字典
        '''
        output = list((list(exec_results.values())[0]).values())[0]['output']
        return output
    
    # bmv2远程配置接口
    # 平台默认的bmv2镜像在/p4_test/目录下提供了不包含arp的基础ip转发的demo.p4
    def p4c_tools(self, node_name, p4_file_path, compiled_path):
        '''利用p4c将p4文件进行编译
        等效于命令: p4c -b bmv2 -o {compiled_path} {p4_file_path}
        
        Args:
            node_name(str): 节点名
            p4_file_path(str): p4文件的绝对路径
            compiled_path(str): 编译后的json和p4i的绝对输出路径
            
        Returns:
            编译执行结果，报错信息会被打印
        '''
        payload = {
            "user": self.user,
            "topo": self.project,
            "node_name": node_name,
            "option": "compile",
            "p4_file_path": p4_file_path,
            "compiled_path": compiled_path
        }
        resp = self._post('/master/bmv2/', json=payload)
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        output = self._output_trans(resp_json["exec_results"])
        return print(output)
    
    def bmv2_service_enable(self, node_name, target, port_mappings, json_path):
        '''选择某一个实例对某一个编译好的json在后台启动服务
        等效于命令: sudo {target} --interface {port_mapping1} --interface {port_mapping} {json_path} &
        
        Args:
            node_name(str): 节点名
            target(str): 服务运行的目标类型，支持simple_switch, simple_router, psa_switch
            port_mappings(list): 端口网卡映射关系，格式为: [0@toh1_1, 1@toh2_1]，需要用户谨慎确定映射关系，映射关系错误时大概率也能启动服务！
            json_path(str): 编译p4文件所生成json文件的绝对路径
            
        Returns:
            服务启动结果，报错信息会被打印
        '''
        # 服务运行的目标检查
        targets = ["simple_switch", "simple_router", "psa_switch"]
        if target not in targets:
            return print(f"当前不支持{target}目标来启动服务，请选择simple_switch, simple_router, psa_switch中的一种!")
        # 简单检查映射列表数据格式
        for mapping in port_mappings:
            if "@" not in mapping:
                return print(f"{mapping}端口映射格式出错，请确保所有的端口映射均为：端口号@网卡名(如1@toh2_1等)格式!")
        payload = {
            "user": self.user,
            "topo": self.project,
            "node_name": node_name,
            "option": "service",
            "state": "start",
            "target": target,
            "port_mappings": port_mappings,
            "json_path": json_path
        }
        resp = self._post('/master/bmv2/', json=payload)
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        # output = self._output_trans(resp_json["exec_results"])
        result = resp_json['msg']
        return print(result)
    
    def bmv2_service_stop(self, node_name):
        '''停止某一个bmv2节点的后台相关服务

        Args:
            node_name(str): 节点名
            
        Returns:
            服务停止结果
        '''
        payload = {
            "user": self.user,
            "topo": self.project,
            "node_name": node_name,
            "option": "service",
            "state": "stop"
        }
        resp = self._post('/master/bmv2/', json=payload)
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)
        result = resp_json['msg']
        return print(result)