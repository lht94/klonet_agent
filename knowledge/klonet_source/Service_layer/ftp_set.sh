#!/bin/bash

# 判断是否已安装vsftp
vsftpd -v
if [ $? -ne 0 ]; then
    apt install -y vsftpd && systemctl start vsftpd && systemctl enable vsftpd
    if [ $? -eq 0 ]; then
        echo "vsftp installation successfully."
    else
        echo "vsftp installation failed."
        exit
    fi

	# 修改vsftp配置文件
	config=/etc/vsftpd.conf
	pamVsftpConfig=/etc/pam.d/vsftpd
	cp $config $config.bak

	sed -i 's/#write_enable=YES/write_enable=YES/g' $config
	sed -i 's/#local_umask=022/local_umask=000/g' $config
	sed -i 's/pam_shells.so/pam_nologin.so/g' $pamVsftpConfig

    if [ $? -eq 0 ]; then
        echo "vsftp config modified successfully."
    else
        echo "vsftp config modification failed."
    fi

    systemctl restart vsftpd
    
	username="ftp_vm_use"
	password="[REDACTED]"
	useradd $username -m -d /home/$username
	echo "$username:$password" | sudo chpasswd
    # userdel -r ftp_test 可以删除用户
	
	if [ $? -eq 0 ]; then
        echo "FTP user $username set successfully."
    else
        echo "FTP user $username set failed."
    fi

else
    echo "vsftp already installed"
fi

# sudo apt-get remove --purge vsftpd 可以删除ftp