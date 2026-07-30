import yaml
import docker
from ..Implement_layer import ContainerManager as container_manager
from ..vemu_config.config import PROJ_CONFIG
import os
from pprint import pprint

# docker_cli = docker.from_env()
# 注意：修改以下字符串，请注意yaml格式！
INIT_FILE = '''alerting: 
  alertmanagers: 
  - static_configs: 
    - targets: null
global: 
  evaluation_interval: 15s
  scrape_interval: 1s
rule_files: null
scrape_configs: 
- job_name: prometheus
  static_configs: 
  - targets: 
    - localhost:9090
'''

def change_pro_file(port_list,
                    file_path= PROJ_CONFIG.prometheus_file_path,
                    file_name = PROJ_CONFIG.prometheus_file_name,
                    scrape_interval=PROJ_CONFIG.prometheus_scrape_interval,
                    choice="add"):
    '''
        输入:
            port_list:添加/删除的ip:port列表
            path:配置文件的路径
            scrape_interval:Prometheus的采集时间
            choice:添加（add）或删除（delete）
        输出：
            无
        功能描述：
            修改Prometheus的配置文件
    '''
    # choice = "add"
    print(file_path + file_name)

    if not os.path.exists(file_path):
        os.mkdir(file_path)

    if not os.path.exists(file_path + file_name):
        print("创建新文件：", file_path + file_name)
        with open(file_path + file_name, "w") as f:
            f.write(INIT_FILE)
    with open(file_path + file_name, 'r') as f:
        content = yaml.load(f)
        # pprint.pprint(content)
        # 修改采集时间
        # scrape_interval = content["global"]["scrape_interval"]
        # print("scrape_interval:", scrape_interval, type(scrape_interval))
        content["global"]["scrape_interval"] = scrape_interval
        ip_target = content["scrape_configs"][0]["static_configs"][0]["targets"]
        print("before_ip_target", ip_target)
        # 不添加IP为xx的配置项，即删除IP为xx的配置项,该接口用于删除prometheus配置
        if choice == "delete":
            new_ip_target = []
            for ip_port in ip_target:
                if ip_port in port_list:
                    continue
                else:
                    new_ip_target.append(ip_port)
            content["scrape_configs"][0]["static_configs"][0]["targets"] = new_ip_target
        # add
        elif choice == "add":
            for ip_port in port_list:
                if ip_port not in ip_target:
                    content["scrape_configs"][0]["static_configs"][0]["targets"].append(ip_port)
        print("after_ip_target:", content["scrape_configs"][0]["static_configs"][0]["targets"])
    with open(file_path + file_name, 'w') as f:
        yaml.dump(content, f)


if __name__ == "__main__":
    # TODO(sw):测试文件
    file_path = "/home/sw/prometheus/prometheus_test2.yaml"
    if not os.path.exists(file_path):
        with open(file_path, "w") as f:
            f.write(INIT_FILE)
    with open(file_path, 'r') as f:
        content = yaml.load(f)
        pprint(content)
