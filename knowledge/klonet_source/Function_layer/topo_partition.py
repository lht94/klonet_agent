import pymetis as pm
import random as rd
from ..vemu_config.config import PROJ_CONFIG

from yaml import load

class topo_adapting_partition:


    def __init__(self, topo_json: dict, worker_list: list):

        self.topo = topo_json.get('networks', ' ')
        self.map_between_vertiveInadjlist_and_neIntopo = {}
        self.map_between_neIntopo_and_vertiveInadjlist = {}
        self.adjcntlist = []
        self.sub_topos = {}
       
        ###
        self.workers = worker_list    # [('ip', core_num*100*rate), (), ()]

    
    def __call__(self):
        self._neIntopo_to_verticeInadjlist()
        self._transform_topo_into_adjcntlist()
        # self.vertice_num = len(self.map_between_vertiveInadjlist_and_neIntopo)
        # self.unvisited_list = [n for n in range(0, self.vertice_num)]    # 用于标记访问过的节点，初始化为vertice
        # print("111",self.unvisited_list)


    def _neIntopo_to_verticeInadjlist_pertype(self, netype: dict, typename: str, vertice=0):

        for k in netype.keys():
            if netype[k]["service"] == "docker":
                self.map_between_vertiveInadjlist_and_neIntopo[str(vertice)] = (k, 
                    typename, netype[k]['resource_limit']['cpu'])
            elif netype[k]["service"] == "kvm":
                self.map_between_vertiveInadjlist_and_neIntopo[str(vertice)] = (k, 
                    typename, str(int(netype[k]['resource_limit']['cpu']) * PROJ_CONFIG.ratio))   # 对虚机的cpu核心数转换为运行时间，同时转换格式
            elif netype[k]["service"] == "hardware":
                self.map_between_vertiveInadjlist_and_neIntopo[str(vertice)] = (k, 
                    typename, "0")    # 为了保证切分逻辑完整性，暂时写死为0
            self.map_between_neIntopo_and_vertiveInadjlist[k] = str(vertice)
            vertice = vertice + 1

        return vertice

    def get_neighbor_from_link(self):
        '''
        通过json文件中的links获取到连接关系
        '''
        neighbors_info = {}
        links = self.topo['links']
        for link in links:
            if links[link]['source'] in neighbors_info:
                neighbors_info[links[link]['source']].append(links[link]['target'])
            else:
                neighbors_info[links[link]['source']] = [links[link]['target']]
            if links[link]['target'] in neighbors_info:
                neighbors_info[links[link]['target']].append(links[link]['source'])
            else:
                neighbors_info[links[link]['target']] = [links[link]['source']]
        return neighbors_info

    def _find_neighbor_of_vertice(self, vertice):
        neighbor_list = []
        name_of_ne = self.map_between_vertiveInadjlist_and_neIntopo[vertice][0]
        type_of_ne = self.map_between_vertiveInadjlist_and_neIntopo[vertice][1]
        if type_of_ne == 'controllers':
            return neighbor_list
        else:
            neighbor_info = self.get_neighbor_from_link()
            # print(neighbor_info)
            if name_of_ne in neighbor_info:
                neighbors_of_this_vertice = neighbor_info[name_of_ne]
                for neighbor in neighbors_of_this_vertice:
                    neighbor_list.append(self.map_between_neIntopo_and_vertiveInadjlist[neighbor])
                neighbor_list.sort()
            else:
                return neighbor_list
        

        # if type_of_ne == 'controllers':
        #     return neighbor_list
        # else:
        #     listofneighbor = self.topo[type_of_ne][name_of_ne]['interfaces']
        #     for interface in listofneighbor:
        #         interface_name = interface['name']
        #         n = len(name_of_ne)
        #         neighbor_ne = interface_name[n:]
        #         for k, v in self.map_between_vertiveInadjlist_and_neIntopo.items():
        #             if v[0] == neighbor_ne:
        #                 neighbor_vertice = k
        #             else:
        #                 continue
        #         neighbor_list.append(neighbor_vertice)
        #         neighbor_list.sort()

        
        # print(self.map_between_vertiveInadjlist_and_neIntopo)
        # print(self.map_between_neIntopo_and_vertiveInadjlist)
        # print("", name_of_ne,",,,",neighbor_list)
        # print(self.get_neighbor_from_link())
        # print("*"*19)
        return neighbor_list


    def _get_random_unvisited_vertice(self):

        if len(self.unvisited_list) != 0:
            position = rd.randint(0, len(self.unvisited_list) - 1)
            # print(len(self.unvisited_list), position, self.unvisited_list)
            vertice_start = self.unvisited_list[position]

            return vertice_start
        else:

            return None




    def _neIntopo_to_verticeInadjlist(self):

        vertice = 0
        controllers = self.topo.get('controllers')
        routers = self.topo.get('routers')
        switches = self.topo.get('switches')
        hosts = self.topo.get('hosts')
        dpdks = self.topo.get('dpdks')

        if controllers is not None:
            vertice1 = self._neIntopo_to_verticeInadjlist_pertype(controllers, 'controllers', vertice)
        else:
            print('No elements in controllers!!!')
        if routers is not None:
            vertice2 = self._neIntopo_to_verticeInadjlist_pertype(routers, 'routers', vertice1)
        else:
            print('No elements in routers')
        if switches is not None:
            vertice3 = self._neIntopo_to_verticeInadjlist_pertype(switches, 'switches', vertice2)
        else:
            print('No elements in switches')
        if hosts is not None:
            vertice4 = self._neIntopo_to_verticeInadjlist_pertype(hosts, 'hosts', vertice3)
        else:
            print('No elements in hosts')
        if dpdks is not None:
            self._neIntopo_to_verticeInadjlist_pertype(dpdks, 'dpdks', vertice4)
        else:
            print('No elements in dpdks')


    def _transform_topo_into_adjcntlist(self):

        neighbor_list = []
        for k in self.map_between_vertiveInadjlist_and_neIntopo:
            neighbor_list = self._find_neighbor_of_vertice(k)
            # print(neighbor_list)
            self.adjcntlist.append(neighbor_list)


    def _fill_one_worker(self, Capacity, Candidate):

        C = Capacity
        cand = Candidate
        Pk = []
        load_pk = 0

        while (load_pk + int(self.map_between_vertiveInadjlist_and_neIntopo[str(cand)][2])) < C:
            Pk.append(cand)
            self.unvisited_list.remove(cand)    # 候选点已经被访问
            load_pk = load_pk + int(self.map_between_vertiveInadjlist_and_neIntopo[str(cand)][2])
            if self.adjcntlist[cand] == []:    # 没有邻居
                cand = self._get_random_unvisited_vertice()
                if cand == None:
                    return 1
            else:    # 有邻居，把邻居全部访问
                neighbors = self.adjcntlist[cand]
                for vj in neighbors:
                    if self.unvisited_list.count(int(vj)):
                        if (load_pk + int(self.map_between_vertiveInadjlist_and_neIntopo[str(vj)][2])) < C:
                            Pk.append(vj)
                            self.unvisited_list.remove(int(vj))
                            load_pk = load_pk + int(self.map_between_vertiveInadjlist_and_neIntopo[str(vj)][2])
                        else:
                            return 1
                    else:
                        continue
                cand = self._get_random_unvisited_vertice()
                if cand == None:
                    return 1

        return 1




    def _forecast_partition_scale(self):


        k = 0    # 划分子图数目k
        cand = self._get_random_unvisited_vertice()    # 从拓扑中选择一个未被标记的节点
        k_max = len(self.workers)
        # print("workers:.....",self.workers)
        self.workers.sort(key=lambda tup: tup[1], reverse = True)
        while self.unvisited_list != []:
            
            if (k + 1) <= k_max:
                k = k + 1
                self._fill_one_worker(self.workers[k-1][1], cand)
                cand = self._get_random_unvisited_vertice()
            else:
                return 0
            # print("198:", self.unvisited_list)
            # print("k:",k)
        return k


    # def topo_partition(self):

    #     k = self._forecast_partition_scale()
    #     list_int = []
    #     for val in self.adjcntlist:
    #         sub_list_int = []
    #         for v in val:
    #             sub_list_int.append(int(v))
    #         list_int.append(sub_list_int)
    #     try:
    #         edgecuts, parts = pm.part_graph(nparts = k,adjacency = list_int)
    #         lists_of_Ne_and_weights = []
    #         for i in range(0, k):
    #             Nelist = []
    #             weights = 0
    #             for vertice, part in enumerate(parts):
    #                 if part == i:
    #                     Nelist.append(self.map_between_vertiveInadjlist_and_neIntopo[str(vertice)][0])
    #                     weights = weights + int(self.map_between_vertiveInadjlist_and_neIntopo[str(vertice)][2])
    #             lists_of_Ne_and_weights.append((Nelist, weights))
    #         lists_of_Ne_and_weights.sort(key = lambda tup: tup[1], reverse = True)
    #         for p, subtopo in enumerate(lists_of_Ne_and_weights):
    #             self.sub_topos.update({self.workers[p][0]: subtopo[0]})
    #     except RuntimeError:
    #         lists_of_Ne_and_weights = [0]

    #     return lists_of_Ne_and_weights


    def _forecast_partition_scale_new(self):

        topo_requirement = 0
        worker_supplement = 0
        k = 0

        for v in self.map_between_vertiveInadjlist_and_neIntopo.values():
            topo_requirement = topo_requirement + int(v[2])

        for v in self.workers:
            worker_supplement = worker_supplement + v[1]

        if topo_requirement >= worker_supplement:
            print("现有资源无法满足拓扑创建需求！！！")
            return 0
        else:
            self.workers.sort(key=lambda tup: tup[1], reverse = True)
            for v in self.workers:
                topo_requirement = topo_requirement - v[1]
                if topo_requirement <= 0:
                    return k + 1
                else:
                    k = k + 1

        

    def topo_partition_new(self):
        
        k = self._forecast_partition_scale_new()
        # print("aaa is {}".format(k))
        list_int = []
        for val in self.adjcntlist:
            sub_list_int = []
            for v in val:
                sub_list_int.append(int(v))
            list_int.append(sub_list_int)
        # print("list_int:", list_int)
        try:
            edgecuts, parts = pm.part_graph(nparts = k,adjacency = list_int)
            lists_of_Ne_and_weights = []
            for i in range(0, k):
                Nelist = []
                weights = 0
                for vertice, part in enumerate(parts):
                    if part == i:
                        Nelist.append(self.map_between_vertiveInadjlist_and_neIntopo[str(vertice)][0])
                        weights = weights + int(self.map_between_vertiveInadjlist_and_neIntopo[str(vertice)][2])
                lists_of_Ne_and_weights.append((Nelist, weights))
            lists_of_Ne_and_weights.sort(key = lambda tup: tup[1], reverse = True)
            for p, subtopo in enumerate(lists_of_Ne_and_weights):
                self.sub_topos.update({self.workers[p][0]: subtopo[0]})
        except RuntimeError:
            lists_of_Ne_and_weights = [0]

        return lists_of_Ne_and_weights



