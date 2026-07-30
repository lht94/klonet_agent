from .common import Manager

class CmdManager(Manager):
    '''命令管理类

    用于在已创建项目的节点中执行shell命令。

    Attributes:
        user(str): 用户名
        project(str): 项目名
    '''
    def __init__(self, user_name, project_name,
                backend_ip=None, backend_port=None):
        super().__init__(backend_ip, backend_port)
        self.user = user_name
        self.project = project_name

    def exec_cmds_in_nodes(self, node2cmds, cmd_timeout_s = '1', block = 'false'):
        """在多个节点中执行多条shell命令

        需注意，若执行较长时间阻塞的命令，如iperf3 -s，后台会执行此命令，
        但会触发超时机制，无法获取到命令的退出码及输出。

        Args:
            node2cmd(dict): 命令字典，key为节点名，value为shell命令列表。如：
                {"h1": ["ls", "ls"],"h2": ["pwd"]}
            cmd_timeout_s(str): 命令执行结果获取时间，默认为1s，可配置为(0, 300]s
            block(str): "true/false"，是否阻塞执行命令
        Returns:
            一个字典，描述了节点命令执行的情况。key为节点名，value为该节点中
            具体命令的执行结果字典。比如：

            {'h1': {'ls': { "exit_code": 0, "output": "bin"}}}

        """
        payload = {
            "user": self.user,
            "topo": self.project,
            "node_and_cmd": node2cmds,
            "cmd_timeout_s": cmd_timeout_s,
            "block": block
        }
        resp = self._post("/master/node_exec_cmd/", json=payload)
        resp_json = self._parse_resp(resp)
        self._check_resp_code(resp_json)

        return resp_json["exec_results"]

    def extract_output(self, dictionary):
        # 字典操作，目的是获取容器节点的网卡信息（去除回环和eth0）
        result = {}
        for key, value in dictionary.items():
            if isinstance(value, dict) and "0_ls /sys/class/net/" in value:
                outvalue = value["0_ls /sys/class/net/"].get("output")
                if outvalue is not None:
                    value1 = outvalue.replace("\n"," ")
                    value2 = value1.replace("lo","")
                    value3 = value2.replace("eth0","")
                    value4 = value3.split()
                result[key] = value4
        return result

    def get_eth_cmd(self, dictionary):
        # 通过容器网卡信息得到对应的关闭校验码命令以及设置mtu命令
        result1 = {}
        result2 = {}
        for key, value_list in dictionary.items():
            cmd1 = ['ifconfig ' + string + ' mtu 1450' for string in value_list]
            cmd2 = ['ethtool -K ' + string + ' rx off tx off' for string in value_list]
            result1[key] = cmd1
            result2[key] = cmd2
        return result1, result2


