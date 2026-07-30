"""
worker 订阅事件，并将事件传送为celery定时任务
"""

from .satool import get_host_ip, pub_sub_redis, datetime, chain, chord, group
from .worker_eventset import celery_asy_func


# 本worker的ip，是订阅的事件名
my_ip = get_host_ip()


def sat_evt_distribute():
    """
    星座事件的接受与传送
    """
    try:
        pubsub = pub_sub_redis.subscribe(channel=my_ip)
        print('Start!')

        for data in pub_sub_redis.get_msgs(pubsub):
            
            # 参数接受
            t = data["time"]
            user = data["user"]
            topo = data["topo"]
            workflow = data["workflow"]
            events = data["events"]
            print(len(events))
                
            
            def create_celery_workflow(task_list, conn=group):
                """
                通过事件列表构建工作流
                """
                if len(task_list) == 1:
                    return celery_asy_func.si(user, topo, *events[task_list[0]])
                task_signatures = [
                    create_celery_workflow(item, chain if conn == group else group)
                    for item in task_list
                ]
                return conn(task_signatures)
            
            
            # 异步任务注册，标定事件执行时间
            wf = create_celery_workflow(workflow)
            # 应用 apply_async 到 group 或 chain 对象上
            wf_result = wf.apply_async(eta=datetime.utcfromtimestamp(t))
            # 打印
            # print(f"< {t, wf_result.id}")
        
    except KeyboardInterrupt:
        print("houx bye~")
