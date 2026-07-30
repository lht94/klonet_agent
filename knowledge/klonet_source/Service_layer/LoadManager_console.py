from ..tools import get_host_ip
import os
from ..Implement_layer.LinkManager.link_operate import shell_execute
from gevent import subprocess
from .vm_cmd_execer import vm_cmd_execer
import time


class DownloadFile(object):
    """
    提供文件下载的具体操作，以供worker调用
    """

    def __init__(self, container_id='', file_path='', NEservice='kvm', NEtype=''):
        """
        初始化

        Args:
            container_id: 容器的id
            file_path: 文件在容器内的位置
        """

        self.container_id = container_id
        self.NEservice = NEservice
        self.NEtype = NEtype
        self.file_path = file_path
        self.file_name = self.file_path.split('/')[-1]

    def cp_file(self, static_path='/home/'):
        """
        将容器内的文件拷贝到worker的静态文件夹

        Args:
            static_path: 静态文件夹的路径

        Return:
            static_url: 静态文件夹的url（不带有worker的ip以及端口号）
        """
        if self.NEservice == 'kvm':

            # 首先启动各项ftp服务
            # 服务器执行ftp_srt.sh文件
            current_folder = os.path.dirname(os.path.abspath(__file__))
            ftp_start_result = shell_execute(f'sudo bash {current_folder}/ftp_set.sh')
            print(ftp_start_result)

            try:

                shell_execute(f'sudo chmod 777 {static_path}')
                print('we made it!')

                file_dir = self.file_path[:self.file_path.rfind('/')]
                commands = ['\x1a',f'cd {file_dir}',f'ftp 192.168.122.1', 'ftp_vm_use', 'vemu',
                            f'cd {static_path}',
                            f'put {self.file_name} {self.file_name}', 'y',
                            'quit']
                if self.NEtype == 'host':
                    # 虚机启动ftp
                    commands = ['sudo yum install ftp -y'] + commands

                # file_size = os.path.getsize(self.worker_file_path)
                # timeout_s = min(file_size//(3e7 * len(commands)),1)
                timeout_s = 999
                time_gap_list = [1]*len(commands)
                timeouts_list = [timeout_s]*len(commands)
                mode = 0
                commands_execer = vm_cmd_execer(vm_id=self.container_id,
                                                #vm_id='lzl_ar1000_store',
                                                vm_NEtype=self.NEtype,
                                                #vm_NEtype='router',
                                                cmd_list=commands,
                                                time_gap_list=time_gap_list,
                                                timeout_s_list=timeouts_list,
                                                mode_list=mode)
                commands_execer.exe_command()

            except:
                return {
                    'code': 0,
                    'msg': 'vm cp Error!'
                }

            if 'Transfer complete' in commands_execer.result_list[0] and commands_execer.code_list[0] == 0:
                static_url = f'/reallyload/?workerip={get_host_ip()}'
                return {
                    'code': 1,
                    'msg': static_url
                }
            else:
                print('vm cp Error!')
                return {
                    'code': 0,
                    'msg': 'vm cp Error!'
                }

        elif self.NEservice == 'docker':
            try:
                # print('*' * 50)
                # print(f'docker cp {self.container_id}:{self.file_path} {static_path}')
                # print('*' * 50)
                # shell_execute(f'docker cp {self.container_id}:{self.file_path} {static_path}')
                
                # choux23
                # https://www.frytea.com/archives/762/
                # https://blog.csdn.net/qq_39919755/article/details/91492265
                shell_execute(f'docker exec {self.container_id} tar -cf - {self.file_path}'
                            f' | tar -xf - -C {static_path}')
            except subprocess.CalledProcessError as e:
                print(e)
                return {
                    'code':0,
                    'msg':'docker cp Error!'
                }

            # os.system(
            #     f'docker cp {self.container_id}:{self.file_path} {static_path}')

            # static_url = f'/reallyload/?workerip={get_host_ip()}&file={self.file_name}'
            static_url = f'/reallyload/?workerip={get_host_ip()}'
            return {
                'code': 1,
                'msg': static_url
            }

        else:
            print("There is a domain not belonging to kvm or docker.")
            return {
                'code': 0,
                'msg': 'domain cp Error!'
            }


class UploadFile(object):
    """
    提供上传文件的具体操作，以供worker调用
    """

    def __init__(self, container_id='', file_path='', NEservice='kvm', NEtype=''):
        """
        初始化

        Args:
            container_id: 容器的id
            file_path: worker本地存放文件的位置
        """

        self.container_id = container_id
        self.NEservice = NEservice
        self.NEtype = NEtype
        self.worker_file_path = file_path
        self.worker_file_name = self.worker_file_path.split('/')[-1]

    def generate_paths(self, input_path):
        if input_path[-1] == '/':
            input_path = input_path[:-1]
        paths = []
        index = input_path.find('/')
        while input_path:
            index = input_path.find('/', index+1)
            if index == -1:
                paths.append(input_path)
                break
            else:
                paths.append(input_path[:index])
        return paths

    def cp_file(self, container_store_path='/home/'):
        """
        将目标文件拷贝到对应容器的container_store_path下

        Args:
            container_store_path: 用户希望container上的文件保存位置。

        Return:
            dict: {
                'code': 0失败，1成功,
                'msg': 提示信息
            }
        """
        if self.NEservice == 'kvm':

            # 首先创建dir
            try:
                if container_store_path[-1] == '/':
                    container_store_path = container_store_path[:-1]

                container_store_path_list = self.generate_paths(
                    container_store_path)
                commands = []
                for store_path in container_store_path_list:
                    commands.append('mkdir '+store_path)
                commands.append(f'cd {container_store_path}')
                time_gap_list = [0]*len(commands)
                timeouts_list = [1]*len(commands)
                mode = 0
                commands_execer = vm_cmd_execer(vm_id=self.container_id,
                                              #vm_id='lzl_ar1000_store',
                                             vm_NEtype=self.NEtype,
                                              #vm_NEtype='router',
                                             cmd_list=commands,
                                             time_gap_list=time_gap_list,
                                             timeout_s_list=timeouts_list,
                                             mode_list=mode)
                commands_execer.exe_command()
                code = commands_execer.code_list[0]
                if code == 0:
                    print("We made it!")
                else:
                    print("sorry, we had try our best to create it, but we filed!")
                    return {
                        'code': 0
                    }
            except:
                if 'commands_execer' in locals():
                    commands_execer.close()
                print("sorry, we had try our best to create it, but we filed!")
                return {
                    'code': 0
                }
            
            # 其次服务器开启ftp，并设置账号密码等
            current_folder = os.path.dirname(os.path.abspath(__file__))
            shell_execute(f"sudo chmod 777 {current_folder}/ftp_set.sh")
            ftp_start_result = shell_execute(f'sudo bash {current_folder}/ftp_set.sh')
            print(ftp_start_result)

            try:
                worker_file_dir = self.worker_file_path[:self.worker_file_path.rfind('/')]
                commands = ['\x1a',f'cd {container_store_path}',f'ftp 192.168.122.1', 'ftp_vm_use', 'vemu',f'cd {worker_file_dir}',
                            f'get {self.worker_file_name} {self.worker_file_name}',
                            'y', 'quit']
                
                if self.NEtype == 'host':
                    # 虚机启动ftp
                    commands = ['sudo yum install ftp -y'] + commands[1:]
                # file_size = os.path.getsize(self.worker_file_path)
                # timeout_s = min(file_size//(3e7 * len(commands)),1)
                timeout_s = 999
                time_gap_list = [1]*len(commands)
                timeouts_list = [timeout_s]*len(commands)
                mode = 0
                commands_execer = vm_cmd_execer(vm_id=self.container_id,
                                                 #vm_id='lzl_ar1000_store',
                                                vm_NEtype=self.NEtype,
                                                #vm_NEtype='router',
                                                cmd_list=commands,
                                                time_gap_list=time_gap_list,
                                                timeout_s_list=timeouts_list,
                                                mode_list=mode)
                commands_execer.exe_command()
                if 'Transfer complete' in commands_execer.result_list[0] and commands_execer.code_list[0] == 0:
                    return {
                        'code': 1,
                        'msg': 'Upload success!'
                    }
                else:
                    return {
                        'code': 0,
                        'msg': 'Upload failed!'
                    }

            except:
                return {
                    'code': 0,
                    'msg': 'Upload failed!'
                }

        elif self.NEservice == 'docker':
            try:
                result = shell_execute(
                    f'docker exec {self.container_id} find {container_store_path}')
            except:
                print("there is no such dir but we will make it for you")
                try:
                    shell_execute(
                        f'docker exec {self.container_id} mkdir -p mkdir {container_store_path}')
                    print("We made it!")
                except:
                    print("sorry, we had try our best to create it, but we filed!")
                    return {
                        'code': 0
                    }
            try:
                # shell_execute(f'docker cp {self.worker_file_path} {self.container_id}:{container_store_path}')
                
                # choux23
                # https://www.frytea.com/archives/762/
                # https://blog.csdn.net/qq_39919755/article/details/91492265
                file_name = self.worker_file_path.split('/')[-1]
                path = self.worker_file_path[:-len(file_name)]
                print(f"cd {path}; "
                            f"tar -cf - {file_name}"
                            f" | docker exec -i {self.container_id} tar -xf - -C {container_store_path}")
                shell_execute(f"cd {path}; "
                            f"tar -cf - {file_name}"
                            f" | docker exec -i {self.container_id} tar -xf - -C {container_store_path}")
                
                return {
                        'code': 1,
                        'msg': 'Upload success!'
                    }
            except subprocess.CalledProcessError as e:
                print(e)
                return {
                    'code': 0,
                    'msg': 'Upload failed!'
                }

        else:
            print("There is a domain not belonging to kvm or docker.")
            return {
                'code': 0
            }
