import os

vemu_uestc_dir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

def save_file(dir, file_name, data):
    '''
    在dir路径处存储文件，文件名为file_name，文件内容为data。若dir不存在，则创建dir。

    Args:
        dir: 文件所在目录的路径
        file_name: 文件名
        data: 文件内容
    '''
    if not os.path.exists(dir):
        os.makedirs(dir)
    with open(f"{dir}/{file_name}", "w") as f:
        f.write(data)

def get_file_content(dir, file_name):
    '''
    获取在dir路径处的文件内容，文件名为file_name

    Args:
        dir: 文件所在目录的路径
        file_name: 文件名

    Returns:
        文件内容（字符串）
    '''
    with open(f"{dir}/{file_name}", "r") as f:
        data = f.read()
    return data

def in_directory(dir1, dir2):
    '''
    判断dir1是否是dir2的子路径

    Args:
        dir1: 路径名1
        dir2: 路径名2

    Returns:
        是子路径则返回True，不是子路径则返回False
    '''
    #make both absolute    
    dir1 = os.path.abspath(dir1)
    dir2 = os.path.abspath(dir2)

    #return true, if the common prefix of both is equal to directory
    #e.g. /a/b/c/d.rst and directory is /a/b, the common prefix is /a/b
    return os.path.commonprefix([dir1, dir2]) == dir2

def check_directory(dir):
    '''
    判断指定路径下的目录是否存在，不存在则创建
    注意以斜杠作为分界，创建将文件名去掉后的目录路径
    
    Args:
        dir: 目录名或文件路径
        (/root/test/或/root/test/axt.txt)
    '''
    tmp = os.path.dirname(dir)
    if not os.path.exists(tmp):
        os.makedirs(tmp)
    else:
        pass
    
def is_empty_directory(dir):
    '''
    判断目标路径下文件夹是否为空
    Args:
        dir: 目录名
        (以/结尾)
    '''
    return not any(os.listdir(dir))

def clear_empty_directory(dir):
    '''
    递归删除空的文件夹
    Args:
        dir: 目录名
        (以/结尾)
    '''
    if is_empty_directory(dir):
        try:
            os.removedirs(dir)
        except:
            pass
        
def check_directory_exits(dir):
    '''
    判断指定路径下的目录是否存在,(区别于check_directory函数)
    注意以斜杠作为分界,参数为文件名去掉后的目录路径
    
    Args:
        dir: 目录名或文件路径
        (/root/test/或/root/test/axt.txt)
    '''
    tmp = os.path.dirname(dir)
    if not os.path.exists(tmp):
        return False
    else:
        return True
    
def check_file_exits(path):
    '''
    判定绝对路径下的文件是否存在
    
    Args:
        path: 文件的绝对路径
        (/root/test/axt.txt)
    '''
    return os.path.exists(path)