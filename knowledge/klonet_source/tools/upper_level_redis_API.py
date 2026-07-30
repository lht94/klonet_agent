from .context import redis_context, Db0Context

# 基于redisAPI.py的上层通用API可置于此文件，便于大家使用，不重复造轮子

def get_projects_on_worker(worker_ip):
    '''
    获取某worker上的所有用户的所有项目（topo），以及该项目上的所有节点

    Args:
        worker_ip: 某worker的ip地址

    Returns:
        projects_on_worker = {
            "user_name": {
                "project1": ["h1", "h2", ...],
                "project2": ["h1", "h2", ...],
                ...
            },
            ...
        }
    '''
    projects_on_worker = {}
    user2db = get_user2db()
    for user, user_db in user2db.items():
        projects_on_worker[user] = {}
        broken_subtopos = set()
        with redis_context(user) as user_db_cli:
            # 获取子拓扑信息
            plane_subtopo_list = user_db_cli.get_all_values(
                "plane_subtopo_list")
            
            # 根据worker_ip查找该用户的子拓扑
            subtopo2worker = user_db_cli.get_all_values("subtopo2worker")
            for subtopo, subtopo_worker_ip in subtopo2worker.items():
                if subtopo_worker_ip == worker_ip:
                    broken_subtopos.add(subtopo)

            # 根据子拓扑查找拓扑名
            topo2subtopo = user_db_cli.get_all_values("topo2subtopo")
            for topo, subtopos in topo2subtopo.items():
                for subtopo in subtopos:
                    if subtopo in broken_subtopos:
                        projects_on_worker[user][topo] = plane_subtopo_list[
                            subtopo]["NEs"]
                        # 反查到拓扑名后，就不用遍历剩余的子拓扑
                        break

    return projects_on_worker

def get_workers_to_nes(user, topo):
    '''
    获取某用户的某项目（拓扑）下的worker ip与节点的对应关系

    Args:
        user: 用户名
        topo: 项目名（拓扑名）

    Returns:
        workers_to_nes = {
            "worker ip": {"节点类型":["该类型下的节点列表"], ...}
        }
        例：
         workers_to_nes = {
            "10.0.1.105": {"host":["h1", "h2"], "switches": ["s1"], ...},
            ...
        }
    '''
    workers_to_nes = {}
    with redis_context(user) as user_db_cli:
        subtopo_list = user_db_cli.get_value("topo2subtopo", topo)
        for subtopo in subtopo_list:
            worker_ip = user_db_cli.get_value("subtopo2worker", subtopo)
            ctns = user_db_cli.get_value("subtopo_service", subtopo)
            workers_to_nes[worker_ip] = ctns

    return workers_to_nes

def get_container_ids(user, topo, container_list):
    '''
    给定指定项目的容器名列表，获取容器id列表
    '''
    container_ids = []
    with redis_context(user) as user_db_cli:
        pipe = user_db_cli._db_conn.pipeline()
        for container_name in container_list:
            pipe.hget(f"{topo}_{container_name}", "NEid")
        
        container_ids = pipe.execute()

        for i, container_id in enumerate(container_ids):
            if container_id: # else pass
                container_ids[i] = container_id.strip("\"")
            
        # print("container_ids: ")
        # print(container_ids)

    return container_ids

def get_domain_NEservice(user, topo, domain_list):
    '''
    给定指定项目的域列表，获取域NEservice，判断是虚机还是容器
    '''
    domain_NEservice = []
    with redis_context(user) as user_db_cli:
        pipe = user_db_cli._db_conn.pipeline()
        for domain_name in domain_list:
            pipe.hget(f"{topo}_{domain_name}", "NEservice")
        
        domain_NEservice = pipe.execute()

        for i, domain_NEservices in enumerate(domain_NEservice):
            if domain_NEservices: # else pass
                domain_NEservice[i] = domain_NEservices.strip("\"")

    return domain_NEservice

def get_domain_type(user, topo, domain_list):
    '''
    给定指定项目的域列表，获取域NEtype，判断是虚机还是容器
    '''
    domain_NEtype = []
    with redis_context(user) as user_db_cli:
        pipe = user_db_cli._db_conn.pipeline()
        for domain_name in domain_list:
            pipe.hget(f"{topo}_{domain_name}", "NEtype")
        
        domain_NEtype = pipe.execute()

        for i, domain_NEtypes in enumerate(domain_NEtype):
            if domain_NEtypes: # else pass
                domain_NEtype[i] = domain_NEtypes.strip("\"")

    return domain_NEtype

def node_list_divide(user, topo, node_list):
    '''
    根据节点列表，判断属于容器还是虚机，随后据划分
    '''
    docker_list = []
    vm_list = []
    domain_NEservice_list = get_domain_NEservice(user, topo, node_list)
    for i in range(len(node_list)):
        if domain_NEservice_list[i] == 'kvm':
            vm_list.append(node_list[i])
        elif domain_NEservice_list[i] == 'docker':
            docker_list.append(node_list[i])
        else:
            # 报错
            raise Exception(
                "There is a domain not belonging to kvm or docker.")
    return docker_list, vm_list

def get_user2db():
    '''
    获取user2DB表的内容

    Returns:
        user2db = {
            "user_name": "DB<N>",
            ...
        }
    '''
    user2db = {}
    with Db0Context() as db0_cli:
        user2db = db0_cli.get_hash_table("user2DB")
    
    return user2db