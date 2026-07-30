#!/bin/bash

user=$1
password=$2
config=/etc/rsyncd.conf
passwd=/etc/rsyncd.passwd

# 安装rsync软件包
rsync --version
if [ $? -ne 0 ]; then
    apt install -y rsync
    if [ $? -eq 0 ]; then
        echo "rsync installation successfully!"
    else
        echo "rsync installation failed."
        exit
    fi

    # root用户增加环境变量
    echo "export RSYNC_PASSWORD=\"$password\"" >> /etc/profile
    source /etc/profile
else
    echo "rsync already installed"
fi

# 修改配置文件
# 每次同步之前都重新拷贝，保证配置文件最新
ls /etc/ | grep rsyncd
if [ $? -ne 0 ]; then    
    touch $config && touch $passwd
else
    cp $config $config.bak && cp $passwd $passwd.bak
    rm $config && rm $passwd
    touch $config && touch $passwd
fi
    echo -e "# /etc/rsyncd: configuration file for rsync daemon mode\n \
\n\
# See rsyncd.conf man page for more options.\n\
\n\
# configuration example:\n\
\n\
uid = root\n\
gid = root\n\
use chroot = no\n\
max connections = 20\n\
pid file = /var/run/rsyncd.pid\n\
log file = /var/log/rsyncd.log\n\
lock file = /var/run/rsync.lock\n\
read only = false\n\
\n\
# exclude = lost+found/\n\
# transfer logging = yes\n\
# timeout = 900\n\
# ignore nonreadable = yes\n\
# dont compress   = *.gz *.tgz *.zip *.z *.Z *.rpm *.deb *.bz2\n\
\n\
# [ftp]\n\
#        path = /home/ftp\n\
#        comment = ftp export area\n" >> $config
echo "$user:$password" >> $passwd
chmod 600 $passwd


# 启动rsync服务
rsync --daemon
if [ $? -eq 0 ]; then
    echo "rsync starts successfully!"
else
    echo "rsync starts failed."
    exit
fi