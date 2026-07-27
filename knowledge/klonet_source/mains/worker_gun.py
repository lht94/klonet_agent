import os
import multiprocessing
from socket import timeout
from vemu_uestc.vemu_config.config import PROJ_CONFIG
from vemu_uestc.Service_layer.server_health_worker import start_heatbeat_process
from vemu_uestc.Service_layer.kvm_image_sync import start_kvm_image_sync
from vemu_uestc.tools.tools import register_worker

debug = True
loglevel = 'debug'
bind = f'0.0.0.0:{PROJ_CONFIG.worker_port}'


keepalive = 5
# daemon = True
worker_connections = 100000
graceful_timeout = 0
timeout = 0

chdir = './'
pidfile = 'worker_gunicorn.pid'
# logfile = 'worker_debug.log'
if PROJ_CONFIG.manager_logger_enable:
    access_log_format = PROJ_CONFIG.gunicorn_access_log_format
    logconfig_dict = PROJ_CONFIG.logging_config_dict
# errorlog = "./worker_errlog"
reload =True
# accesslog = "./worker_logs"

# production mode
# workers = multiprocessing.cpu_count() * 2 + 1
# develop mode 
workers = 4
worker_class = 'sync'

x_forwarded_for_header = 'X-FORWARDED-FOR'

# 心跳进程，注意本进程的代码不参与gunicorn的worker代码自动重载
if PROJ_CONFIG.heartbeat_enabled:
    start_heatbeat_process()
    
if (PROJ_CONFIG.only_default_image_sync or PROJ_CONFIG.only_web_kvm_image_sync or
    PROJ_CONFIG.only_self_image_sync):
    start_kvm_image_sync()

def post_worker_init(worker):
    '''
    gunicorn worker初始化时的回调函数
    若不加此函数，则start_heatbeat_process()在gunicorn worker自动重载时会报错。

    https://docs.gunicorn.org/en/stable/settings.html#post-worker-init
    https://github.com/benoitc/gunicorn/issues/1391#issuecomment-867379030
    '''
    import atexit
    from multiprocessing.util import _exit_function
    atexit.unregister(_exit_function)
    worker.log.info(f"worker post_worker_init done, (pid: {worker.pid})")
