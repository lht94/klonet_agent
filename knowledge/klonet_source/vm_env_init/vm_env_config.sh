#!/bin/bash
  
# 如果是云服务器，一定要先保证虚拟云服务器支持kvm虚拟化！

# 一、搭建拓扑环境依赖
# 0.如果有libvritd服务，先给他终止掉
echo "Try to stopping origin libvirtd, you can ignore the error about file remove"
systemctl stop libvirtd
killall libvirtd
rm /run/libvirtd.pid
# 1.基础libvirt环境及依赖包安装

apt-get install qemu-kvm qemu-system virt-manager bridge-utils vlan -y
# 网上有指出libvirt-bin被拆成了两个包libvirt-daemon-system libvirt-clients(debian和一些比较新的系统ubuntu22)
apt-get install libvirt-bin -y
apt-get install libvirt-daemon-system libvirt-clients -y

systemctl is-active libvirtd
if [ $? -eq 0 ]; then
    echo "libvirt service is active."
else
    echo "libvirt service failed."
fi
usermod -aG libvirt $USER
usermod -aG kvm $USER

qemu_conf=/etc/libvirt/qemu.conf
sed -i 's/#user = "root"/user = "root"/g' $qemu_conf
sed -i 's/#group = "root"/group = "root"/g' $qemu_conf


systemctl start libvirtd.service
systemctl enable libvirtd.service

# 2.安装libvirt的python SDK及相应依赖包

apt update
# apt upgrade

apt install libvirt-dev python3-dev python3-pip -y
# 必须为pip3.8
python3.8 -m pip install libvirt-python

# 3.启动NAT网络
# （2024.7.6更新 wudx）
# 将默认的NAT网络改成192.128.122.0/24网段，以避免某些情况下与教研室内网192.168.0.0/16冲突

# 关闭原有的virbr0
virsh net-destroy default
virsh net-undefine default
# 启动新的virbr0
cp ./install_vm_environment/default.xml /etc/libvirt/qemu/networks/default.xml

virsh net-define /etc/libvirt/qemu/networks/default.xml
virsh net-start default
virsh net-autostart default # 该步报错可以忽略，之前装libvirt时本身就链接了autostart的文件

# 关闭virbr0上的包校验
ethtool -K virbr0 tx off rx off

# 二、执行命令、ssh等其他环境依赖

# 1.安装python包
python3.8 -m pip install paramiko
python3.8 -m pip install timeout_decorator

# 2.执行iptables表项配置
iptables -t filter -I FORWARD -o virbr0 -j DOCKER

# 3.修改libvirt配置文件
config=/etc/libvirt/libvirtd.conf
cp $config $config.bak

sed -i 's/#listen_tls = 0/listen_tls = 0/g' $config
sed -i 's/#listen_tcp = 1/listen_tcp = 1/g' $config
sed -i 's/#tcp_port = "16509"/tcp_port = "16509"/g' $config
sed -i 's/#listen_addr = "192.168.0.1"/listen_addr = "0.0.0.0"/g' $config
sed -i 's/#auth_tcp = "sasl"/auth_tcp = "none"/g' $config
sed -i 's/unix_sock_rw_perms = "0770"/unix_sock_rw_perms = "0777"/g' $config

if [ $? -eq 0 ]; then
    echo "libvirt config modified successfully."
else
    echo "libvirt config modification failed."
fi

service libvirtd stop

libvirtd --daemon --listen --config  /etc/libvirt/libvirtd.conf

if [ $? -eq 0 ]; then
    echo "libvirt service begin successfully."
else
    echo "libvirt service failed."
fi

# 4.消除LIBVIRT_FWI和LIBVIRT_FWO两条链在平台组网时的转发限制
# wudx更新，某些libvirt会存在一些链（LIBVIRT_FWI, LIBVIRT_FWO)规则会拒绝掉除了走virbr0的其他所有流量
# 在此处插入接受所有其他流量保证其在网络拓扑内的正常通信
# 如果报错可以忽略
echo "You can ignore the following error about LIBVIRT iptables"

iptables -I LIBVIRT_FWI 1 -j ACCEPT
iptables -I LIBVIRT_FWO 1 -j ACCEPT