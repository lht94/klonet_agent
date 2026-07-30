# vm_7
#       host_vm--host_vm
#            vxlan
# iperf(3)：只记得tcp打流不正常，udp好像是正常的？（可能也需要指定-l参数？）
# 利用-m参数指定MSS为1410（即MTU=1450），tcp打流正常，需要为vxlan预留50字节的开销

# vm_8
#       host_d--host_d
#            vxlan
# iperf(3)：只记得tcp打流不正常，udp好像是正常的？（可能也需要指定-l参数？）

# vm_9
#       host_d--ar1000--host_d
#           vxlan  veth_pair
# iperf(3):tcp打流正常,udp需要指定-l参数才打流正常

# vm_10
#       host_d--ne40e--host_d
#         vxlan     vxlan
# iperf(3):tcp，udp打流均正常


from vemu_api import *


if __name__ == "__main__":
    user_name = "super_wudx"
    project_name = "dk2"
    back_end_ip = "192.168.1.33"
    back_end_port = "25522"
    
    image_manager = ImageManager(user_name,backend_ip=back_end_ip,backend_port=back_end_port)
    project_manager = ProjectManager(user_name,backend_ip=back_end_ip,backend_port=back_end_port)
    node_manager = NodeManager(user_name, project_name,backend_ip=back_end_ip,backend_port=back_end_port)
    link_manager = LinkManager(user_name, project_name,backend_ip=back_end_ip,backend_port=back_end_port)
    cmd_manager = CmdManager(user_name, project_name,backend_ip=back_end_ip,backend_port=back_end_port)
    
    topo = Topo()
    images = image_manager.get_images()
    ubuntu = images["ubuntu"]
    kvm_image = image_manager.get_kvm_image()
    ne40e = kvm_image['bbbb.qcow2']
    print(kvm_image)
    demo_h1 = topo.add_node(image=ne40e, service="kvm", vm_port_num=2)
    demo_h2 = topo.add_node(image=ne40e, service="kvm", vm_port_num=2)
    # demo_h2 = topo.add_node(image=ubuntu) 
    # demo_h1 = topo.add_node(service="kvm",vm_config={"kvm_image":{"image_path":"default", "qcow2_size":-1}, "type":"host", "port_num":-1})
    # # demo_h2 = topo.add_node(service="kvm",vm_config={"kvm_image":{"image_path":"default", "qcow2_size":-1}, "type":"router", "port_num":4})
    # demo_h2 = topo.add_node(service="kvm",vm_config={"kvm_image":{"image_path":"default", "qcow2_size":-1}, "type":"host", "port_num":-1})
    # demo_h1 = topo.add_node(service="kvm",vm_config={"kvm_image":{"image_path":"default", "qcow2_size":-1}, "type":"host", "port_num":1})
    # demo_h2 = topo.add_node(service="kvm",vm_config={"kvm_image":{"image_path":"default", "qcow2_size":-1}, "type":"router", "port_num":2})
    # demo_h3 = topo.add_node(service="kvm",vm_config={"kvm_image":{"image_path":"default", "qcow2_size":-1}, "type":"router", "port_num":3})
    # demo_h4 = topo.add_node(service="kvm",vm_config={"kvm_image":{"image_path":"default", "qcow2_size":-1}, "type":"host", "port_num":1})
    # demo_h1 = topo.add_node(service="kvm",vm_config={"kvm_image":{"image_path":"default", "qcow2_size":-1}, "type":"host", "port_num":1})
    # demo_h2 = topo.add_node(service="kvm",vm_config={"kvm_image":{"image_path":"default", "qcow2_size":-1}, "type":"host", "port_num":1})
    # demo_h3 = topo.add_node(image=ubuntu)
    # demo_h4 = topo.add_node(image=ubuntu)
    # link1 = topo.add_link(demo_h1, demo_h2)
    # link1 = topo.add_link(demo_h1 ,demo_h2, src_IP="192.168.2.1/24",dst_IP="192.168.2.2/24",src_port=1,dst_port=30)
    link1 = topo.add_link(demo_h1 ,demo_h2, src_port=1, dst_port=1)
    # link2 = topo.add_link(demo_h2, demo_h3, src_port=2, dst_port=1)
    # link3 = topo.add_link(demo_h3, demo_h4, src_port=2, dst_port=1)
    # link4 = topo.add_link(demo_h5, demo_h3, src_IP="192.168.44.2/24", src_port=1, dst_port=3)


    # link2 = topo.add_link(demo_h2, demo_h3, src_port=2, dst_port=1)
    # link3 = topo.add_link(demo_h2, demo_h4, src_port=3, dst_IP="10.0.3.1/24")
    # link2 = topo.add_link(demo_h2, demo_h3, src_port=1)
    # demo_h2 = topo.add_node(ubuntu,service="docker",resource_limit={"cpu":"1", "mem":"2048"},vm_config={"kvm_image":{"image_path":"default", "qcow2_size":5}, "type":"host"})


    is_deploy =False
    if is_deploy:
        project_manager.deploy(project_name, topo)
        # project_manager.destroy(project_name)

        # 下面代码是为了关闭拓扑中容器节点的接口校验码（防止ne40e镜像出现tcp连接问题），并设置mtu为1450（防止vxlan出现tcp连接问题）  --gjh
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
            # print(nodes)
            # print(docker_cmd)
            # print(docker_eth)
            # print(docker_mtu)
            # print(docker_checksum)
            # print(re1)
            # print(re2)
            # project_manager.destroy(project_name) 
    else:
        project_manager.destroy(project_name)
