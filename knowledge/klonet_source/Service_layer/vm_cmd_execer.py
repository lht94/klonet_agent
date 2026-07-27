import libvirt
import secrets
import re
import time
import multiprocessing
import timeout_decorator
import subprocess

class resvStream_processing():

    def __init__(self, vm_NEtype, stream, random_string, timeout_s):
        # super(resvStream_processing, self).__init__()  # super: 用于调用超类
        self.vm_NEtype = vm_NEtype
        self.stream = stream
        self.random_string = random_string
        # self.end_time = timeout_s + time.time() + 0.5
        self.data_all = ''
        self.code = None
        self.timeout_s = timeout_s
        self.time_out_code = False
        # self.queue = result_queue
        self.temp_data = b''

    def run(self):  # start时会自动调用run
        @timeout_decorator.timeout(self.timeout_s+0.5)
        def run_all():
            try:
                while True:
                    data = self.stream.recv(2048)
                    # 防止一个符号的16进制数没传完导致无法编码
                    try:
                        data = (self.temp_data+data).decode('utf-8')
                        # print(data, end='', flush=True)
                        self.temp_data = b''
                    except:
                        self.temp_data += data
                        data = ''

                    self.data_all += data

                    if self.random_string[-1] in data:  # 这个if是为了减少re的使用次数，因为re匹配费时
                        # 当 random_string 过长的会导致出现echo发出的时候出现' \r'需要去除才能匹配

                        data_all_copy = self.data_all.replace(' \r', '')
                        matches = re.findall(self.random_string, data_all_copy)

                        if len(matches) >= 2:
                            # 去除clear
                            if self.vm_NEtype == "host":
                                clear_flag = "\x1b[H\x1b[J"
                            else:
                                clear_flag = "\x1B[2J\r\n"
                            index = self.data_all.find(clear_flag)
                            self.data_all = self.data_all[index + len(clear_flag):]
                            
                            # 去除多余的随机码
                            for nn in range(len(matches)-2):
                                index = self.data_all.rfind(self.random_string)
                                self.data_all = self.data_all[:index]

                            # 用于确定代码code的
                            last_index = self.data_all.rfind(
                                ' for '+self.random_string)
                            first_index = self.data_all[:last_index].rfind(
                                'result:')+len('result:')
                            try:
                                self.code = int(
                                    self.data_all[first_index:last_index])
                            except:
                                if self.vm_NEtype == "host":
                                    self.code = None
                                else:
                                    self.code = self.data_all[first_index:last_index]
                                    if '$' in self.code:
                                        self.code = 0
                                    else:
                                        self.code = None

                            self.data_all = self.data_all[:self.data_all.find(self.random_string)]

                            break

            except timeout_decorator.timeout_decorator.TimeoutError as e:
                print(e)
                self.time_out_code = True
                self.code = None
                self.data_all = f"This cmd is executed but failed to be executed within TIMEOUT={self.timeout_s}s. We stop executing it and therefore cannot get its exit_code and output."

            except Exception as e:
                print(e)
                self.code = None
                self.data_all = f"{repr(e)}"

        run_all()

def init_end_command(number):
    # 随机生成结束命令
    while True:
        random_bytes = secrets.token_bytes(number)
        random_string = random_bytes.hex()
        if '\r' not in random_string:
            break
    command = f"echo \"result:$? for {random_string}\""
    return command, random_string

def vm_cmd_exec(stream, vm_NEtype, cmd_list, timeout_s, gaptime_list=None, init_flag=0):
    try:
        commands = cmd_list
        end_command, random_string = init_end_command(
            16)  # 设置结束string

        if init_flag == 0:
            if vm_NEtype == "host":
                stream.send(('\x03 quit\n this make erro then clear it \n clear \n').encode(
                    'utf-8'))  # 去除之前残留的命令行，防止出错
            else:
                stream.send(('this make erro then clear it \n cls \n').encode(
                    'utf-8'))  # 去除之前残留的命令行，防止出错
            time.sleep(0.5)
        else:
            user, passwd = get_user_passwd(vm_NEtype=vm_NEtype)
            init_command = ["", user, passwd, "\n"]
            for i in range(len(init_command)):
                print('now, initing......')
                stream.send((init_command[i]+'\n').encode('utf-8'))
                time.sleep(0.5)

        for i in range(len(commands)):
            print('now, command is:'+commands[i])
            stream.send((commands[i]+'\n').encode('utf-8'))
            if gaptime_list == None:
                continue
            time.sleep(gaptime_list[i])

        time.sleep(0.5)
        if vm_NEtype == "host":
            stream.send((end_command+'\n').encode('utf-8'))
        else:
            stream.send((end_command+'\n').encode('utf-8'))
            stream.send((end_command+'\n').encode('utf-8'))

        # 子线程负责接收数据
        #result_queue = multiprocessing.Queue()
        recv_command_processing = resvStream_processing(
            vm_NEtype, stream, random_string, timeout_s)
        # recv_command_processing.start()
        # recv_command_processing.join()
        recv_command_processing.run()

        # time_out_code = result_queue.get()
        # code = result_queue.get()
        # data_all = result_queue.get()

        if recv_command_processing.time_out_code:
            if vm_NEtype == "host":
                stream.send(('\x03').encode('utf-8'))
                stream.send(('quit\n').encode('utf-8'))
            else:
                stream.send(('\x03').encode('utf-8'))
                stream.send(('sys\n').encode('utf-8'))
                stream.send(('quit\n').encode('utf-8'))

    except Exception as e:
        recv_command_processing.code = None
        recv_command_processing.data_all = f"{repr(e)}"

    return recv_command_processing.code, recv_command_processing.data_all

class vm_cmd_execer():
    def __init__(self, vm_id, vm_NEtype, cmd_list, time_gap_list, timeout_s_list, mode_list):
        # self.vm_id = 'lzl_ne40e_1'
        # self.vm_NEtype = 'router'
        self.vm_id = vm_id
        self.vm_NEtype = vm_NEtype
        self.cmd_list = cmd_list
        self.time_gap_list = time_gap_list
        self.timeout_s_list = timeout_s_list
        self.mode = mode_list
        self.code_list = []
        self.result_list = []

        self.conn = libvirt.open('qemu:///system')
        vm = self.conn.lookupByName(self.vm_id)
        self.stream = self.conn.newStream()  # 必须为阻塞流
        console = vm.openConsole(None, self.stream, flags=libvirt.VIR_DOMAIN_CONSOLE_FORCE)

    def exe_command(self):
        if self.mode == 1: #1模式代表逐条执行，返回每条命令的返回结果
            for i in range(len(self.cmd_list)):
                code, result = vm_cmd_exec(
                    self.stream, self.vm_NEtype, [self.cmd_list[i]], self.timeout_s_list[i])
                self.code_list.append(code)
                self.result_list.append(result)
                if code == None:
                    break
                time.sleep(self.time_gap_list[i])
            try:
                self.stream.finish()
            except libvirt.libvirtError as e:
                print(f"Error occurred: {e}")
                self.stream.abort()
            finally:
                self.conn.close()

        elif self.mode == 0: #0模式代表一起执行，只返回最后命令的返回结果
            code, result = vm_cmd_exec(
                self.stream, self.vm_NEtype, self.cmd_list, sum(self.timeout_s_list), self.time_gap_list)
            self.code_list.append(code)
            self.result_list.append(result)
            try:
                self.stream.finish()
            except libvirt.libvirtError as e:
                print(f"Error occurred: {e}")
                self.stream.abort()
            finally:
                self.conn.close()

        elif self.mode == 2: #2模式代表一起执行，只返回最后命令的返回结果，并且其是再没有输入密码和账号的时候自动输入账号和密码
            code, result = vm_cmd_exec(
                self.stream, self.vm_NEtype, self.cmd_list, sum(self.timeout_s_list), self.time_gap_list,init_flag=1)
            self.code_list.append(code)
            self.result_list.append(result)
            try:
                self.stream.finish()
            except libvirt.libvirtError as e:
                print(f"Error occurred: {e}")
                self.stream.abort()
            finally:
                self.conn.close()

def vm_simple_send(vm_id, cmd):
    conn = libvirt.open('qemu:///system')
    try:
        vm = conn.lookupByName(vm_id)
        stream = conn.newStream()  # 必须为阻塞流
        console = vm.openConsole(None, stream, flags=libvirt.VIR_DOMAIN_CONSOLE_FORCE)
        stream.send((cmd + '\n').encode('utf-8'))
        stream.finish()
    except libvirt.libvirtError as e:
        print(f"Error occurred: {e}")
        # 如果流还没关闭，则尝试中止
        if not stream.isClosed():
            stream.abort()
    finally:
        conn.close()

def init_vm(vm_id,vm_NEtype):
    if vm_NEtype == 'host':
        cmd_list = [
                    "sudo sed -i 's/^BOOTPROTO=.*/BOOTPROTO=dhcp/' /etc/sysconfig/network-scripts/ifcfg-eth0",
                    "sudo sed -i 's/^ONBOOT=.*/ONBOOT=yes/' /etc/sysconfig/network-scripts/ifcfg-eth0",
                    "sudo sed -i '/^IPADDR=/d' /etc/sysconfig/network-scripts/ifcfg-eth0",
                    "sudo sed -i '/^NETMASK=/d' /etc/sysconfig/network-scripts/ifcfg-eth0",
                    "sudo sed -i '/^GATEWAY=/d' /etc/sysconfig/network-scripts/ifcfg-eth0",
                    'sudo systemctl restart network',
                    'sudo yum install openssh-server -y',
                    'sudo systemctl start sshd',
                    'sudo systemctl enable sshd',
                    'sudo systemctl disable firewalld',
                    ]
        time_gap_list = [1.5] * len(cmd_list)
        timeout_s_list = [1] * len(cmd_list)
    else:
        cmd_list = [
                    "\x1a"
                    "sys",
                    "int g0/0/0",
                    "ip address dhcp-alloc",
                    "user-interface vty 0 4",
                    "authentication-mode aaa",
                    "protocol inbound ssh",
                    "user privilege level 15",
                    "aaa",
                    "local-user root password irreversible-cipher vemu",
                    "local-user root privilege level 15",
                    "Y",
                    "local-user root service-type ssh",
                    "local-user root ftp-directory flash:/",
                    "quit",
                    "ssh user root authentication-type password",
                    "ssh server permit interface g0/0/0",
                    "stelnet server enable",
                    "sftp server enable",
                    ]
        time_gap_list = [0.5] * len(cmd_list)
        timeout_s_list = [1] * len(cmd_list)
    mode = 2
    init_cmd_excer = vm_cmd_execer( vm_id = vm_id, 
                                    vm_NEtype = vm_NEtype, 
                                    cmd_list = cmd_list, 
                                    time_gap_list = time_gap_list, 
                                    timeout_s_list = timeout_s_list, 
                                    mode = mode)
    try:
        init_cmd_excer.exe_command()
        if init_cmd_excer.code_list[0] != 0:
            raise Exception(init_cmd_excer.result_list[0])
    except Exception as e:
        print(e)
        print(init_cmd_excer.code_list)
        print('vm init error!!!!!!')

def shell_execute(cmd,check=True) -> str:
    '''
        输入：要执行的shell命令\n
        输出：命令执行后的标准输出\n
        功能描述：使用subprocess.run()执行shell命令
    '''
    completed_process = subprocess.run(
        cmd, 
        shell=True, # 执行shell命令
        capture_output=True, # 效果与设置stdout=PIPE, stderr=PIPE一样
        text=True, # 将stdin, stdout, stderr修改为string模式
        check=check, # 开启检查，若出错则raise CalledProcessError
        )

    #print('# ' + cmd)
    if check == True:
        return completed_process.stdout.rstrip() # 加rstrip去除字符串末尾的回车
    else:
        return completed_process.returncode

def get_user_passwd(vm_NEtype):
    if vm_NEtype == 'host':
        return 'root', '123'
    else:
        return 'super', '123'

# vm_id = 'lzl_ar1000_1'
# NEtype = 'router'
# init_vm(vm_id=vm_id, vm_NEtype=NEtype)
# time.sleep(1)
# hostname = re.findall(r'(?:\d{1,3}\.)+(?:\d{1,3})', shell_execute(f'virsh domifaddr {vm_id} | grep ip'))[0]
# print(hostname)