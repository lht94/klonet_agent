from flask_socketio import emit
from flask_socketio import SocketIO
from ..vemu_config.config import PROJ_CONFIG

'''
对应的前端代码示例：


'''

def push_msg(msg:str):
    '''
    @socketio.on("push_event")
    将指定消息推送至前端。
    注意，此函数为创建SocketIO对象然后推送一条消息，如需推送大量消息，请手动创建对象
    并多次调用socketio.emit()

    Args:
        msg: 要推送至前端的消息

    Returns:
        None
    ''' 
    print('in push msg...')
    if PROJ_CONFIG.push_msg_enabled:
        socketio = SocketIO(
            message_queue=(f"amqp://{PROJ_CONFIG.rabbitmq_ip}:"
                           f"{PROJ_CONFIG.rabbitmq_port}"))
        print(f'push data to client {msg}')
        socketio.emit('push_event', {'data': f"{msg}"})
        

def connected(msg):
    '''
    @socketio.on("connected_event")
    建立连接时发送的消息

    Args:
        msg: 前端在建立连接时发来的消息
    
    Returns:
        None
    '''
    print(f"received msg from client: {msg['data']}")
    emit('push_event', {'data': f"您已与数据进度推送服务建立连接!"})