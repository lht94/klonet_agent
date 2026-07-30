class Ne_re_base:
    # redis数据库节点表的key -> json中节点信息的key
    ne_property = {'NEimage': 'image_name', 'NEtype': 'type', 'NEsubtype': 'subtype', 'NEx': 'x', 'NEy': 'y',
                   'name': 'name', 'NEresource': 'resource_limit', 'NElinestyle':'linestyle', 'NEservice': 'service'}

    def __init__(self, ne_name, ne_table):
        self.__dict__.update(ne_table)
        self.name = ne_name
        self.table = {}
        """
            self.table中的内容对应json中节点的key-value
            "h1" : {
                        'name':
                        'image_name':
                        'type':
                        'subtype':
                        'x':
                        'y':
                        'interfaces':
                        'gateway':
                        'service':
                    }
        """

    def __call__(self):
        table_info = self.table.setdefault(self.name, {})
        for k, v in Ne_re_base.ne_property.items():
            table_info.setdefault(v, getattr(self, k))
        for k, v in getattr(self, 'NEconfig', {}).items():
            setattr(self, k, v)

    def update_negateway(self, gateway):
        table_info = self.table.get(self.name)
        table_info['gateway'] = gateway

    def update_interfaces(self):
        ifa_property = {'name': 'name', 'ip': 'ip',
                        'mask': 'netmask'}
        table_info = self.table.get(self.name)
        interfaces = []
        for k, v in self.__dict__.items():
            if k.startswith('link'):
                ifa = {}
                for kk, vv in ifa_property.items():
                    ifa.setdefault(vv, v.get(kk, ''))
                interfaces.append(ifa)
        table_info['interfaces'] = interfaces

    def update_config(self, config=None):
        table_info = self.table.get(self.name)
        if config:
            table_info['config'] = config
        else:
            table_info['config'] = getattr(self, 'config')


class Ne_re_host(Ne_re_base):
    def __call__(self):
        super().__call__()
        self.update_negateway(getattr(self, 'NEgateway', ''))
        self.update_interfaces()


class Ne_re_switch(Ne_re_base):
    def __call__(self):
        super().__call__()
        self.update_config()


class Ne_re_router(Ne_re_base):
    def __call__(self):
        super().__call__()
        self.update_negateway(getattr(self, 'NEgateway', ''))
        self.update_interfaces()
        self.update_config()


class Ne_re_controller(Ne_re_base):
    def __call__(self):
        super().__call__()
        self.update_config()

class Ne_re_dpdk(Ne_re_base):
    def __call__(self):
        super().__call__()
        self.update_config()


class Link_re_base:
    # redis数据库链路表的key -> json中链路信息的key
    link_property = {'name': 'name',
                     'sourceNE': 'source', 'targetNE': 'target',
                     'sourceType': 'sourceType', 'targetType': 'targetType',
                     'sourceIP': 'sourceIP', 'targetIP': 'targetIP',
                     'tcConfig':'config'
                     }

    def __init__(self, link_name, link_table):
        self.__dict__.update(link_table)
        self.name = link_name
        self.table = {}
        """
        self.table中的内容对应json中链路的key-value
            "l1" : {
                        'name':
                        'source':
                        'sourceIP':
                        'sourceType':
                        'target':
                        'targetIP':
                        'targetType':
                    }
        """

    def __call__(self):
        table_info = self.table.setdefault(self.name, {})
        try:
            for k, v in Link_re_base.link_property.items():
                table_info.setdefault(v, getattr(self, k))
        except AttributeError:
            pass


class Topo_aggregate:
    def __init__(self, **kwargs):
        """
        成员变量：
            (节点名、链路名) -> 节点（链路）表信息
            hosts -> []
            switches -> []
            routers -> []
            controllers -> []
        :param kwargs:
        """
        self.__dict__.update(kwargs)
        self.network = {}

    def __call__(self):
        # 节点分类索引 -> 节点对应的处理方法
        ne_type = {'hosts': '_hosts_aggregate', 'switches': '_switches_aggregate',
                   'routers': '_routers_aggregate', 'controllers': '_controllers_aggregate',
                   'dpdks': '_dpdks_aggregate'}
        for k, v in ne_type.items():
            func = getattr(self, v)
            func(k)
        self._links_aggregate()

    def _hosts_aggregate(self, ne_type_index):
        hosts = getattr(self, ne_type_index, [])
        info = self.network.setdefault(ne_type_index, {})
        for k in hosts:
            ne_re_ob = Ne_re_host(k, getattr(self, k))
            ne_re_ob()
            info.update(ne_re_ob.table)

    def _switches_aggregate(self, ne_type_index):
        switches = getattr(self, ne_type_index, [])
        info = self.network.setdefault(ne_type_index, {})
        for k in switches:
            ne_re_ob = Ne_re_switch(k, getattr(self, k))
            ne_re_ob()
            info.update(ne_re_ob.table)

    def _controllers_aggregate(self, ne_type_index):
        controllers = getattr(self, ne_type_index, [])
        info = self.network.setdefault(ne_type_index, {})
        for k in controllers:
            ne_re_ob = Ne_re_controller(k, getattr(self, k))
            ne_re_ob()
            info.update(ne_re_ob.table)

    def _routers_aggregate(self, ne_type_index):
        routers = getattr(self, ne_type_index, [])
        info = self.network.setdefault(ne_type_index, {})
        for k in routers:
            ne_re_ob = Ne_re_router(k, getattr(self, k))
            ne_re_ob()
            info.update(ne_re_ob.table)

    def _dpdks_aggregate(self, ne_type_index):
        dpdks = getattr(self, ne_type_index, [])
        info = self.network.setdefault(ne_type_index, {})
        for k in dpdks:
            ne_re_ob = Ne_re_dpdk(k, getattr(self, k))
            ne_re_ob()
            info.update(ne_re_ob.table)

    def _links_aggregate(self):
        links = getattr(self, 'links', [])
        info = self.network.setdefault('links', {})
        for k in links:
            link_re_ob = Link_re_base(k, getattr(self, k))
            link_re_ob()
            info.update(link_re_ob.table)
