from vemu_uestc.vemu_config.config import PROJ_CONFIG


debug = True
loglevel = 'debug'
# 这里应该使用局域网IP地址
bind = f"0.0.0.0:{PROJ_CONFIG.data_server_port}"


keepalive = 5
# daemon = True
worker_connections = 100000
graceful_timeout = 0
timeout = 0

chdir = './'
pidfile = 'data_server_gunicorn.pid'
# logfile = 'worker_debug.log'

access_log_format = '%(t)s %(p)s %(h)s "%(r)s" %(s)s %(L)s %(b)s %(f)s" "%(a)s"' 
# errorlog = "./worker_errlog"
reload =True
# accesslog = "./worker_logs"

# production mode
# workers = multiprocessing.cpu_count() * 2 + 1
# develop mode 
workers = 4
# worker_class = 'sync'
worker_class = 'gunicorn.workers.ggevent.GeventWorker'

x_forwarded_for_header = 'X-FORWARDED-FOR'
