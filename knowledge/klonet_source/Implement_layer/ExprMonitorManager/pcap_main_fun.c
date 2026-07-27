#include <stdio.h>
#include <stdlib.h>
#include <stdbool.h>
#include "packet_capprocess.h"
#include <unistd.h>
#include <string.h>

// gcc test.c packet_capprocess.c json_manipulate.c -o test -I/usr/local/include/json-c -L/usr/local/lib -ljson-c -lpcap

int 
main(int argc, char **argv)
{
    /* about conf analysis. */
    const char jsconfile[] = "./conf/conf.json";
    char *nicname = NULL;
    char *file_name = NULL;

    if(argc != 4){
        printf("usage: %s {nicname} {filter_expression} {file_name}\n", argv[0]);
        exit(-1);
    }
    nicname = argv[1];
    file_name = argv[3];

    stat = (stat_t *)malloc(STAT_LEN);

    int count;

    start_sniff(nicname, (u_char *)&count, argv[2], file_name);

    // curl = curl_easy_init();
    
    // if(curl){
    //     /* start to capture packets. */
    //     curl_easy_setopt(curl, CURLOPT_URL, "http://10.1.1.104:8086/write?db=mydb"); 
    //     // start_sniff(nicname, (u_char *)&count);
    //     curl_easy_cleanup(curl);
    // }
    // else{
    //     printf("failed to create curl.");
    // }

    return 0;
}



