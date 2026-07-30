import time
import math
# import multiprocessing
import billiard as multiprocessing

from .redisAPI import UserMapRedis
from .influxAPI import *
from ..webserver.socketio_handlers import push_msg
from ..tools.context import redis_context
from ..tools.context import judge_user_exist
from ..tools.context import check_table_key


class DataServerManager():
    def __init__(self) -> None:
        self.IDENTIFY_WINDOW_MS = PROJ_CONFIG.identify_window_ms
        self.DELAY_SAMPLE_RATE = PROJ_CONFIG.delay_sample_rate
        self.DATA_SERVER_FLASK_PORT = PROJ_CONFIG.data_server_port
        self.DATA_SERVER_IP = PROJ_CONFIG.data_server_ip
        self.INFLUX_DB_PORT = PROJ_CONFIG.influxdb_port
         # 指标计算时批量处理个数
        self.CALC_BATCH_SIZE = 1000
        # 计算间隔(i.e.粒度)
        self.INTERVAL_S = PROJ_CONFIG.interval_s
        # 实验结果绘图数据点数
        self.EXPR_FIGURE_DOT_NUM = PROJ_CONFIG.expr_figure_dot_num
        self.filed_map = {
            "throughput":"throughput_bps",
            "delay":"delay_ns",
            "loss":"pkt_loss_rate",
            "total_throughput":"total_throughput_bits"
            }

    def start_raw_data_calc(self, user, topo, expr):
        '''
        将指定用户的指定实验的原始数据计算为指标数据，并存入时序数据库中

        Args:
            user: 用户名
            topo: 拓扑名
            expr: 实验名
            
        Returns:
            None
        '''
        print("start calculate raw data to performance data")
        print("calculate user: " + user)
        print("calculate expr: " + expr)
        print("IDENTIFY_WINDOW_MS = " + str(self.IDENTIFY_WINDOW_MS))
        print("CALC_BATCH_SIZE = " + str(self.CALC_BATCH_SIZE))
        print("DELAY_SAMPLE_RATE = " + str(self.DELAY_SAMPLE_RATE))
        print("INTERVAL_S = " + str(self.INTERVAL_S))
        start_time = time.time()

        user_db_map = UserMapRedis()
        user_db_cli = user_db_map.get_user_db(user)
        table = '{}_monitor'.format(topo)
        events_to_monitor = user_db_cli.get_value(table, expr)
        user_db_map.close()
        
        for event_seq, event in enumerate(events_to_monitor, 1):
            print(f"deal with event {event_seq}")  
            print("performance = " + event["performance"])

            if event["performance"] == "throughput":
                self._throughput_raw_data_calc(user, topo, expr, event_seq)
            elif event["performance"] == "loss":
                self._loss_raw_data_calc(user, topo, expr, event_seq)
            elif event["performance"] == "delay":
                self._delay_raw_data_calc(user, topo, expr, event_seq)
            elif event["performance"] == "srtt":
                print("srtt don't need to calculate, continue")
            else:
                raise ValueError("don't support this performance.")
        
        end_time = time.time()
        print("calculate done! calculation time: " + str(end_time-start_time))
        
        push_msg("指标数据计算完成！您现在可以下载指标数据。")
        print("push \"calc done\" to front end.")
        
        user_db_cli.close()

    def _throughput_raw_data_calc(self, user, topo, expr, event_seq):
        '''
        当事件的指标为吞吐时，将指定用户的指定实验的原始数据计算为吞吐率和总吞吐量，并
        存入时序数据库中

        Args:
            user: 用户名
            topo: 拓扑名（项目名）
            expr: 实验名
            event_seq: 事件序号
            
        Returns:
            None
        '''
        interval_ns = int(self.INTERVAL_S * 1000000000)
        
        query_sentence = (f"SELECT * FROM \"{user}_{topo}_{expr}_{event_seq}_"
                         "dst_raw_data\"")
        r_json = read_influx(query_sentence)
        if "series" not in r_json["results"][0].keys():
            print(f"The result of [{query_sentence}] is empty. Return.")
            return 1

        start_time = get_rows_of_response(r_json)[0][0]
        total_recv_bytes = 0
        cur_interval_end = interval_ns
        cur_interval_recv_bytes = 0

        cap_src_time_col = get_column_of_key(r_json, "time")
        pkt_payload_col = get_column_of_key(r_json, "payload_size")

        huge_post_data = ""
        q_num = 0
        rows = get_rows_of_response(r_json)
        rows_len = len(rows)

        for i, row in enumerate(rows):
            cap_time = row[cap_src_time_col]
            pkt_payload = row[pkt_payload_col]
            relative_time = cap_time - start_time  # 相对时间(since start_time)

            while relative_time > cur_interval_end:  # 找到属于自己的区间, 
                                                    #区间左闭右开[ )
                print(f"{cur_interval_recv_bytes * 8 / self.INTERVAL_S},"
                      f" {cur_interval_end}")
                q_num += 1
                throughput_bps = cur_interval_recv_bytes * 8 / self.INTERVAL_S
                interval_end_time = start_time+cur_interval_end
                huge_post_data += (f"perf_data,user_name={user},"
                                   f"expr={topo}_{expr},"
                                   f"event_seq={event_seq},perf=\"throughput\""
                                   f" throughput_bps={throughput_bps} "
                                   f"{interval_end_time}\n")

                cur_interval_end += interval_ns  # 超出当前区间，切换至下一区间
                cur_interval_recv_bytes = 0  # 切换了区间，本区间的吞吐量归0
            
            cur_interval_recv_bytes += pkt_payload
            total_recv_bytes += pkt_payload

            if q_num == self.CALC_BATCH_SIZE:
                write_influx(huge_post_data)
                huge_post_data = ""
                q_num = 0
            elif i == rows_len - 1:
                huge_post_data += (f"perf_data,user_name={user},"
                                   f"expr={topo}_{expr},"
                                   f"event_seq={event_seq},"
                                   "perf=total_throughput "
                                   f"total_throughput_bits={total_recv_bytes*8}"
                                )
                write_influx(huge_post_data) 
        
        print( "total throughput = " + str(total_recv_bytes*8/(1024**3)) +
            " Gbytes" )

    def _loss_raw_data_calc(self, user, topo, expr, event_seq):
        '''
        当事件的指标为丢包时，将指定用户的指定实验的原始数据计算为丢包率，并存入时序数
        据库中

        Args:
            user: 用户名
            topo: 拓扑名（项目名）
            expr: 实验名
            event_seq: 事件序号
            
        Returns:
            None
        '''
        query_sentence = (f"SELECT count(*) FROM \"{user}_{topo}_"
                         f"{expr}_{event_seq}_src_raw_data\"")
        r = read_influx(query_sentence)
        if "series" not in r["results"][0].keys():
            print(f"The result of [{query_sentence}] is empty. Return.")
            return 1 

        col = get_column_of_key(r, "count_payload_size")
        src_pkt_num = get_rows_of_response(r)[0][col]
        
        query_sentence = (f"SELECT count(*) FROM \"{user}_{topo}_"
                          f"{expr}_{event_seq}_dst_raw_data\"")
        r = read_influx(query_sentence)
        if "series" not in r["results"][0].keys():
            print(f"The result of [{query_sentence}] is empty. Return.")
            return 1 

        col = get_column_of_key(r, "count_payload_size")
        dst_pkt_num = get_rows_of_response(r)[0][col]
        
        pkt_loss_rate = float(src_pkt_num - dst_pkt_num) / src_pkt_num

        write_influx(f"perf_data,user_name={user},expr={topo}_{expr},"
                     f"event_seq={event_seq},perf=loss"
                     f" pkt_loss_rate={pkt_loss_rate}")

        print( "src_pkt_num = " + str(src_pkt_num))
        print( "dst_pkt_num = " + str(dst_pkt_num))
        print( "pkt_loss_rate = " + str(pkt_loss_rate) )

    def _delay_raw_data_calc(self, user, topo, expr, event_seq):    
        '''
        当事件的指标为时延时，将指定用户的指定实验的原始数据计算为逐包时延，并存入时序
        数据库中

        Args:
            user: 用户名
            topo: 拓扑名（项目名）
            expr: 实验名
            event_seq: 事件序号
            
        Returns:
            None
        '''
        # TODO(MaTie): 这里是直接将该次事件的所有数据拉取至内存，
        #              数据量增大后可能会爆内存
        query_sentence = ("SELECT payload_size,ip_id,frag_offset"
                        f" FROM \"{user}_{topo}_{expr}_{event_seq}_"
                        "src_raw_data\"")
        src_r = read_influx(query_sentence)
        if "series" not in src_r["results"][0].keys():
            print(f"The result of [{query_sentence}] is empty. Return.")
            return 1 
        rows = get_rows_of_response(src_r)
        time_offset = get_column_of_key(src_r, "time")
        ip_id_offset = get_column_of_key(src_r, "ip_id")   
        frag_offset_offset = get_column_of_key(src_r, "frag_offset")

        del src_r # 节约内存

        # 每sample_interval个数据计算一次
        sample_interval = int(self.DELAY_SAMPLE_RATE ** (-1))
        rows_len = len(rows)

        def delay_calc_task(sub_deal_range, total_calc_num):
            batch_size = self.CALC_BATCH_SIZE
            huge_post_data = ""
            huge_q = ""
            q_num = 0
            cur_process_calc_size = 0
            cur_process_continue_num = 0

            for i in sub_deal_range:
                # 查询dst节点在[src_time_ns, src_time_ns + 
                # IDENTIFY_WINDOW_MS*(10**6)]的ip_id和offset相同的数据包信息
                huge_q += ("SELECT \"payload_size\" FROM "
                        f"\"{user}_{topo}_{expr}_{event_seq}_dst_raw_data\" "
                        "WHERE \"ip_id\"=\'" + str(rows[i][ip_id_offset]) + 
                        "\' AND \"frag_offset\"=\'" + 
                        str(rows[i][frag_offset_offset]) + "\' AND \"time\"> "+ 
                        str(rows[i][time_offset]) +" AND \"time\"< " + 
                        str(rows[i][time_offset] + 
                        self.IDENTIFY_WINDOW_MS*(10**6)) + ";")
                q_num += 1

                if q_num == self.CALC_BATCH_SIZE or i == sub_deal_range[-1]:
                    dst_r = read_influx(huge_q)

                    # 处理剩余src点
                    if i == sub_deal_range[-1]:
                        batch_size = q_num
                    
                    total_calc_num.value += batch_size
                    cur_process_calc_size += batch_size

                    for j in range(batch_size):
                        if ("series" not in dst_r["results"][j].keys() or 
                            len(dst_r["results"][j]["series"]) != 1):
                            # 若符合查询条件的数据包不存在或大于1个，
                            # 就忽略本src包的时延计算
                            cur_process_continue_num += 1
                            continue
                        else:
                            # 若符合查询条件的数据包等于1个，
                            # 就按dst_time_ns-src_time_ns来计算时延
                            dst_time_ns = (dst_r["results"][j]["series"]
                                        [0]["values"][0][time_offset])
                            delay_ns = (int(dst_time_ns) - 
                                        rows[i-(batch_size-j-1)*sample_interval]
                                        [time_offset])
                            # 在写入数据库时,最好按字典序排列TAG.https://help.aliyun.com/document_detail/113118.html?spm=a2c4g.11186623.6.730.58357130LcK3Ee
                            huge_post_data += (f"perf_data,event_seq={event_seq},"
                                            f"expr={topo}_{expr},perf=delay,"
                                            f"user_name={user} "
                                            f"delay_ns={delay_ns} {dst_time_ns}"
                                            "\n")
                    write_influx(huge_post_data)
                    huge_post_data = ""
                    huge_q = ""
                    q_num = 0
            print("Process " + str(os.getpid()) + " Done. cur_process_calc_size = "
                + str(cur_process_calc_size) + " cur_process_continue_num = "
                + str(cur_process_continue_num))

        deal_range = range(0, rows_len, sample_interval)
        total_task_size = len(deal_range)
        print("total task size: ", total_task_size)
        # 每个CPU逻辑核分配一部分计算任务
        sub_task_num = int(os.cpu_count()/2)
        sub_size  = int(total_task_size / sub_task_num)

        # 边界情况处理，此情况一般不会出现
        if total_task_size < sub_task_num:
            sub_task_num = 1
            sub_size = total_task_size

        print("sub task num ", sub_task_num)
        print("sub task size: ", sub_size)
        
        # 计算任务切分与创建
        total_calc_num = multiprocessing.Value("I", 0)
        processes = []
        for i in range(sub_task_num):
            sub_deal_range = range(0)
            if i != sub_task_num - 1:
                sub_deal_range = deal_range[i*sub_size:(i+1)*sub_size]
            else:
                sub_deal_range = deal_range[i*sub_size:]
            p = multiprocessing.Process(target=delay_calc_task, 
                                        args=(sub_deal_range,total_calc_num,))
            processes.append(p)
            p.start()
        
        for proc in processes:
            proc.join()
        print("delay calculation Done!")

    def get_expr_figure_data(self, user, project_name, expr, subevent_seqs, perf, figure_type):
        '''
        获取指定子事件的绘图所需数据。

        Args:
            user: 用户名
            project_name: 项目名
            expr: 监控服务名
            subevent_seq: 监控子事件序号
            perf: 性能指标类型
            figure_type: 图表类型
            
        Returns:
            根据性能指标类型的不同而不同
        '''
        if perf == "throughput" or perf == "delay":
            if figure_type == "line":
                return(self.get_expr_line_data(user, project_name, expr, subevent_seqs, perf))
            else:
                raise ValueError("不支持的图表类型！")
        elif perf == "total_throughput" or perf == "loss":
            if figure_type == "histogram":
                return(self.get_expr_histogram_data(
                    user, project_name, expr, subevent_seqs, perf))
            else:
                raise ValueError("不支持的图表类型！")

    def get_expr_histogram_data(self, user, project_name, expr, subevent_seqs, 
        perf):
        '''
        获取指定监控服务的柱状图所需数据。

        Args:
            user: 用户名
            project_name: 项目名
            expr: 监控服务名
            subevent_seqs: 监控子事件序号列表
            perf: 性能指标类型
            
        Returns:
            {
                "datasets": [
                    {
                        "label": 性能指标类型,
                        "data": [
                            子事件1的总吞吐量/丢包率,
                            ...
                        ]
                    }
                ],
                "labels": [
                    "subevent 1",
                    ...
                ]
            }
            
            如：
            {
                "datasets": [
                    {
                        "label": "total_throughput",
                        "data": [
                            321084512
                        ]
                    }
                ],
                "labels": [
                    "subevent 1"
                ]
            }
        '''
        bar = {
                "datasets":[
                    {
                        "label":perf,
                        "data":[]
                    }   
                ],
                "labels":[]
            }
        for subevent_seq in subevent_seqs:
            bar["datasets"][0]["data"].append(self.get_subevent_histogram_data(
                user, project_name, expr, subevent_seq, perf))
            bar["labels"].append(f"subevent {subevent_seq}")
        
        return bar

    
    def get_subevent_histogram_data(self, user, project_name, expr, 
        subevent_seq, perf):
        '''
        获取指定子事件的柱状图所需数据。

        Args:
            user: 用户名
            project_name: 项目名
            expr: 监控服务名
            subevent_seq: 监控子事件序号
            perf: 性能指标类型
            
        Returns:
            data: 总吞吐量/丢包率
        '''
        query_sentence = (f"SELECT * FROM perf_data WHERE "
            f"user_name=\'{user}\' and expr=\'{project_name}_{expr}\' and "
            f"event_seq=\'{subevent_seq}\' and perf=\'{perf}\' LIMIT 1")
        print(query_sentence)
        r_json = read_influx(query_sentence)
        if "series" not in r_json["results"][0].keys():
            raise ValueError(f"The result of [{query_sentence}] is empty.")
        rows = get_rows_of_response(r_json)
        perf_offset = get_column_of_key(r_json, f"{self.filed_map[perf]}")
        data = rows[0][perf_offset]
        
        return data

    def get_expr_line_data(self, user, project_name, expr, subevent_seqs, perf):
        '''
        获取指定监控服务的折线图所需数据。

        Args:
            user: 用户名
            project_name: 项目名
            expr: 监控服务名
            subevent_seqs: 监控子事件序号列表
            perf: 性能指标类型
            
        Returns:
            {
                "datasets": [
                    {
                        "label": 子事件名
                        "data": [
                            [相对时间(ns), 吞吐率(bps)/时延(ns)],
                            ...
                        ]
                    },
                    ...
                ]
            }
            
            如：
            {
                "datasets": [
                    {
                        "label": "subevent 1"
                        "data": [
                            [0, 40573.913043478264], 
                            [21000000000, 39436.19047619047]
                        ]
                    }
                ]
            }
        '''
        datasets = []
        for subevent_seq in subevent_seqs:
            print(subevent_seq)
            dataset = {}
            
            dataset["label"] = f"subevent {subevent_seq}"
            dataset["data"] = self.get_subevent_line_data(
                user, project_name, expr, subevent_seq, perf)
            
            datasets.append(dataset)
        
        return {"datasets": datasets}

    def get_subevent_line_data(self, user, project_name, 
                               expr, subevent_seq, perf):
        '''
        获取指定子事件的折线图所需数据。
        数据的数量取决于self.EXPR_FIGURE_DOT_NUM参数。
        若指标数据个数大于self.EXPR_FIGURE_DOT_NUM，数据是按一定间隔聚合
        （计算一定间隔内数据的平均值）给出。
        若指标数据个数小于self.EXPR_FIGURE_DOT_NUM，数据直接给出

        Args:
            user: 用户名
            project_name: 项目名
            expr: 监控服务名
            subevent_seq: 监控子事件序号
            perf: 要查询数据的性能指标
            
        Returns:
            [
                [相对时间(ns), 吞吐率(bps)/时延(ns)],
                ...
            ]
            如：
            [
                [0, 40573.913043478264], 
                [21000000000, 39436.19047619047]
            ]
        '''
        count = self.get_perf_data_count(user, project_name, expr, 
            subevent_seq, perf)

        if count > self.EXPR_FIGURE_DOT_NUM:
            # 若数据数大于所需数据点数，聚合数据
            print("获取数据方式：聚合")
            rows = self._group_data(user, project_name, expr, 
                subevent_seq, perf)
        else:
            # 若数据数小于所需数据点数，不聚合
            print("获取数据方式：直接")
            query_sentence = (f"SELECT {self.filed_map[perf]} FROM perf_data"
                f" WHERE user_name=\'{user}\' and "
                f"expr=\'{project_name}_{expr}\' and "
                f"event_seq=\'{subevent_seq}\'")

            print(query_sentence)

            r_json = read_influx(query_sentence)
            if "series" not in r_json["results"][0].keys():
                raise(f"The result of [{query_sentence}] is empty.")
            rows = get_rows_of_response(r_json)

        self._handle_figure_data(rows)

        return rows

    def _group_data(self, user, project_name, expr, subevent_seq, perf):
        '''
        以聚合的方式
        数据是按一定间隔聚合（计算一定间隔内数据的平均值）给出。数据的数量取决于
        self.EXPR_FIGURE_DOT_NUM参数。

        Args:
            user: 用户名
            project_name: 项目名
            expr: 监控服务名
            subevent_seq: 监控子事件序号
            perf：指标数据类型
            
        Returns:
            [
                [相对时间(ns), 吞吐率(bps)],
                ...
            ]
            如：
            [
                [0, 40573.913043478264], 
                [21000000000, 39436.19047619047]
            ]
        '''
        # 获取实验的持续时间
        query_sentence = (f"SELECT {self.filed_map[perf]} FROM perf_data WHERE "
            f"user_name=\'{user}\' and expr=\'{project_name}_{expr}\' and "
            f"event_seq=\'{subevent_seq}\' LIMIT 1")
        r_json = read_influx(query_sentence)
        if "series" not in r_json["results"][0].keys():
            raise(f"The result of [{query_sentence}] is empty.")
        rows = get_rows_of_response(r_json)
        time_offset = get_column_of_key(r_json, "time")
        start_time_ns = rows[0][time_offset]

        query_sentence = (f"SELECT {self.filed_map[perf]} FROM perf_data WHERE "
            f"user_name=\'{user}\' and expr=\'{project_name}_{expr}\' and "
            f"event_seq=\'{subevent_seq}\' ORDER BY time DESC LIMIT 1")
        r_json = read_influx(query_sentence)
        if "series" not in r_json["results"][0].keys():
            raise(f"The result of [{query_sentence}] is empty.")
        rows = get_rows_of_response(r_json)
        time_offset = get_column_of_key(r_json, "time")
        end_time_ns = rows[0][time_offset]

        duration_ns = end_time_ns - start_time_ns
        
        # 计算聚合间隔
        group_interval_str = self._calc_group_interval_str(duration_ns)

        query_sentence = (f"SELECT mean({self.filed_map[perf]}) FROM perf_data"
            f" WHERE user_name=\'{user}\' and expr=\'{project_name}_{expr}\' "
            f"and event_seq=\'{subevent_seq}\' and time<={end_time_ns} GROUP BY "
            f"time({group_interval_str}) LIMIT {self.EXPR_FIGURE_DOT_NUM}")
        
        print(query_sentence)

        r_json = read_influx(query_sentence)
        if "series" not in r_json["results"][0].keys():
            raise(f"The result of [{query_sentence}] is empty.")
        rows = get_rows_of_response(r_json)

        print(f"user: {user} topo: {project_name} expr: {expr} "
            "subevent_seq: {subevent_seq}")
        print(f"实验持续时间：{duration_ns/(10**9)}s")
        print(f"聚合间隔：{group_interval_str}")
        print(f"实际数据数：{len(rows)}")

        return rows

    def _calc_group_interval_str(self, duration_ns):
        '''
        根据指标数据的持续时间，选择合适单位的聚合间隔，并返回聚合间隔字符串，
        如39ns

        Args:
            duration_ns: 指标数据的持续时间，单位为ns
            
        Returns:
            group_interval_str: 聚合间隔字符串，如39ns。时间的单位为
                秒(s)/毫秒(ms)/微秒(u)/纳秒(ns)
        '''
        if duration_ns > self.EXPR_FIGURE_DOT_NUM * (10**9):
            group_interval_str = str(math.ceil(
                duration_ns / self.EXPR_FIGURE_DOT_NUM / (10**9))) + "s"
        elif duration_ns > self.EXPR_FIGURE_DOT_NUM * (10**6):
            group_interval_str = str(math.ceil(
                duration_ns / self.EXPR_FIGURE_DOT_NUM / (10**6))) + "ms"
        elif duration_ns > self.EXPR_FIGURE_DOT_NUM * (10**3):
            group_interval_str = str(math.ceil(
                duration_ns / self.EXPR_FIGURE_DOT_NUM / (10**3))) + "u" # us
        else:
            group_interval_str = str(int(
                duration_ns / self.EXPR_FIGURE_DOT_NUM)) + "ns"

        return group_interval_str

    def _handle_figure_data(self, rows):
        '''
        处理聚合完毕的绘图数据，包括计算相对时间(us)，转换None数据为0，格式调整

        Args:
            rows: 聚合完毕的监控数据，如
                [
                    [1620385824420000000, 50496],
                    [1620385824450000000, None]
                ]
            
        Returns:
            group_interval_str: 聚合间隔字符串，如39ns。时间的单位为
                秒(s)/毫秒(ms)/微秒(u)/纳秒(ns)
        '''
        start_time = rows[0][0]

        # 格式调整应该放在服务器上比较好吧...减轻浏览器压力，不过消耗的带宽变多了
        for i in range(len(rows)):
            rows[i] = {
                'x': rows[i][0] - start_time,
                'y': rows[i][1] if rows[i][1] != None else 0
            }

    def get_perf_type(self, user, project_name, expr, subevent_seq) -> str:
        '''
        获取指定子事件的性能指标类型

        Args:
            user: 用户名
            project_name: 项目名
            expr: 监控服务名
            subevent_seq: 监控子事件序号
            
        Returns:
            perf: 指定子事件的性能指标类型
        '''
        query_sentence = (f"SELECT * FROM perf_data WHERE "
            f"user_name=\'{user}\' and expr=\'{project_name}_{expr}\' and "
            f"event_seq=\'{subevent_seq}\' LIMIT 1")
        print(query_sentence)
        r_json = read_influx(query_sentence)
        if "series" not in r_json["results"][0].keys():
            raise ValueError(f"The result of [{query_sentence}] is empty.")
        rows = get_rows_of_response(r_json)
        perf_offset = get_column_of_key(r_json, "perf")
        perf = rows[0][perf_offset]
        
        # 特殊处理一下。当时不知道为什么存的时候\"throughput\"
        perf = "throughput" if perf == "\"throughput\"" else perf

        return perf

    def get_perf_data_count(self, user, project_name, expr, subevent_seq, perf):
        '''
        获取指定子事件的性能指标类型

        Args:
            user: 用户名
            expr: 监控服务名
            subevent_seq: 监控子事件序号
            
        Returns:
            perf: 指定子事件的性能指标类型
        '''
        # 获取指标数据个数
        query_sentence = (f"SELECT count({self.filed_map[perf]}) FROM perf_data "
            f"WHERE user_name=\'{user}\' and expr=\'{project_name}_{expr}\' and "
            f"event_seq=\'{subevent_seq}\'")
        print(query_sentence)
        r_json = read_influx(query_sentence)
        if "series" not in r_json["results"][0].keys():
            raise ValueError(f"The result of [{query_sentence}] is empty.")
        rows = get_rows_of_response(r_json)
        count_offset = get_column_of_key(r_json, f"count")
        count = rows[0][count_offset]
        print(f"指标数据个数：{count}")

        return count

    @staticmethod
    def check_expr_name(user, project_name, expr_name):
        '''
        检查某一监控服务名是否重复。

        TODO(MaTie, 20210518): 监控数据在设计时忘记加了project这一级做区分，因此
            同一用户的不同项目的监控服务名不可以重复。现在加入project这级区分可能
            会影响性能从而影响0527的deadline，因此暂不加。后续考虑使用user,project,
            expr_name等信息做哈希映射，顺便还能提高influx相关的运行速度。

        Args:
            user: 用户名
            project_name: 项目名
            expr: 要检查的监控服务名
            
        Returns:
            0则重复，1则不重复
        '''
        query_sentence = (f"SELECT * FROM perf_data "
            f"WHERE user_name=\'{user}\' and "
            f"expr=\'{project_name}_{expr_name}\' LIMIT 1")
        print(query_sentence)
        r_json = read_influx(query_sentence)
        if "series" not in r_json["results"][0].keys():
            # 不重复
            return 1
        else:
            # 重复
            return 0