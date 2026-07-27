from ..Implement_layer.LinkManager import shell_execute

def get_container_pid(container_id):
    '''
    根据容器id获取容器pid
    '''
    container_pid = shell_execute("sudo docker inspect " + container_id + 
        " -f {{.State.Pid}}")

    return container_pid