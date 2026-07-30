# vemu_api_demo.py，用于演示vemu_api的使用
from vemu_api import *
import time
#from ..vemu_config.config import PROJ_CONFIG

if __name__ == "__main__":
    # 用户名和项目名配置
    user_name = "sw"
    project_name = ""
    
    # 管理类的后端ip和端口号可由参数指定（优先级高），或读取vemu_api包中
    # 的配置文件（config.py）
    backend_ip = "192.168.1.124"
    backend_port = 10021

    project_manager = ProjectManager(user_name, backend_ip, backend_port)
    project_lists=project_manager.get_projects()

    for project_name in project_lists :
        project_manager.destroy(project_name)
        print(f"Destroy {project_name} successfully! Please check the effect at "
        "the frontend!")
        time.sleep(3)
    
    print("Delete jobs done!")