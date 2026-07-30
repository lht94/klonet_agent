from vemu_uestc.webserver import celery
import vemu_uestc.webserver as webserver
from vemu_uestc.webserver.app_factory import create_celery_app




app = create_celery_app(celery=celery, mysql=webserver.mysql)

# 运行命令：
# /usr/local/python3/bin/celery -A celery_worker.celery worker --loglevel=info
