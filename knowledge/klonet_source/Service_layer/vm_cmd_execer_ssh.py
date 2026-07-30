import secrets
import re
import time
import paramiko
import socket
from ..Implement_layer.LinkManager.link_operate import shell_execute
import timeout_decorator

def init_end_command(number):
    # 随机生成结束命令
    while True:
        random_bytes = secrets.token_bytes(number)
        random_string = random_bytes.hex()
        if '\r' not in random_string:
            break
    command = f"echo \"result:$? for {random_string}\""
    return command, random_string

def vm_cmd_exec(ssh_client, channel, vm_NEtype, cmd_list, timeout_s, gaptime_list=None):
    commands = cmd_list
    if vm_NEtype == "host":
        try:
            for i in range(len(commands)):
                print('now, command is:'+commands[i])
                stdin, stdout, stderr = ssh_client.exec_command(commands[i],timeout=timeout_s)
                res, err = stdout.read().decode("utf-8"), stderr.read().decode("utf-8")
                data_all = res if res else err
                if gaptime_list != None:
                    time.sleep(gaptime_list[i])

            # 等待命令执行完成
            code = stdout.channel.recv_exit_status()

        except socket.timeout as e:
            code = None
            data_all = f"Exception('This cmd is executed but failed to be executed within TIMEOUT={timeout_s}s. We stop executing it and therefore cannot get its exit_code and output.')"
        
        except Exception as e:
            code = None
            data_all = f"{repr(e)}"
    
    else:
        try:
            _, random_string = init_end_command(16)
            for i in range(len(commands)):
                print('now, command is:'+commands[i])
                channel.send(commands[i]+'\n')
                if gaptime_list != None:
                    time.sleep(gaptime_list[i])
            channel.send('echo '+random_string+'\n')

            @timeout_decorator.timeout(timeout_s + 0.5)
            def resv_data(channel,random_string):
                output = ''
                while True:
                    temp_data = channel.recv(1024).decode('utf-8')
                    output += temp_data
                    if random_string[-1] in temp_data:
                        if random_string in output:
                            break 
                return output
            
            output = resv_data(channel, random_string)

            if 'Error' not in output[output.find(commands[-1]):output.rfind(random_string)]:
                code = 0
            else:
                code = 127
            data_all = output
        
        except timeout_decorator.timeout_decorator.TimeoutError as e:
            print(e)
            channel.send('\x03')
            channel.send('sys\n')
            channel.send('quit\n')
            code = None
            data_all = f"Exception('This cmd is executed but failed to be executed within TIMEOUT={timeout_s}s. We stop executing it and therefore cannot get its exit_code and output.')"

        except Exception as e:
            code = None
            data_all = f"{repr(e)}"

    return code, data_all

class vm_cmd_execer():
    def __init__(self, vm_id, vm_NEtype, cmd_list, time_gap_list, timeout_s_list, mode_list):
        self.vm_id = vm_id
        self.vm_NEtype = vm_NEtype
        self.cmd_list = cmd_list
        self.time_gap_list = time_gap_list
        self.timeout_s_list = timeout_s_list
        self.mode_list = mode_list
        self.code_list = []
        self.result_list = []

        # 定义SSH连接参数
        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        # SSH连接
        self.hostname = re.findall(r'(?:\d{1,3}\.)+(?:\d{1,3})', shell_execute(f'virsh domifaddr {self.vm_id} | grep ip'))[0]
        #self.hostname = '192.168.3.102'
        self.username = "root"
        self.password = "[REDACTED]"
        self.ssh_client.connect(self.hostname, username=self.username,password=self.password, look_for_keys=False,compress=True)
        self.channel = self.ssh_client.invoke_shell()

    def exe_command(self):
        if self.mode_list == 1:
            for i in range(len(self.cmd_list)):
                code, result = vm_cmd_exec(
                    self.ssh_client, self.channel, self.vm_NEtype, [self.cmd_list[i]], self.timeout_s_list[i])
                self.code_list.append(code)
                self.result_list.append(result)
                if code == None:
                    break
                time.sleep(self.time_gap_list[i])

        elif self.mode_list == 0:
            code, result = vm_cmd_exec(
                self.ssh_client, self.channel, self.vm_NEtype, self.cmd_list, sum(self.timeout_s_list), self.time_gap_list)
            self.code_list.append(code)
            self.result_list.append(result)

    def close(self):
        self.ssh_client.close() 
        self.channel.close()

class vm_file_transformer():
    def __init__(self,vm_id) -> None:
        self.vm_id = vm_id
        self.hostname = re.findall(r'(?:\d{1,3}\.)+(?:\d{1,3})', shell_execute(f'virsh domifaddr {self.vm_id} | grep ip'))[0]
        self.username = "root"
        self.password = "[REDACTED]"

        self.transform = paramiko.Transport(self.hostname, 22)
        self.transform.connect(username=self.username, password=self.password)
        self.sftp = paramiko.SFTPClient.from_transport(self.transform)
    
    def get_file(self,remote_file,local_file):
        self.sftp.get(remote_file,local_file, prefetch= True)

    def put_file(self,local_file,remote_file):
        def generate_paths(input_path):
            # 使用正则表达式匹配路径中的分隔符，包括斜杠和冒号
            parts = re.split(r'[/:]', input_path)
            # 去除空字符串
            parts = [part for part in parts if part]
            # 如果路径以斜杠或冒号开头，则在第一个部分之前添加回去
            if input_path.startswith('/'):
                parts[0] = input_path[0] + parts[0]
            else:
                parts[0] = parts[0] + ':/'
            return parts
        
        container_store_path = remote_file[:remote_file.rfind('/')]
        store_path_list = generate_paths(container_store_path)
        remote_file = remote_file[remote_file.rfind('/')+1:]

        for path in store_path_list:
            try:
                self.sftp.chdir(path)
            except:
                self.sftp.mkdir(path)
                self.sftp.chdir(path)
        self.sftp.put(local_file,remote_file,confirm=True)

    def close(self):
        # 关闭
        self.sftp.close
        self.transform.close()
