user=$1
mod=$2
path=$3
config=/etc/rsyncd.conf
passwd=/etc/rsyncd.passwd
# 在配置文件中追加同步模块
echo -e "[$mod]\n\
path = $path\n\
auth users = $user\n\
secrets file = $passwd" >> $config