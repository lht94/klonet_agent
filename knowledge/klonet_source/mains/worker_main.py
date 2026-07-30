from vemu_uestc.webserver import app_factory
import vemu_uestc.webserver as webserver
from vemu_uestc.tools import get_host_ip

flask_app = app_factory.create_worker_app(celery=webserver.celery)
flask_app.config['ENV'] = 'development'
flask_app.config['DEBUG'] = True


if __name__ == '__main__':
    # import logging
    # logging.basicConfig(level=logging.INFO)
    # http_server = WSGIServer(('', 5001), flask_app)
    # flask_app.run(
    #     host=app_config['HOST'],
    #     port=app_config['PORT'],
    #     debug=app_config['DEBUG'],
    # )
    # http_server.serve_forever()
    pass
