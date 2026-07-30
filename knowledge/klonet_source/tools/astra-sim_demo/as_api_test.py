import configparser
import os
import re
import time
import requests
import json
import math
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vemu_api import *

# 将字符串转换为合适的类型
def convert_value(value):
    """Convert string values into appropriate types (int, float, or list)."""
    if ',' in value:
        return [convert_value(v.strip()) for v in value.split(',')]
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value  # Default is to return the value as a string

# 读取单个节点的 INI 文件
def read_node_ini_file(node_ini_file_path):
    config = configparser.ConfigParser()
    config.read(node_ini_file_path)
    
    node_data = {}

    for section in config.sections():
        data = {}
        for key, value in config.items(section):
            data[key] = convert_value(value)
        node_data[section] = data
    
    return node_data

# 生成每个节点的文件
def generate_node_files_for_single_node(node_data, project_input_folder_path, node_name):
    # 获取节点的数据
    node_data_for_node = node_data.get(node_name)
    if not node_data_for_node:
        raise ValueError(f"Node {node_name} not found in provided data.")

    # 可能存在嵌套情况，我们需要进一步展开字典
    if isinstance(node_data_for_node, dict):
        # 如果嵌套层次为两层，我们选择获取嵌套字典的第一层
        if len(node_data_for_node) == 1:
            node_data_for_node = list(node_data_for_node.values())[0]
    
    # 网络部分的处理
    network_section = {
        "topology": node_data_for_node.get("topologies"),
        "npus_count": node_data_for_node.get("npus-count"),
        "bandwidth": node_data_for_node.get("bandwidth"),
        "latency": node_data_for_node.get("latency"),
    }

    # 确保每个字段都为列表
    for key in ["topology", "npus_count", "bandwidth", "latency"]:
        if network_section[key] is not None and not isinstance(network_section[key], list):
            network_section[key] = [network_section[key]]

    # 系统部分的处理
    system_data = {}
    possible_keys = [
        "scheduling-policy", "endpoint-delay", "active-chunks-per-dimension",
        "preferred-dataset-splits", "all-reduce-implementation", "all-gather-implementation",
        "reduce-scatter-implementation", "all-to-all-implementation", "collective-optimization",
        "local-mem-bw", "boost-mode"
    ]
    
    for key in possible_keys:
        if key in node_data_for_node:
            if key in ["all-reduce-implementation", "all-gather-implementation", "reduce-scatter-implementation", "all-to-all-implementation"]:
                # 对某些字段，确保它们是列表
                system_data[key] = [node_data_for_node[key]] if isinstance(node_data_for_node[key], str) else node_data_for_node[key]
            else:
                system_data[key] = node_data_for_node[key]

    # 创建输出文件夹
    node_output_folder_path = os.path.join(project_input_folder_path, node_name)
    os.makedirs(node_output_folder_path, exist_ok=True)

    # 文件路径
    network_output_file_path = os.path.join(node_output_folder_path, 'network.yml')
    system_output_file_path = os.path.join(node_output_folder_path, 'system.json')

    # 写入 network.yml 文件
    with open(network_output_file_path, 'w') as yml_file:
        yml_content = (
            f"topology: [{', '.join(map(str, network_section['topology']))}]\n"
            f"npus_count: [{', '.join(map(str, network_section['npus_count']))}]\n"
            f"bandwidth: [{', '.join(map(str, network_section['bandwidth']))}]\n"
            f"latency: [{', '.join(map(str, network_section['latency']))}]\n"
        )
        yml_file.write(yml_content)

    # 写入 system.json 文件
    with open(system_output_file_path, 'w') as json_file:
        json.dump(system_data, json_file, indent=2)

    print(f"Files for node {node_name} have been generated successfully!")

    # 处理 workload 文件
    workload_file_name = node_data_for_node.get('workload')  # 获取工作负载文件名
    if workload_file_name:
        # 判断工作负载文件是否是txt文件
        if workload_file_name.endswith('.txt'):
            workload_file_path = os.path.join(project_input_folder_path, workload_file_name)
            workload_file_name = os.path.splitext(workload_file_name)[0]
            # 检查文件是否存在
            if os.path.exists(workload_file_path):
                print(f"Workload file found: {workload_file_path}")
            else:
                print(f"Warning: Workload file {workload_file_path} does not exist.")
                workload_file_path = None
        else:
            workload_file_path = None
    else:
        workload_file_path = None
    
    return workload_file_name, workload_file_path

# 文件上传函数
def upload_file_to_container(ip, port, user, topo, node_name, file_path, destination_path):
    url = f"http://{ip}:{port}/file/uload/"
    payload = {
        "user": user,
        "topo": topo,
        "ne_name": node_name,
        "file_path": destination_path
    }
    with open(file_path, "rb") as f:
        file = {"file": f}
        resp = requests.post(url=url, data=payload, files=file)
        #print(f"Response status code for {node_name}: {resp.status_code}")

# 上传文件夹
def upload_directory_to_container(ip, port, user, topo, node_name, directory_path):
    for root, dirs, files in os.walk(directory_path):
        for file in files:
            file_path = os.path.join(root, file)
            destination_path = "/app/astra-sim/tests/text/inputs"
            upload_file_to_container(ip, port, user, topo, node_name, file_path, destination_path)

# 上传单个节点的文件
def upload_files_for_single_node(ip, port, user, topo, node_name, project_input_folder_path):
    source_path = os.path.join(project_input_folder_path, node_name)
    if os.path.exists(source_path):
        upload_directory_to_container(ip, port, user, topo, node_name, source_path)
        print(f"Files uploaded for node: {node_name}")
    else:
        logging.warning(f"Source path {source_path} does not exist. Skipping upload operation.")

# 获取所有节点的文件名（基于 .ini 文件）
def get_node_names_from_ini_files(base_folder_path):
    node_names = []
    for filename in os.listdir(base_folder_path):
        if filename.endswith(".ini"):
            node_name = os.path.splitext(filename)[0]
            node_names.append(node_name)
    return node_names

# 执行命令并获取结果
def execute_commands_for_node(workload_file_path, node_name, workload_file_name, cmd_manager):
    # 处理workload输入
    if workload_file_path:
        destination_path = '/app/astra-sim/tests/text/text_workloads'
        upload_file_to_container(
            backend_ip, backend_port, user_name, project_name, node_name, workload_file_path, destination_path
        )
        print(f"Workload file uploaded to containers.")
    
    # 利用转换器生成标准的工作负载层输入，作为后续运行as的输入
    convert_command = {
        node_name: [
            f'bash -c "cd /app/astra-sim/tests/text && bash text_converter.sh {workload_file_name}"'
        ]
    }
    covert_results = cmd_manager.exec_cmds_in_nodes(convert_command)
    
    # 运行Astra-sim节点得到结果
    exec_commands = {
        node_name: [
            f'bash -c "cd /app/astra-sim/tests/text && bash run_as.sh {workload_file_name}"'
        ]
    }
    exec_results = cmd_manager.exec_cmds_in_nodes(exec_commands)
    
    # 将执行结果写入对应节点的日志文件
    log_filename = os.path.join(base_output_folder_path, f"execution_{node_name}.log")
    with open(log_filename, 'w') as log_file:
        log_file.write(f"Execution results for node {node_name}:\n")
        log_file.write(f"{exec_results}\n")
    
    # 提取梯度大小    
    weight_command = {
        node_name: [
            f'bash -c "cd /app/astra-sim/tests/text/text_workloads && awk \'END {{last_allreduce=\\"\\\"; for(i=1;i<=NF;i++) if ($i==\\"ALLREDUCE\\") last_allreduce=$(i+1); if (last_allreduce != \\"\\") print last_allreduce}}\' {workload_file_name}.txt"'
        ]
    }
    weight_size = cmd_manager.exec_cmds_in_nodes(weight_command)

    return covert_results, weight_size, exec_results

# 处理执行结果
def process_execution_results(node_name, weight_size, exec_results, workload_file_name, result_file_path):
    # 处理并储存执行结果
    gradient_size = weight_size.get(node_name, {}).get(f'0_bash -c "cd /app/astra-sim/tests/text/text_workloads && awk \'END {{last_allreduce=\\"\\\"; for(i=1;i<=NF;i++) if ($i==\\"ALLREDUCE\\") last_allreduce=$(i+1); if (last_allreduce != \\"\\") print last_allreduce}}\' {workload_file_name}.txt"', {}).get('output', '').strip()

    local_log_path = os.path.join(base_output_folder_path, f"execution_{node_name}.log")
    with open(local_log_path, 'r') as log_file:
        log_contents = log_file.read()
        pattern = r"sys\[\d+\] finished, (\d+) cycles"
        matches = re.findall(pattern, log_contents)
        time_info = matches[-1] if matches else None

    with open(result_file_path, "w") as result_file:
        result_file.write(f"Node: {node_name}, Gradient size: {gradient_size}, Time: {time_info}\n")
    #print(f"Node: {node_name}, Gradient size: {gradient_size}, Time: {time_info}")

# Astra-sim API 接口
def astra_sim_api(node_ini_file_name):
    """
    Astra-sim API 接口：接受节点的 INI 文件名作为输入，执行命令并返回时间和梯度信息。
    :param node_ini_file_name: 节点的 INI 配置文件名
    :return: 时间和梯度信息
    """
    # 读取节点的 INI 配置文件
    node_ini_file_path = os.path.join(base_input_folder_path, f'{node_ini_file_name}.ini')
    node_data = read_node_ini_file(node_ini_file_path)
    print(f"INI file {node_ini_file_path} read successfully.")
    
    # 获取工作负载文件名及其路径
    workload_file_name, workload_file_path = generate_node_files_for_single_node(node_data, project_input_folder_path, node_ini_file_name)
    upload_files_for_single_node(backend_ip, backend_port, user_name, project_name, node_ini_file_name, project_input_folder_path)
    
    # 执行命令
    covert_results, weight_size, exec_results = execute_commands_for_node(workload_file_path, node_ini_file_name, workload_file_name, cmd_manager)
    
    # 处理执行结果并返回时间和梯度信息
    result_file_path = os.path.join(base_output_folder_path, f"{node_ini_file_name}_execution_results.log")
    process_execution_results(node_ini_file_name, weight_size, exec_results, workload_file_name, result_file_path)

    # 提取并返回时间和梯度信息
    with open(result_file_path, "r") as result_file:
        result_content = result_file.read()
        gradient_size = re.search(r"Gradient size: (\S+)", result_content).group(1) if "Gradient size" in result_content else None
        time_info = re.search(r"Time: (\S+)", result_content).group(1) if "Time" in result_content else None
    
    return time_info, gradient_size


# 主函数
if __name__ == "__main__":

    # 用户名和项目名配置
    user_name = "slzl"
    project_name = "astra-sim1"
    backend_ip = "192.168.1.33"
    backend_port = 25888
        
    image_manager = ImageManager(user_name, backend_ip, backend_port)
    project_manager = ProjectManager(user_name, backend_ip, backend_port)
    node_manager = NodeManager(user_name, project_name, backend_ip, backend_port)
    link_manager = LinkManager(user_name, project_name, backend_ip, backend_port)
    cmd_manager = CmdManager(user_name, project_name, backend_ip, backend_port)
    images = ImageManager(user_name, backend_ip, backend_port).get_images()
    astra_image = images["astra-sim"]

    topo = Topo()
    nodes = {}


    # 获取当前文件所在目录
    current_directory = os.path.dirname(os.path.abspath(__file__))
    print(f"Current directory: {current_directory}")

    base_input_folder_path = os.path.join(current_directory, 'node_input')
    print(f"Base input folder path: {base_input_folder_path}")
    base_output_folder_path = os.path.join(current_directory, 'node_output')
    print(f"Base output folder path: {base_output_folder_path}")
    os.makedirs(base_output_folder_path, exist_ok=True)
    project_input_folder_path = os.path.join(base_input_folder_path, project_name)
    if not os.path.exists(project_input_folder_path):
        os.makedirs(project_input_folder_path)
        
    # 获取节点的名称（自动从 ini 文件中提取）
    node_names = get_node_names_from_ini_files(base_input_folder_path)
    print(f"Node names: {node_names}")
    node_data = {}
    
    # 确定节点的合理坐标并布局
    rect_width = 800  # 矩形的宽度
    rect_height = 600  # 矩形的高度
    nodes_per_row = 2  # 每行的节点数

    for i, node_name in enumerate(node_names):
        row = i // nodes_per_row  # 行数
        col = i % nodes_per_row  # 列数
        # 计算节点的位置
        x = (col + 1) * (rect_width / (nodes_per_row + 1))
        y = (row + 1) * (rect_height / (len(node_names) // nodes_per_row + 1))
        location = {"x": x, "y": y}
        nodes[node_name] = topo.add_node(astra_image, node_name=node_name, location=location)
    
    # 项目创建
    ProjectManager(user_name, backend_ip, backend_port).deploy(project_name, topo)
    print(f"Deploy {project_name} successfully! Please check the effect at the frontend! Sleep 20s...")
    time.sleep(20)
    
    for node_name in node_names:
        # 调用 astra_sim_api 并获取返回的时间和梯度信息
        time_info, gradient_size = astra_sim_api(node_name)
        print(f"Node: {node_name}, Time: {time_info}, Gradient size: {gradient_size}")

    # 结束程序之前，销毁项目
    time.sleep(5)
    project_manager.destroy(project_name)
    print(f"Destroy {project_name} successfully! Please check the effect at the frontend!")
    print("as_api_test done!")
