import traceback
import os
import datetime
from flask import Flask, render_template, session
from flask_cors import CORS
from flask_socketio import SocketIO
from flask_login import LoginManager
from flask_sockets import Sockets
from flask_uploads import configure_uploads, ALL, patch_request_class, IMAGES, ARCHIVES, UploadSet

from vemu_uestc.webserver.api import ne_health
from .celery_uitls import init_celery
from .socketio_handlers import connected
from .web_back.web_terminal_impl import *
from .web_back.user_manager import UserManager
from ..vemu_config.config import PROJ_CONFIG
from ..Service_layer.redisAPI import UserMapRedis,UserDB
from ..Service_layer.mysql_models import UserLogin
from ..Service_layer.mysql_api.auth import init_tables
from ..Implement_layer.ImageRegistryManager.upload_set import VemuUploadSet

PKG_NAME = os.path.dirname(os.path.realpath(__file__)).split("/")[-1]
login_manager = LoginManager()

dockerfiles = VemuUploadSet('dockerfiles', ALL) # 后续再检查名字
icons = VemuUploadSet('icons', IMAGES)
attachments = VemuUploadSet('attachments', ARCHIVES)
image_tar = VemuUploadSet('imagetar', ARCHIVES)


def create_app(app_name=PKG_NAME, **kwargs):
    app = Flask(__name__, static_folder='../expr_monitor_user_data/', static_url_path='/static')
    if kwargs.get('celery'):
        init_celery(kwargs.get('celery'), app)
    
    def register_api(view, endpoint, url):
        view_func = view.as_view(endpoint)
        app.add_url_rule(url, view_func=view_func, methods=['POST', 'DELETE', 'GET', 'PUT'])


def create_master_app(app_name=PKG_NAME, **kwargs):
    """
    创建master server的工厂函数
    """
    app = Flask(__name__, static_folder='../expr_monitor_user_data/', static_url_path='/static')
    
    app.config["SQLALCHEMY_DATABASE_URI"] = ("mysql://root:[REDACTED]@"
        f"{PROJ_CONFIG.mysql_ip}:{PROJ_CONFIG.mysql_port}/"
        f"{PROJ_CONFIG.mysql_database}?charset=utf8")
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['LOGIN_DISABLED'] = PROJ_CONFIG.login_required_disabled
    app.config['PERMANENT_SESSION_LIFETIME'] = datetime.timedelta(hours=1)
    # 上传文件地址
    app.config['UPLOADED_DOCKERFILES_DEST'] = PROJ_CONFIG.image_registry_dir
    app.config['UPLOADED_ICONS_DEST'] = PROJ_CONFIG.image_registry_dir
    app.config['UPLOADED_ATTACHMENTS_DEST'] = PROJ_CONFIG.image_registry_dir
    # 设置上传文件大小限制
    patch_request_class(app, PROJ_CONFIG.upload_file_size_limit_byte) 
    configure_uploads(app, (dockerfiles, icons, attachments))

    if kwargs.get('celery'):
        init_celery(kwargs.get('celery'), app)
    if kwargs.get('mysql'):
        kwargs.get('mysql').init_app(app)
        try:
            kwargs.get('mysql').create_all(app=app) # 建空表
            with app.app_context():
                # TODO：在这初始化表格很蠢，特别是gunicorn的worker多的时候，但是
                # 暂时又没办法
                init_tables()
                # 在mysql里面初始化管理员账户
                user_manager = UserManager()
                try:
                    user_manager.create_user("sadmin", "vemu123", "18888888888", "1234567890@qq.com", "super_admin")
                except:
                    pass
                try:
                    user_manager.create_user("admin", "vemu123", "18888888888", "1234567890@qq.com", "admin")
                except:
                    pass
        except Exception as e:
            if e.__dict__['orig'].args[0] == 1050: # Table already exists error.
                pass
            else:
                traceback.print_exc()
                raise

    login_manager.session_protection = 'strong'
    login_manager.login_view = '/master/user_login/'
    login_manager.init_app(app)
    @login_manager.user_loader
    def load_user(generated_id):
        return UserLogin.get(generated_id)
    
    # 在redis里面初始化管理员账户
    try:
        user_re_map = UserMapRedis()
        user_re_map.set_user_db('sadmin')
        user_re_map.close()
    except:
        pass
 
    try:
        user_re_map = UserMapRedis()
        user_re_map.set_user_db('admin')
        user_re_map.close()
    except:
        pass

    # 注册视图函数并与URL关联
    def register_api(view, endpoint, url):
        view_func = view.as_view(endpoint)
        app.add_url_rule(url, view_func=view_func, methods=['POST', 'DELETE', 'GET', 'PUT'])

    from .api.node import master_node
    from .api.resource import master_resource
    from .api.topo import master_topo
    from .api.link import master_link
    from .api.monitor import master_monitor, node_gpu_monitor
    from .api.traffic import master_traffic
    from .api.task_status import task_status
    from .api.typical_topo import typical_topo
    from .api.expr_result import master_expr_result
    from .api.worker_register import worker_register
    from .api.plat_monitor import master_platform_monitor
    from .api.user_management import user_register, user_login, \
        user_logout, user_basic_info, modify_info, forget_passwd, user_audit, super_setpwd
    from .api.monitor import redis_monitor, monitor_type, monitor_status
    from .api.traffic import redis_traffic, traffic_status
    from .api.topo import redis_topo
    from .api.static_project import static_project_api, project_list
    from .api.save_project import save_project_api
    from .api.dynamic_modify import master_link_api, master_container_api
    from .api.image import image_views
    from .api.deployed_proj import deployed_proj
    from .api.data_server import master_expr_figure
    from .api.data_server import data_server
    from .api.link_health import link_health_master_api
    from .api.file_load import master_download
    from .api.file_load import master_upload
    from .api.log import master_log
    from .api.batch_exec_cmd import batch_exec_cmd_master
    from .api.permissions_management import permission_check 
    from .api.permissions_management import super_delete
    from .api.permissions_management import image_manage
    from .api.permissions_management import change_user
    from .api.permissions_management import private_store
    from .api.permissions_management import public_store
    from .api.permissions_management import user_role
    from .api.image_registry import image_registry_api
    from .api.permissions_management import get_one_image
    from .api.permissions_management import image_get_all_user
    from .api.health_check import health_check, heartbeat
    from .api.ne_health import master_ne_health
    from .api.node_exec_cmd import node_exec_cmd_master
    from .api.topo import process_bar, json_convert
    from .api.ssh_connect import ssh_service_master
    from .api.docker_swarm import swarm_master
    from .api.satellite import sat_master
    from .api.sdn_query import sdn_info_query_master
    from .api.sflow import master_sflow_monitor
    from .api.experiment_registry import experiment_commit_api
    from .api.experiment_registry import experiment_upload_api
    from .api.experiment_registry import experiment_redeploy_api
    from .api.experiment_registry import experi_scripts_download
    from .api.permissions_management import experi_store
    from .api.kvm_image import kvm_image_upload, kvm_image_views, master_kvm_sync
    from .api.dynamic_modify import master_kvm_api
    from .api.dynamic_modify import master_interface_api
    from .api.sflow import master_sflow_query
    from .api.hardware import hardware_upload, hardware_views, id_views
    from .api.computing_power_simulation import performance_views
    from .api.traffic import master_traffic_gen
    from .api.node_exec_cmd import ovs_cmd, bmv2_cmd
    from .api.traffic import redis_traffic_gen
    from .api.topo import auto_position_cal
    from .api.topo import topo_recover
    from .api.rdma import master_rdma

    """用户相关"""


    # 用户注册、登录、注销
    register_api(user_register.UserRegisterAPI, 'user_register_api', '/master/user_register/')
    register_api(user_login.UserLoginAPI, 'user_login_api', '/master/user_login/')
    register_api(user_logout.UserLogoutAPI, 'user_logout_api', '/master/user_logout/')
    register_api(forget_passwd.ForgetPasswdAPI, 'forget_passwd_api', '/master/forget_passwd/')
    # 获取用户基本信息，包括用户id和用户名
    register_api(user_basic_info.UserBasicInfoAPI, 'user_basic_info_api', '/master/user_basic_info/')
    # 修改密码
    register_api(modify_info.ModifyPasswordAPI, 'modify_password_api', '/master/modify_password/')
    # 超级管理员判定
    register_api(permission_check.PermissionCheck, 'perm_test_api', '/master/perm/check/')
    # 超级管理员删除用户
    register_api(super_delete.SuperDelete, 'super_delete_user', '/master/perm/superdelete/')
    # 超级修改其他成员角色
    register_api(change_user.ChangeUser, 'change_user_role', '/master/perm/changeuser/')
    # 查询用户角色
    register_api(user_role.UserRole, 'get_user_role', '/master/perm/role/')
    # 超级管理员审核用户注册
    register_api(user_audit.UserAuditAPI, 'user_audit_api', '/master/user_audit/')
    # 得到平台所有普通用户和管理员列表，用于管理员和超级管理员用户
    register_api(image_get_all_user.ImageGetAllUser, 'image_get_user', '/master/perm/imageuser/')
    # 高权限用户重置低权限用户密码
    register_api(super_setpwd.SetPwdAPI, 'set_pwd_api', '/master/setpwd/')

    """项目相关"""


    # 获取某项目的拓扑，流量，监控信息
    register_api(static_project_api.StaticProjectAPI, 'static_project_api', '/my/project/<project_name>/')
    # 保存项目信息，做持久化存储
    register_api(save_project_api.SaveProjectAPI, 'save_project_api', '/my/project/')
    # 获取当前用户的所有项目列表
    register_api(project_list.StaticProjectListAPI, 'static_project_list_api', '/my/project_list/')
    # 获取用户某个已创建项目的拓扑，流量，监控信息
    register_api(deployed_proj.DeployedProjectAPI, 'deployed_project_api', '/re/project/<project_name>/')
    # redis聚合已创建拓扑列表
    register_api(redis_topo.redis_topo_info, 'redis_topo_list', '/re/project/')
    # redis中某个拓扑的信息
    register_api(redis_topo.redis_topo_info, 'redis_topo', '/re/project/<project_name>/topo/')
    # redis中获取已创建拓扑节点列表及网卡信息
    register_api(redis_topo.redis_topo_nes2interfaces, 'redis_topo_nes2interfaces', '/re/project/<project_name>/ne_info/')
    # 获取某项目的节点、链路数量以及创建时间信息
    register_api(redis_topo.redis_topo_list_and_info, 'redis_topo_list_and_info', '/re/project/topo_list_and_info/')
    # 状态灯（查询拓扑是否创建）、拓扑创建、拓扑删除
    register_api(master_topo.TopoDeployAPI, 'master_topo_api', '/master/topo/')
    # 动态增加、删除节点
    register_api(master_container_api.DynamicContainerAPI, 'dynamic_container_api', '/modification/container/')
    # 动态增加、删除链路
    register_api(master_link_api.DynamicLinkAPI, 'dynamic_link_api', '/modification/link/')
    # 动态增加、删除kvm节点
    register_api(master_kvm_api.DynamicKvmAPI, 'dynamic_kvm_api', '/modification/kvm/')
    # 动态修改kvm节点端口命名
    register_api(master_interface_api.DynamicInterfaceAPI, 'dynamic_interface_api', '/modification/interface/')
    # 典型topo生成API
    register_api(typical_topo.TypicalTopoAPI, 'typical_topo_api', '/generate')
    # 拓扑启动节点服务
    register_api(master_topo.TopoServiceAPI, 'master_service_api', '/master/service/')
    # redis中某拓扑的节点信息
    register_api(redis_topo.redis_topo_node, "redis_topo_node_info", '/re/project/<project_name>/node/')
    # redis中某拓扑的链路信息
    register_api(redis_topo.redis_topo_link, "redis_topo_link_info", '/re/project/<project_name>/link/')
    #redis中某拓扑节点所在的worker_ip地址信息
    register_api(redis_topo.redis_topo_worker_ip, "redis_topo_worker_ip", '/re/project/<project_name>/worker_ip/')
    # 拓扑创建的进度条
    register_api(process_bar.ProcessBarAPI, 'process_bar_api', '/master/process_bar/')
    # 拓扑节点的位置自动生成
    register_api(auto_position_cal.AutoPositionCal, 'auto_position_calculate', '/master/auto_position_cal/')


    """节点相关"""


    # 启动或停止节点urpf服务
    register_api(master_node.NodesUrpfConfigAPI, 'master_node_urpf_api', '/master/node/urpf/')
    # 节点网络服务
    register_api(master_node.NodesNetworkConfigAPI, 'master_node_network_api', '/master/node/network/')
    # 批量命令执行
    register_api(batch_exec_cmd_master.BatchExecCmdAPI, 'batch_exec_cmd_api', '/master/batch_exec_cmd/')
    # 指定容器节点执行多条命令，如配置路由IP
    register_api(node_exec_cmd_master.NodeExecCmdAPI, 'node_exec_cmd_api', '/master/node_exec_cmd/')
    # download文件下载
    register_api(master_download.DownloadFileAPI, 'download_file_api', '/file/dload/')
    # upload文件上传
    register_api(master_upload.UploadFileAPI, 'upload_file_api', '/file/uload/')
    # ssh服务
    register_api(ssh_service_master.SSHServiceAPI, 'ssh_service_api', '/master/ssh_service/')
    # 修改网元（容器）的端口映射
    register_api(ssh_service_master.ModifyNePortMapping, 'modify_port_api', '/master/modify_port_mapping/')
    # 文件下载进度条
    register_api(process_bar.DownloadProcessMasterAPI, 'download_process_bar_api', '/master/download_process/')
    # 文件上传进度条
    register_api(process_bar.UploadProcessMasterAPI, 'upload_process_bar_api', '/master/upload_process/')
    # ovs远程配置接口相关
    register_api(ovs_cmd.OvsCmdAPI, 'ovs_remote_cmd', '/master/ovs_cmd/')
    # bmv2远程配置接口相关
    register_api(bmv2_cmd.Bmv2CmdAPI, 'bmv2_remote_cmd', '/master/bmv2/')
    # KVM虚拟机配置rdma
    register_api(master_rdma.SendRDMACmdAPI,'send_rdma_cmd_api','/master/send_rdma_cmd/')


    """链路相关"""


    register_api(master_link.LinkConfigAPI, 'master_link_api', '/master/link/')
    register_api(master_link.LinkQueryAPI, 'master_linkquery_api', '/master/linkquery/')
    register_api(master_link.StLinkConfigAPI, 'master_stlink_api', '/master/stlink/')
    register_api(master_link.MmlinkConfigAPI, 'master_mmlink_api', '/master/mmlink/')
    register_api(master_link.LinkMonitorAPI, 'master_link_monitor_api', '/master/link_monitor/')
    register_api(master_link.LinkMonitorConfigAPI, 'master_link_monitor_config_api', '/master/link_monitor/config/')
    register_api(master_link.DelayAPI, 'master_delay_api', '/master/delay/')

    """拓扑恢复"""
    register_api(topo_recover.TopoRecoverAPI, 'topo_recover_api', '/topo/recover/')

    """流量相关"""


    # 实时返回iperf3流量信息
    register_api(redis_traffic_gen.RedisTrafficGenAPI, 'redis_traffic_gen_api', '/master/redis_traffic_gen/')
    # 流量服务创建（切分并运行流量程序）、流量服务停止（删除切分信息并停止流量程序）
    register_api(master_traffic.TrafficAPI, 'master_traffic_api', '/master/traffic/')
    # 获取已创建拓扑节点列表及网卡信息
    register_api(redis_traffic.TrafficRedisAPI, 'traffic_redis_api', '/re/project/<project_name>/traffic_app/')
    # 流量服务删除（从redis删除信息）、流量服务修改（修改redis信息）、获取流量服务json （从redis获取信息）
    register_api(redis_traffic.TrafficRedisAPI, 'traffic_redis_app_api', '/re/project/<project_name>/traffic_app/<app_name>/')
    # 状态灯（查询流量是否已创建）
    register_api(traffic_status.TrafficStatusAPI, 'traffic_status_api', '/re/project/<project_name>/traffic/<traffic_name>/status/')
    # 流量模版上传
    register_api(redis_traffic.TrafficTemplateAPI, 'traffic_template_api', '/re/project/<project_name>/traffic_templates/')
    register_api(master_traffic.TemplateUseAPI, 'master_template_api', '/master/template/')
    # 新的流量发生器
    register_api(master_traffic_gen.MasterTrafficGenAPI, 'master_traffic_gen_api', '/master/traffic_gen/')
    # 流量发生器信息保存、修改、删除、查询
    register_api(master_traffic_gen.MasterTrafficSaveAPI, 'redis_traffic_save_info', '/master/traffic_save/')

    """监控相关"""


    # 监控服务创建（切分并运行监控程序）、监控服务停止（删除切分信息并停止监控程序，计算指标）
    register_api(master_monitor.MonitorAPI, 'master_monitor_api', '/master/monitor/')
    # 新增监控服务（写信息至redis）、删、查、改
    register_api(redis_monitor.redis_monitor_info, 'redis_monitor_api', '/re/project/<project_name>/monitor/<monitor_name>/')
    # TC队列监控
    register_api(master_monitor.MonitorTcQueueAPI, 'master_monitor_api_tc_queue', '/master/monitor/tc/queue/')
    register_api(redis_monitor.redis_monitor_info, 'redis_monitor_list', '/re/project/<project_name>/monitor/')    
    # 状态灯（查询监控是否已创建）
    register_api(monitor_status.MonitorStatusAPI, 'monitor_status_api', '/re/project/<project_name>/monitor/<monitor_name>/status/')
    # 监控服务结果查询
    register_api(master_expr_result.ExprDataAPI, 'expr_result_api', '/master/expr/')
    register_api(master_expr_figure.MasterExprFigureAPI, 'master_expr_figure_api', '/master/expr_figure/')
    register_api(monitor_type.MonitorEventTypesAPI, "monitor_event_types_api", '/re/project/<project_name>/monitor/<monitor_name>/types/')
    # TODO(MaTie, 20210609): 暂时先把文件下载功能放在master上。这样master和dataserver是不能分开的
    # 为了解决data_server没有可用端口的问题
    register_api(data_server.DataServerAPI, 'data_server_api', '/data-server/expr/')
    # 节点GPU用量监测相关，目前还只是类似一个单次查询nvidia-smi的接口
    register_api(node_gpu_monitor.NodeGpuMonitorAPI, 'node_gpu_monitor', '/master/node_gpu_monitor/')
    
    
    """镜像仓库相关"""


    # 操作界面镜像仓库json描述
    register_api(image_views.ImageAPI, 'image_list', '/my/image/')
    # 操作界面节点编辑json描述
    register_api(image_views.EditAPI, 'edit_list', '/my/edit/')
    # 镜像上传
    register_api(image_registry_api.ImageUploadAPI, "image_upload_api", '/image/upload/')
    # 管理员和超级管理员获取所有用户的私有镜像
    register_api(image_manage.ImageManage, 'image_mamange', '/master/perm/image/')
    # 返回私有镜像仓库列表
    register_api(private_store.PrivateStore, 'get_private_image_store', '/master/perm/privatestore/')
    # 返回公有镜像仓库列表
    register_api(public_store.PublicStore, 'get_public_image_store', '/master/perm/publicstore/')
    # 返回checkname用户的私有镜像，用于管理员和超级管理员用户
    register_api(get_one_image.OneManage, 'get_one_image', '/master/perm/oneimage/')
    
    """实验仓库相关"""
    # 将实验上传保存到实验仓库
    register_api(experiment_upload_api.ExperimentUploadAPI, 'experiment_upload_api', '/master/experiment/upload/')
    # 提交实验所有节点镜像
    register_api(experiment_commit_api.ReqExperimentCommitAPI, 'req_experiment_commit_api', '/master/experiment/commmit/')
    # 重新部署实验仓库中的某个实验
    register_api(experiment_redeploy_api.ExperimentRedeployAPI, 'experiment_redeploy_api', '/master/experiment/redeploy/')
    # 下载某个实验的脚本
    register_api(experi_scripts_download.ScriptsDownloadAPI, 'experiment_scripts_download', '/master/scripts/download/' )
    # 返回实验仓库的实验列表
    register_api(experi_store.ExperiStore, 'get_experiment_store', '/master/perm/experi_store/')
    
    # =====KVM=====
    # KVM虚拟机镜像上传
    register_api(kvm_image_upload.KVMIamgeUploadAPI, 'KVM_image_upload', '/master/kvm_image/upload/')
    
    # KVM镜像信息
    register_api(kvm_image_views.KVMImageAPI, 'kvm_image_list', '/my/kvm_image/')
    
    # KVM镜像同步
    register_api(master_kvm_sync.ImageSyncAPI, 'master_kvm_image_sync', '/master/sync_kvm_image/')
    register_api(master_kvm_sync.SelfImageInfoAPI, 'self_image_info', '/master/get_self_image_info/')

    # ====hardware====
    # hardware用户备案真实设备
    register_api(hardware_upload.HardwareUploadAPI, 'hardware_upload', '/master/hardware/upload/')
    # 用户查询可用设备
    register_api(hardware_views.GetHardwareAPI, 'get_hardware', '/my/hardware/')
    # 用户按照id查询设备配置
    register_api(id_views.GetIdAPI, 'get_id', '/my/id_hardware/')
    

    """算力仿真相关"""

    # ====算力仿真====
    # 查询一个节点的性能performance
    register_api(performance_views.GetPerformanceAPI, 'get_performance', '/master/ne_performance/')

    """系统相关"""


    # 日志
    register_api(master_log.LogtestAPI, 'master_log_api', '/master/logtest/')
    register_api(master_log.LoginfoQueryAPI, 'master_loginfoquery_api', '/master/loginfoquery/')
    # 健康状态检查
    register_api(health_check.HealthCheckApi, "health_check", "/server_health/")
    register_api(heartbeat.HeartbeatApi, "heartbeat", "/master/heartbeat/")
    register_api(heartbeat.QuerySingleProjectHealthApi, "query_single_project_health", "/master/heartbeat_health/")
    register_api(heartbeat.QueryUserAllProjectHealthApi, "query_user_all_project_health", "/master/heartbeat_health_all/")
    register_api(master_ne_health.NeCheckAPI, 'master_ne_check_api', '/master/ne_health/')
    # 跨宿主机链路健康检查
    register_api(link_health_master_api.LinkCheckerAPI, 'link_checker_api', '/master/checklink/')
    register_api(link_health_master_api.RecordLinkHealthAPI, 'record_link_health_api', '/master/link_report/')
    # worker注册
    register_api(worker_register.RegisterWorkerAPI, 'register_worker_api', '/master/worker/<worker_ip>/')
    #resource
    register_api(master_resource.ResourceAPI, 'master_resource_api', '/master/resource/')
    # docker swarm相关
    register_api(swarm_master.DockerSwarmMaster, 'docker_swarm', '/master/swarm/')
    # promethus
    register_api(master_platform_monitor.PlatMonitorFileAPI, 'master_platmonitor_file_api', '/master/platmonitor_file/')
    # 创建、删除监控组件
    register_api(master_platform_monitor.PlatMonitorAPI, 'master_platmonitor_api', '/master/platmonitor/')
    # 监控结果查询
    register_api(master_platform_monitor.PlatMonitorNeQueryAPI, 'master_platmonitor_api_ne_query', '/master/platmonitor_ne_query/')
    register_api(master_platform_monitor.PlatMonitorHostQueryAPI, 'master_platmonitor_api_host_query', '/master/platmonitor_host_query/')


    """其他"""


    # 查询celery中异步任务的执行状态
    register_api(task_status.TaskStatusAPI, 'task_status_api', '/master/task/<task_id>/')
    # kdl原始节点数据到json的转换
    register_api(json_convert.JsonConvertAPI, 'json_convert_api', '/master/json_convert/')


    """第三方"""


    # 星座参数查询与修改
    register_api(sat_master.SatelliteWalkerAPI, 'satellite_walker_api', '/satellite/walker/')
    register_api(sat_master.SatelliteGndAPI, 'satellite_gnd_modify_api', '/satellite/gnd/')
    register_api(sat_master.SatelliteSatAPI, 'satellite_sat_api', '/satellite/sat/')
    register_api(sat_master.SatelliteAllGndAPI, 'satellite_gnd_all_api', '/satellite/allgnd/')
    register_api(sat_master.SatelliteSDN, 'satellite_sdn_api', '/satellite/sdn/')
    register_api(sat_master.SatellitePreDraw, 'satellite_predraw_api', '/satellite/predraw/')
    register_api(sat_master.SatelliteGenerateTraffic, 'satellite_traffic_api', '/satellite/traffic/')
    register_api(sat_master.MonitorRealtime, 'monitor_realtime_api', '/satellite/monitor-realtime/')
    
    # 虚仿相关
    register_api(sdn_info_query_master.SwitchDpidAPI, 'switch_dpid', '/switch_dpid/')
    register_api(sdn_info_query_master.HostMacAPI, 'host_mac', '/host_mac/')
    register_api(sdn_info_query_master.LinkPortAPI, 'link_port', '/link_port/')
    # Kc
    # sflow实时监控
    register_api(master_sflow_monitor.SflowAPI, 'sflow_realtime_api', '/master/sflow/')
    register_api(master_sflow_query.SflowQueryAPI, 'sflow_query_api', '/master/sflowquery/')
    # 增加跨域访问支持
    CORS(app, supports_credentials=True, resources={r"/*": {"origins": "*"}})
    return app


def create_worker_app(app_name=PKG_NAME, **kwargs):
    """
    创建 worker_server 的工厂函数
    """
    app = Flask(__name__, static_folder='../expr_monitor_user_data/', static_url_path='/static')
    if kwargs.get('celery'):
        init_celery(kwargs.get('celery'), app)

    # 注册视图函数并与URL关联
    def register_api(view, endpoint, url):
        view_func = view.as_view(endpoint)
        app.add_url_rule(url, view_func=view_func, methods=['POST', 'DELETE', 'GET', 'PUT'])

    from .api.node import worker_node
    from .api.resource import worker_resource
    from .api.topo import worker_topo
    from .api.link import worker_link
    from .api.monitor import worker_monitor
    from .api.traffic import worker_traffic
    from .api.expr_result import worker_expr_result
    from .api.dynamic_modify import worker_link_api, worker_container_api, worker_kvm_api
    from .api.plat_monitor import worker_platform_monitor
    from .api.link_health import link_health_worker_api
    from .api.ne_health import worker_ne_health
    from .api.file_load import worker_download
    from .api.file_load import worker_upload
    from .api.batch_exec_cmd import batch_exec_cmd_worker
    from .api.image_registry import image_pull_api
    from .api.image_registry import image_delete_api
    from .api.health_check import health_check
    from .api.image_registry import image_commit_api
    from .api.node_exec_cmd import node_exec_cmd_worker
    from .api.ssh_connect import ssh_service_worker
    from .api.docker_swarm import swarm_worker
    from .api.topo import process_bar
    from .api.experiment_registry import worker_all_images_commit_api
    from .api.experiment_registry import worker_all_images_pull_api
    from .api.kvm_image import worker_image_upload
    from .api.satellite import sat_worker
    from .api.sflow import worker_sflow_monitor
    from .api.traffic import worker_traffic_gen

    # worker
    register_api(worker_traffic.TrafficAPI, 'worker_traffic_api', '/worker/traffic/<role>/')
    # 新的流量发生器
    register_api(worker_traffic_gen.WorkerTrafficGenAPI, 'worker_traffic_gen_api', '/worker/traffic_gen/')
    register_api(worker_monitor.MonitorAPI, 'worker_monitor_api', '/worker/monitor/')
    register_api(worker_monitor.MonitorTcQueueAPI, 'worker_monitor_api_tc_queue', '/worker/monitor/tc/queue/')
    register_api(worker_topo.TopoDeployAPI, 'worker_topo_api', '/worker/topo/')
    register_api(worker_topo.TopoServiceAPI, 'worker_service_api', '/worker/service/')

    # resource
    register_api(worker_resource.ResourceAPI, 'worker_resource_api', '/worker/resource/')

    # node
    register_api(worker_node.NodesUrpfConfigAPI, 'worker_node_urpf_api', '/worker/node/urpf/')
    register_api(worker_node.NodesNetworkConfigAPI, 'worker_node_network_api', '/worker/node/network/')

    #link
    register_api(worker_link.StLinkConfigAPI, 'worker_stlink_api', '/worker/stlink/')
    register_api(worker_link.MmlinkConfigAPI, 'worker_mmlink_api', '/worker/mmlink/')
    register_api(worker_link.ThroughputQueriesAPI, 'worker_throughput_api', '/worker/throughput/')
    register_api(worker_link.DelayAPI, 'worker_delay_api', '/worker/delay/')

    register_api(worker_expr_result.ExprDataAPI, 'expr_result_api', '/worker/expr/')
    # prometheus
    register_api(worker_platform_monitor.PlatMonitorAPI, "worker_prometheus_deploy_api", '/worker/platmonitor/')
    
    # 链路健康检查
    register_api(link_health_worker_api.LinkCheckerAPI, 'link_health_checker_api', '/worker/checklink/')
    register_api(link_health_worker_api.L2PingReplyerAPI, 'l2_ping_replyer_api', '/worker/l2ping_replyer/')
    
    # 节点健康检查
    register_api(worker_ne_health.NeCheckAPI, 'worker_ne_check_api', '/worker/ne_health/')
    # dynamic modify
    register_api(worker_link_api.DynamicVethLink, 'dynamic_link_api', '/modification/vethlink/')
    register_api(worker_link_api.DynamicVxlanLink, 'dynamic_vxlanlink_api', '/modification/vxlanlink/')
    register_api(worker_container_api.DynamicContainerAPI, 'dynamic_container_api', '/modification/container/')
    register_api(worker_kvm_api.DynamicKvmAPI, 'dynamic_kvm_api', '/modification/kvm/')

    # 节点恢复
    register_api(worker_container_api.ContainerStartAPI, 'container_start_api', '/worker/container/start/')
    register_api(worker_container_api.OvsStartAPI, 'ovs_start_api', '/worker/ovs/start/')
    
    # download
    register_api(worker_download.DownlaodFileAPI,
                 'worker_download_aip', '/worker/dload/')
    # upload
    register_api(worker_upload.UploadFileAPI,
                'worker_upload_api', '/worker/uload/')
    
    # 文件下载进度条
    register_api(process_bar.DownloadProcessWorkerAPI, 'download_process_api', '/worker/download_process/')
    # 文件上传进度条
    register_api(process_bar.UploadProcessWorkerAPI, 'upload_process_api', '/worker/upload_process/')

    # 批量命令执行
    register_api(batch_exec_cmd_worker.BatchExecCmdAPI, 'batch_exec_cmd_api', '/worker/batch_exec_cmd/')
    # 指定容器节点执行多条命令，如配置路由IP
    register_api(node_exec_cmd_worker.NodeExecCmdAPI, 'node_exec_cmd_api', '/worker/node_exec_cmd/')

    # worker镜像pull、删除
    register_api(image_pull_api.ImagePullAPI, "image_pull_api", '/image/pull/')
    register_api(image_delete_api.ImageDeleteAPI, "image_delete_api", '/image/delete/')
    register_api(image_commit_api.ImageCommitAPI, "image_commit_api", '/image/commit/')
    
    # worker上传包含在实验中所有容器镜像
    register_api(worker_all_images_commit_api.WorkerAllImagesCommitAPI, 'worker_all_images_commit_api', '/worker/all_images/commit/')
    # worker拉取实验中本地没有的镜像
    register_api(worker_all_images_pull_api.WorkerAllImagesPullAPI, 'worker_all_images_pull_api', '/worker/all_images/pull/')
    
    # worker上接收上传的KVM虚机镜像
    register_api(worker_image_upload.WokerImageUploadAPI, "KVM_image_upload", '/worker/kvm_image/upload/')
    
    # 服务状态检查
    register_api(health_check.HealthCheckApi, "health_check", "/server_health/")

    # post: ssh服务的开启/关闭
    # get: ssh服务的网元连接信息获取，包括worker的ip和网元所有的端口映射
    register_api(ssh_service_worker.SSHServiceAPI, 'ssh_service_api', '/worker/ssh_service/')
    # 修改网元（容器）的端口映射
    register_api(ssh_service_worker.ModifyNePortMapping, 'modify_port_api', '/worker/modify_port_mapping/')

    # docker swarm相关
    register_api(swarm_worker.DockerSwarmWorker, 'docker_swarm', '/worker/swarm/')

    # 卫星
    register_api(sat_worker.SatelliteGenerateTraffic, 'satellite_traffic_api', '/satellite/traffic/')
    register_api(sat_worker.MonitorRealtime, 'monitor_realtime_api', '/satellite/monitor-realtime/')

    #sflow监控
    register_api(worker_sflow_monitor.SflowAPI, 'sflow_realtime_api', '/worker/sflow/')  

    # 增加跨域访问支持
    CORS(app, supports_credentials=True, resources={r"/*": {"origins": "*"}})
    return app


def create_data_server_app(app_name=PKG_NAME, **kwargs):
    app = Flask(__name__, static_folder='../expr_monitor_user_data/', static_url_path='/static')
    if kwargs.get('celery'):
        init_celery(kwargs.get('celery'), app)
    
    def register_api(view, endpoint, url):
        view_func = view.as_view(endpoint)
        app.add_url_rule(url, view_func=view_func, methods=['POST', 'DELETE', 'GET', 'PUT'])

    from .api.data_server import data_server
    from .api.data_server import expr_figure

    # 数据计算服务器
    register_api(data_server.DataServerAPI, 'data_server_api', '/data-server/expr/')
    register_api(expr_figure.ExprFigureAPI, 'expr_figure_api', '/data-server/expr_figure/')

    # 增加跨域访问支持
    CORS(app, supports_credentials=True, resources={r"/*": {"origins": "*"}})
    return app


def create_websocket_app():
    app = Flask(__name__)
    # CORS(app, supports_credentials=True, resources={r"/*": {"origins": "*"}})
    socketio = SocketIO(
        app, 
        message_queue=(f"amqp://{PROJ_CONFIG.rabbitmq_ip}:"
                       f"{PROJ_CONFIG.rabbitmq_port}"),
        cors_allowed_origins='*')

    @app.route("/")
    def index():
        return render_template("test_socketio.html")
    
    socketio.on_event("connected_event", connected)
    
    return app


def create_web_terminal_app():
    # TODO(mt): 用flask-socketio改写
    app = Flask(__name__)
    
    sockets=Sockets(app)
    @app.route("/")
    def index():
        return render_template("test_webterminal.html")

    @sockets.route('/container/')  # start container socket
    def enter_container(ws):
        print("@sockets.route('/container/')")
        start_web_socket(ws)

    return app


def create_celery_app(app_name=PKG_NAME, **kwargs):
    """
    创建 worker_server 的工厂函数
    """
    app = Flask(__name__, static_folder='../expr_monitor_user_data/', static_url_path='/static')

    app.config["SQLALCHEMY_DATABASE_URI"] = ("mysql://root:[REDACTED]@"
        f"{PROJ_CONFIG.mysql_ip}:{PROJ_CONFIG.mysql_port}/"
        f"{PROJ_CONFIG.mysql_database}?charset=utf8")
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    if kwargs.get('celery'):
        # https://flask.palletsprojects.com/en/1.1.x/patterns/celery/
        init_celery(kwargs.get('celery'), app)
    if kwargs.get('mysql'):
        kwargs.get('mysql').init_app(app)

    from .api.monitor import worker_monitor
    from .tasks.topo import master_deploy_topo
    from ..satellite.worker_eventset import celery_asy_func

    return app
