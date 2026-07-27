from ..Implement_layer.ContainerManager import run_shell
from ..tools.context import redis_context

class NeCheckWorker:
    '''
    子拓扑的节点健康状态检查

    Attributes:
        user: 用户名
        topo: 拓扑名
        subtopo: 子拓扑名
        ne2id: 前端节点名到uuid的映射关系
    '''
    def __init__(self, user, topo, subtopo):
        self.user = user
        self.topo = topo
        self.subtopo = subtopo
        self.ne2id = {}
    
    def check(self):
        '''
        检查节点是否启动着

        Returns:
            error_nes: list, 在数据库中但不在宿主机中运行的容器
        '''
        # 未运行节点统计
        error_nes = []
        # 获取子拓扑下的所有节点，更新ne2id字典
        self._get_subtopo_ne()
        # 统计所有运行着的docker容器节点
        all_ne = run_shell("docker ps").decode()
        # 统计子拓扑中未运行着的docker容器节点，统计为未运行节点
        for ne_name, ne_id in self.ne2id.items():
            if ne_id not in all_ne:
                error_nes.append(ne_name)
        # 返回未运行节点
        return error_nes

    def _get_subtopo_ne(self):
        '''
        获取子拓扑下的所有节点
        '''
        with redis_context(self.user) as user_db_cli:
            # 获取子拓扑中所有节点
            ne_list = user_db_cli.get_value("plane_subtopo_list",
                                            self.subtopo)['NEs']
            # 对每个节点
            for ne in ne_list:
                # 读取节点的uuid，并记录在字典中
                table_name = f"{self.topo}_{ne}"
                ne_id = user_db_cli.get_value(table_name, "NEid")
                self.ne2id[ne] = ne_id

