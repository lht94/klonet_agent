from vemu_uestc.webserver import app_factory
import vemu_uestc.webserver as webserver
from vemu_uestc.tools import get_host_ip

# 启动方式：
# gunicorn -c gun.py --worker-class eventlet -w 1 master_main:flask_app -b 127.0.0.1:5000


# flask_app = app_factory.create_app(celery=webserver.celery)
flask_app = app_factory.create_master_app(
    celery=webserver.celery, mysql=webserver.mysql)

flask_app.config['ENV'] = 'development'
flask_app.config['DEBUG'] = True
# 可使用 python3.8 -c 'import os; print(os.urandom(16))' 产生密匙
flask_app.config['SECRET_KEY'] = "\xfa\x9f\xf9\xe0\xe4!\x912>\x8611\x02%e\x91"

if __name__ == '__main__':
    # import logging
    # logging.basicConfig(level=logging.INFO)
    # http_server = WSGIServer(('', 5000), flask_app)
    # # flask_app.run(
    # #     host=app_config['HOST'],
    # #     port=app_config['PORT'],
    # #     debug=app_config['DEBUG'],
    # # )
    # http_server.serve_forever()
    pass
