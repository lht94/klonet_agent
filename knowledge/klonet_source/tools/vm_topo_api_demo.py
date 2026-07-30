# 平台的脚本仍然是需要在vemu_api同级进行使用
# 注意：以下脚本仅为参考，需要根据实际情况更改如镜像路径等参数，才可以正常执行


from vemu_api import *
import time

if __name__ == "__main__":
    # 用户名拓扑名配置
    user_name = "admin"
    project_name = "demo_test"  # 拓扑名尽量不要超过5个字符
    back_end_ip = "192.168.1.33"
    back_end_port = "20223"
    
    # 加载各个管理类
    image_manager = ImageManager(user_name,backend_ip=back_end_ip,backend_port=back_end_port)
    project_manager = ProjectManager(user_name,backend_ip=back_end_ip,backend_port=back_end_port)
    node_manager = NodeManager(user_name, project_name,backend_ip=back_end_ip,backend_port=back_end_port)
    link_manager = LinkManager(user_name, project_name,backend_ip=back_end_ip,backend_port=back_end_port)
    cmd_manager = CmdManager(user_name, project_name,backend_ip=back_end_ip,backend_port=back_end_port)
    
    # 获取docker镜像
    images = image_manager.get_images()
    ubuntu = images["ubuntu"]
    
    # 以上操作均跟平台原本api无异
    # --------------------------huawei_vm_PART--------------------------------
    # 以下主要针对add_node()和add_link()两个api进行了修改
    # 并给出相关的使用示例以及详细说明
    topo = Topo()
    
    '''添加节点'''
    # 添加一个容器节点（与平台原本无差异）
    docker_h1 = topo.add_node(image=ubuntu)
    
    # 添加一个虚拟机镜像
    # 获取KVM镜像
    kvm_images = image_manager.get_kvm_image()
    ne40e = kvm_images['ne40e.qcow2']
    centos7 = kvm_images["centos7.qcow2"]
    # 为虚拟机增加了service和vm_port_num参数，并修改了image和resource_limit参数
    '''
    image(Image): 节点所用镜像的Image对象，需先通过KVM镜像相关API获取
    service(str): 节点服务类型，"docker" or "kvm"(虚机必须指定为"kvm")，默认"docker"
    resource_limit(dict): 资源限制。例子：
    {
        "cpu": "2", # KVM虚拟cpu个数，建议不要超过4
        "mem": "1024" # KVM内存大小，以2的10次方倍数来设置，如1024，单位：Mbytes
    }
    "vm_port_num": -1  # 虚机默认开启的端口数量，建议手动指定，默认值-1表示按照默认设置端口：host - 1，router - 9
    '''
    
    # 
    # 建议不要指定节点名称，名称过长可能会导致出现bug！！！
    #
    
    # 添加虚拟机节点，使用平台默认的路由器镜像
    vm_h2 = topo.add_node(image=ne40e, service="kvm", vm_port_num=8)
    # # 添加虚拟机节点，使用用户通过web端上传的路由器镜像
    # vm_h3 = topo.add_node(image=centos7, service="kvm", vm_port_num=-1)
    # # 添加虚拟机节点，直接指定后端镜像路径, 确保每个worker上均有此镜像，且绝对路径相同
    # vm_h4 = topo.add_node(image=centos7, service="kvm", vm_port_num=-1)

    # 添加硬件节点
    # 获取用户可用的硬件设备
    hardware = image_manager.get_hardware(type='host')
    if len(hardware) == 0:
        raise Exception("用户没有可用的硬件设备，请等待资源释放或者手动备案加入设备!")
    else:
        id = hardware[0]
        print(id)
    # 获取设备的配置信息
    config = image_manager.get_hardware_config(id, type='host')
    # 增加节点
    hardware_h3 = topo.add_node(service="hardware",type="host",config=config)
    
    '''添加链路'''
    # 为涉及到虚拟机的链路增加了src_port和dst_port参数
    '''
    提醒：当链路源或目的一侧为虚拟机时，对应的src_IP或dst_IP参数不生效！
        涉及虚机的一侧必须手动指定下面的两个端口参数，不指定会报错！
        
    src_port(int): 源节点端口的index，从1开始，上限为该节点port_num设置的端口上限
    dst_port(int): 目的节点端口的index，从1开始，上限为该节点port_num设置的端口上限
    '''
    
    # docker_h1连接到vm_h2的第1个端口
    link1 = topo.add_link(docker_h1 ,vm_h2, src_IP="10.0.10.1/24", dst_port=1)
    # vm_h3的第1个端口连接到vm_h2的第3个端口上
    # link2 = topo.add_link(vm_h3, vm_h2, src_port=1, dst_port=3)
    # # vm_h4的第1个端口连接到vm_h2的第5个端口上
    # link3 = topo.add_link(vm_h4, vm_h2, src_port=1, dst_port=5)
    # link2 = topo.add_link(hardware_h3, vm_h2, dst_port=2)
    print(topo.__dict__)
    '''部署拓扑'''
    project_manager.deploy(project_name, topo)
    # time.sleep(60)
    
    
    # '''Docker容器的特殊设置'''    
    # # 有容器与虚机混合组网的拓扑中，需要固定执行以下代码(无需更改，固定执行便可)
    # # 以取消容器的接口包校验，防止与ne40e连接时出现TCP连接出现问题，并且将mtu设置为1450，避免vxlan出错
    # nodes = topo.get_nodes()
    # exec_cmd = cmd_manager.exec_cmds_in_nodes
    # docker_cmd = {obj.name: ["ls /sys/class/net/"]for obj in nodes.values() if obj.service == 'docker'}
    # if docker_cmd:
    #     docker_result = exec_cmd(docker_cmd)
    #     # print(docker_result)
    #     docker_eth = cmd_manager.extract_output(docker_result)
    #     docker_mtu, docker_checksum = cmd_manager.get_eth_cmd(docker_eth)
    #     re1=exec_cmd(docker_mtu)
    #     re2=exec_cmd(docker_checksum)
    # time.sleep(60)
        
        
    # '''直接命令执行（不区分容器或虚机）'''
    # cmd = {"n1":["ls"], "n2":["dis int"], "n3":["ls", "touch test.txt"]}
    # print(cmd_manager.exec_cmds_in_nodes(cmd))
    # time.sleep(120)
    
    
    # '''启动服务SSH登录(不区分容器或虚机）'''
    # # 为虚机建立22端口到宿主机的映射，列表里index为偶数表示虚拟机端口，为奇数表示worker宿主机端口
    # node_manager.modify_port_mapping("n3", [22, 32247])
    # # 获取端口映射结果
    # map_port = node_manager.get_port_mapping("n3")
    # print(map_port)
    # # ...在局域网内使用ssh客户端进行登录
    # time.sleep(240)
    
    
    # # '''项目删除'''
    # project_manager.destroy(project_name)