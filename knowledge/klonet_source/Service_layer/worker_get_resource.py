import psutil
from vemu_uestc.tools.tools import get_host_ip

class WorkerResource:
    '''
    获取宿主机的CPU、MEM使用情况

    Attributes:
        ip: 宿主机的IP
        resource_dict: dict, 资源汇总信息的字典，包括CPU和MEM
    '''
    def __init__(self):
        
        self.ip = get_host_ip()
        self.resource_dict = {}
        self.resource_dict['worker_ip'] = self.ip
    
    def get_resource(self):
        '''
        返回剩余资源情况
        样例如下:
        {
            'worker_ip': 'yy',
            'cpu_time': {     // 每个cpu空余时间
                'time_sum': 'xx',
                'each_cpu': {
                    '0': 'xx',    
                    '1': 'xx',
                    ...
                }
            },
            'cpu_core': 'xx', // cpu个数
            'mem': 'xx'       // 剩余内存
        }
        '''
        self._get_cpu_time()
        self._get_mem()
        self._get_cpu_core()
        return self.resource_dict
    
    def _get_cpu_time(self):
        '''
        获取worker每个cpu的剩余量
        '''
        cpu = psutil.cpu_percent(interval=1, percpu=True)
        cpu_dict = self.resource_dict.setdefault('cpu_time', {})
        time_sum = 0
        cpu_dict['each_cpu'] = {}
        for i in range(len(cpu)):
            remain_cpu = 100 - cpu[i]
            cpu_dict['each_cpu'][i] = remain_cpu
            time_sum += remain_cpu
        cpu_dict['time_sum'] = time_sum
    
    def _get_cpu_core(self):
        '''
        获取worker核心数
        '''

        self.resource_dict['cpu_core'] = psutil.cpu_count()
    
    def _get_mem(self):
        '''
        获取worker剩余内存
        '''
        mem = (psutil.virtual_memory().total - 
               psutil.virtual_memory().used) / (2 ** 20)
        self.resource_dict['mem'] = mem
    
