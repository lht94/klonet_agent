import socket 
from celery import Celery
from ..vemu_config.config import PROJ_CONFIG
from flask_sqlalchemy import SQLAlchemy

hostname = socket.gethostname()


def make_celery(app_name=__name__):
    """
    初始化Celery实例
    Args:
        app_name (str): celery app 名称

    Returns:
        celery_instance (Celery): celery实例
    """
    # 从config.py读取信息
    host = PROJ_CONFIG.master_ip
    redis_port_db = PROJ_CONFIG.celery_redis_port_db
    rabbitmq_port_db = PROJ_CONFIG.celery_rabbitmq_port_db

    # 得到redis和rabbitmq的数据库地址
    redis_url = f'redis://default:[REDACTED]@{host}:{redis_port_db}'
    rabbitmq = f'redis://default:[REDACTED]@localhost:{rabbitmq_port_db}'
    
    # 返回celery实例
    return Celery(
        app_name,
        backend=redis_url,
        broker=rabbitmq,
        # https://docs.celeryq.dev/en/stable/userguide/configuration.html#std-setting-broker_pool_limit
        # https://stackoverflow.com/questions/12923758/celery-error-connection-reset-by-peer
        broker_pool_limit = None,
        broker_heartbeat=30,   # 心跳检测（秒）
        broker_connection_timeout=30,  # 连接超时（秒）
        broker_connection_retry=True,   # 启用自动重连
        broker_connection_max_retries=3  # 最大重试次数
    )


celery = make_celery()
mysql = SQLAlchemy()