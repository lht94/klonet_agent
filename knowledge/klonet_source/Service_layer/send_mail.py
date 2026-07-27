import smtplib
from email.mime.text import MIMEText
from email.header import Header

from ..Service_layer.mysql_api.user_info import get_user_info_by_user_name
from ..vemu_config.config import PROJ_CONFIG


def send_mail_to(user_mail, msg, subject):
    """
    发送邮件函数

    Args:
        user_mail: 发送邮件到达的邮箱地址
        msg:       正文信息
        subject:   主题
    
    Returns: 布尔值, True为成功发送, False为发送错误
    """

    # 若邮箱功能使能，才进行邮件发送
    if not PROJ_CONFIG.mail_enable:
        return False
    
    # 编辑邮件内容
    # 正文
    message = MIMEText(msg, 'plain', 'utf-8')
    # 发送者
    message['From'] = "948409821@qq.com"
    # 接收者
    message['To'] =  Header(user_mail, 'utf-8')
    # 主题
    message['Subject'] = Header(subject, 'utf-8')

    # 发送邮件
    try:
        smtpObj = smtplib.SMTP() 
        smtpObj.connect(PROJ_CONFIG.mail_host)                       # 连接服务器
        smtpObj.login(PROJ_CONFIG.mail_user, PROJ_CONFIG.mail_pass)  # 登录服务器
        smtpObj.sendmail(PROJ_CONFIG.mail_user, \
            [user_mail], message.as_string())      # 发送
        smtpObj.quit()                             # 退出
        print('邮件发送成功!')
        return True
    except smtplib.SMTPException as e:
        print('邮件发送失败, 发生错误:', e)   # 打印错误
    return False


def send_welcome_mail_to(user_mail):
    """
    发送欢迎邮件给用户, 于用户注册时进行

    Args:
        user_mail: 用户邮箱

    Returns: 布尔值, True为成功发送, False为失败发送
    """
    return send_mail_to(user_mail, PROJ_CONFIG.mail_msg_welcome, \
        "欢迎来到Klonet!")


def send_forget_passwd_mail_to(user, validation_code):
    """
    发送忘记密码邮件给用户, 于用户忘记密码希望进行重置时进行

    Args:
        user:            用户
        validation_code: 验证码

    Returns: 布尔值, True为成功发送, False为失败发送
    """
    # 通过user, 确定mysql里的接收方邮箱
    user_mail = get_user_info_by_user_name(user).email
    # 发送邮件并返回
    return send_mail_to(user_mail, PROJ_CONFIG.mail_msg_forget_passwd + \
        validation_code, "验证码: " + validation_code)
