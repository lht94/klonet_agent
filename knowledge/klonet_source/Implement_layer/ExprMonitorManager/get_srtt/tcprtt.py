#!/usr/bin/python
# @lint-avoid-python-3-compatibility-imports
#
# tcprtt    Summarize TCP RTT as a histogram. For Linux, uses BCC, eBPF.
#
# USAGE: tcprtt [-h] [-T] [-D] [-m] [-i INTERVAL] [-d DURATION]
#           [-p LPORT] [-P RPORT] [-a LADDR] [-A RADDR] [-b] [-B]
#
# Copyright (c) 2020 zhenwei pi
# Licensed under the Apache License, Version 2.0 (the "License")
#
# 23-AUG-2020  zhenwei pi  Created this.

from __future__ import print_function
from array import array
import ctypes
from sys import path
from bcc import BPF
from time import sleep, strftime
from socket import inet_ntop, AF_INET
import socket, struct
import argparse
import ctypes as ct

from .filewrite import write_file

# arguments
# Dont set the "-i" and "-d" argument when not in sampling mode
examples = """examples:
    ./tcprtt            # summarize TCP RTT
    ./tcprtt -i 1 -d 10 # print 1 second summaries, 10 times
    ./tcprtt -m -T      # summarize in millisecond, and timestamps
    ./tcprtt -p         # filter for local port
    ./tcprtt -P         # filter for remote port
    ./tcprtt -a         # filter for local address
    ./tcprtt -A         # filter for remote address
    ./tcprtt -b         # show sockets histogram by local address
    ./tcprtt -B         # show sockets histogram by remote address
    ./tcprtt -D         # show debug bpf text
    ./tcprtt -S         # set the sampling mode on
    ./tcprtt -e         # set the event name
    ./tcprtt -l         # set the path where the file locates
"""

# need to add a parser, to decide the way keeping the data: histogram or array(sampling)
 
parser = argparse.ArgumentParser(
    description="Summarize TCP RTT as a histogram",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=examples)
parser.add_argument("-i", "--interval",
    help="summary interval, seconds")
parser.add_argument("-d", "--duration", type=int, default=99999,
    help="total duration of trace, seconds")
parser.add_argument("-T", "--timestamp", action="store_true",
    help="include timestamp on output")
parser.add_argument("-m", "--milliseconds", action="store_true",
    help="millisecond histogram")

# parser of FILTER: two ports and two ips
parser.add_argument("-p", "--lport",
    help="filter for local port")
parser.add_argument("-P", "--rport",
    help="filter for remote port")
parser.add_argument("-a", "--laddr",
    help="filter for local address")
parser.add_argument("-A", "--raddr",
    help="filter for remote address")

# "byaddr" is useless for us
# parser.add_argument("-b", "--byladdr", action="store_true",
#     help="show sockets histogram by local address")
# parser.add_argument("-B", "--byraddr", action="store_true",
#     help="show sockets histogram by remote address")
# *** args decide the way of keeping data, when keep the rtt in array, the rtt is sampled
#           per-period ***
parser.add_argument("-S", "--sampling", action="store_true", 
    help="sampling mode, get the rtt per-period")

parser.add_argument("-D", "--debug", action="store_true",
    help="print BPF program before starting (for debugging purposes)")
parser.add_argument("--ebpf", action="store_true",
    help=argparse.SUPPRESS)

# parsers for monitor file
parser.add_argument("-e", "--event",
    help="event name")
parser.add_argument("-l", "--locate",
    help="path for files to locate")

args = parser.parse_args()
if not args.interval:
    args.interval = args.duration

# define BPF program
# for avoiding the float-number computing, the srtt is 8 times of the real sRTT,
#       so srtt = ts->srtt_us 
bpf_text = """
#ifndef KBUILD_MODNAME
#define KBUILD_MODNAME "bcc"
#endif
#include <uapi/linux/ptrace.h>
#include <linux/tcp.h>
#include <net/sock.h>
#include <net/inet_sock.h>
#include <bcc/proto.h>

typedef struct sock_key {
    u64 addr;
    u64 slot;
} sock_key_t;

STORAGE

int trace_tcp_rcv(struct pt_regs *ctx, struct sock *sk, struct sk_buff *skb)
{
    struct tcp_sock *ts = tcp_sk(sk);
    u32 srtt = ts->srtt_us >> 3;
    const struct inet_sock *inet = inet_sk(sk);
    u16 sport = 0;
    u16 dport = 0;
    u32 saddr = 0;
    u32 daddr = 0;

    bpf_probe_read_kernel(&sport, sizeof(sport), (void *)&inet->inet_sport);
    bpf_probe_read_kernel(&dport, sizeof(dport), (void *)&inet->inet_dport);
    bpf_probe_read_kernel(&saddr, sizeof(saddr), (void *)&inet->inet_saddr);
    bpf_probe_read_kernel(&daddr, sizeof(daddr), (void *)&inet->inet_daddr);

    LPORTFILTER
    RPORTFILTER
    LADDRFILTER
    RADDRFILTER

    FACTOR

    STORE

    return 0;
}
"""


# *** FILTER: codes below define the filters for the tcp_connection, including IPs ***
# ***         and ports                                                            ***

# filter for local port
if args.lport:
    bpf_text = bpf_text.replace(b'LPORTFILTER',
        b"""if (ntohs(sport) != %d)
        return 0;""" % int(args.lport))
else:
    bpf_text = bpf_text.replace(b'LPORTFILTER', b'')

# filter for remote port
if args.rport:
    bpf_text = bpf_text.replace(b'RPORTFILTER',
        b"""if (ntohs(dport) != %d)
        return 0;""" % int(args.rport))
else:
    bpf_text = bpf_text.replace(b'RPORTFILTER', b'')

# filter for local address
if args.laddr:
    bpf_text = bpf_text.replace(b'LADDRFILTER',
        b"""if (saddr != %d)
        return 0;""" % struct.unpack("=I", socket.inet_aton(args.laddr))[0])
else:
    bpf_text = bpf_text.replace(b'LADDRFILTER', b'')

# filter for remote address
if args.raddr:
    bpf_text = bpf_text.replace(b'RADDRFILTER',
        b"""if (daddr != %d)
        return 0;""" % struct.unpack("=I", socket.inet_aton(args.raddr))[0])
else:
    bpf_text = bpf_text.replace(b'RADDRFILTER', b'')


# show msecs or usecs[default]
if args.milliseconds:
    bpf_text = bpf_text.replace('FACTOR', 'srtt /= 1000;')
    label = "msecs"
else:
    bpf_text = bpf_text.replace('FACTOR', '')
    label = "usecs"

print_header = "srtt"
# event name and path analysis
event = args.event
path = args.locate

# first, decide using the sample-mode or histogram for exhibit the rtt
if args.sampling:
    bpf_text = bpf_text.replace('STORAGE', 'BPF_ARRAY(samp_srtt, u32, 1);')
    bpf_text = bpf_text.replace('STORE',
            b"""int key = 0;
        u32 ud = 5;
        u32 * val;
        val = samp_srtt.lookup(&key);
        if(val)
        {
            *val = srtt;
        }
        else
        {
            return 0;
        }
        """)
# else:
#     # show byladdr/byraddr histogram
#     if args.byladdr:
#         bpf_text = bpf_text.replace('STORAGE',
#             'BPF_HISTOGRAM(hist_srtt, sock_key_t);')
#         bpf_text = bpf_text.replace('STORE',
#             b"""sock_key_t key;
#         key.addr = saddr;
#         key.slot = bpf_log2l(srtt);
#         hist_srtt.increment(key);""")
#         print_header = "Local Address: "
#     elif args.byraddr:
#         bpf_text = bpf_text.replace('STORAGE',
#             'BPF_HISTOGRAM(hist_srtt, sock_key_t);')
#         bpf_text = bpf_text.replace('STORE',
#             b"""sock_key_t key;
#         key.addr = daddr;
#         key.slot = bpf_log2l(srtt);
#         hist_srtt.increment(key);""")
#         print_header = "Remote Address: "
else:
    bpf_text = bpf_text.replace('STORAGE', 'BPF_HISTOGRAM(hist_srtt);')
    bpf_text = bpf_text.replace('STORE', 'hist_srtt.increment(bpf_log2l(srtt));')

# debug/dump ebpf enable or not
if args.debug or args.ebpf:
    print(bpf_text)
    if args.ebpf:
        exit()

# load BPF program
b = BPF(text=bpf_text)
b.attach_kprobe(event="tcp_rcv_established", fn_name="trace_tcp_rcv")

print("Tracing TCP RTT... Hit Ctrl-C to end.")


# the addr data in the struct of sock is network sequence, and this func
#       first, trans this data into byte flow
#       second, trans this data from network sequence to dotted-decimal format
#       third, encode the str to bytes flow ?

# def print_section(addr):
#     if args.byladdr:
#         return inet_ntop(AF_INET, struct.pack("I", addr)).encode()
#     elif args.byraddr:
#         return inet_ntop(AF_INET, struct.pack("I", addr)).encode()



# output
exiting = 0 if args.interval else 1
if args.sampling:
    # get the array from kernel function
    samp = b.get_table("samp_srtt")
else:
    dist = b.get_table("hist_srtt")

seconds = 0
# write-file object declare
w = write_file(path)
if args.sampling:
    w.write_samp_title()
i = 0
while (1):
    try:
        sleep(int(args.interval))
        seconds = seconds + int(args.interval)
        i += float(args.interval)
    except KeyboardInterrupt:
        exiting = 1

    # print()
    # print the timestamp, useless!
    # if args.timestamp:
    #     print("%-8s\n" % strftime("%H:%M:%S"), end="")
    
    # print log2_histogram, but this is useless. 
    # dist.print_log2_hist(label, section_header=print_header, section_print_fn=print_section)
    # dist.clear()

    # extract the distribution data from histogram to a file

    # new a array to save the value of each log2_index, the max log2_index is 65,
    #       fit for the 64-bit integer 

    if args.sampling:
        # samp[0] = ct.c_uint32(5)
        v = samp[0]
        if v and not exiting:
            w.write_samp(v.value, i)

    else:
        vals = [0] * 65
        for k, v in dist.items():
            vals[k.value] = v.value

        w.write_hist(vals)
        dist.clear()

    if exiting or seconds >= args.duration:
        exit()

w.close()