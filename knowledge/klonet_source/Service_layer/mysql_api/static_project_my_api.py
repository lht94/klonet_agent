from ...Service_layer.mysql_models import Projects, TrafficApps, MonitorEvents
from ...webserver import mysql

def check_project_existence(user_id, project_name):
    '''
    通过用户id查询该用户的指定项目名是否存在

    Args:
        user_id: 用户id
        project_name: 项目名

    Returns:
        如果指定项目名存在则返回True，否则返回False
    '''
    try:
        q = mysql.session.query(Projects).filter_by(user_id=user_id, 
                project_name=project_name)
        is_project_exists = mysql.session.query(q.exists()).scalar()

        return True if is_project_exists else False
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_project_list(user_id):
    '''
    通过用户id查询该用户的已保存项目列表

    Args:
        user_id: 用户id

    Returns:
        该用户的已保存项目列表，如：
        [
            {
                "name": "string", # 项目名
                "create_time": "string", # 项目创建时间
            }
        ],

        若查询结果为空，则返回空列表[]
    '''
    try:
        projects = mysql.session.query(Projects.project_name,
            Projects.create_time, Projects.update_time).filter_by(
                user_id=user_id).all()
        
        if not projects:
            return []
        
        # 组成新的列表返回
        result = [{"name":project[0], "create_time":project[1],
            "modified_time":project[2]} for project in projects]

        return result
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_traffic_list(user_id, project_name):
    '''
    通过用户id和项目名查询该用户的该项目的流量名列表
    Args:
        user_id: 用户id
        project_name: 项目名

    Returns:
        该用户的该项目的流量名列表，若查询结果为空，则返回空列表[]
    '''
    try:
        traffics = mysql.session.query(TrafficApps.traffic_app_name).join(
            Projects, Projects.project_id==TrafficApps.project_id).filter_by(
                project_name=project_name, user_id=user_id)
        result = [traffic[0] for traffic in traffics] if traffics else []
        return result
    except Exception as e:
        mysql.session.rollback()
        raise e

def get_monitor_list(user_id, project_name):
    '''
    通过用户id和项目名查询该用户的该项目的监控名列表
    Args:
        user_id: 用户id
        project_name: 项目名

    Returns:
        该用户的该项目的监控名列表，若查询结果为空，则返回空列表[]
    '''
    try:
        monitors = mysql.session.query(MonitorEvents.monitor_event_name).join(
            Projects, Projects.project_id==MonitorEvents.project_id).filter_by(
                project_name=project_name, user_id=user_id)
        result = [monitor[0] for monitor in monitors] if monitors else []
        return result
    except Exception as e:
        mysql.session.rollback()
        raise e