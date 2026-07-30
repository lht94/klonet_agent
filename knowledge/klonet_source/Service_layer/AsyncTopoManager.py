from threading import Thread
from queue import Queue
import queue

from .NEManager import VethLink, VxLANLink, DefaultNEDeleter
from ..Implement_layer.LinkManager import delete_vxlan
from ..Function_layer.deploy_process_bar import ProcessBarDelete, ProcessBarDeploy
from ..tools.log_tools import FLASK_LOGGER

class ThreadTasks(object):
    """
    线程基类
    """
    def __init__(self, tasks: list):
        """
        Args:
            tasks (list): 基于线程分派的任务列表
        """
        self.queue = Queue()
        tasks_num = len(tasks)
        worker_num = tasks_num // 2
        # 根据任务数量确定worker线程数量,  1 <= worker_num <= 20
        if worker_num >= 20:
            self.worker_size = 20
        elif worker_num == 0 and tasks_num != 0:
            self.worker_size = tasks_num
        else:
            self.worker_size = worker_num


class LinkCreateTasks(ThreadTasks):
    """
    创建链路的线程任务管理父类
    """
    def wait_task_done(self, finished_step, user_db_cli, topo):
        """
        创建并等待任务完成
        """
        tasks_num = self.queue.qsize()
        threads = []

        for _ in range(self.worker_size):
            t = LinkCreatorThread(self.queue, finished_step, tasks_num, user_db_cli, topo)
            t.start()
            threads.append(t)
        
        for thread in threads:
            thread.join()
        self.queue.join()

        FLASK_LOGGER.debug('==> link create tasks done')
        return {'code': 1, 'msg': 'link create success'}


class VethCreateTasks(LinkCreateTasks):
    """
    创建veth-pair的链路线程任务管理类
    """
    def __init__(self, links, topo=None, re_cli=None):
        """
        links (list):    链路数目
        topo (str):      拓扑名称
        re_cli (UserDB): Redis数据库连接
        """
        super().__init__(links)
        for link in links:
            self.queue.put_nowait(VethLink(topo, link, re_cli))


class VxLANCreateTasks(LinkCreateTasks):
    """
    创建vxlan的链路线程任务管理类
    """
    def __init__(self, links: list, topo: str, re_cli):
        """
        links (list):    链路数目
        topo (str):      拓扑名称
        re_cli (UserDB): Redis数据库连接
        """
        super().__init__(links)
        for link in links:
            self.queue.put_nowait(VxLANLink(topo, link, re_cli))


class NeCreateTasks(ThreadTasks):
    """
    创建节点容器的线程任务管理类
    """
    def __init__(self, tasks: list):
        """
        tasks (list):    节点创建任务列表
        """
        super().__init__(tasks)
        for task in tasks:
            self.queue.put_nowait(task)

    def wait_task_done(self, finished_step, user_db_cli, topo):
        """
        阻塞等待所有的创建任务成功返回
        """
        tasks_num = self.queue.qsize()
        threads = []

        for _ in range(self.worker_size):
            t = NeCreateThread(self.queue, finished_step, tasks_num, user_db_cli, topo)
            t.start()
            threads.append(t)

        for thread in threads:
            thread.join()
        self.queue.join()
        FLASK_LOGGER.debug('==> ne create tasks done')
        return {'code': 1, 'msg': 'ne create success'}


class NeDelTasks(ThreadTasks):
    """
    删除节点容器的线程任务管理类
    """

    def __init__(self, tasks):
        """
        tasks (list):   节点创建任务列表
        """
        super().__init__(tasks)
        for task in tasks:
            self.queue.put_nowait(DefaultNEDeleter(task))

    def wait_task_done(self, finished_step, user_db_cli, topo):
        """
        阻塞等待所有的节点删除任务成功返回
        """
        tasks_num = self.queue.qsize()
        threads = []

        for _ in range(self.worker_size):
            t = NeDelThread(self.queue, finished_step, tasks_num, user_db_cli, topo)
            t.start()
            threads.append(t)
        
        for thread in threads:
            thread.join()
        self.queue.join()

        FLASK_LOGGER.debug('==> ne delete tasks done')
        return {'code': 1, 'msg': 'ne del success'}


class VxlanDelTasks(ThreadTasks):
    """
    删除vxlan的线程任务管理类
    """

    def __init__(self, tasks):
        """
        tasks (list):   节点创建任务列表
        """
        super().__init__(tasks)
        for task in tasks:
            self.queue.put_nowait(task)

    def wait_task_done(self, finished_step, user_db_cli, topo):
        """
        阻塞等待所有的vxlan删除任务成功返回
        """
        tasks_num = self.queue.qsize()
        threads = []

        for _ in range(self.worker_size):
            t = VxlanDelThread(self.queue, finished_step, tasks_num, user_db_cli, topo)
            t.start()
            threads.append(t)
        
        for thread in threads:
            thread.join()
        self.queue.join()

        FLASK_LOGGER.debug('==> vxlan del tasks done')
        return {'code': 1, 'msg': 'vxlan del success'}


class VxlanDelThread(Thread):
    """
    删除vxlan的线程任务类
    """
    def __init__(self, task_queue, finished_step, tasks_num, user_db_cli, topo):
        """
        task_queue (list): 线程任务队列
        """
        self.exc = None
        self.queue = task_queue
        self.finished_step = finished_step
        self.user_db_cli = user_db_cli
        self.tasks_num = tasks_num
        self.topo = topo
        super().__init__()

    def run(self):
        try:
            while True:
                try:
                    ovs_target = self.queue.get(block=False)
                    delete_vxlan(ovs_target)
                    # 进度条值更新
                    ProcessBarDelete(self.finished_step + (1-self.queue.qsize()/self.tasks_num), self.user_db_cli, self.topo)

                except queue.Empty:
                    return
                self.queue.task_done()
        except:
            import sys
            # Save details of the exception thrown but don't rethrow,
            # just complete the function
            self.exc = sys.exc_info()

    def join(self):
        Thread.join(self)
        if self.exc:
            msg = "Thread '%s' threw an exception: %s" % (self.getName(), self.exc[1])
            new_exc = Exception(msg)
            raise new_exc.with_traceback(self.exc[2])


class NeDelThread(Thread):
    """
    删除节点的线程任务类
    """
    def __init__(self, task_queue, finished_step, tasks_num, user_db_cli, topo):
        """
        task_queue (list): 线程任务队列
        """
        self.exc = None
        self.queue = task_queue
        self.finished_step = finished_step
        self.user_db_cli = user_db_cli
        self.tasks_num = tasks_num
        self.topo = topo
        super().__init__()

    def run(self):
        try:
            while True:
                try:
                    deleter = self.queue.get(block=False)
                    deleter.stop_and_delete()
                    # 进度条值更新
                    ProcessBarDelete(self.finished_step + (1-self.queue.qsize()/self.tasks_num), self.user_db_cli, self.topo)

                except queue.Empty:
                    return
                self.queue.task_done()
        except:
            import sys
            # Save details of the exception thrown but don't rethrow,
            # just complete the function
            self.exc = sys.exc_info()

    def join(self):
        Thread.join(self)
        if self.exc:
            msg = "Thread '%s' threw an exception: %s" % (self.getName(), self.exc[1])
            new_exc = Exception(msg)
            raise new_exc.with_traceback(self.exc[2])


class LinkCreatorThread(Thread):
    """
    链路创建的线程任务类
    """
    def __init__(self, task_queue, finished_step, tasks_num, user_db_cli, topo):
        """
        task_queue (list): 线程任务队列
        """
        self.exc = None
        self.queue = task_queue
        self.finished_step = finished_step
        self.user_db_cli = user_db_cli
        self.tasks_num = tasks_num
        self.topo = topo
        super().__init__()

    def run(self):
        try:
            while True:
                try:
                    creator = self.queue.get(block=False)
                    result = creator.create_link()
                    FLASK_LOGGER.debug(result)
                    creator.write_info(result)
                    # 进度条值更新
                    ProcessBarDeploy(self.finished_step + (1-self.queue.qsize()/self.tasks_num), self.user_db_cli, self.topo)

                except queue.Empty:
                    return
                self.queue.task_done()
        except:
            import sys
            # Save details of the exception thrown but don't rethrow,
            # just complete the function
            self.exc = sys.exc_info()

    def join(self):
        Thread.join(self)
        if self.exc:
            msg = "Thread '%s' threw an exception: %s" % (self.getName(), self.exc[1])
            new_exc = Exception(msg)
            raise new_exc.with_traceback(self.exc[2])


class NeCreateThread(Thread):
    """
    节点创建的线程任务类
    """
    def __init__(self, task_queue, finished_step, tasks_num, user_db_cli, topo):
        """
        task_queue (list): 线程任务队列
        """
        self.exc = None
        self.queue = task_queue
        self.finished_step = finished_step
        self.user_db_cli = user_db_cli
        self.tasks_num = tasks_num
        self.topo = topo
        super().__init__()

    def run(self):
        try:
            while True:
                try:
                    create_func, args = self.queue.get(block=False)
                    create_func(*args)
                    # 进度条值更新
                    ProcessBarDeploy(self.finished_step + (1-self.queue.qsize()/self.tasks_num), self.user_db_cli, self.topo)

                except queue.Empty:
                    return
                self.queue.task_done()

        except:
            import sys
            # Save details of the exception thrown but don't rethrow,
            # just complete the function
            self.exc = sys.exc_info()

    def join(self):
        Thread.join(self)
        if self.exc:
            msg = "Thread '%s' threw an exception: %s" % (self.getName(), self.exc[1])
            new_exc = Exception(msg)
            raise new_exc.with_traceback(self.exc[2])
        
        
class KVMImageSyncTasks(ThreadTasks):
    """
    同步用户自上传镜像的任务管理类
    
    暂时弃用：不知道为什么子线程run执行完后但依然不退出，导致线程的join一直阻塞住
    """
    def __init__(self, tasks: list):
        """
        tasks: (list), # 镜像同步列表
        """
        super().__init__(tasks)
        for task in tasks:
            self.queue.put_nowait(task)
    
    def wait_task_done(self):
        """
        阻塞等待所有镜像同步任务都成功返回
        """
        threads = []
        # 创建self.worker_size个线程（消费者）
        for _ in range(self.worker_size):
            t = KVMImageSyncThread(self.queue)
            t.start()
            threads.append(t)
        
        for thread in threads:
            thread.join()
        self.queue.join()
        return {'code': 1, 'msg': '镜像同步完毕'}
    
class KVMImageSyncThread(Thread):
    """
    镜像同步的线程任务类
    """
    def __init__(self, task_queue):
        """
        task_queue: (list), # 线程任务队列
        """
        self.exc = None
        self.queue = task_queue
        super().__init__()
        
        
    def run(self):
        try:
            while True:
                try:
                    sync_func, args = self.queue.get(block=False)
                    sync_func(*args)    # 执行任务的承载函数
                except queue.Empty:
                    return
                self.queue.task_done()
        except:
            import sys
            # Save details of the exception thrown but don't rethrow,
            # just complete the function
            self.exc = sys.exc_info()

    def join(self):
        Thread.join(self)
        if self.exc:
            msg = "Thread '%s' threw an exception: %s" % (self.getName(), self.exc[1])
            new_exc = Exception(msg)
            raise new_exc.with_traceback(self.exc[2])