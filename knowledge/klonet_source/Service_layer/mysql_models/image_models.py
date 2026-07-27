from ...webserver import mysql
from sqlalchemy.dialects.mysql import *


class Image(mysql.Model):
    __table_args__ = {
        'mysql_engine': 'InnoDB',
        'comment': '镜像表',
        'mysql_charset': 'utf8'
    }

    image_id = mysql.Column(BIGINT(unsigned=True), primary_key=True,
                            autoincrement=True, nullable=False)
    user_id = mysql.Column(BIGINT(unsigned=True), )
