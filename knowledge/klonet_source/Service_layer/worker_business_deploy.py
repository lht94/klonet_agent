from asyncio.subprocess import PIPE
import docker
import time
from .deploy_error import *

from .redisAPI import UserMapRedis

from nsenter import Namespace
from ..Implement_layer.LinkManager.link_operate import get_pid, shell_execute
from gevent import subprocess
import os
import traceback

docker_client = docker.from_env()
# traffic_run.py配置
traffic_run_script = '/traffic_run.py'

# traffic_gen配置
traffic_gen_dir = '/tg_test/TrafficGenerator/'

# pkt_gen2配置
pkt_gen2_dir = '/pkt_gen_test/'
pkt_gen2_script = 'pkt_gen.py'
pkt_gen2_cdf_name = 'MY_CDF.txt'  # 无用配置，现采用传参形式

# pkt_gen1配置
pkt_gen1_dir = '/pkt_gen_test/'
pkt_gen1_script = 'pkt_gen1.py'

# 当前目录
current_path = os.getcwd()

# 静态资源目录
static_path = f'{current_path}/vemu_uestc/static_resources'


class TrafficManager:
    """
    运行流量服务实例
    
    Attributes:
        role: 流量服务的角色
        user: 用户名
        topo: 拓扑名
        app_name: 流量服务名
        error_log: 错误信息的日志名
        user_db_cli: 用户数据库实例
        
    """
    def __init__(self, role, user, topo, app_name):
        self.role = role
        self.user = user
        self.topo = topo
        self.app_name = f"{self.topo}_{app_name}_{self.role}"  # liuliang_f1_pkt_gen1
        self.error_log = self.app_name + "_error.log"
        # self.temp_log = f"/{topo}_{app_name}_log.txt"
        # self.pid_file = f"/{topo}_{app_name}_pid.txt"
        user_db_map = UserMapRedis()
        self.user_db_cli = user_db_map.get_user_db(self.user)
        user_db_map.close()

    def traffic_gen_business_deploy(self, traffic_gen_list):
        """
        创建traffic_gen流量服务类型
        Args:
            traffic_gen_list: traffic_gen流量服务描述信息
        Raise:
            ValueError: 流量服务的角色参数不对
        """
        self.traffic_gen_list = traffic_gen_list
        try:
            if self.role == 'traffic_server':
                self._traffic_gen_business_deploy_servers()
            elif self.role == 'traffic_client':
                self._traffic_gen_business_deploy_clients()
            else:
                raise ValueError('role参数不对， 只能是 traffic_server 或者 traffic_client')
        except Exception as e:
            print(e)
            raise e
        finally:
            self.user_db_cli.close()

    def _traffic_gen_business_deploy_servers(self):
        """
        创建traffic_gen的服务端
        Raises:
            TrafficServerError:流量发生器server端启动进程出现错误引发该异常
        """
        container_pid_pool = []
        for server in self.traffic_gen_list:
            name, _, port = server.split(':')
            # server不能-d，否则父进程会认为子进程已经结束
            command = f'\'{static_path}/tg_test/TrafficGenerator/bin/server -p {port}\'\"'
            container_name = self.user_db_cli.get_value(f'{self.topo}_{name}', 'NEid')
            py_cmd = "bash -c \"python3.8 " + static_path + traffic_run_script + " --appname " + \
                    self.app_name + " --cmd " + command + \
                    f" 2>{static_path}/traffic_error_log/{self.error_log}"

            container_pid = get_pid(container_name)
            container_pid_pool.append(container_pid)
            try:
                with Namespace(container_pid, 'net'):
                    subprocess.Popen(py_cmd, shell=True, stdin=PIPE, stdout=PIPE)
            except:
                print('traffic_gen_server创建出错')

        # 检查应用是否启动
        for pid in container_pid_pool:
            # 对每个容器检查命名空间里是否有对应的进程
            with Namespace(pid, 'net'):
                output = subprocess.check_output(f'ps -ef', shell=True).decode("utf-8")
                check_cmd = f"ls -l {static_path}/traffic_error_log/ | grep {self.error_log}| awk \'{{print $5}}\'"
                log_size = subprocess.check_output(
                    check_cmd,
                    shell=True).decode('utf-8')
            if self.app_name not in output:
                if log_size != "0":
                    raise TrafficServerError('traffic server deploy error')
            
                
    def _traffic_gen_business_deploy_clients(self):
        """
        创建traffic_gen的客户端
        Raises:
            TrafficClientError:流量发生器client端写入文件或启动进程出现错误引发该异常
        """
        container_pid_pool = []
        for client in self.traffic_gen_list:
            container_name = self.user_db_cli.get_value(f'{self.topo}_{client["client_name"]}', 'NEid')
            client_conf = f'{container_name}_client_config.txt'
            incast_client_conf = f'{container_name}_incast_client_config.txt'
            self.traffic_gen_write_config_file(client, container_name)

            # 拼接命令，方式有点迷
            if client["mode"] == "1":
                command = "\'" + static_path + traffic_gen_dir + "bin/incast-client"
                command += " -c " + incast_client_conf
            else:
                command = "\'" + static_path + traffic_gen_dir + "bin/client"
                command += " -c " + client_conf
            for param, value in client["cli_param"].items():
                if value != "":
                    command += " -{} {}".format(param, value)
            command += "\'\""
            py_cmd = "bash -c \"python3.8 " + static_path + traffic_run_script + " --appname " + \
                     f"{self.app_name}" + " --cmd " + command + \
                     f" 2>{static_path}/traffic_error_log/{self.error_log}"
            container_pid = get_pid(container_name)
            container_pid_pool.append(container_pid)

            # 在当前容器net namespace执行创建traffic_client
            try:
                # 此处的try无法去掉，因为Popen无线程阻塞，得不到返回值;逻辑上也不能阻塞
                with Namespace(container_pid, 'net'):
                    # 必须切换工作路径，client -c无法识别带.和/的路径
                    os.chdir(f'./vemu_uestc/static_resources{traffic_gen_dir}conf/')
                    subprocess.Popen(py_cmd, shell=True, stdin=PIPE, stdout=PIPE)
                    os.chdir(current_path)
            except Exception as e:
                print('traffic_client创建出错')
                traceback.print_exc()
        

    def traffic_gen_write_config_file(self, client_json, cont_name): # 路径写死
        '''
        写入流量发生器的配置文件到指定节点中,traffic_gen项目源码需要这些文件
        Args:
            client_json:client的配置json信息
        Raises:
            TrafficClientError: 创建traffic_gen时写入文件出错引发该异常
        '''
        client_conf = f'conf/{cont_name}_client_config.txt'
        incast_client_conf = f'conf/{cont_name}_incast_client_config.txt'
        MY_CDF_conf = f'conf/{cont_name}_MY_CDF.txt'
        container_config_path = static_path + traffic_gen_dir + client_conf
        container_cdf_path = static_path + traffic_gen_dir + MY_CDF_conf
        config_txt = ""
        cdf_txt = ""
        # cdf_txt
        # CDF一定需要排好顺序, 否则client端启动参数会有问题
        req_size_list = sorted(client_json["client_config"]["req_size_dist"].keys(), key=lambda x: int(x))
        for req_size in req_size_list:
            dis = client_json["client_config"]["req_size_dist"][req_size]
            if dis != "1":
                cdf_txt = cdf_txt + req_size + " " + dis + "\n"
            else:
                cdf_txt = cdf_txt + req_size + " " + dis
        # config_txt
        for server in client_json["client_config"]["server_list"]:
            # 在Master处理后，client的server_list已经是IP_port形式
            _, ip, port = server.split(":")
            config_txt = config_txt + "server " + ip + " " + port + "\n"
        for rate, weight in client_json["client_config"]["rate"].items():
            config_txt = config_txt + "rate " + rate + " " + weight + "\n"
        for dscp, weight in client_json["client_config"]["dscp"].items():
            config_txt = config_txt + "dscp " + dscp + " " + weight + "\n"
        if client_json["mode"] == "1":
            container_config_path = static_path + traffic_gen_dir + incast_client_conf
            for fanout, weight in client_json["client_config"]["fanout"].items():
                config_txt = config_txt + "fanout " + fanout + " " + weight + "\n"
        config_txt = config_txt + "req_size_dist " + container_cdf_path

        # 文件写入
        container_name = self.user_db_cli.get_value('{}_{}'.format(
                        self.topo, client_json["client_name"]), 'NEid')
        
        command1 = f'echo \"{config_txt}\" > {container_config_path}'
        command2 = f'echo \"{cdf_txt}\" > {container_cdf_path}'
        exit_code1 = ""
        exit_code2 = ""
        exit_code1 = shell_execute(command1)  # 正确返回空，错误返回错误码
        exit_code2 = shell_execute(command2)
        if exit_code1:
            raise TrafficClientError(str("error1"))
        if exit_code2:
            raise TrafficClientError(str("error2"))
        

    def traffic_stop(self, traffic_gen_list):
        """
        停止流量服务

        Args:
            traffic_gen_list: 流量服务的描述信息
        Raise:
            TrafficStopError: 停止流量发送器或pkt_gen2/pkt_gen1应用时出现错误,引发该异常
        """
        
        container_pid_poll = []
        
        cmd = f"kill $(ps -ef | grep {self.app_name} | grep -v 'grep' | awk '{{print $2}}')"
        if self.role == 'traffic_server':
            for server in traffic_gen_list:
                name, _, port = server.split(':')
                container_name = self.user_db_cli.get_value(f'{self.topo}_{name}', 'NEid')
                container_pid = get_pid(container_name)
                container_pid_poll.append(container_pid)
                try:
                    print(f'traffic_server pid={container_pid}')
                    with Namespace(container_pid, 'net'):
                        shell_execute(cmd)  # 执行删除进程采用阻塞方式
                except:
                    print('出错')  # 打印出错有可能是流量发生完毕，流量进程已不存在
        elif self.role == 'traffic_client':
            for client in traffic_gen_list:
                container_name = self.user_db_cli.get_value(f'{self.topo}_{client["client_name"]}', 'NEid')
                container_pid = get_pid(container_name)
                container_pid_poll.append(container_pid)
                del_cmd = f'rm {static_path}{traffic_gen_dir}conf/{container_name}*.txt'
                try:
                    shell_execute(del_cmd)
                    print(f'traffic_client pid={container_pid}')
                    with Namespace(container_pid, 'net'):
                        shell_execute(cmd)  # 执行删除进程采用阻塞方式
                except:
                    print('出错')  # 打印出错有可能是流量发生完毕，流量进程已不存在
        elif self.role == 'pkt_gen2' or 'pkt_gen1':
            for src in traffic_gen_list:
                container_name = self.user_db_cli.get_value('{}_{}'.format(self.topo, src["src"]), 'NEid')
                container_pid = get_pid(container_name)
                container_pid_poll.append(container_pid)
                try:
                    print(container_pid)
                    with Namespace(container_pid, 'net'):
                        shell_execute(cmd)  # 执行删除进程采用阻塞方式
                except:
                    print('出错')  # 打印出错有可能是流量发生完毕，流量进程已不存在
        # 检查
        for pid in container_pid_poll:
            with Namespace(pid, 'net'):
                output = subprocess.check_output(f'ps -ef', shell=True).decode("utf-8")
            if self.app_name in output:
                print(f'Error! app_name={self.app_name} still exist')
                raise TrafficStopError(self.role)


    def pkt_gen2_business_deploy(self, src_list):
        '''
        完成worker服务器中关于pkt_gen2的client的服务创建

        Args:
            src_list:从数据库中获取的worker的server配置列表
        
        Raises:
            Pktgen2ClientError:网络汇聚包流量(pktgen2)的client端写入文件或启动进程出现错误,引发该异常
        '''
        self.traffic_gen_list = src_list
        container_pid_pool = []
        for src in self.traffic_gen_list:
            command = '\'python3.8 ' + f'{current_path}/vemu_uestc/static_resources' + pkt_gen2_dir + pkt_gen2_script
            for key, value in src.items():
                if key != "pkt_length":  # pktlength作为cdf文件另行处理
                    if key == "src_ip" or key == "dst_ip":
                        command += ' {} {}'.format("--" + key[:3], value)  # 需要输入的参数为节点ip
                    elif key == "src" or key == "dst":
                        continue
                    else:
                        command += ' {} {}'.format("--" + key, value)
            # TODO(sw):pkt_gen2在用argparse解析时，因此强转为int和float,之后要告知前端修改传参的类型
            pkt_length_cdf = {}
            for key, value in src["pkt_length"].items():
                pkt_length_cdf[int(key)] = float(value)
            # 去除字符串中的空格，否则解析会有问题
            pkt_length_cdf = str(src["pkt_length"]).replace(" ", "")
            command += " {} {}\'".format("--cdf_file", f"{pkt_length_cdf}")
            
            container_name = self.user_db_cli.get_value('{}_{}'.format(self.topo, src["src"]), 'NEid')
            container_pid = get_pid(container_name)
            container_pid_pool.append(container_pid)

            try:
                with Namespace(container_pid, 'net'):
                    py_cmd2 = "bash -c \"python3.8 " + f'{current_path}/vemu_uestc/static_resources' + traffic_run_script + " --appname " + \
                    f"{self.app_name}" + " --cmd " + command + f"\""
                    subprocess.Popen(py_cmd2, shell=True, stdin=PIPE, stdout=PIPE)
            except:
                print('出错')

        # 检查应用是否启动
        time.sleep(0.5)
        for pid in container_pid_pool:
            # 对每个容器检查命名空间里是否有对应的进程
            with Namespace(pid, 'net'):
                output = subprocess.check_output(f'ps -ef', shell=True).decode("utf-8")
            if self.app_name not in output:
                print("检查时，流量进程不存在")
                raise Pktgen2ClientError("pkt_gen2 client deploy error")
            else:
                print("检查时，流量进程存在")
            
        # 关闭数据库客户端
        self.user_db_cli.close()


    def pkt_gen1_business_deploy(self, src_list):
        '''
        完成worker服务器中关于pkt_gen1的client的服务创建

        Args:
            src_list:从数据库中获取的worker的server配置列表
        
        Raises:
            Pktgen1ClientError: pktgen1的client端启动进程出现错误,引发该异
        '''
        container_pid_pool = []
        for src in src_list:
            """
            src={'src': 'h1', 'dst': 'h2', 'src_ip': '10.1.1.2', 'dst_ip': '10.1.1.3', 'rate': '1', 'duration': '3', 'pkt_length': '1000', 'dist': 'normal', 'normal_scale': '1', 'ip_tos': '0', 'ip_ttl': '64', 'ip_id': '0', 'proto': 'tcp', 'tcp_header': {'tcp_window': '500', 'sport': '10000', 'dport': '10000'}, 'udp_header': {}}
            """
            command = '\'python3.8 ' + f'{current_path}/vemu_uestc/static_resources' + pkt_gen1_dir + pkt_gen1_script
            """command='python3 /pkt_gen_test/pkt_gen1.py"""
            for key, value in src.items():
                if key == "src" or key == "dst":
                    continue
                elif key == "udp_header" or  key == "tcp_header": # proto时已经处理过
                    continue
                elif key == "normal_scale":
                    if src["dist"] == "exp":
                        continue
                    else:
                        #dist是normal或exp，如果是normal就执行下面一行
                        command += ' {} {}'.format("--" + key, value)
                elif key == "proto":
                    command += ' {} {}'.format("--" + key, value)
                    if value == "udp":
                        for key, value in src["udp_header"].items():
                            command += ' {} {}'.format("--" + key, value)
                    if value == "tcp":
                        for key, value in src["tcp_header"].items():
                            command += ' {} {}'.format("--" + key, value)
                else:
                    command += ' {} {}'.format("--" + key, value)
                    """
                    最后命令是：command='python3 /pkt_gen_test/pkt_gen1.py --src_ip 10.1.1.2 --dst_ip 10.1.1.3 --rate 1 --duration 3 --pkt_length 1000 --dist normal --normal_scale 1 --ip_tos 0 --ip_ttl 64 --ip_id 0 --proto tcp --tcp_window 500 --sport 10000 --dport 10000
                    """
            container_name = self.user_db_cli.get_value('{}_{}'.format(self.topo, src["src"]), 'NEid')  # 源节点的容器id
            container_pid = get_pid(container_name)
            container_pid_pool.append(container_pid)
        
            try:
                with Namespace(container_pid, 'net'):
                    """这里提交的时候还是先保留，万一有bug还能修回来"""
                    py_cmd2 = "bash -c \"python3.8 " + f'{current_path}/vemu_uestc/static_resources' + traffic_run_script + " --appname " + \
                    f"{self.app_name}" + " --cmd " + command + f"\'\""
                    subprocess.Popen(py_cmd2, shell=True, stdin=PIPE, stdout=PIPE)
            except:
                print('出错')
        time.sleep(0.5)
        
        for pid in container_pid_pool:
            # 对每个容器检查命名空间里是否有对应的进程
            with Namespace(pid, 'net'):
                output = subprocess.check_output(f'ps -ef', shell=True).decode("utf-8")
            if self.app_name not in output:
                print("检查时，流量进程不存在")
                raise Pktgen1ClientError("pkt_gen1 client deploy error")
            else:
                print("检查时，流量进程存在")
        # 关闭数据库客户端
        self.user_db_cli.close()

    def pkt_gen2_write_cdf_txt(self, src_name, cdf_dict):  # 暂时没用到本函数
        '''
        写入pkt_gen2的cdf文件到指定节点中
        Args:
            client_json:client的配置json信息
        Raises:
            Pktgen2ClientError:pkt_gen2写入文件出错,引发该异常
        '''
        container_cdf_path = pkt_gen2_dir + pkt_gen2_cdf_name
        # cdf_txt
        # CDF一定需要排好顺序, 否则client端启动参数会有问题
        cdf_txt = ""
        req_size_list = sorted(cdf_dict.keys(), key=lambda x: int(x))
        # req_size_list = {'200':'0.2', '1200': '1'}
        for req_size in req_size_list:
            dis = cdf_dict[req_size]
            if dis != "1":  # 累积概率
                cdf_txt = cdf_txt + req_size + " " + dis + "\n"
            else:
                cdf_txt = cdf_txt + req_size + " " + dis
        # 文件写入
        # 这里应该使用的是容器的名字 NE_id
        container_name = self.user_db_cli.get_value('{}_{}'.format(self.topo, src_name), 'NEid')
        container = docker_client.containers.get(container_id=container_name)
        command = "bash -c \"echo \'{}\' > {}\"".format(cdf_txt, container_cdf_path)
        exit_code, output = container.exec_run(cmd=command, demux=True)
        if exit_code != 0:
            raise Pktgen2ClientError(str(output[1], encoding="utf-8"))


if __name__ == "__main__":

    # traffic_gen worker创建测试
    traffic_gen_server_list = ["h2:192.168.1.2:5001"]
    traffic_gen_client_list = [
        {
            "mode": "0",
            "client_name":  "h1",
            "client_config":  {
                "server_list":  [
                    "h2:192.168.1.2:5001"
                ],
                "req_size_dist":  {
                    "100":  "0.1",
                    "200":  "0.4",
                    "1000":  "0.7",
                    "10000":  "1"
                },
                "dscp":  {
                    "0":  "25",
                    "1":  "25",
                    "2":  "50"
                },
                "rate":  {
                    "1Mbps":  "50",
                    "2Mbps":  "50"
                }
            },
            "cli_param":  {
                "b":  "1",
                "t":  "30",
                "n":  "",
                "s":  "12"
            }
        }
    ]
    # traffic_gen_business_deploy(traffic_gen_server_list, traffic_gen_client_list)

    # pkt_gen2 worker创建测试
    pkt_gen2_src_list = [
        {
            "src": "h1",
            "dst":  "h2",
            "src_ip":  "192.168.1.1",
            "dst_ip":  "192.168.1.2",
            "rate":  "20",
            "pkt_length":  {
                "40":  "0.7",
                "200":  "0.9",
                "500":  "1"
            },
            "duration":  "30",
            "on_k":  "2",
            "on_min":  "1",
            "off_k":  "2",
            "off_min":  "2"
        }
    ]
    # roles = ["pktgen2", "traffic_server", "traffic_client"]
    # for role in roles:
    #     if role == "pktgen2":
    #         traffic_stop(role, pkt_gen2_src_list, "sw", "topo1")
    #     elif role == "traffic_server":
    #         traffic_stop(role, traffic_gen_server_list, "sw", "topo1")
    #     elif role == "traffic_client":
    #         traffic_stop(role, traffic_gen_client_list, "sw", "topo1")