#ifndef _PCAP_H_
#define _PACP_H_

#include <pcap.h>
#include <stdbool.h>
#include <signal.h>
#include <curl/curl.h>
#include <string.h>

#define MAXBYTES2CAPTURE 2048
#define ETHER_ADDR_LEN  6 
#define ETHER_TYPE_LEN  2
#define ETHER_HDR_LEN   \
	(ETHER_ADDR_LEN * 2 + ETHER_TYPE_LEN)
#define IP_HDR_LEN 20
#define TCP_HDR_LEN 20
#define UDP_HDR_LEN 8
#define IPtuple5_LEN 13
#define STAT_LEN 2

#define BUFFER_SIZE 4*1024*1024 // 4GB, libpcapdefault: 2GB
#define TIMEOUT 50 // ms

#define TOTAL_LEN_OFFSET   \
    ETHER_HDR_LEN + 2
#define ID_OFFSET   \
    ETHER_HDR_LEN + 4
#define FRAG_OFFSET_OFFSET   \
    ETHER_HDR_LEN + 6
#define PROTO_OFFSET   \
    ETHER_HDR_LEN + 9

#define TCP_PROTO 6
#define UDP_PROTO 17

/* define the ether header. */
typedef struct ethhdr
{
    /* data */
    u_int8_t s_addr[ETHER_ADDR_LEN];
    u_int8_t d_addr[ETHER_ADDR_LEN];     
    u_int16_t ether_type;
}ethhdr_t;

// !!! 这得出的结果其实并不对，有大端和小端的问题
/* define the ip header. */
typedef struct iphdr
{
    /* data */
    u_int8_t version_ihl;      /* version and header length. */
    u_int8_t type_of_service;  /* ip service type. */
    u_int16_t total_length;    /* length of packet. */
    u_int16_t packet_id;       /* ID of packet. */
    u_int16_t fragment_offset;        
    u_int8_t time_to_live;
    u_int8_t proto_id;         /* ID of protocol. */
    u_int16_t hdr_checksum;    /* header checksum. */
    u_int32_t src_addr;        /* source address. */
    u_int32_t dst_addr;        /* destination address. */
}iphdr_t;

/*define the tcp header. */
typedef struct tcphdr
{
    /* data */
    u_int16_t src_port;  /* TCP source port. */
	u_int16_t dst_port;  /* TCP destination port. */
	u_int32_t sent_seq;  /* TX data sequence number. */
	u_int32_t recv_ack;  /* RX data acknowledgement sequence number. */
	u_int8_t  data_off;  /* Data offset. */
	u_int8_t  tcp_flags; /* TCP flags */
	u_int16_t rx_win;    /* RX flow control window. */
	u_int16_t cksum;     /* TCP checksum. */
	u_int16_t tcp_urp;   /* TCP urgent pointer, if any. */
}tcphdr_t;

/* define the udp header. */
typedef struct udphdr
{
    /* data */
    u_int16_t src_port;    /* UDP source port. */
	u_int16_t dst_port;    /* UDP destination port. */
	u_int16_t dgram_len;   /* UDP datagram length. */
	u_int16_t dgram_cksum; /* UDP datagram checksum. */
}udphdr_t;

/* IP "5" tuple: src-dst addr, src-dst port, IP_proto_id. */
typedef struct IPtuple5
{
    /* data */
    u_int32_t src_addr;      /* source address. */
    u_int32_t dst_addr;      /* destination address. */
    u_int16_t src_port;      /* source port. */
	u_int16_t dst_port;      /* destination port. */
    u_int8_t proto_id;       /* IP protocl ID. */
}IPtuple5_t;

typedef struct statistic
{
    u_int16_t ip_total_length;    /* length of ip packet. */
}stat_t;


/* Define the hdrs of packets. */
extern ethhdr_t *eth_hdr;
extern iphdr_t *ip_hdr;
extern tcphdr_t *tcp_hdr;
extern udphdr_t *udp_hdr;

/* Malloc the space for IP-tuple. */
extern IPtuple5_t *ipTuple;
extern stat_t *stat;

// extern CURL *curl;
// extern CURLcode res;


/* trans int 2 ipaddress. */
void
int2ip(u_int32_t ipaddr, u_int8_t *ipv4addr);

/* Call back function, that process the packet received */
void
process_packet(unsigned char *arg, const struct pcap_pkthdr *packet_header, const unsigned char *packet_content);

/* Func to start the sniff */
void
start_sniff(const char *nic, unsigned char *arg, const char *filter, const char *file_name);

static void
signal_handler(int signum);


#endif