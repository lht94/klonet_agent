#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pcap.h>
#include <netinet/in.h>
#include "packet_capprocess.h"


pcap_t *descr;
struct pcap_stat *ps; // ps_recv, ps_drop(由于操作系统buffer满而丢的包), ps_ifdrop(被网卡或网络设备丢的包)
static volatile bool force_quit;
pcap_dumper_t* out_file; 
int captured_pkt_count = 0;

/* Define the hdrs of packets. */
ethhdr_t *eth_hdr;
iphdr_t *ip_hdr;
tcphdr_t *tcp_hdr;
udphdr_t *udp_hdr;
IPtuple5_t *ipTuple;
stat_t *stat;

uint16_t *p_total_length; 

/* func to capture the signal from keyboard. */
static void
signal_handler(int signum)
{
	if (signum == SIGINT || signum == SIGTERM || signum == SIGUSR1 ) {
		printf("\n\nSignal %d received, preparing to exit...\n",
				signum);
        force_quit = true;

        //pcap_stats(descr, ps);
		pcap_breakloop(descr);
	}
}


void
int2ip(u_int32_t ipaddr, u_int8_t *ipv4addr)
{
    /* ipv4 addr: a.b.c.d. */
    u_int8_t a = 0;
    u_int8_t b = 0;
    u_int8_t c = 0;
    u_int8_t d = 0;
    
    a = (ipaddr & 0xff000000) >> 24;
    b = (ipaddr & 0x00ff0000) >> 16;
    c = (ipaddr & 0x0000ff00) >> 8;
    d = (ipaddr & 0x000000ff);
    ipv4addr[0] = a;
    ipv4addr[1] = b;
    ipv4addr[2] = c;
    ipv4addr[3] = d;
}

// 这里的参数格式必须按这么写，否则pcap_loop不不知道这个函数怎么用
// pkthdr包含着被捕捉的数据包的相关信息
// packet_content为数据包内容（暂时不知道包不包含头部）
void
process_packet(u_char *arg, const struct pcap_pkthdr *pkthdr, const u_char *packet_content)
{
    pcap_dump((u_char*)out_file, pkthdr, packet_content);
    captured_pkt_count++;


    // stat->ip_total_length = *( (uint16_t*)(packet_content + TOTAL_LEN_OFFSET) );
    // char d[50];
    // sprintf(d, "stat value=%hu", stat->ip_total_length);
           
    // curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long) strlen(d));
    // curl_easy_setopt(curl, CURLOPT_POSTFIELDS, d);

    // res = curl_easy_perform(curl);
    // /* Check for errors */ 
    // if(res != CURLE_OK){
    //     fprintf(stderr, "curl_easy_perform() failed: %s\n", curl_easy_strerror(res));
    // }

    // pcap_breakloop(descr);
    
    // printf("%hd ", ntohs(stat->ip_total_length));


    // int i = 0;
    // /* Trans the type of arg to int */
    // int *counter = (int *)arg;

    // printf("Packet_Count: %d\n", ++(*counter));        /* should be recorded. */
    // printf("Packet length : %d\n", pkthdr->len);
    // printf("Number of bytes : %d\n", pkthdr->caplen);
    // printf("Time capture the packet: %ld.%ld\n", pkthdr->ts.tv_sec, pkthdr->ts.tv_usec);    /* should be recorded. */
    
    // /* IP tuples info. */
    // eth_hdr = (ethhdr_t *)packet_content;
    // u_int16_t ethType = 0;
    // ethType = ntohs(eth_hdr->ether_type);
    // printf("Ethernet type is: 0x%04x\n", ethType);

    // if (ethType == 0x0800) {
    //     printf("IPv4 is used.\n");
    //     printf("\n");

    //     pcap_dump((u_char*)out_file, pkthdr, packet_content);

    //     /* Record the metrics of IP. */
    //     ip_hdr = (iphdr_t *) (packet_content + ETHER_HDR_LEN); // 指针+地址，相当于得到拆掉头部后的数据包地址
    //     ipTuple->src_addr = ip_hdr->src_addr;      /* should be recorded. */
    //     ipTuple->dst_addr = ip_hdr->dst_addr;      /* should be recorded. */
    //     ipTuple->proto_id = ip_hdr->proto_id;      /* should be recorded. */

    //     /* Print the info of IP. */
    //     u_int8_t ipv4addr[4];      /* array statement. */
    //     int2ip(ipTuple->src_addr, ipv4addr);
    //     printf("Source ip_address is : %d.%d.%d.%d\n",ipv4addr[3],ipv4addr[2],ipv4addr[1],ipv4addr[0]);
    //     int2ip(ipTuple->dst_addr, ipv4addr);
    //     printf("Destination ip_address is : %d.%d.%d.%d\n",ipv4addr[3],ipv4addr[2],ipv4addr[1],ipv4addr[0]);
    //     printf("Protocol ID is: %u\n", ipTuple->proto_id);

    //     /* Process the trans-layer protocol. */
    //     if (ip_hdr->proto_id == 6) {
    //         printf("TCP is used.\n");
    //         tcp_hdr = (tcphdr_t *) (packet_content + ETHER_HDR_LEN + IP_HDR_LEN);
    //         ipTuple->src_port = ntohs(tcp_hdr->src_port);      /* should be recorded. */
    //         ipTuple->dst_port = ntohs(tcp_hdr->dst_port);      /* should be recorded. */
    //         printf("Source port is: %u\n", ipTuple->src_port);
    //         printf("Destination port is: %u\n", ipTuple->dst_port);
    //     }

    //     else if (ip_hdr->proto_id == 17) {
    //         printf("UDP is used.\n");
    //         udp_hdr = (udphdr_t *) (packet_content + ETHER_HDR_LEN + IP_HDR_LEN);
    //         ipTuple->src_port = ntohs(udp_hdr->src_port);      /* should be recorded. */
    //         ipTuple->dst_port = ntohs(udp_hdr->dst_port);      /* should be recorded. */
    //         printf("Source port is: %u\n", ipTuple->src_port);
    //         printf("Destination port is: %u\n", ipTuple->dst_port);
    //     }

    //     else {
    //         printf("Other trans-layer protocol is used\n");
    //         ipTuple->dst_port = 0;
    //         ipTuple->src_port = 0;
    //     }    
    // }

    // printf("\n\n");
}

void
start_sniff(const char *nic, unsigned char *arg, const char *filter, const char *file_name)
{
    descr = NULL;
    char errbuf[PCAP_ERRBUF_SIZE]; // 至少要分配PCAP_ERRBUF_SIZE大小
    const char *device = nic;
    memset(errbuf, 0, PCAP_ERRBUF_SIZE);
    ps = (struct pcap_stat *) malloc(12);
    struct bpf_program fp;


    if (device == NULL) {
        fprintf(stderr, "There is no device designated.\n");
        exit(2); 
    }
    
    printf("Opening device: %s\n", device);

    /* Open the device in promiscuous mode */
    descr = pcap_create(device, errbuf);
    pcap_set_snaplen(descr, ETHER_HDR_LEN + IP_HDR_LEN + TCP_HDR_LEN);// 设置捕捉长度
    pcap_set_promisc(descr, 1);// 开启混杂模式   
    pcap_set_buffer_size(descr, BUFFER_SIZE);
    pcap_set_timeout(descr, TIMEOUT);
    int e;
    if( (e = pcap_activate(descr) ) != 0){
        fprintf(stderr, "active: %s\n",pcap_statustostr(e));
    }

    //pcap_setdirection(descr, PCAP_D_IN);// 仅捕捉流入设备的流量
    
    // 返回一个pcap_t的interface handler
    // 第一个参数为要打开的网卡名
    // 第二个参数为要捕捉的字节数
    // 第三个参数为是否开启混杂模式（即网卡是否接收目的地址不是自己的数据包）
    // 第四个参数为将信息从内核空间拷贝到用户空间前内核等待多少毫秒（设定时似乎没什么套路）
    //descr = pcap_open_live(device, MAXBYTES2CAPTURE, 1, 512, errbuf); // 旧方式
    
    if (descr == NULL) {
        fprintf(stderr, "Couldn't open device: %s\n", errbuf);
        exit(2);
    }

    force_quit = false;
    signal(SIGINT, signal_handler);
	signal(SIGTERM, signal_handler);
    signal(SIGUSR1, signal_handler);

    /* 将结果保存至文件 */
    out_file = pcap_dump_open(descr, file_name);

    /* Lets try and compile the program.. non-optimized */
    if(pcap_compile(descr, &fp, filter, 0, PCAP_NETMASK_UNKNOWN ) == -1){ 
        fprintf(stderr, "Error calling pcap_compile\n"); 
        fprintf(stderr, "%s\n", pcap_geterr(descr));
        fprintf(stderr, "%s\n", filter);
        exit(1); 
    }
    /* set the compiled program as the filter */
    if(pcap_setfilter(descr, &fp) == -1){ 
        fprintf(stderr,"Error setting filter\n"); 
        exit(1); 
    }

    /* Packets capture loop, and process the packet. */
    // -1表示一直抓包，只有出现error时才会return
    // 每当有数据包可以被读取时，将会调用这里的process_packet函数
    printf("Start Sniffing...");

    // FILE *flag_file = NULL;
    // flag_file = fopen("start.txt", "w+");
    // fputc(1, flag_file);
    // fclose(flag_file);


    if (pcap_loop(descr, -1, process_packet, arg) < 0) { // 如果pcap_loop被错误或pcap_breakloop打断，则返回-1

        if (force_quit) {
            printf("Capture loop has been stopped by keyboard signal.\n");
            pcap_stats(descr, ps); // 获取统计数据
            printf("ps_recv: %u\n", ps->ps_recv);
            printf("ps_drop: %u\n", ps->ps_drop);
            printf("ps_ifdrop: %u\n", ps->ps_ifdrop);
            printf("captured_pkt_count: %d\n", captured_pkt_count);

            // FILE *flag_file = NULL;
            // flag_file = fopen("./finish1.txt", "w+");
            // fputc(1, flag_file);
            // fclose(flag_file);
        }
        else
        {
            printf("captured_pkt_count: %d\n", captured_pkt_count);
            fprintf(stderr, "Capture loop is over.\n");


            // FILE *flag_file = NULL;
            // flag_file = fopen("finish2.txt", "w+");
            // fputc(1, flag_file);
            // fclose(flag_file);

            exit(2);
        }        
    } 

    /* Close the handle. */
    pcap_close(descr);

    // pcap_stats(descr, ps);
    // printf("ps_recv: %u\n", ps->ps_recv);
    // printf("ps_drop: %u\n", ps->ps_drop);
    // printf("ps_ifdrop: %u\n", ps->ps_ifdrop);

}


