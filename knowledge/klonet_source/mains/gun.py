import os
from socket import timeout
from gevent import monkey
from numpy import mask_indices
from vemu_uestc.vemu_config.config import PROJ_CONFIG
from vemu_uestc.Function_layer.server_health_master import start_worker_health_check_process, remove_all_broken_projects_table

monkey.patch_all(thread=False, socket=False)

debug = True
loglevel = 'debug'
bind = f'0.0.0.0:{PROJ_CONFIG.master_port}'

keepalive = 5
# daemon = True
worker_connections = 100000
graceful_timeout = 0
timeout = 0

chdir = './'
pidfile = 'gunicorn.pid'
# logfile = 'debug.log'

if PROJ_CONFIG.manager_logger_enable:
    access_log_format = PROJ_CONFIG.gunicorn_access_log_format
    logconfig_dict = PROJ_CONFIG.logging_config_dict
# access_log_format = '%(t)s %(p)s %(h)s "%(r)s" %(s)s %(L)s %(b)s %(f)s" "%(a)s"' 
# errorlog = "./errlog"
reload = True
# accesslog = "./logs"

# production mode
# workers = multiprocessing.cpu_count() * 2 + 1
# develop mode 
workers = 4
worker_class = 'gunicorn.workers.ggevent.GeventWorker'

x_forwarded_for_header = 'X-FORWARDED-FOR'

# 心跳进程，注意本进程的代码不参与gunicorn的worker代码自动重载
if PROJ_CONFIG.heartbeat_enabled:
    start_worker_health_check_process()
else:
    remove_all_broken_projects_table()

def post_worker_init(worker):
    '''
    gunicorn master初始化时的回调函数
    start_worker_health_check_process()在gunicorn master自动重载时会报错。

    https://docs.gunicorn.org/en/stable/settings.html#post-worker-init
    https://github.com/benoitc/gunicorn/issues/1391#issuecomment-867379030
    '''
    import atexit
    from multiprocessing.util import _exit_function
    atexit.unregister(_exit_function)
    worker.log.info(f"worker post_worker_init done, (pid: {worker.pid})")