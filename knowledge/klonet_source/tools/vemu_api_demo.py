# vemu_api_demo.py，用于演示vemu_api的使用
from vemu_api import *
import time

if __name__ == "__main__":
    # 用户名和项目名配置
    user_name = "demo_user"
    project_name = "demo_test"
    
    # 管理类的后端ip和端口号可由参数指定（优先级高），或读取vemu_api包中
    # 的配置文件（config.py）
    backend_ip = "220.243.137.82"
    backend_port = 12352

    image_manager = ImageManager(user_name, backend_ip, backend_port)
    project_manager = ProjectManager(user_name, backend_ip, backend_port)
    node_manager = NodeManager(user_name, project_name, backend_ip, 
        backend_port)
    link_manager = LinkManager(user_name, project_name, backend_ip,
        backend_port)
    cmd_manager = CmdManager(user_name, project_name, backend_ip, backend_port)
    
    '''拓扑设计'''
    images = image_manager.get_images()

    ubuntu_image = images["ubuntu"]
    ovs_image = images["ovs"]

    topo = Topo()
    h1 = topo.add_node(ubuntu_image, node_name="h1",
        location={"x": 711, "y": 352})
    h2 = topo.add_node(ubuntu_image, node_name="h2",
        location={"x": 365, "y": 352})
    s1 = topo.add_node(ovs_image, node_name="s1",
        location={"x": 538, "y": 452})
    topo.add_link(h1, s1, link_name="l1", src_IP="192.168.1.1/24")
    topo.add_link(s1, h2, link_name="l2", dst_IP="192.168.1.2/24")

    '''项目创建'''
    project_manager.deploy(project_name, topo)
    print(f"Deploy {project_name} successfully! Please check the effect at the "
        "frontend! Sleep 20s...")
    time.sleep(20)

    '''项目获取'''
    projects_list = project_manager.get_projects()
    print("Projects_list: ", projects_list)

    '''节点获取'''
    nodes_dict = node_manager.get_nodes()
    print("Nodes_dict: ", nodes_dict)
    temp_node = node_manager.get_node("h1")
    print("The result of get_node is: ", temp_node)

    '''链路获取'''
    links_dict = link_manager.get_links()
    print("Links_dict: ", links_dict)
    temp_link = link_manager.get_link("l1")
    print("The result of get_link is: ", temp_link)

    '''链路配置'''
    src_link_config = LinkConfiguration(bw_kbps="2000", 
        queue_size_bytes="200000", link="l1", ne="h1")
    dst_link_config = LinkConfiguration(bw_kbps="4000", delay_us="30000",
        link="l1", ne="s1")
    link_manager.config_link(src_link_config, dst_link_config)
    print(f"Config l1 successfully! Please check the effect at the "
        "frontend! Sleep 20s...")
    time.sleep(20)
    link_manager.clear_link_configuration("l1")
    print(f"Clear l1 configuration successfully! Please check the effect "
        "at the frontend! Sleep 20s...")
    time.sleep(20)
    
    '''命令执行'''
    exec_results = cmd_manager.exec_cmds_in_nodes(
        {"h1": ["ls"], "h2": ["ifconfig"]})
    print("Exec_results: ", exec_results)

    '''动态增删'''
    h3 = node_manager.dynamic_add_node("h3", ubuntu_image)
    print("Add h3 successfully! Please check the effect at the frontend! "
        "Sleep 20s...")
    time.sleep(20) # 请在前端查看动态增加节点效果
    
    link_manager.dynamic_add_link("l3", h3, s1, 
        src_IP="192.168.1.1/24")
    print("Add l3 successfully! Please check the effect at the frontend! "
        "Sleep 20s...")
    time.sleep(20) # 请在前端查看动态增加链路效果
    
    link_manager.dynamic_delete_link("l3")
    print("Delete l3 successfully! Please check the effect at the "
        "frontend! Sleep 20s...")
    time.sleep(20) # 请在前端查看动态删除链路效果
    
    node_manager.dynamic_delete_node("h3")
    print("Delete h3 successfully! Please check the effect at the "
        "frontend! Sleep 20s...")
    time.sleep(20) # 请在前端查看动态删除节点效果

    '''ssh服务'''
    node_manager.ssh_service("h1", True)
    node_manager.modify_port_mapping("h1", [22, 34567, 80, 35711])
    print(node_manager.get_port_mapping("h1"))
    print("SSH started successfully! Sleep 20s...")
    time.sleep(20)

    '''网卡对应'''
    print('nickname2realname:', node_manager.get_nic_nickname2realname("s1"))
    print('realname2nickname:', node_manager.get_nic_realname2nickname("s1"))
    print("Port and NIC matched successfully! Sleep 20s...")
    time.sleep(20)

    '''项目删除'''
    project_manager.destroy(project_name)
    print(f"Destroy {project_name} successfully! Please check the effect at "
        "the frontend!")
    print("vemu_api_demo done!")