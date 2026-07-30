/* gcc -o analyse_pcap analyse_pcap.c -lpcap */
#include <stdio.h>
#include <pcap.h>
#include <stdlib.h>
#include "packet_capprocess.h"

#define BUF_DATA_NUM 3000
#define ROW_DATA_SIZE 300

unsigned long recv_bytes=0;
u_int64_t last_cap_time=0;
int duplicate_num=0;

pcap_t *handle; 
CURL *curl;
CURLcode res;

char* USER_DATA_DIR;
char* USER_NAME;
char* TOPO_NAME;
char* EXPR_NAME;
char* EVENT_SEQ;
char* PERF;
char* NODE_TYPE;
char* PCAP_FILE_NAME;

int buf_i = 0;
// TODO: 用结构体代替
u_int64_t cap_times[BUF_DATA_NUM];
u_int16_t ip_ids[BUF_DATA_NUM];
u_int16_t payload_sizes[BUF_DATA_NUM];
u_int16_t frag_offsets[BUF_DATA_NUM];

char row_data[ROW_DATA_SIZE];
char influx_data[BUF_DATA_NUM*ROW_DATA_SIZE];

void save_buf_data_to_db() {
    long response_code = 0;

    /* 存储剩余数据 */
    // printf("buffer has %d points, save!\n", buf_i);
    for(int i = 0; i < buf_i; i++){
        sprintf(row_data, "%s_%s_%s_%s_%s_raw_data,frag_offset=%hu,ip_id=%hu,perf=%s payload_size=%hu %ld\n", USER_NAME, TOPO_NAME, EXPR_NAME, EVENT_SEQ, NODE_TYPE, frag_offsets[i], ip_ids[i], PERF, payload_sizes[i], cap_times[i]);
        strcat(influx_data, row_data);
    }
    
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, influx_data);
    
    res = curl_easy_perform(curl);
    if (res != CURLE_OK) {
        fprintf(stderr, "curl_easy_perform error, error code: %d", res);
        exit(1);
    }

    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &response_code);
    // 若响应码非204，则出错（curl_easy_setopt会将错误打印至标准输出）
    if (response_code != 204){
        curl_easy_cleanup(curl);
        pcap_close(handle);
        exit(1);
    }
}

int main(int argc, char **argv) {

    if (argc != 12) { 
   	    fprintf(stderr, "Usage: %s <DB_IP> <DB_PORT> <DB_NAME> <USER_DATA_DIR> <USER_NAME> <TOPO_NAME> <EXPR_NAME> <EVENT_SEQ> <PERF> <NODE_TYPE> <PCAP_FILE_NAME>\n", argv[0]); 
        exit(1);
    }

    /* 读取输入参数（其实只是想换个名字，否则argv太抽象，用起来不方便）*/
    char* db_ip = argv[1];
    char* db_port = argv[2];
    char* db_name = argv[3];
    USER_DATA_DIR = argv[4];
    USER_NAME = argv[5];
    TOPO_NAME = argv[6];
    EXPR_NAME = argv[7];
    EVENT_SEQ = argv[8];
    PERF = argv[9];
    NODE_TYPE = argv[10];
    PCAP_FILE_NAME = argv[11];
    

    char pcap_file_name_with_dir[300];
    sprintf(pcap_file_name_with_dir, "%s/%s/%s/%s/%s/%s", USER_DATA_DIR, USER_NAME, TOPO_NAME, EXPR_NAME, EVENT_SEQ, PCAP_FILE_NAME);

    // 设置要写入的数据库

    char db_write_url[50];
    sprintf(db_write_url, "http://%s:%s/write?db=%s", db_ip, db_port, db_name);

    char errbuf[PCAP_ERRBUF_SIZE];  
    handle = pcap_open_offline(pcap_file_name_with_dir, errbuf); 

    if (handle == NULL) { 
        fprintf(stderr,"Couldn't open pcap file %s: %s\n", pcap_file_name_with_dir, errbuf); 
        return(1); 
    }

    curl = curl_easy_init();

    if(curl){
        /* start to capture packets. */
        curl_easy_setopt(curl, CURLOPT_URL, db_write_url);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, BUF_DATA_NUM*ROW_DATA_SIZE);
        if (pcap_loop(handle, 0, process_packet, NULL) < 0) { // 读取完成后会退出循环
            fprintf(stderr, "\npcap_loop() failed: %s\n", pcap_geterr(handle));
            exit(1);
        }

        save_buf_data_to_db();

        curl_easy_cleanup(curl);
    }
    else{
        printf("failed to create curl.");
    }

    pcap_close(handle);
    return 0;
}

/**
 * 输入：由pcap_loop传来的arg参数，指向数据包时间戳和长度的pcap_pkthdr型指针，指向数据包的前caplen(抓包时定义的参数)个字节的指针packet_content
 * 输出：无
 * 功能描述：本函数作为pcap_loop的回调函数，每次从.pcap文件中读取到一个数据包时就会调用本函数。然后将该数据包的原始数据发送至数据库
**/
void process_packet(u_char *arg, const struct pcap_pkthdr *pkthdr, const u_char *packet_content)
{
    uint8_t proto_type = ntohs(*((uint8_t*)(packet_content + PROTO_OFFSET)));
    uint8_t proto_hdr_len;
    if(proto_type == TCP_PROTO){
        proto_hdr_len = TCP_HDR_LEN;
    }
    else if(proto_type == UDP_PROTO){
        proto_hdr_len = UDP_HDR_LEN;
    }
    else{
        proto_hdr_len = 0; // 若是其他协议，只好粗略计算，将协议头计算为0
    }

    
    ip_ids[buf_i] = ntohs(*((uint16_t*)(packet_content + ID_OFFSET) ));
    cap_times[buf_i] = pkthdr->ts.tv_sec * 1000000000 + pkthdr->ts.tv_usec * 1000;    
    frag_offsets[buf_i] = (ntohs(*((uint16_t*)(packet_content + FRAG_OFFSET_OFFSET))))<<3; // frag_offset的值为其13位实际值乘8，这与wireshark的显示是一致的
    payload_sizes[buf_i] = ntohs(*((uint16_t*)(packet_content + TOTAL_LEN_OFFSET))) - IP_HDR_LEN - proto_hdr_len;

    buf_i++;
    if(buf_i == BUF_DATA_NUM){
        // 每BUF_DATA_NUM个数据存储一次
        // printf("buffer has %d points, save!\n", BUF_DATA_NUM);
        save_buf_data_to_db();

        memset(influx_data, 0, BUF_DATA_NUM*ROW_DATA_SIZE);
        buf_i = 0;
    }

}