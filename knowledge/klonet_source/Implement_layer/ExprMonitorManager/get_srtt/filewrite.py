# compating python2.7

import csv

class write_file():

    def __init__(self, file_name):
        self._f_ = open(file_name, 'w')
        self._csv_writer_ = csv.writer(self._f_)

    def write_hist(self, vals):
        ''' 
        write the number of each log2_index, for future use of drawing histogram.
        para1: name of a monitor event;
        para2: name of the directory
        para3: the values to write, whose type is list
        '''
        idx_min = -1
        idx_max = -1 # how many indexs used in vals
        flag = 1 # flag denotes the first index not "0"
        
        # construct the head of the table
        self._csv_writer_.writerow(["log2_index_low/us", "log2_index_high/us", "counts"])

        for i, v in enumerate(vals):
            if flag and v > 0:
                flag = 0
                idx_min = i
            if v > 0: idx_max = i
        
        for i in range(idx_min, idx_max + 1):
            low = (1<<i) >> 1
            high = (1<<i) - 1
            if (low == high):
                low -= 1
            val = vals[i]

            self._csv_writer_.writerow([low, high, val])

    def write_samp_title(self):
        # self._csv_writer_.writerow(["time/s", "srtt"])
        pass  # don't need title

    def write_samp(self, val, time):
        '''
        annotation
        '''

        # self._csv_writer_.writerow(["time/s", "srtt"])
        self._csv_writer_.writerow([time, val])

    def close(self):
        self._csv_writer_.close()
