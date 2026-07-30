# 平台的脚本仍然是需要在vemu_api同级进行使用
# 注意：以下脚本仅为参考，需要根据实际情况更改如镜像路径等参数，才可以正常执行


from vemu_api import *
import time

if __name__ == "__main__":
    # 用户名拓扑名配置
    user_name = "slzl"
    project_name = "ts1"
    back_end_ip = "192.168.1.33"
    back_end_port = "33222"
    flag = 5 # 0代表删除拓扑，1代表创建拓扑
    
    # 加载各个管理类
    image_manager = ImageManager(user_name,backend_ip=back_end_ip,backend_port=back_end_port)
    project_manager = ProjectManager(user_name,backend_ip=back_end_ip,backend_port=back_end_port)
    node_manager = NodeManager(user_name, project_name,backend_ip=back_end_ip,backend_port=back_end_port)
    link_manager = LinkManager(user_name, project_name,backend_ip=back_end_ip,backend_port=back_end_port)
    cmd_manager = CmdManager(user_name, project_name,backend_ip=back_end_ip,backend_port=back_end_port)

    # 获取docker镜像
    images = image_manager.get_images()
    ubuntu = images["ubuntu"]
    
    # 获取KVM镜像，目前仅NE40E和CENTOS7可用
    kvm_images = image_manager.get_kvm_image()
    centos7 = kvm_images["centos7"]
    # centos7 = kvm_images["centos7.qcow2"]
    # ne40e = kvm_images["ne40e.qcow2"]
    
    if flag == 1:
        topo = Topo()

        vm_h1 = topo.add_node(service="kvm", image=centos7, vm_port_num=3, portname=['lily','hanse','sily'])
        dock_h2 = topo.add_node(image=ubuntu)
        # vm_h2 = topo.add_node(service="kvm", image=ne40e, resource_limit={"cpu": "4", "mem": "4096"}, vm_port_num=3, portname = ['lily','hanse','sily'])
        
        link1 = topo.add_link(vm_h1 ,dock_h2, src_port=1, dst_IP="192.168.134.1/24") 
        # link2 = topo.add_link(dock_h2, vm_h2, src_IP="192.168.135.1/24", dst_port=1)  
        
        '''部署拓扑'''
        project_manager.deploy(project_name, topo)
 
        '''Docker容器的特殊设置'''    
        # 有容器与虚机混合组网的拓扑中，需要固定执行以下代码(无需更改，固定执行便可)
        # 以取消容器的接口包校验，防止与ne40e连接时出现TCP连接出现问题，并且将mtu设置为1450，避免vxlan出错
        nodes = topo.get_nodes()
        exec_cmd = cmd_manager.exec_cmds_in_nodes
        docker_cmd = {obj.name: ["ls /sys/class/net/"]for obj in nodes.values() if obj.service == 'docker'}
        if docker_cmd:
            docker_result = exec_cmd(docker_cmd)
            # print(docker_result)
            docker_eth = cmd_manager.extract_output(docker_result)
            docker_mtu, docker_checksum = cmd_manager.get_eth_cmd(docker_eth)
            re1=exec_cmd(docker_mtu)
            re2=exec_cmd(docker_checksum)
    
    elif flag == 2:
        '''动态增加网元'''
        # 创建新的网元
        node_manager.dynamic_add_node("n4", service="kvm", image=ne40e, vm_port_num=3, portname = ['lily','hanse','sily'])
        node_manager.dynamic_add_node("n5", image=ubuntu, service="docker")
    
    elif flag == 3:
        ''''动态删除网元'''
        # 删除网元
        node_manager.dynamic_delete_node("n1", service="kvm")
        node_manager.dynamic_delete_node("n2", service="docker")
        node_manager.dynamic_delete_node("n3", service="kvm")
        node_manager.dynamic_delete_node("n4", service="kvm")

    elif flag == 4:
        ''''动态添加链路'''
        """
        postman
        增加链路

        example:
            POST 192.168.1.33:33222/modification/link/

            {
                "user": "slzl",
                "topo": "ts1",
                "info": {
                    "name": "l1",
                    "source": "n1",
                    "sourceIP": "",
                    "sourceType": "router",
                    "target": "n2",
                    "targetIP": "",
                    "targetType": "host",
                    "VMsourcePort": 1,
                    "VMtargetPort": 1,
                }
            }
        """

        """
        删除链路

        example:
            DELETE 192.168.1.33:33222/modification/link/

            {
                "user": "slzl",
                "topo": "ts1",
                "info": {
                    "name": "l1",
                    "source": "n1",
                    "target": "n2"
                }
            }
        """
        # 获取对应网页
        source_node = node_manager.get_node("n1")
        target_node = node_manager.get_node("n2")
        link_manager.dynamic_add_link(link_name="l1", src_node=source_node, dst_node=target_node,vm_sourcePort=1)
    elif flag == 5:
        ''''动态删除链路'''
        # 删除对应链路
        link_manager.dynamic_delete_link("l1")
    elif flag == 6:
        '''动态修改虚机端口名称'''
        node_manager.dynamic_modify_kvminterface(node_name="n1",interface="eth1",new_name="hghhh")
        
    elif flag == 0:
        '''项目删除'''
        project_manager.destroy(project_name)