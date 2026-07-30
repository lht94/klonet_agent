# 该函数用于将已创建的celery实例的配置进行更新
# 使用flask_app 的 app_context
def init_celery(celery, app):
    """
    更新已经创建的celery实例的配置信息，加载flask app的上下文信息
    Args:
        celery (Celery): Celery实例
        app     (flask_app): flask app 实例
    Returns:
        None
    """
    celery.conf.update(app.config)
    TaskBase = celery.Task

    class ContextTask(TaskBase):
        """
        在 Celery Task中加载flask上下文
        """
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    # 更新celery 的task
    celery.Task = ContextTask
