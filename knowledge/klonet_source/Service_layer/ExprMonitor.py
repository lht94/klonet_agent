from .redisAPI import UserMapRedis
from . import worker_expr_monitor
from .topo_deploy_errors import PcapDeployError
from ..tools import get_host_ip
from ..webserver.webapp import celery_app



def deploy_pcap_monitor(user, topo, expr):
    try:
        pcap_processing_list = worker_expr_monitor.deploy_pcap_monitor(user, topo, expr)
        return pcap_processing_list
    except:
        raise PcapDeployError('user: {} topo: {} expr:{} pcap deploy on worker{} failed'.format(
                            user, topo,  expr, get_host_ip()))

# 返回的是AsyncResult
def terminate_pcap_monitor(user, topo, expr, pcap_processing_list):
    worker_expr_monitor.terminate_pcap_monitor(pcap_processing_list)
    # 这里是没有返回值的 save_raw_data_to_db
    # 也许之后的有些是不需要返回parent_id的
    # 如果之后需要将创建等操作也换成异步执行的话, 可能taskapi的视图函数需要额外的处理
    # 流量创建， 使用group
    # 这里使用chain是必要的， 因为这里有逻辑上执行的先后顺序
    task_chain = save_raw_data_to_db.s(user, topo, expr) | raw_data_calculate.s(user, topo, expr)
    res = task_chain()
    return {'task_id': res.id, 'parent_task_id': res.parent.id}
    
# link 会将第一个任务的结果作为第二个任务的第一个参数？
# 如果没有值返回呢
@celery_app.task(track_started=True)
def save_raw_data_to_db(user, topo, expr):
    worker_expr_monitor.save_raw_data_to_db(user, topo, expr)

# 这里暂时没有找到好的方法来省略前一个结果的result
# 因为前一个task的运行结果会作为下一个任务的第一个参数
# 所以这里使用关键字参数会更好？
@celery_app.task(track_started=True)
def raw_data_calculate(parrenr_task_result, user, topo, expr):
    worker_expr_monitor.start_raw_data_calc(user, topo, expr)
