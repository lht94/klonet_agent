from vemu_api import *

# 请确保该IP和PORT可达
MASTER_IP = "192.168.1.124"
MASTER_PORT = "10021"

if __name__ == "__main__":
    # 用户名和项目名配置
    user_name = "sw"
    project_name = "demo_test"
    
    image_manager = ImageManager(user_name, backend_ip=MASTER_IP, 
        backend_port=MASTER_PORT)
    project_manager = ProjectManager(user_name, backend_ip=MASTER_IP, 
        backend_port=MASTER_PORT)
    node_manager = NodeManager(user_name, project_name, backend_ip=MASTER_IP, 
        backend_port=MASTER_PORT)
    link_manager = LinkManager(user_name, project_name, backend_ip=MASTER_IP, 
        backend_port=MASTER_PORT)
    cmd_manager = CmdManager(user_name, project_name, backend_ip=MASTER_IP, 
        backend_port=MASTER_PORT)
    
    '''拓扑设计'''
    images = image_manager.get_images()

    # 可仿照此处，添加时间同步镜像的对象
    ubuntu_image = images["ubuntu"]
    ovs_image = images["ovs"]
    ryu_image = images["ryu"]

    # 设置ovs属性
    ovs_image.config["stp"] = False
    ovs_image.config["controllers"] = ["c1"]

    '''
    拓扑形状：
    
    h1       s1
      \     /  \
       \   /     \
        s2--------s3----h2
        / \      /
       /   \   /
    h3       s4             c1(与所有交换机相连)

    host ip: h<x>的ip地址为192.168.1.x
    时延：s2->s1 s2->s3 s2->s4均设置了30ms时延
    '''
    topo = Topo()
    h1 = topo.add_node(ubuntu_image, node_name="h1", location={"x":137, "y":384})
    h2 = topo.add_node(ubuntu_image, node_name="h2", location={"x":706, "y":303})
    h3 = topo.add_node(ubuntu_image, node_name="h3", location={"x":148, "y":205})
    s1 = topo.add_node(ovs_image, node_name="s1", location={"x":407, "y":137})
    s2 = topo.add_node(ovs_image, node_name="s2", location={"x":301, "y":263})
    s3 = topo.add_node(ovs_image, node_name="s3", location={"x":533, "y":266})
    s4 = topo.add_node(ovs_image, node_name="s4", location={"x":405, "y":395})
    c1 = topo.add_node(ryu_image, node_name="c1", location={"x":200, "y":21})
    
    topo.add_link(h1, s2, link_name="l1", src_IP="192.168.1.1/24")
    topo.add_link(h3, s2, link_name="l2", src_IP="192.168.1.3/24")
    topo.add_link(h2, s3, link_name="l3", src_IP="192.168.1.2/24")
    topo.add_link(s2, s1, link_name="l4")
    topo.add_link(s2, s3, link_name="l5")
    topo.add_link(s2, s4, link_name="l6")
    topo.add_link(s1, s3, link_name="l7")
    topo.add_link(s3, s4, link_name="l8")

    '''项目创建'''
    projects = project_manager.get_projects()
    if project_name not in projects: # 只有当项目不存在时才创建
        project_manager.deploy(project_name, topo)
        print("Now, we start ssh service in c1...")
        node_manager.ssh_service("c1", True, passwd="[REDACTED]") # 启动ssh服务
        print("SSH service has started!")
        node_manager.modify_port_mapping("c1", [80, 35711])

    else:
        print("This project has already been deployed, so we do not deploy it "
            "again.")

    '''链路配置'''
    link_attributes = {
        ("s2", "s1"): ( # 链路两端节点
            {"bw_kbps": "10000", "delay_us": "30000"}, # s2侧出端队列
            {"bw_kbps": "10000"}, # s1侧出端队列
            ),
        ("s2", "s3"): (
            {"bw_kbps": "10000", "delay_us": "30000"},
            {"bw_kbps": "10000"},
            ),
        ("s2", "s4"): (
            {"bw_kbps": "10000", "delay_us": "30000"},
            {"bw_kbps": "10000"},
            ),
    }

    links = link_manager.get_links()
    for link_name in links:
        link_key = (links[link_name].source, links[link_name].target)
        if link_key in link_attributes.keys():
            properties = {}
            properties.update(link_attributes[link_key][0])
            properties.update({"link": link_name, "ne": link_key[0]})
            src_link_config = LinkConfiguration(**properties)

            properties = {}
            properties.update(link_attributes[link_key][1])
            properties.update({"link": link_name, "ne": link_key[1]})
            dst_link_config = LinkConfiguration(**properties)
            
            link_manager.config_link(src_link_config, dst_link_config)
            print(f"config {link_key} done!")
                
    '''SSH服务获取'''
    # 这里获取端口映射
    print("Port mapping on c1 is: (the worker ip is the LAN ip, if you access"
        " the server by WAN, please use the WAN ip)")
    print(node_manager.get_port_mapping("c1"))

    print("Initialization complete! Please upload sdn_path.py and vemu_api "
        "folder to c1 and run!")
    
    # '''命令执行'''
    # exec_results = cmd_manager.exec_cmds_in_nodes(
    #     {"demo_h1": ["ls"], "demo_h2": ["ifconfig"]})
    # print("exec_results: ", exec_results)

    # '''动态增删'''
    # demo_h3 = node_manager.dynamic_add_node("demo_h3", ubuntu_image)
    # print("Add demo_h3 successfully! Please check the effect at the frontend! "
    #     "Sleep 20s...")
    # time.sleep(20) # 请在前端查看动态增加节点效果
    
    # link_manager.dynamic_add_link("demo_l3", demo_h3, demo_s1, 
    #     src_IP="192.168.1.1/24")
    # print("Add demo_l3 successfully! Please check the effect at the frontend! "
    #     "Sleep 20s...")
    # time.sleep(20) # 请在前端查看动态增加链路效果
    
    # link_manager.dynamic_delete_link("demo_l3")
    # print("Delete demo_l3 successfully! Please check the effect at the "
    #     "frontend! Sleep 20s...")
    # time.sleep(20) # 请在前端查看动态删除链路效果
    
    # node_manager.dynamic_delete_node("demo_h3")
    # print("Delete demo_h3 successfully! Please check the effect at the "
    #     "frontend! Sleep 20s...")
    # time.sleep(20) # 请在前端查看动态删除节点效果

    # '''项目删除'''
    # project_manager.destroy(project_name)
    # print(f"Destroy {project_name} successfully! Please check the effect at "
    #     "the frontend!")
    # print("vemu_api_demo done!")