class TableNotExistError(ValueError):
    """当表不存在但是尝试从表中进行读取操作时引发该异常"""


class KeyNotExistError(ValueError):
    """当查询的键不存在时， 引发该异常"""


class SourceTypeError(ValueError):
    """查询的资源种类不存在时，引发该异常"""


class NoFreeDbForUserError(RuntimeError):
    """数据库的用户数已经到达最大值，再继续创建用户时，引发该异常"""


class DbAlreadyExistError(RuntimeError):
    """用户已经创建了数据库再创建数据库时候，引发该异常"""


class DbCreateFailedError(ValueError):
    """数据库创建失败时，引发该异常"""

class DbNotExistError(RuntimeError):
    """尝试使用未创建的数据库时，引发该异常"""

class RedisAPIError(RuntimeError):
    """跟redis运行时的相关的错误, 就是应该不断的
        向上抛出异常,最外层再做处理
    """
