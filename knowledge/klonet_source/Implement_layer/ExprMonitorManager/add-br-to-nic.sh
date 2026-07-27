#!/bin/bash
if (($#!=2))
then
  echo "usage: $0 {nic_name} {cidr}"
  exit 1
fi

nicName=$1
cidr=$2
echo "add br to ${nicName}..."

# 添加网桥
sudo ip link add name ${nicName}-br type bridge
sudo ip link set  ${nicName}-br up
sudo ip link set dev ${nicName} master  ${nicName}-br
sudo ip addr del ${cidr} dev ${nicName}
sudo ip addr add ${cidr} dev ${nicName}-br

# 关闭网桥的tso gso gro
sudo ethtool -K ${nicName}-br tso off
sudo ethtool -K ${nicName}-br gso off
sudo ethtool -K ${nicName}-br gro off