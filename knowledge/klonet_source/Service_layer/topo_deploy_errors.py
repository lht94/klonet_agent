class NEContainerCreateError(RuntimeError):
    '''节点创建出错时，抛出该错误'''

class LinkCreateError(RuntimeError):
    '''链路创建出错时，抛出该错误'''

class VXLinkCreateError(RuntimeError):
    '''vxlan link 创建出错时，抛出该错误'''

class TrafficGenError(RuntimeError):
    '''流发生器创建出错时，抛出该错误'''

class PackageGenError(RuntimeError):
    '''流发生器创建出错时，抛出该错误'''

class PcapDeployError(RuntimeError):
    '''创建Pcap程序出错时， 抛出该异常'''

class OVSStartError(RuntimeError):
    '''ovs交换机启动命令非正常执行的时候，抛出该异常'''

class RedisTrafficError(RuntimeError):
    '''涉及流量与Redis交互出错时，抛出该异常'''
