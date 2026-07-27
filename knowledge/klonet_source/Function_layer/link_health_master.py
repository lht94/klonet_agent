import grequests
from ..tools.context import redis_context
from ..vemu_config.config import PROJ_CONFIG
from ..Service_layer.redis_error import TableNotExistError, KeyNotExistError

# TODO(tie): veth-pair不知道会不会坏，先不提供检查

class LinkCheckerMaster:
    """
    拓扑的链路健康状态检查

    Attributes:
        user: 用户名
        project_name: 项目拓扑名
    """
    def __init__(self, user, project_name):
        self.user = user
        self.project_name = project_name
    
    def _get_subtopos_and_ips(self):
        '''
        获取指定拓扑的子拓扑名及其对应的worker_ip

        Returns:
            subtopos: 拓扑的子拓扑列表
            worker_ips: worker_ip列表
        '''
        with redis_context(self.user) as user_db_cli:
            # 创建一个redis管道
            pipe = user_db_cli._db_conn.pipeline()
            # 获取拓扑的若干子拓扑
            subtopos = user_db_cli.get_value("topo2subtopo", self.topo)
            # 将多个redis操作添加到管道中
            for subtopo in subtopos:
                pipe.hget("subtopo2worker", subtopo)
            # 执行管道中所有命令，返回一个包含所有命令结果的列表
            # 该列表为部署该拓扑的所有worker的ip
            # 获取带引号的若干worker_ip，如 '"172.31.0.4"'
            worker_ips_temp = pipe.execute()

            # 去掉引号，如 '"172.31.0.4"' -> '172.31.0.4'
            worker_ips = []
            for worker_ip in worker_ips_temp:
                worker_ip = worker_ip.strip("\"")
                worker_ips.append(worker_ip)

            # 返回
            return subtopos, worker_ips

    def send_start_signal(self, type,
                          is_check_once=None, 
                          check_round_interval_s=None):
        '''
        向worker发送开始检查或创建l2ping_replyer的信号

        Args:
            type: 向worker发送的链路检测信号类型，可选值为checklink或l2ping_replyer
            is_check_once: 是否只进行一次检测
            check_round_interval_s: 每次检测的时间间隔，单位秒
        
        Returns:
            若类型为l2ping_replyer，返回空字典{}
            若类型为checklink，返回已损坏的vxlan统计字典
        
        Raises:
            若存在worker给master的响应出现错误（code为0），则 raise RuntimeError
        '''
        # 类型可选值为checklink或l2ping_replyer
        assert(type=="checklink" or type=="l2ping_replyer")
        # 获取subtopo名及其对应worker_ip
        subtopos, worker_ips = self._get_subtopos_and_ips()

        # 异步请求列表
        reqs = []
        # 对每个worker，生成url和字段，并加入请求列表
        # 两种类型，对应两个不同的url
        for i, worker_ip in enumerate(worker_ips):
            worker_url = (f"http://{worker_ip}:{PROJ_CONFIG.worker_port}"
                f"/worker/{type}/")
            start_dict = {
                "user": self.user,
                "project_name": self.project_name,
                "subtopo": subtopos[i],
                "is_check_once": is_check_once,
                "check_round_interval_s": check_round_interval_s,
            }
            req = grequests.post(worker_url, json=start_dict)
            reqs.append(req)

        # 进行异步请求
        resps = grequests.map(reqs)
        
        # worker错误信息统计
        is_error = False
        error_msgs = []
        # 已损坏的vxlan统计，将作为返回值
        # 若类型为l2ping_replyer，不进行统计
        broken_vxlans = {}

        # 对每个worker响应
        for i, resp in enumerate(resps, 1):
            if type == "checklink":
                # worker响应中已损坏的vlan
                broken_vxlan_list = resp.json()["broken_vxlans"]
                # 获取vxlan详情，并加入已损坏的vxlan统计字典
                for borken_vxlan in broken_vxlan_list:
                    broken_vxlans[borken_vxlan] = \
                        self._get_vxlan_detail(borken_vxlan)
            # 统计worker发生错误的情况
            if resp.json()["code"] != 1:
                return_msg = resp.json()["msg"]
                error_msgs.append(f"发往worker的第{i}个请求失败！"
                                  f"请求url：{worker_url}，"
                                  f"worker返回的msg为：{return_msg}")
                is_error = True
        # 错误抛出
        if is_error:
            raise RuntimeError(f"{error_msgs}")
        # 返回
        return broken_vxlans

    def stop_check(self):
        '''
        向worker发送停止检查的信号

        Args:
            self
        
        Returns:
            None
        
        Raises:
            若存在worker给master的响应出现错误（code为0），则 raise RuntimeError
        '''
        # 获取subtopo名及其对应worker_ip
        subtopos, worker_ips = self._get_subtopos_and_ips()

        # 进行异步请求
        reqs = []
        for i, worker_ip in enumerate(worker_ips):
            worker_url = (f"http://{worker_ip}:{PROJ_CONFIG.worker_port}"
                "/worker/checklink/")
            stop_dict = {
                "user": self.user,
                "project_name": self.project_name,
                "subtopo": subtopos[i]
            }
            req = grequests.delete(worker_url, json=stop_dict)
            reqs.append(req)
        resps = grequests.map(reqs)

        # worker错误信息统计
        error_msgs = []
        is_error = False

        # 对每个worker响应
        for i, resp in enumerate(resps, 1):
            # 统计worker发生错误的情况
            if resp.json()["code"] != 1:
                return_msg = resp.json()["msg"]
                error_msgs.append(f"发往worker的第{i}个请求失败！"
                                  f"请求url：{worker_url}，"
                                  f"worker返回的msg为：{return_msg}")
                is_error = True
        # 错误抛出
        if is_error:
            raise RuntimeError(f"{error_msgs}")

    def record_check_report(self, subtopo, broken_vxlan_list):
        '''
        记录检查报告，将损坏的链路列表写入redis

        Args:
            subtopo: 报告的子拓扑名
            broken_vxlan_list: 报告的损坏vxlan列表

        Returns:
            None
        
        Raises:
            若存在链路损坏情况，则 raise RuntimeError
        '''
        # 生成已损坏链路字典
        broken_links = {"vxlans": broken_vxlan_list}
        print(f"{self.project_name}_broken_links-{subtopo}: {broken_links}")
        # 将已损坏链路字典写入数据库
        with redis_context(self.user) as user_db_cli:
            # try:
            #     broken_links = user_db_cli.get_value(
            #         f"{self.project_name}_broken_links", subtopo)
            # except (TableNotExistError, KeyNotExistError):
            #     broken_links = {"vxlans": []}
            user_db_cli.set_value(f"{self.project_name}_broken_links",
                                  subtopo, broken_links)
        # 总结报告
        self._summary_report()

    def _summary_report(self):
        '''
        总结报告

        Args:
            self
        
        Returns:
            None
        
        Raises:
            若存在链路损坏情况，则 raise RuntimeError
        '''
        def green_str(string):
            """
            将传入的字符串转换为绿色的终端输出
            """
            return(f"\033[0;32m{string}\033[0m")

        with redis_context(self.user) as user_db_cli:
            # 读取已损坏链路字典
            reports = user_db_cli.get_all_values(
                f"{self.project_name}_broken_links")
            # 统计包含已损坏链路的子拓朴数
            reports_num = len(reports.keys())
            # 统计总的子拓朴数
            subtopo_num = len(user_db_cli.get_value(
                "topo2subtopo", self.project_name))
            # 打印输出
            print(f"current report num: {reports_num} of {subtopo_num}")
            
            # 对每个子拓扑，统计链路损坏结果
            broken_summary = {}
            for subtopo, report in reports.items():
                if report["vxlans"]:
                    broken_summary.setdefault(subtopo, report)
            # 若出现链路损坏情况，raise RuntimeError
            if broken_summary:
                raise RuntimeError("vxlan broken! broken list:"
                    f" {broken_summary}")
            # 未出现链路损坏情况，打印输出 “所有链路健康” 的信息
            else:
                print(f"all links are {green_str('healthy')}!")

    def _get_vxlan_detail(self, vxlan_name):
        '''
        获取vxlan详情

        Args:
            vxlan_name: vxlan名称
        
        Returns:
            vxlan_detail: {
                "sourceNE": "源节点名",
                "targetNE": "目的节点名",
                # "VNI": "VNI值", # 感觉用户没必要知道VNI值
            }
        '''
        # 通过读取数据库，获取vxlan的详细信息
        # 包括vxlan两端的网元（源节点、目的节点）
        vxlan_detail = {}
        with redis_context(self.user) as user_db_cli:
            link_name = user_db_cli.get_value(f"{self.project_name}_"
                f"{vxlan_name}", "partof")
            vxlan_detail["sourceNE"] = user_db_cli.get_value(
                f"{self.project_name}_{link_name}", "sourceNE")
            vxlan_detail["targetNE"] = user_db_cli.get_value(
                f"{self.project_name}_{link_name}", "targetNE")
        # 返回
        return vxlan_detail
            

class LinkRecoverMaster:
    def __init__(self) -> None:
        pass

    