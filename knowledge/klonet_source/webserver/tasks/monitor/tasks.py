import grequests  # 必须导入，否则worker无法启动

from ....webserver import celery
from ....Service_layer import worker_expr_monitor
from ....Service_layer.data_server_manager import DataServerManager


def terminator_pcap_monitor(user, topo, expr, processing_list):
    '''
    终止pcap程序、将数据存入数据库，并对性能指标进行计算

    Args:
        user: 用户名
        topo: 拓扑名
        expr: 实验标识
        processing_list: 各监控程序的进程列表

    Returns:
        dict: {'task_id': res.id, 'parent_task_id': res.parent.id}
    '''
    print('正在停止用户的pcap进程...')
    worker_expr_monitor.terminate_processings(processing_list)
    print('停止进程完成....开始写入数据库并计算原始数据')
    result = save_raw_data_to_db.delay(user, topo, expr)
    print(result.id)
    return {'task_id':result.id}


def data_server_start_calc(user, topo, expr):
    result = raw_data_calc.delay(user, topo, expr)
    print(f"task id: {result.id}")
    return {'task_id':result.id}


@celery.task(track_started=True)
def raw_data_calc(user, topo, expr):
    ds_manager = DataServerManager()
    ds_manager.start_raw_data_calc(user, topo, expr)


@celery.task(track_started=True)
def save_raw_data_to_db(user, topo, expr):
    worker_expr_monitor.save_raw_data_to_db(user, topo, expr)
