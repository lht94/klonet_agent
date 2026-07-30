

class TrafficServerError(ValueError):
    """流量发生器server端启动进程出现错误，引发该异常"""


class TrafficClientError(ValueError):
    """流量发生器client端写入文件或启动进程出现错误，引发该异常"""


class TrafficStopError(ValueError):
    """停止流量发送器或pkt_gen2应用时出现错误，引发该异常"""

class Pktgen1ClientError(ValueError):
    """pktgen1的client端启动进程出现错误，引发该异常"""

class Pktgen2ClientError(ValueError):
    """网络汇聚包流量(pktgen2)的client端写入文件或启动进程出现错误，引发该异常"""

class LinkConfigError(RuntimeError):
    '''设置链路tc失败时， 引发该异常'''

class LinkInterfaceDeleteError(RuntimeError):
    '''删除链路配置失败时， 引发该异常'''


class ExprMonitorWorkerError(RuntimeError):
    '''ExprMonitorWorker出现错误时， 引发该异常'''


class PlatformDeployError(RuntimeError):
    '''创建平台监控，检测到监控容器未启动时，引发该异常'''

class ErrorInfo():
    '''异常信息数据结构'''
    def __init__(self, err_code, err_msg, err_module, err_detail = '') -> None:
        self.err_code = err_code
        self.err_msg = err_msg
        self.err_module = err_module
        self.err_detail = err_detail
    def __str__(self):
        return f'\n error_code:{self.err_code} \
                \n error_msg:{self.err_msg} \
                \n error_module:{self.err_module} \
                \n error_detail:{self.err_detail}'

class ExampleError(Exception):
    '''示例异常，无实用'''
    def __init__(self, error_msg) -> None: # 按需添加所需参数
        super().__init__()
        # 查阅错误代码对照表，可按需逐序添加
        self.err_code = '99999'
        # 可以以参数的方式传入
        self.err_msg = error_msg
        self.err_module = 'example'
        self.err_detail = ''
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
        # 表示该错误的具体原因，推荐解决方案，开发人员根据详细的错误返回码作出正确的反应
    def __str__(self):
        return str(self.err_info)

class UnkonwnError(Exception):
    '''未知异常'''
    def __init__(self) -> None:
        super().__init__()
        self.err_code = '00000'
        self.err_msg = 'Unknown Error'
        self.err_module = 'Unknown'
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class VemuSystemError(Exception):
    '''系统异常'''
    def __init__(self) -> None:
        super().__init__()
        self.err_code = '10001'
        self.err_msg = f"System error"
        self.err_module = 'System'
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class ServiceUnavailableError(Exception):
    '''服务暂停异常'''
    def __init__(self) -> None:
        super().__init__()
        self.err_code = '10002'
        self.err_msg = f"Service unavailable"
        self.err_module = 'System'
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class RemoteServiceError(Exception):
    '''远程服务异常'''
    def __init__(self) -> None:
        super().__init__()
        self.err_code = '10003'
        self.err_msg = f"Remote service error"
        self.err_module = 'System'
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class ServiceExpiredError(Exception):
    '''服务超时异常'''
    def __init__(self) -> None:
        super().__init__()
        self.err_code = '10004'
        self.err_msg = f"Service expired"
        self.err_module = 'System'
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class SystemBusyError(Exception):
    '''系统繁忙异常'''
    def __init__(self) -> None:
        super().__init__()
        self.err_code = '10005'
        self.err_msg = f"Too many pending tasks, system is busy"
        self.err_module = 'System'
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class IllegalRequestError(Exception):
    '''非法请求异常'''
    def __init__(self) -> None:
        super().__init__()
        self.err_code = '10006'
        self.err_msg = f"Illegal request"
        self.err_module = 'System'
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class IllegalUserError(Exception):
    '''非法用户异常'''
    def __init__(self) -> None:
        super().__init__()
        self.err_code = '10007'
        self.err_msg = f"Invalid user"
        self.err_module = 'System'
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class UserRequestTimesOutOfLimitError(Exception):
    '''用户请求次数超过限制异常'''
    def __init__(self) -> None:
        super().__init__()
        self.err_code = '10008'
        self.err_msg = f"User requests out of rate limit"
        self.err_module = 'System'
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class IPRequestTimesOutOfLimitError(Exception):
    '''ip请求超过次数限制异常'''
    def __init__(self) -> None:
        super().__init__()
        self.err_code = '10009'
        self.err_msg = f"IP requests out of rate limit"
        self.err_module = 'System'
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class RequestBodyLengthOutOfLimitError(Exception):
    '''请求体长度超过限制异常'''
    def __init__(self) -> None:
        super().__init__()
        self.err_code = '10010'
        self.err_msg = f"Request body length over limit"
        self.err_module = 'System'
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class InsufficientAppPermissionsError(Exception):
    '''应用接口访问权限异常'''
    def __init__(self) -> None:
        super().__init__()
        self.err_code = '10011'
        self.err_msg = f"Insufficient app permissions"
        self.err_module = 'System'
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class ParaMissedError(Exception):
    '''参数缺失异常'''
    def __init__(self, para, module) -> None:
        super().__init__()
        self.err_code = '10012'
        self.err_msg = f'Miss required parameter {para} , see doc for more info'
        self.err_module = module
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class ParaValueInvalidError(Exception):
    '''参数值非法异常'''
    def __init__(self, para, module, exp_para, get_para) -> None:
        super().__init__()
        self.err_code = '10013'
        self.err_msg = f"Parameter {para}'s value invalid, expect {exp_para} , \
                but get {get_para} , see doc for more info"
        self.err_module = module
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)

class ParaError(Exception):
    '''参数异常'''
    def __init__(self, module, msg) -> None:
        super().__init__()
        self.err_code = '10014'
        self.err_msg = f"Param error, {msg}"
        self.err_module = module
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)   

class RedisError(Exception):
    '''Redis数据库异常'''
    def __init__(self, module) -> None:
        super().__init__()
        self.err_code = '10015'
        self.err_msg = f"Redis error"
        self.err_module = module
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)   

class MysqlError(Exception):
    '''Mysql数据库异常'''
    def __init__(self, module) -> None:
        super().__init__()
        self.err_code = '10016'
        self.err_msg = f"Mysql error"
        self.err_module = module
        self.err_detail = ' '
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)   

class NodeIpv4UrpfConfigPathError(Exception):
    '''节点ipv4 uRPF配置异常'''
    def __init__(self, path) -> None:
        super().__init__()
        self.err_code = '20201'
        self.err_msg = f"This ipv4 uRPF config path {path} does not exist"
        self.err_module = 'Node'
        self.err_detail = 'This type of configuration is applicable to containers based on Ubuntu images.The configuration path of other systems may vary depending on the Linux system version of the container'
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)   

class LinkOvsBridgePortDeleteError(Exception):
    '''链路ovs网桥端口删除异常'''
    def __init__(self, ne_id, bridge_name, port_name) -> None:
        super().__init__()
        self.err_code = '20201'
        self.err_msg = f"Ovs{ne_id} {bridge_name} {port_name} deletion failed"
        self.err_module = 'Link'
        self.err_detail = 'When the node or link is dynamically deleted, the port deletion corresponding to the ovs bridge fails, which may be due to an error caused by the change of the rules for bridge naming and port naming. Query the field information such as port and nic in the database and enter the ovs container to query the bridge and port names through the ovs-vsctl show to check whether the information is consistent'
        self.err_info = ErrorInfo(self.err_code, self.err_msg, self.err_module, self.err_detail)
    def __str__(self):
        return str(self.err_info)   