import os
import time


class MySnow:

    def __init__(self):
        self.start = int(time.mktime(time.strptime('2020-04-20 00:00:00', "%Y-%m-%d %H:%M:%S")))
        self.last = int(time.time())
        self.countID = 0
        self.array = ["a", "b", "c", "d", "e", "f",
                      "g", "h", "i", "j", "k", "l", "m", "n", "o", "p", "q", "r", "s",
                      "t", "u", "v", "0", "1", "2", "3", "4", "5",
                      "6", "7", "8", "9"]

    def get_id(self):

        # 时间差部分
        now = int(time.time())
        temp = now - self.start
        if len(str(temp)) < 8:  # 时间差不够13位的在前面补0
            length = len(str(temp))
            s = "0" * (8 - length)
            temp = s + str(temp)
        if now == self.last:
            self.countID += 1  # 同一时间差，序列号自增
        else:
            self.countID = 0  # 不同时间差，序列号重新置为0
            self.last = now
        # 进程号部分
        pid = str(os.getpid())
        if len(pid) < 5:
            length = len(pid)
            s = "0" * (5 - length)
            pid = s + pid
        # 自增序列号部分
        if self.countID == 999:  # 序列号自增3位满了，睡眠一秒钟
            time.sleep(1)
        countIDdata = str(self.countID)
        if len(countIDdata) < 3:  # 序列号不够3位的在前面补0
            length = len(countIDdata)
            s = "0" * (3 - length)
            countIDdata = s + countIDdata
        id = str(temp) + pid + countIDdata
        id_bit = bin(int(id, 10))
        id_bit = str(id_bit)[2:]
        # 补齐55位2进制数
        if len(str(id_bit)) < 55:
            length = len(str(id_bit))
            s = "0" * (55 - length)
            id_bit = s + str(id_bit)
        id = []
        # 5位->1个字符
        for i in range(0, 11):
            start = i * 5
            end = i * 5 + 5
            val = int(id_bit[start:end], 2)
            id.append(self.array[val % 32])
        return "".join(id)


class SnowFlake(object):
    """
    支持同一主机下的并发, 需考虑进程号和worker id？(若只有一个master, 则不需要考虑)
    # 因为只在master上使用，故物理机制有一台
    # 号码组成为 64bit  1(标识号) + 41(时间戳) + 16(worker 进程号) + 6(顺序标识)
    """
    precision = 1e3
    worker_id_bits = 16
    sequence_bits = 6
    sequence_mask = 2 ** sequence_bits - 1
    flag = 0

    def __init__(self):
        # self.start_time = self.init_time
        self.last = int(time.time() * self.precision)
        self.count = 0

    def get_id(self):
        timestamp_bits = self._get_timestamp()  # 获得时间戳 (41位)
        worker_id_bits = self._get_worker_id()  # 获得进程号 (16位)
        count_bits = self._align_bits(self.sequence_bits, bin(self.count)[2:])  # 自增部分 (6位)
        id_bits = str(self.flag) + timestamp_bits + worker_id_bits + count_bits  # 二进制字符串 (64位)，转变为十六进制输出 (16位)
        return hex(int(id_bits, 2))[2:]

    def _get_timestamp(self):
        now = int(time.time() * self.precision)  # 毫秒时间戳
        assert now >= self.last, "时钟回拨"       # 现在的时间不可以比上次的时间小
        if now == self.last:     # 如果两次时间相同，count 自增
            self.count = (self.count + 1) & self.sequence_mask
            # 到达序列的最大号了
            if self.count == 0:
                time.sleep(0.1)
                now = int(time.time() * self.precision)
                self.last = now
        else:
            self.count = 0
            self.last = now
        return bin(now)[2:]

    @staticmethod
    def _align_bits(bit_length, bin_str):
        # 高位补0, 长度对齐为self.timestamp_bits
        return '0' * (bit_length - len(bin_str)) + bin_str

    def _get_worker_id(self):
        worker_pid = os.getpid()
        return self._align_bits(self.worker_id_bits, bin(worker_pid)[2:])

class SnowFlakekvm(object):
    """
    # 因为只在master上使用，故物理机制有一台
    # 号码组成为 64bit  1(标识号) + 33(时间戳) + 10(顺序标识)
    """
    time_bits = 33
    sequence_bits = 10
    sequence_mask = 2 ** sequence_bits - 1
    flag = 1

    def __init__(self):
        # self.start_time = self.init_time
        self.last = int(time.time())
        self.count = 0

    def get_id(self):
        timestamp_bits = self._get_timestamp()  # 获得时间戳 (31位)
        time_stamp = self._align_bits(self.time_bits, timestamp_bits[2:]) #高位补0，时间戳成为33位
        count_bits = self._align_bits(self.sequence_bits, bin(self.count)[2:])  # 自增部分 (10位)
        id_bits = str(self.flag) + time_stamp + count_bits  # 二进制字符串 (44位)，转变为十六进制输出 (11位)
        return hex(int(id_bits, 2))[2:]

    def _get_timestamp(self):
        now = int(time.time())  # 毫秒时间戳
        assert now >= self.last, "时钟回拨"       # 现在的时间不可以比上次的时间小
        if now == self.last:     # 如果两次时间相同，count 自增
            self.count = (self.count + 1) & self.sequence_mask
            # 到达序列的最大号了
            if self.count == 0:
                time.sleep(1)
                now = int(time.time())
                self.last = now
        else:
            self.count = 0
            self.last = now
        return bin(now)[2:]

    @staticmethod
    def _align_bits(bit_length, bin_str):
        # 高位补0, 长度对齐为self.timestamp_bits
        return '0' * (bit_length - len(bin_str)) + bin_str


if __name__ == '__main__':
    snow = SnowFlake()
    times = 100
    li = []
    for _ in range(times):
        li.append(snow.get_id())
    print(len(set(li)) == times)
