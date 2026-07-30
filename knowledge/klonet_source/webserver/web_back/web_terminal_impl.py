import os
import time
import docker
import threading
from socket import timeout
from flask import request
import libvirt
import paramiko

from ...Service_layer.redisAPI import UserMapRedis

class ClientSSH(object):
    '''
    真实设备hardware的SSH连接类
    '''
    def __init__(self, host_ip, name, password):
        self.host_ip = host_ip      # 真是设备ip
        self.name = name            # 用户名（安全考虑，建议是普通用户）
        self.password = password    # 密码
        self.ssh_client = None      # ssh client
        self.channel = None         # 会话
        self.running = True         # 运行状态
        
        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh_client.connect(hostname=self.host_ip, username=self.name, password=self.password)
        transport = self.ssh_client.get_transport()
        self.channel = transport.open_session() # 打开会话
        self.channel.get_pty()              # 获取伪终端PTY
        self.channel.invoke_shell()         # 启动交互式shell会话
        
        
    def check_channel(self):
        # 检查channel状态
        if self.channel.exit_status_ready():
            self.cleanup()
            self.stop()
            print("SSH Channel stopped!")
            return False
        else:
            return True
        
    def send_command_to_hardware(self, command):
        # 发送命令
        if self.channel:
            self.channel.send(command.encode('utf-8'))
            
    def stop(self):
        self.running = False
        
    def cleanup(self):
        if self.channel:
            self.channel.close()
        if self.ssh_client:
            self.ssh_client.close()
        self.stop()

class SSHStreamThread(threading.Thread):
    '''
    线程类，用于接收ssh的输出并发送至前端ws
    '''
    def __init__(self, ws, ssh_client):
        super(SSHStreamThread, self).__init__()
        self.ws = ws
        self.ssh_client = ssh_client
        
    def run(self):
        while not self.ws.closed:
            try:
                ssh_Stdout = self.ssh_client.channel.recv(1024)
                
                if ssh_Stdout is not None:
                    self.ws.send(str(ssh_Stdout, encoding='utf-8', errors='replace'))
                else:
                    print("ssh channel is close!")
                    self.ws.close()
            except Exception as e:
                self.ws.close()
                if self.ssh_client:
                    self.ssh_client.cleanup()
                break
            
class SSHBeatWS(threading.Thread):
    '''
        线程类，用于检查服务器是否响应
    '''
    def __init__(self, ws, ssh_client):
        super(SSHBeatWS, self).__init__()
        self.ws = ws
        self.ssh_client = ssh_client
        
    def run(self):
        while self.ssh_client.check_channel() and not self.ws.closed:
            time.sleep(2)
        if not self.ws.closed:  # 如果是ssh关闭但ws没关闭
            self.ws.close()
        if self.ssh_client.channel:
            self.ssh_client.cleanup()

class ClientHandler(object):
    '''
    docker client类，对底层的docker.APIClient进行了封装
    '''
    def __init__(self, **kwargs):
        self.dockerClient = docker.APIClient(**kwargs)

    @property
    def client(self):
        return self.dockerClient

    def creatTerminalExec(self, containerid, cmd="/bin/bash"):
        execOptions = {
            "tty": True,
            "stdin": True,
            "stdout": True
        }

        execId = self.dockerClient.exec_create(containerid, cmd, **execOptions)
        return execId["Id"]

    def startTerminalExec(self, execId):
        '''
        启动exec实例

        Args:
            execId: exec示例id
        Returns:
            socket: 伪tty的socket对象
        '''
        socket = self.dockerClient.exec_start(execId, socket=True, tty=True)
        return socket

class Console(object):
    '''
    kvm console类，Console类负责处理与虚机的交互
    '''
    def __init__(self, uri, kvm_id = None, kvm_name = None):
        self.uri = uri
        self.kvm_id = kvm_id
        self.kvm_name = kvm_name
        # # 注册相关输入组件
        # self.register()
        # 打开与libvirt的连接
        self.connection = libvirt.open(uri)
        self.stream = None

        # 如果同时两者都有 以ID为准
        try:
            if self.kvm_id != None:
                self.domain = self.connection.lookupByID(self.kvm_id)
            else:
                self.domain = self.connection.lookupByName(self.kvm_name)
            
            # 存储虚机状态
            self.state = self.domain.state(0)
        except Exception as err:
            print(err)        


    # 检查虚拟机控制台状态
    def check_console(self):
        self.state = self.domain.state(0)
        # print(self.state)
        if not hasattr(self,'stream'):
            self.stream = None
        if (self.state[0] == libvirt.VIR_DOMAIN_RUNNING or
            self.state[0] == libvirt.VIR_DOMAIN_PAUSED):
            if  self.stream is None:
                # 创建虚拟机控制台流并打开虚拟机控制台
                self.stream = self.connection.newStream()
                try:
                    self.domain.openConsole(None, self.stream, flags=libvirt.VIR_DOMAIN_CONSOLE_FORCE)
                except Exception as err:
                    print(err)
                # 注册控制台数据可读事件处理函数
        # 感觉进不来
        else:
            if self.stream:
                # 如果虚拟机处于不运行状态，移除控制台流
                # self.stream.abort()
                # self.stream.finish()
                del self.stream
                self.stream = None

        # return self.run_console
        return self.stream is not None

    # 发送命令到虚拟机控制台
    def send_command_to_vm(self,command):
        if self.stream:
            self.stream.send(command.encode('utf-8'))

class KVMStreamThread(threading.Thread):
    '''
        线程类，用于接收libvirt stream的输出并发送至前端websocket
    '''
    def __init__(self, ws, Console):
        super(KVMStreamThread, self).__init__() # super: 用于调用超类
        self.ws = ws
        self.Console = Console
    
    def run(self): # 线程start时会自动调用run
        # 吃掉上次残余的输出
        if not self.ws.closed:
            KVMStreamStdout = self.Console.stream.recv(1024)
            while b'\r\n' not in KVMStreamStdout:
                KVMStreamStdout = self.Console.stream.recv(1024)
                pass
            self.ws.send(str(KVMStreamStdout.replace(b'\r\n', b''), encoding='ascii', errors='replace'))
            while not self.ws.closed:
                try:
                    # dockerStreamStdout = self.terminalStream.recv(2048)
                    KVMStreamStdout = self.Console.stream.recv(1024)
                    # print(type(KVMStreamStdout))
                    
                    if KVMStreamStdout is not None:
                        self.ws.send(str(KVMStreamStdout, encoding='ascii', errors='replace'))

                    else:
                        print("kvm daemon socket is close")
                        self.ws.close()
                # except timeout:
                #     print('Receive from docker timeout.')
                except Exception as e:
                    self.ws.close()
                    if hasattr(self.Console,'stream'):
                        del self.Console.stream
                    break

class DockerStreamThread(threading.Thread):
    '''
        线程类，用于接收docker stream的输出并发送至前端websocket
    '''
    def __init__(self, ws, terminalStream):
        super(DockerStreamThread, self).__init__() # super: 用于调用超类
        self.ws = ws
        self.terminalStream = terminalStream

    def run(self): # 线程start时会自动调用run
        while not self.ws.closed:
            try:
                dockerStreamStdout = self.terminalStream.recv(2048)
                if dockerStreamStdout is not None:
                    self.ws.send(str(dockerStreamStdout, encoding='utf-8'))
                else:
                    print("docker daemon socket is close")
                    self.ws.close()
            # except timeout:
            #     print('Receive from docker timeout.')
            except Exception as e:
                # print("docker daemon socket err: %s" % e)
                self.ws.close()
                break

class KVMBeatWS(threading.Thread):
    '''
        线程类，用于检查服务器是否响应
    '''
    def __init__(self, ws, kvm_console):
        super(KVMBeatWS, self).__init__()
        self.ws = ws
        self.kvm_console = kvm_console

    def run(self):
        while self.kvm_console.check_console() and not self.ws.closed:
            time.sleep(1) # 写死为1秒检查一次
        # 如果控制台关闭且websocket流没关闭 则关闭websocket流
        if not self.ws.closed:
            self.ws.close()
        # 否则是由于控制台意外关闭导致 若流没有被成功去除 去除流
        elif hasattr(self.kvm_console,'stream'):
            del self.kvm_console.stream

class DockerBeatWS(threading.Thread):
    '''
        线程类，用于检查服务器是否响应
    '''
    def __init__(self, ws, docker_client):
        super(DockerBeatWS, self).__init__()
        self.ws = ws
        self.docker_client = docker_client

    def run(self):
        while not self.ws.closed:
            time.sleep(10) # 写死为10秒检查一次
            # 如果服务器不响应，会抛出docker.errors.APIError异常
            self.docker_client.ping()

def start_web_socket(ws):
    if request.method == "GET":
        # print(request.args)
        user = request.args.get("user")
        topo = request.args.get("topo")
        ne_name = request.args.get("ne")
        

        
        print(f"enter web terminal. user:{user}, topo:{topo}, ne:{ne_name}")

            # 获取该节点所在worker及该节点的id
        user_db_map = UserMapRedis()
        user_db_cli = user_db_map.get_user_db(user)
        user_db_map.close()
        
        worker_ip = user_db_cli.get_worker_ip_by_ne_name(topo, ne_name)
        ne_id = user_db_cli.get_value(f"{topo}_{ne_name}", "NEid")
        # 获取该节点的类型，即容器还是虚机
        ne_service = user_db_cli.get_value(f"{topo}_{ne_name}",'NEservice')
        
        user_db_cli.close()
        # print(user, topo, net, container_id, cmd, worker_ip)


    if ne_service == "docker":
        base_url = "tcp://" + worker_ip + ":2375" # DOCKER_HOST
        # 86400s = a week 
        dockercli = ClientHandler(base_url=base_url, timeout=86400)
        terminalExecId = dockercli.creatTerminalExec(ne_id) # cmd
        # ._sock是什么意思？
        terminalStream = dockercli.startTerminalExec(terminalExecId)._sock 
        
        # 子线程1：接收docker输出并发送至前端
        terminalThread = DockerStreamThread(ws, terminalStream)
        terminalThread.start()
        
        # 子线程2：心跳
        beat_thread = DockerBeatWS(ws, dockercli.client)
        beat_thread.start()

        # 主线程：接收前端输入并发送至docker
        try:
            while not ws.closed:
                message = ws.receive()
                if message is not None:
                    sed_msg = bytes(message, encoding='utf-8')
                    if sed_msg != b'__ping__':
                        terminalStream.send(sed_msg)
        except Exception as err:
            print(err)
        finally:
            ws.close()
            terminalStream.close()
            dockercli.dockerClient.close()

    elif ne_service == "kvm":
        uri = f'qemu+tcp://{worker_ip}/system'
        # 创建控制台对象
        console = Console(uri, kvm_name = ne_id)

        # 主线程：接收前端输入并发送至虚机
        try:
            # 检查控制台状态
            if not console.check_console():
                raise RuntimeError("控制台没有成功启动")
            # 子线程1：接收虚机输出并发送至前端
            consoleThread = KVMStreamThread(ws, console)
            consoleThread.start()

            # 子线程2：心跳
            beat_thread = KVMBeatWS(ws, console)
            beat_thread.start()

            console.send_command_to_vm("\r")
            while not ws.closed:
                message = ws.receive()
                # 不理解这个message怎么跟docker的处理方式会不一样，，，，
                if message is not None:
                    if message != '__ping__':
                        console.send_command_to_vm(message)
        except Exception as err:
            if hasattr(console,'stream'):
                del console.stream
        finally:
            ws.close()
            # 抹去可能剩下的指令
            if hasattr(console,'stream') and console.stream:
                console.send_command_to_vm(",,,\rclear\r")
                del console.stream
            console.connection.close()
        
    elif ne_service == 'hardware':
        # 数据库获取管控信息
        neconfig = user_db_cli.get_value(f"{topo}_{ne_name}",'NEconfig')
        login_name = neconfig['config']['user']
        login_passwd = neconfig['config']['password']
        ip = neconfig['config']['IP']
        
        ssh_client = ClientSSH(ip, login_name, login_passwd)   # 先假设ne_id存储的真实设备的管控ip地址
        
        try:
            if not ssh_client.check_channel():
                raise RuntimeError("会话启动失败")
            # 子线程1：接收ssh的输出，反馈给websocket
            sshThread = SSHStreamThread(ws, ssh_client)
            sshThread.start()
            
            # 子线程2： 心跳
            beat_thread = SSHBeatWS(ws, ssh_client)
            beat_thread.start()
            
            ssh_client.send_command_to_hardware("\r")
            while not ws.closed:
                # wudx
                # 我也不理解这个ssh的message怎么跟docker的处理方式会不一样 :) hhhh
                # 反正是可以运行的
                message = ws.receive()
                if message is not None:
                    if message != '__ping__':   # 不是心跳信息
                        ssh_client.send_command_to_hardware(message)
        except Exception as e:
            print(e)
            if ssh_client:
                ssh_client.cleanup()
        finally:
            ws.close()
            # 抹去可能剩下的指令
            if ssh_client.check_channel() and ssh_client.channel:
                ssh_client.send_command_to_hardware("\rclear\r")
            ssh_client.cleanup()