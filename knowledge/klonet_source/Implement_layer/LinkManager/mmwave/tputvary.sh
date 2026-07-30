#!/bin/bash
set -e

# https://witestlab.poly.edu/blog/aqm-mmwave/

interface=$1
link_scenario=$2
queue_type=$3
file_path=$4
loss_rate=$5
bandwidth_scaling=$6
# 参数检查
if [ $# != 6 ];then
	echo "Arguments is wrong!"
	echo "Usage: $0 <interface> <link_scenario> <queue_type> <file_path> <loss_rate> <bandwidth_scaling>"
	exit 1
fi

if [ "$link_scenario" != "lb" ] && [ "$link_scenario" != "mobb" ] && \
	[ "$link_scenario" != "sb" ] && [ "$link_scenario" != "sl" ]; then
	echo "$link_scenario is not supported." 
	exit 1
fi

# 初始化hrb队列
# sudo tc qdisc del dev "$interface" root
if  [ "$loss_rate" != "0%" ] ;then
sudo tc qdisc replace dev "$interface" root handle 1: netem loss "$loss_rate"
sudo tc qdisc replace dev "$interface" parent 1: handle 2: htb default 3
sudo tc class add dev "$interface" parent 2: classid 2:3 htb rate 100gbit
fi

if  [ "$loss_rate" == "0%" ] ;then
sudo tc qdisc replace dev "$interface" root handle 1: netem
sudo tc qdisc replace dev "$interface" parent 1: handle 2: htb default 3
sudo tc class add dev "$interface" parent 2: classid 2:3 htb rate 100gbit
fi


# 添加指定队列类型
if [ "$queue_type" == "largefifo" ]; then	
    queue_size=7500000
	#必须这么处理，limit参数不允许小数
	queue_size=$(echo "scale=0;$queue_size*$bandwidth_scaling/1" |bc)
	sudo tc qdisc replace dev "$interface" parent 2:3 handle 3: \
		bfifo limit "$queue_size"
elif [ "$queue_type" == "fq_codel" ]; then
	queue_size=5000
	queue_size=$(echo "scale=0;$queue_size*$bandwidth_scaling/1" |bc)
	sudo tc qdisc replace dev "$interface" parent 2:3 handle 3: \
		fq_codel limit "$queue_size" target 10ms
elif [ "$queue_type" == "pie" ]; then
	queue_size=5000
	queue_size=$(echo "scale=0;$queue_size*$bandwidth_scaling/1" |bc)
	sudo tc qdisc replace dev "$interface" parent 2:3 handle 3: \
		pie limit "$queue_size" target 10ms
elif [ "$queue_type" == "smallfifo" ]; then
	queue_size=1875000
	queue_size=$(echo "scale=0;$queue_size*$bandwidth_scaling/1" |bc)
	sudo tc qdisc replace dev "$interface" parent 2:3 handle 3: \
		bfifo limit "$queue_size"
else
	echo "$queue_type is not supported." 
	exit 1
fi



while true
do
# 进行带宽变换
	while IFS=, read -r tput tdiff
	do 
		tdiff=${tdiff::-1}
		ts=$(date +%s.%N)
	 	tput=$(echo "$tput*$bandwidth_scaling" |bc)
		echo "$ts, $tdiff, $tput"
		
		sudo tc class replace dev "$interface" parent 2: classid 2:3 \
			htb rate "$tput"mbit
		
		sleep "$tdiff"
	done < "$file_path"/"$link_scenario"-tput.csv

	# tc -s -d class show dev $interface # 查看队列配置结果
done