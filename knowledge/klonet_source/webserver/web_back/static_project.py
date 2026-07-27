import os
import traceback
import shutil
import json
from ...Service_layer.mysql_models import Projects, TrafficApps, MonitorEvents
from ...webserver import mysql
from ...vemu_config.config import PROJ_CONFIG
from ...tools.file_tool import save_file, get_file_content, in_directory
from ...Service_layer.mysql_api.user_info import get_user_name_by_user_id
from ...Service_layer.mysql_api.static_project_my_api import check_project_existence, get_traffic_list, get_monitor_list
from ...Function_layer.deployed_proj_manager import retrieve_project_json
from ...tools.log_tools import UserLogLevel, UserLogger

STATIC_PROJECT_DIR = PROJ_CONFIG.static_project_dir

def project_save_as(user_id, static_project_name, deployed_project_name):
    '''
    将项目另存为静态文件。
    从redis中聚合项目信息，并将json文件名存在相关数据库表项中，将文件存在磁盘中

    项目路径：
        $dir = vemu_uestc/static_projects/<user_name>/<project_name>
    拓扑文件路径：
        $dir/<project_name>.json
    流量文件路径：
        $dir/traffics/<traffic_app_name>.json
    监控文件路径：
        $dir/monitors/<monitor_event_name>.json

    重复判定规则：
        同一用户下的项目名不能重复（依靠数据库检查）
        同一项目下的流量文件名不能重复（由于key值不能重复，无需检查）
        同一项目下的监控文件名不能重复（由于key值不能重复，无需检查）

    Args:
        user_id: 用户id
        project_name: 项目名
        data: 
        {
            "topo": "string", # 字符串化的拓扑json
            "traffics": { # default: {} 流量服务json列表
                "traffic_app_name":"string",# 字符串化的流量服务配置json
            },
            "monitors": { # default: {} 监控服务json列表
                "monitor_event_name":"string" # 字符串化的监控服务配置json
            }
        }

    Returns:
        {
            "code": 1, # 0为保存失败。1为保存成功
            "msg": "success" # 若保存成功则为success，若保存失败则为错误信息
        }
    '''
    # TODO: 文件与数据库之间要是不一致咋办？
    try:
        if check_project_existence(user_id, static_project_name):
            return {"code":0, "msg":"项目名已存在"}
        user_name = get_user_name_by_user_id(user_id)
        user_project_dir = (f"{STATIC_PROJECT_DIR}/{user_name}"
                           f"/{static_project_name}")
        traffic_app_dir = f"{user_project_dir}/traffics"
        monitor_event_dir = f"{user_project_dir}/monitors"

        # 获取拓扑、流量、监控信息
        project_json = retrieve_project_json(user_name, deployed_project_name)
        if (project_json["code"] == 0):
            return {
                "code":0, 
                "msg":f"从redis获取项目信息失败。错误信息：{project_json['msg']}"
            }
        project_json = project_json["project"]

        # 拓扑
        project = Projects()
        project.user_id = user_id
        project.project_name = static_project_name
        
        mysql.session.add(project)
        mysql.session.flush()
        
        save_file(user_project_dir, f"{static_project_name}.json", 
            json.dumps(project_json["topo"]))

        # 流量
        for traffic_name, traffic_json in project_json["traffics"].items():
            traffic_app = TrafficApps()
            traffic_app.project_id = project.project_id
            traffic_app.traffic_app_name = traffic_name
            mysql.session.add(traffic_app)

            save_file(traffic_app_dir, f"{traffic_name}.json", 
                json.dumps(traffic_json))

        # 监控
        for monitor_name, monitor_detail in project_json["monitors"].items():
            monitor_event = MonitorEvents()
            monitor_event.project_id = project.project_id
            monitor_event.monitor_event_name = monitor_name
            mysql.session.add(monitor_event)

            monitor_json = {
                monitor_name: monitor_detail
            }

            save_file(monitor_event_dir, f"{monitor_name}.json", 
                json.dumps(monitor_json))

        # 日志输出
        logger = UserLogger(user_name, UserLogLevel.First)
        logger.log_to_mysql(f'保存项目{deployed_project_name}为静态项目{static_project_name}')

        mysql.session.commit()
        return {"code":1, "msg":"success"}
    except Exception as e:
        mysql.session.rollback()
        traceback.print_exc()

def get_project(user_id, project_name):
    '''
    获取项目的拓扑，流量，监控信息。

    Args:
        user_id: 用户id
        project_name: 项目名
        data: 
        {
            "topo": "string", # 字符串化的拓扑json
            "traffics": { # default: {} 流量服务json列表
                "traffic_app_name":"string",# 字符串化的流量服务配置json
            },
            "monitors": { # default: {} 监控服务json列表
                "monitor_event_name":"string" # 字符串化的监控服务配置json
            }
        }

    Returns:
        {
            "code": 1, # 0为获取失败。1为获取成功
            "msg": "success" # 若获取成功则为success，若获取失败则为错误信息
            "project": {
                "topo": "string", # 字符串化的拓扑json
                "traffics": { # default: {} 流量服务json列表
                    "traffic_app_name":"string",# 字符串化的流量服务配置json
                },
                "monitors": { # default: {} 监控服务json列表
                    "monitor_event_name":"string" # 字符串化的监控服务配置json
                }
            }
        }
    '''
    try:
        if not check_project_existence(user_id, project_name):
            return {"code": 0, "msg": "项目不存在", "project":{}}
        
        user_name = get_user_name_by_user_id(user_id)
        user_project_dir = f"{STATIC_PROJECT_DIR}/{user_name}/{project_name}"
        traffic_app_dir = f"{user_project_dir}/traffics"
        monitor_event_dir = f"{user_project_dir}/monitors"

        project = {}

        # 拓扑
        project["topo"] = json.loads(get_file_content(user_project_dir, 
            f"{project_name}.json"))
        
        # 流量
        traffic_name_list = get_traffic_list(user_id, project_name)
        project["traffics"] = {}
        for traffic_name in traffic_name_list:
            data = json.loads(get_file_content(
                traffic_app_dir, f"{traffic_name}.json"))
            project["traffics"][traffic_name] = data

        # 监控
        monitor_name_list = get_monitor_list(user_id, project_name)
        project["monitors"] = {}
        for monitor_name in monitor_name_list:
            data = json.loads(get_file_content(
                monitor_event_dir, f"{monitor_name}.json"))
            project["monitors"][monitor_name] = data[monitor_name]

        return {"code":1, "msg":"success", "project":project}
        
    except Exception as e:
        mysql.session.rollback()
        traceback.print_exc()
        raise e

def delete_project_folder(project_folder_name):
    '''
    删除指定项目的文件夹（包含其目录下的所有文件及文件夹）。
    若文件夹不存在，不报错
    若用户文件夹为空，则删除这个空文件夹

    Args:
        project_folder_name: 项目文件夹名

    Returns:
        None
    '''
    try:
        # 该判断很重要！！！！请勿去掉！！！！
        if in_directory(project_folder_name, STATIC_PROJECT_DIR):
            # 删除project_folder_name文件夹及其所有内容
            shutil.rmtree(project_folder_name)

            # 若用户文件夹为空，则删除这个空文件夹
            user_folder = os.path.dirname(project_folder_name)
            if not os.listdir(user_folder):
                os.rmdir(user_folder) # 只会删除空文件夹，较为安全
        else:
            raise ValueError(f"试图删除{project_folder_name},"
                f"该文件夹是{STATIC_PROJECT_DIR}以外的文件夹")
    except FileNotFoundError:
        pass

def delete_project(user_id, project_name):
    '''
    获取项目的拓扑，流量，监控信息。

    Args:
        user_id: 用户id
        project_name: 项目名
        data: 
        {
            "topo": "string", # 字符串化的拓扑json
            "traffics": { # default: {} 流量服务json列表
                "traffic_app_name":"string",# 字符串化的流量服务配置json
            },
            "monitors": { # default: {} 监控服务json列表
                "monitor_event_name":"string" # 字符串化的监控服务配置json
            }
        }

    Returns:
        {
            "code": 1, # 0为获取失败。1为获取成功
            "msg": "success" # 若获取成功则为success，若获取失败则为错误信息
            "project": {
                "topo": "string", # 字符串化的拓扑json
                "traffics": { # default: {} 流量服务json列表
                    "traffic_app_name":"string",# 字符串化的流量服务配置json
                },
                "monitors": { # default: {} 监控服务json列表
                    "monitor_event_name":"string" # 字符串化的监控服务配置json
                }
            }
        }
    '''
    try:
        if not check_project_existence(user_id, project_name):
            return {"code": 0, "msg": "项目不存在"}
        
        user_name = get_user_name_by_user_id(user_id)
        user_project_dir = f"{STATIC_PROJECT_DIR}/{user_name}/{project_name}"
        delete_project_folder(user_project_dir)

        # 删除流量表/监控表/项目表相关行
        mysql.session.query(TrafficApps).filter(
            Projects.user_id==user_id, 
            Projects.project_name==project_name,
            TrafficApps.project_id==Projects.project_id).delete(
                synchronize_session=False)
        
        mysql.session.query(MonitorEvents).filter(
            Projects.user_id==user_id, 
            Projects.project_name==project_name,
            MonitorEvents.project_id==Projects.project_id).delete(
                synchronize_session=False)
        
        mysql.session.query(Projects).filter_by(
            user_id=user_id, project_name=project_name).delete(
                synchronize_session=False)

        mysql.session.commit()

        # 日志输出
        logger = UserLogger(user_name, UserLogLevel.First)
        logger.log_to_mysql(f'删除静态项目{project_name}')

        return {"code":1, "msg":"success"}      
    except Exception as e:
        mysql.session.rollback()
        traceback.print_exc()
        raise e