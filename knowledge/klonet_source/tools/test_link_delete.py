from vemu_api import *
import time

if __name__ == "__main__":
    # 用户名和项目名配置
    user_name = "sw"
    project_name = "222"
    
    # 管理类的后端ip和端口号可由参数指定（优先级高），或读取vemu_api包中
    # 的配置文件（config.py）
    backend_ip = "192.168.1.124"
    backend_port = 10021

    image_manager = ImageManager(user_name, backend_ip, backend_port)
    project_manager = ProjectManager(user_name, backend_ip, backend_port)
    node_manager = NodeManager(user_name, project_name, backend_ip, backend_port)
    link_manager = LinkManager(user_name, project_name, backend_ip, backend_port)
    cmd_manager = CmdManager(user_name, project_name, backend_ip, backend_port)
    
    '''拓扑设计'''
    images = image_manager.get_images()

    ubuntu_image = images["ubuntu"]
    ovs_image = images["ovs"]
    router_image = images["quagga"]

    topo = Topo()
    h1 = topo.add_node(ubuntu_image, node_name="h1")
    h2 = topo.add_node(ubuntu_image, node_name="h2")
    r1 = topo.add_node(router_image, node_name="r1")
    r2 = topo.add_node(router_image, node_name="r2")
    s1 = topo.add_node(ovs_image, node_name="s1")
    s2 = topo.add_node(ovs_image, node_name="s2")
    topo.add_link(h1, s1, link_name="l1", src_IP="192.168.1.2/24")
    topo.add_link(s1, s2, link_name="l7")
    topo.add_link(s1, h2, link_name="l2", dst_IP="192.168.2.2/24")
    topo.add_link(h1, h2, link_name="l3", dst_IP="192.168.3.1/24", src_IP="192.168.3.2/24")
    topo.add_link(r1, s1, link_name="l4", src_IP="192.168.4.1/24")
    topo.add_link(r1, h1, link_name="l5", src_IP="192.168.5.1/24", dst_IP="192.168.5.2/24")
    topo.add_link(r1, r2, link_name="l6", src_IP="192.168.6.1/24", dst_IP="192.168.6.2/24")

    '''项目创建'''
    project_manager.deploy(project_name, topo)
    print(f"Deploy {project_name} successfully!")

    '''初始参数'''
    rpt_times = 50

    ################### 测试修改链路配置 ###################
    del_time_list = []
    add_time_list = []
    '''00000 删除与新增l1中的tc规则(多命令)'''
    for i in range(rpt_times):
        '''删除链路'''
        t = time.time()
        cmd_manager.exec_cmds_in_nodes({"s1": ["tc qdisc add dev toh1 root netem loss 100%"]})
        cmd_manager.exec_cmds_in_nodes({"h1": ["tc qdisc add dev tos1 root netem loss 100%"]})
        del_time_list.append(time.time() - t)

        '''新增链路'''
        t = time.time()
        cmd_manager.exec_cmds_in_nodes({"s1": ["tc qdisc del dev toh1 root"]})
        cmd_manager.exec_cmds_in_nodes({"h1": ["tc qdisc del dev tos1 root"]})
        add_time_list.append(time.time() - t)
    print(del_time_list)
    print(add_time_list)

    del_time_list = []
    add_time_list = []
    '''00000’ 删除与新增l1中的tc规则(单命令)'''
    for i in range(rpt_times):
        '''删除链路'''
        t = time.time()
        cmd_manager.exec_cmds_in_nodes({"s1": ["tc qdisc add dev toh1 root netem loss 100%"]})
        del_time_list.append(time.time() - t)

        '''新增链路'''
        t = time.time()
        cmd_manager.exec_cmds_in_nodes({"s1": ["tc qdisc del dev toh1 root"]})
        add_time_list.append(time.time() - t)
    print(del_time_list)
    print(add_time_list)

    del_time_list = []
    add_time_list = []
    '''11111 删除与新增l5中的tc规则'''
    for i in range(rpt_times):
        '''删除链路'''
        t = time.time()
        cmd_manager.exec_cmds_in_nodes({"r1": ["tc qdisc add dev toh1 root netem loss 100%"]})
        cmd_manager.exec_cmds_in_nodes({"h1": ["tc qdisc add dev tor1 root netem loss 100%"]})
        del_time_list.append(time.time() - t)

        '''新增链路'''
        t = time.time()
        cmd_manager.exec_cmds_in_nodes({"r1": ["tc qdisc del dev toh1 root"]})
        cmd_manager.exec_cmds_in_nodes({"h1": ["tc qdisc del dev tor1 root"]})
        add_time_list.append(time.time() - t)
    print(del_time_list)
    print(add_time_list)

    del_time_list = []
    add_time_list = []
    '''11111 删除与新增l6中的tc规则'''
    for i in range(rpt_times):
        '''删除链路'''
        t = time.time()
        cmd_manager.exec_cmds_in_nodes({"r1": ["tc qdisc add dev tor2 root netem loss 100%"]})
        cmd_manager.exec_cmds_in_nodes({"r2": ["tc qdisc add dev tor1 root netem loss 100%"]})
        del_time_list.append(time.time() - t)

        '''新增链路'''
        t = time.time()
        cmd_manager.exec_cmds_in_nodes({"r1": ["tc qdisc del dev tor2 root"]})
        cmd_manager.exec_cmds_in_nodes({"r2": ["tc qdisc del dev tor1 root"]})
        add_time_list.append(time.time() - t)
    print(del_time_list)
    print(add_time_list)



    ##################### 测试增删链路 #####################
    

    del_time_list = []
    add_time_list = []
    '''00000 重复删除与新增switch到switch'''
    for i in range(rpt_times):
        '''删除链路'''
        t = time.time()
        link_manager.dynamic_delete_link("l7")
        del_time_list.append(time.time() - t)

        '''新增链路'''
        t = time.time()
        link_manager.dynamic_add_link("l7", s1, s2)
        add_time_list.append(time.time() - t)
    print(del_time_list)
    print(add_time_list)

    del_time_list = []
    add_time_list = []
    '''11111 重复删除与新增ubuntu到switch'''
    for i in range(rpt_times):
        '''删除链路'''
        t = time.time()
        link_manager.dynamic_delete_link("l1")
        del_time_list.append(time.time() - t)

        '''新增链路'''
        t = time.time()
        link_manager.dynamic_add_link("l1", h1, s1, src_IP="192.168.3.2/24")
        add_time_list.append(time.time() - t)
    print(del_time_list)
    print(add_time_list)

    del_time_list = []
    add_time_list = []
    '''22222 重复删除与新增ubuntu到ubuntu'''
    for i in range(rpt_times):
        '''删除链路'''
        t = time.time()
        link_manager.dynamic_delete_link("l3")
        del_time_list.append(time.time() - t)

        '''新增链路'''
        t = time.time()
        link_manager.dynamic_add_link("l3", h1, h2, dst_IP="192.168.3.1/24", src_IP="192.168.3.2/24")
        add_time_list.append(time.time() - t)
    print(del_time_list)
    print(add_time_list)

    del_time_list = []
    add_time_list = []
    '''33333 重复删除与新增router到switch'''
    for i in range(rpt_times):
        '''删除链路'''
        t = time.time()
        link_manager.dynamic_delete_link("l4")
        del_time_list.append(time.time() - t)

        '''新增链路'''
        t = time.time()
        link_manager.dynamic_add_link("l4", r1, s1, src_IP="192.168.3.2/24")
        add_time_list.append(time.time() - t)
    print(del_time_list)
    print(add_time_list)

    del_time_list = []
    add_time_list = []
    '''44444 重复删除与新增router到ubuntu'''
    for i in range(rpt_times):
        '''删除链路'''
        t = time.time()
        link_manager.dynamic_delete_link("l5")
        del_time_list.append(time.time() - t)

        '''新增链路'''
        t = time.time()
        link_manager.dynamic_add_link("l5", r1, h1, dst_IP="192.168.3.1/24", src_IP="192.168.3.2/24")
        add_time_list.append(time.time() - t)
    print(del_time_list)
    print(add_time_list)

    del_time_list = []
    add_time_list = []
    '''55555 重复删除与新增router到router'''
    for i in range(rpt_times):
        '''删除链路'''
        t = time.time()
        link_manager.dynamic_delete_link("l6")
        del_time_list.append(time.time() - t)

        '''新增链路'''
        t = time.time()
        link_manager.dynamic_add_link("l6", r1, r2, dst_IP="192.168.3.1/24", src_IP="192.168.3.2/24")
        add_time_list.append(time.time() - t)
    print(del_time_list)
    print(add_time_list)

    '''项目删除'''
    project_manager.destroy(project_name)