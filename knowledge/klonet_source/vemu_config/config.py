from enum import Enum
import os
import logging.config
from concurrent_log_handler import ConcurrentRotatingFileHandler

'''
使用本配置文件时，请导入PROJ_CONFIG !!!
'''

# 每次有代码提交至develop上时，请务必更新版本号，代表一个新版本
class VersionConfig(object):
    # 版本号格式：xxxx(年).xx(月).xx(日).x(当日的第x次提交)
    __version__ = "2024.4.27.1"

class FileloadConfig(object):
    up_end_dir = '/root/vemu_static/upload'
    down_end_dir = '/root/vemu_static'
    # up_temp_dir = os.path.abspath(os.path.dirname(os.path.dirname(__file__))) + '/vemu_static/upload'
    # down_temp_dir = os.path.abspath(os.path.dirname(os.path.dirname(__file__))) + '/vemu_static'
    up_temp_dir = '/home/vemu_static/upload'
    down_temp_dir = '/home/vemu_static'
    
class BaseConfig(object):
    master_ip = '192.168.1.124'
    master_port = 12000
    worker_port = 12001
    # 是否给容器做资源限制(quota模式，与下面的CPU_SET互斥)
    resource_limit_enable = False
    # wudx
    # 是否利用CPU_SET方式进行容器资源隔离
    # 以下两种模式互斥，不能同时为True！
    # 此外，CPU_SET资源限制必须搭配SPLIT_WITH_TOPO_RESOURCE拓扑切分使用！
    node_iso_resource_limit_CpuSet = False  # 1.以节点为单位资源隔离（高性能模式）
    topo_iso_resource_limit_CpuSet = False  # 2.以拓扑为单位资源隔离（基本模式，节省资源，更推荐！）
    single_user_CpuNum = 100    # 每个用户核心数目配额
    basic_os_cores = 3          # 留存给系统基本命令操作的核心数目（建议不要设置过大）
    
    # KVM虚拟机cpu核心数向容器cpu运行换算比例
    # 用于容器虚拟机统一切分算法
    ratio = 100

class MysqlConfig(object):
    mysql_port = 3307
    mysql_user = 'root'
    mysql_password = '[REDACTED]'
    mysql_database = 'mydb'
    #  静态项目文件夹路径
    static_project_dir = (os.path.abspath(os.path.dirname(
        os.path.dirname(__file__))) + "/static_projects")
    
    #  实验仓库复现实验脚本文件夹路径
    static_scripts_dir = (os.path.abspath(os.path.dirname(
        os.path.dirname(__file__))) + "/static_scripts")

class ExprMonitorConfig(object):
    # 网络实验监控用户数据文件夹路径
    user_data_dir = (os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
                     + "/expr_monitor_user_data")
    # 吞吐指标计算间隔(秒)
    interval_s = 1
    # 时延指标计算识别窗口
    identify_window_ms = 786
    # 时延指标计算采样率
    delay_sample_rate = 0.2
    # 存储srtt指标文件名
    srtt_file_name = "srtt.csv"
    # 实验结果绘图数据点数
    expr_figure_dot_num = 50

class InfluxDbConfig(object):
    data_server_flask_port = 5020
    influxdb_port = 8086
    influxdb_name = 'mydb'

class RabbitmqConfig(object):
    rabbitmq_port = 5672

class RedisConfig(object):
    redis_port = 8368
    redis_password = '[REDACTED]'
    redis_user = 'default'
    worker_list = 'worker_list'
    redis_db_count = 9999

class CeleryConfig(object):
    container_port = 6379
    celery_redis_port_db = f'{container_port}/2'
    celery_rabbitmq_port_db =  f'{container_port}/3'

class PrometheusConfig(object):
    prometheus_file_path = (os.path.abspath(os.path.dirname(os.path.dirname(__file__))) +
                            "/prometheus")
    prometheus_file_name = '/prometheus_test.yml'
    prometheus_port = '9090'
    prometheus_scrape_interval = '10s' # 默认10s采集一个点
    grafana_port = '10038'
    node_exporter = '8001'
    cadvisor = '8002'

class SplitOption(Enum):
    # 不开启资源获取的切分（旧切分，代码中包含不切分、均分等切分方式）
    NO_SPLIT = 0
    # 根据物理资源剩余量进行切分(不启动资源量化、仅根据前端传的资源需求量)
    SPLIT_WITH_RESOURCE = 1
    # 根据物理资源剩余量进行切分(开启资源量化——网络中可能存在的流量重新计算需求)
    SPLIT_WITH_QUANTIFICATION = 2
    # 结合广度优先遍历和资源进行切分
    SPLIT_WITH_TOPO_RESOURCE = 3

class SplitConfig(object):
    split_option = SplitOption.SPLIT_WITH_TOPO_RESOURCE

class FileDownloadConfig(object):
    # 上传下载相关配置
    public_ip = 'https://vlab.uestc.edu.cn/course-klonet'  # 公网ip
    public_port = '80'  # nginx监听的端口，默认为80

class ImageConfig(object):
    image_registry_ip = "192.168.1.124"
    image_registry_port = 5024
    remote_docker_daemon_port = 2375
    upload_file_size_limit_byte = 1 * 1024 * 1024 * 1024  # 镜像上传文件限制
    image_registry_dir = (os.path.abspath(os.path.dirname(
        os.path.dirname(__file__))) + "/image_registry_files")  # 镜像文件保存位置
    nic_list = ['enp61s0f3']
    # 垃圾回收手动开关
    GC_enable = False
    
    # ======KVM image params======
    # kvm镜像大小的限制与容器共用upload_file_size_limit_byte
    # kvm镜像默认路径
    kvm_image_registry_dir = (os.path.abspath(os.path.dirname(
        os.path.dirname(__file__))) + "/kvm_image_registry")
    default_host_image = "centos7.qcow2"
    default_router_image = "ne40e.qcow2"
    default_switch_image = ""   # TODO:后续有可用镜像时设置
    default_controller_image = ""   # TODO:后续有可用镜像时设置
    
    # 虚机镜像同步机制（一个比较耗时的同步功能）
    # 镜像同步机制仅在当前worker被第一次加入worker_list前生效
    # 同时该机制会自动将worker注册进worker_list
    # 下面三个设置要保证master和worker上设置一致！
    only_default_image_sync = True  # 仅有平台默认镜像的同步
    only_web_kvm_image_sync = True  # 仅有web端上传镜像的同步
    only_self_image_sync = True     # 仅有用户非web上传镜像的同步
    # 镜像同步工具rsync所用的密码用户
    rsync_user = "vemu"
    rsync_password = "[REDACTED]"
    
    # ======Nvidia ON List========
    # (TODO)wudx:这种方式目前看着有点呆，先应对交付
    # 设置能使用GPU的完整容器镜像名
    nvidia_on_list = [
        # "host/ubuntu"
    ]

class ExtendConfig(object):
    push_msg_enabled = False
    login_required_disabled = True
    # 所有容器创建时是否挂载系统时钟
    mount_host_clock_enabled = True
    
class UserConfig(object):
    # 是否需要注册时验证码以及管理员审核
    register_audit = False
    # 是否需要登录过期功能
    login_expired = False
    # 是否需要限制多点登录功能
    multi_login_allowed = False

class HardwareConfig(object):
    switch_list = 'switch_list'
    hardware_list = 'hardware_list'
    hardware_worker = '192.168.1.33'

class HeartbeatHealthConfig(object):
    # 心跳检查worker健康机制相关配置

    # 更改此选项后，需要重新启动master和worker（非自动重载代码）
    # 心跳检查worker健康机制建议仅长期创建、不频繁启停master/worker时开启。
    #   - 若worker被停止，则：
    #       1. master认为该worker已经掉线，将其从worker_list中移除；
    #       2. master认为该worker上创建的所有子拓扑已失效，并在各用户数据库中创建
    #          broken_projects表，记录相关失效拓扑；
    #       3. 此时前端的定时请求会检查出相关失效拓扑，并通知用户删除。
    #   - 若不小心启停了worker，导致master对worker的健康状态造成误判，可将此选项设为
    #     False后重启master。即可：
    #       1. 在worker_list中注册本worker
    #       2. 在worker_list中重新删除所有的broken_projects表
    heartbeat_enabled = False
    heartbeat_table_name = "worker_last_heartbeat_timestamps"
    broken_projects_table_name = "broken_projects"

class ProcessBarConfig(object):
    # 存储在每个用户数据库的表名前缀
    pb_table_name_prefix = 'process_bar_table'
    # 进度条有关的各种功能用法
    pb_usages = ('deploy', 'delete')
    # 创建进度条各个过程的比例
    deploy_process_bar_proportion = {
        'ne_create': 1,
        'veth_create': 1,
        'vxlan_create': 1,
        'l3_service': 0,
        'l2_service': 1,
        'other_service': 0,
        'dpdk_service': 0,
    }
    # 删除进度条各个过程的比例
    delete_process_bar_proportion = {
        'ne_delete': 1,
        'vxlan_delete': 1,
        'overlay_delete': 1,
        'db_delete': 1,
    }

class ContainerPortConfig(object):
    # 端口映射配置，默认给所有容器开启哪些端口，需映射到宿主机
    container_port_default = [] # 此功能目前偶尔有端口占用冲突问题
    # 宿主机默认开启的可用端口映射，规定起止端口
    port_from = 5001
    port_to = 40000
    host_ports = list(range(port_from, port_to+1))

class MailConfig(object):
    mail_enable =  False             # 邮件功能使能，True为开启，False为关闭
    mail_host = "smtp.qq.com"        # STMP服务器
    mail_user = "948409821@qq.com"   # 用户名
    mail_pass = "[REDACTED]"   # 口令
    code_max_waiting_time = 300      # 验证码最大等待时间
    # 欢迎邮件的信息
    mail_msg_welcome = "欢迎来到Klonet!\n\nKlonet: 利用轻量级虚拟化技术，实现网络设备控制平面和数据平面的模拟，融合网络模拟和网络仿真的技术优势，搭建轻量级、高保真、易编程的大规模网络集成模拟平台。"
    # 忘记密码邮件的信息
    mail_msg_forget_passwd = f"From Klonet\n\n你的平台登录验证码{int(code_max_waiting_time/60)}分钟内有效。\n\n验证码为: "

class SatelliteConfig():
    ##################### 物理学常数 #####################
    earth_a = 6378.137          # 【WGS-84】地球长半轴
    earth_b = 6356.752314       # 【WGS-84】地球短半轴
    earth_r = 6371.393          # 【WGS-84】地球平均半径
    GM = 3986005 * 10 ** 8      # 【WGS-84】地球引力和地球质量的乘积
    light_speed = 299792458     # 光速，单位m/s
    boltzmann_k = 1.380649e-23  # 玻尔兹曼常数，单位J/K
    #################### 卫星通信常数 ####################
    freq = {                    # 选用Ku波段
        "sat-sat": 13e9,        # 星间链路频率，单位Hz
        'sat-gnd up': 14e9,     # 星地链路，上行频率，单位Hz
        'sat-gnd down': 12e9,   # 星地链路，下行频率，单位Hz
    }
    L_a = 4                     # 系统中其他损耗，包含大气吸收损耗、雨衰等，单位dB
    pkt_avg_len = 700           # 物理层数据包的平均长度，用于计算丢包率，单位bit
    bw = {                       # 默认的链路带宽，单位kbps 
        "sat-sat": 850000,       # 星间链路
        "sat-gnd up": 700000,    # 星地上行
        "sat-gnd down": 800000,  # 星地下行
    }
    ###################### 天线参数 ######################
    # EIRP和G/T含义介绍：https://www.cnblogs.com/lsgxeva/p/13618420.html
    # 参数取值参考Starlink：https://zhuanlan.zhihu.com/p/166352918
    sat_EIRP = 36.7    # 卫星天线有效全向辐射功率，单位dBW
    sat_GT = 32.8/300  # 卫星天线接收系统平直因数，单位dB/K
    # 地面站天线的三个等级，等级越小越厉害。三值含义：
    # 1、天线有效全向辐射功率，单位dBW
    # 2、天线接收系统平直因数，单位dB/K
    # 3、天线可视角度范围，单位度
    gnd_dev_level = [
        [13, .10, 160],
        [13, .09, 120],
        [12, .09, 100]
    ]
    ###################### 网络参数 ######################
    # 星间链路地址块
    sat_link_ip = '10.0.0.0/8'
    # 链路子网掩码
    link_subnet_mask = '255.255.255.252'
    # 实时监控端到端测试ping的发包数
    ping_pkt_num = 8
    # 每个对地小子网的掩码
    sat_gnd_subnet_mask = '255.255.255.240'
    ###################### 程序参数 ######################
    refresh_interval_para = 10     # 【动态定时刷新】初始的星座世界刷新间隔系数
    no_run_too_long_max_count = 5  # 【动态定时刷新】未超时的最大次数
    max_time_speed = 200          # 最快时间加速速度
    max_time_start = 16725196800  # 初始时间限制在1970-01-01 08:00:00 ~ 2500-1-1 0:00:00
    max_rs = 200                  # 星间最大延迟
    max_try_visible_time_left = 720  # 【剩余可见时间】最大试探次数，720次对应12小时
    det_t_visible_time_left = 1      # 【剩余可见时间】试探时间间隔，单位为分钟
    frontend_sat_a = 650          # 【星座显示】长半轴
    frontend_sat_b = 320          # 【星座显示】短半轴
    frontend_sat_offset = 6       # 【星座显示】微小偏移
    max_satlog_length = 22   # 最长卫星日志条目上限
    default_ryu_name = 'c1'  # 开启SDN时，默认的ryu控制器名称
    tunnel_mode = 'gre'    # 开启IP-TUNNEL模式时的隧道模式
    sat_table_name = '_sat_topo'
    container_log_file = '/satellite.log'
    sat_enable = True             # 星座功能总开关

class LogggingConfig(object):
    """日志配置类
    
    主要从控制、格式、记录器三方面完成对日志的配置，更新配置需要重启服务，完成配置后，
    可使用管理日志记录器(ManagerLogger)和用户日志记录器(UserLogger)完成日志的记录。
    导入:from tool.log_tools import * 
    
    推荐方法:
    
        FLASK_LOGGER.info('这是一个测试')

        logger = ManagerLogger()
        logger.info('这是一个测试')
        logger.warning('再来一次')

        logger = ManagerLogger(logger = 'celery')
        logger.debug('这是另一个测试')

        ManagerLogger().error('错误信息')

    需要指出的是，为避免频繁创建类的开销，应该尽量使用第一种，有需要时可以用其它方法
    gunicorn日志输出包括很多冗余信息，提供的信息有限，不便于集成，应当尽量使用flask或
    者其它自定义的日志记录器来定位错误信息，逐渐淘汰gunicorn的日志输出，原则上每一处
    try...except...语句均应包含一句日志消息用于记录异常上下文。
    """

    # 控制
    # 使能 True/False，True对应开启，False对应关闭
    # 管理日志器使能，为 false 则关闭所有管理日志记录器
    manager_logger_enable = True
    # 为 false 则关闭对应等级的日志记录器输出，可用于调试时输出特定级别的日志信息
    debug_enable = True
    info_enable = True
    warning_enable = True
    error_enable = True
    critical_enable = True
    # 用户日志器使能，为 false 则关闭所有用户日志记录器
    user_logger_enable = True

    # 格式
    # 远程地址 状态行 状态码 请求时间
    gunicorn_access_log_format = '%(h)s "%(r)s" %(s)s %(L)s' 
    # 用户日志存储条目上限
    mysql_events_count_First = 20
    mysql_events_count_Second = 10
    redis_events_count_First = 20
    redis_events_count_Second = 20
    # 文件位置
    loggging_access_filepath = f'{os.getcwd()}/vemu_uestc/logs/access.log'
    loggging_error_filepath = f'{os.getcwd()}/vemu_uestc/logs/error.log'
    # 文件大小上限 10Mb
    file_maxBytes = 1024 * 1024 * 10   
    # 文件备份数量
    file_backupCount = 1

    # 记录器配置，需要对记录器、格式器、处理器、过滤器有基本认识
    logging_config_dict = {
        "version": 1,
        # 是否禁用现有记录器
        "disable_existing_loggers": False,
        # 格式器 格式器作用在处理器上
        "formatters": {
            "simple": {"format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"},
            "generic": {
                "format": "%(asctime)s,%(msecs)03d - %(message)s", # 打日志的格式
                "datefmt": "%Y-%m-%d %H:%M:%S",# 时间显示格式
                "class": "logging.Formatter"
            },
            "gunicorn":{
                "format": "%(asctime)s,%(msecs)03d - %(filename)s " + \
                        "[line:%(lineno)d]:%(funcName)s - "+ \
                        "%(levelname)s - %(process)d - %(message)s" ,
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "class": "logging.Formatter"
            }
        },
        # 处理器集合
        "handlers": {
            # 控制台 
            "console": {
                "class": "logging.StreamHandler",
                "level": "DEBUG",
                "formatter": "generic",
                "stream": "ext://sys.stdout",
            },
            "gunicorn_console": {
                "class": "logging.StreamHandler",
                "level": "DEBUG",
                "formatter": "gunicorn",
                "stream": "ext://sys.stdout",
            },
            "file_handler": {
                "class": "concurrent_log_handler.ConcurrentRotatingFileHandler",
                "level": "DEBUG",
                "formatter": "generic",
                "filename":loggging_access_filepath,
                "maxBytes": file_maxBytes,
                "backupCount": file_backupCount,
                "encoding": "utf8",
            },
            "error_file_handler": {
                "class": "concurrent_log_handler.ConcurrentRotatingFileHandler",
                "level": "WARNING",
                "formatter": "generic",
                "filename":loggging_error_filepath,
                "maxBytes": file_maxBytes,
                "backupCount": file_backupCount,
                "encoding": "utf8",
            },
            "gunicorn_file_handler": {
                "class": "concurrent_log_handler.ConcurrentRotatingFileHandler",
                "level": "DEBUG",
                "formatter": "gunicorn",
                "filename":loggging_access_filepath,
                "maxBytes": file_maxBytes,
                "backupCount": file_backupCount,
                "encoding": "utf8",
            },
            "gunicorn_error_file_handler": {
                "class": "concurrent_log_handler.ConcurrentRotatingFileHandler",
                "level": "WARNING",
                "formatter": "gunicorn",
                "filename":loggging_error_filepath,
                "maxBytes": file_maxBytes,
                "backupCount": file_backupCount,
                "encoding": "utf8",
            }

        },
        # 记录器集合，所有输出到控制台的日志由console记录器来管理
        "loggers": {
            # gunicorn记录器的配置，必须保证记录器名字正确，否则无法获取
            "gunicorn.access": {
                "level": "DEBUG", 
                "handlers": ["gunicorn_console","gunicorn_file_handler",\
                        "gunicorn_error_file_handler"], 
                "propagate": 0
            },
            "gunicorn.error": {
                "level": "DEBUG", 
                "handlers": ["gunicorn_console","gunicorn_file_handler",\
                        "gunicorn_error_file_handler"], 
                "propagate": 0
            },
            "flask": {
                "level": "DEBUG", 
                # 处理器列表，传递进来的日志消息用哪些处理器
                "handlers": ["file_handler","error_file_handler"], 
                # 是否传递给父记录器
                "propagate": 0
            },
            # 存在部分问题，暂时未能将celery的日志集成到flask当中
            # "celery": {
            #     "level": "DEBUG", 
            #     "handlers": ["file_handler","error_file_handler"], 
            #     "propagate": 0
            # },
            
            # 未使用
            "console": {
                "level": "DEBUG", 
                "handlers": ["console"], 
                "propagate": 0
            }
            
        } 
    }

class CommonConfig(MysqlConfig, InfluxDbConfig, ExprMonitorConfig,
                   RabbitmqConfig, RedisConfig, ExtendConfig, UserConfig, CeleryConfig,
                   PrometheusConfig, FileDownloadConfig, ImageConfig,
                   HeartbeatHealthConfig, SplitConfig, BaseConfig,
                   ProcessBarConfig, ContainerPortConfig, LogggingConfig, FileloadConfig,
                   MailConfig, SatelliteConfig, VersionConfig, HardwareConfig):
    pass


class VemuConfig(CommonConfig):
    master_ip = "192.168.1.124"
    master_port = 12000
    worker_port = 12001
    web_terminal_port = 5005
    image_registry_ip = master_ip
    data_server_port = 5010
    data_server_ip = master_ip
    mysql_ip = master_ip
    rabbitmq_ip = master_ip
    influxdb_ip = master_ip

class WtxConfig(CommonConfig):
    # master_ip = '192.168.1.33'
    master_ip = '192.128.122.94'
    master_port = 45551
    worker_port = 45552
    public_port = 45553
    web_terminal_port = 5114
    data_server_port = 5010
    data_server_flask_port = data_server_port
    data_server_ip = master_ip
    image_registry_ip = master_ip
    mysql_ip = '127.0.0.1'
    rabbitmq_ip = master_ip
    influxdb_ip = master_ip
    public_ip= master_ip
    redis_user = 'default'
    redis_password = '[REDACTED]'
    celery_redis_port_db = '6379/6'
    celery_rabbitmq_port_db =  '6379/7'
    worker_list = 'worker_list'

class VRConfig(CommonConfig):
    """
    虚仿使用配置
    """
    master_ip = '192.168.1.124'
    master_port = 10021
    worker_port = 13001
    worker_list = 'worker_list'
    web_terminal_port = 10012
    data_server_port = 5011
    data_server_flask_port = data_server_port
    data_server_ip = master_ip
    image_registry_ip = master_ip
    mysql_ip = '127.0.0.1'
    rabbitmq_ip = master_ip
    influxdb_ip = master_ip
    public_port = 12345
    redis_user = 'default'
    redis_password = '[REDACTED]'

class GkcConfig(CommonConfig):
    master_ip = '192.168.1.33'
    master_port = 20223
    worker_port = 20224
    data_server_port = 5010
    data_server_flask_port = data_server_port
    data_server_ip = master_ip
    image_registry_ip = "192.168.1.33"
    mysql_ip = '127.0.0.1'
    rabbitmq_ip = master_ip
    influxdb_ip = master_ip
    public_port = 12345
    redis_user = 'default'
    redis_password = '[REDACTED]'
    celery_redis_port_db = '6379/2'
    celery_rabbitmq_port_db =  '6379/3'
    worker_list = 'worker_list_huawei'
# 开启心跳机制，从而启动程序后maser和worker之间可以互相发现，更新数据库中的表项
    # heartbeat_enabled = True

PROJ_CONFIG = WtxConfig()
logging.config.dictConfig(PROJ_CONFIG.logging_config_dict)
