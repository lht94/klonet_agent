from flask.views import MethodView
from flask import request
import numpy as np
import networkx as nx
import json

SEED = 42       # 位置计算随机种子
OFFSET = 50     # 人工设置的偏移，避免坐标最小节点完全在画布边缘
class AutoPositionCal(MethodView):
    """
    /master/auto_position_cal/
    
    POST 拓扑节点位置计算接口
    
    """
    def post(self):
        """
        利用用户提供的邻接拓扑矩阵，自动化生成各个节点的对应位置坐标
        
        输入：
            'adjacent_matrix'(list(list)): 拓扑邻接对称矩阵（嵌套字典），示例：
                [
                    [0, 1, 0, 0],
                    [1, 0, 1, 1],
                    [0, 1, 0, 1],
                    [0, 1, 1, 0]
                ]
            'scale'(int): 拓扑缩放尺度
        返回：
            各节点的位置计算结果
        """
        topo_info = json.loads(request.get_data(as_text=True))
        adjacent_matrix = topo_info['adjacent_matrix']
        span = topo_info['scale']
        # 邻接矩阵合法性检查
        if not adjacent_matrix_if_legal(adjacent_matrix):
            return {'code': 0, 'msg': '拓扑邻接矩阵不合法，请检查后重试'}
        pos_int_normal = cal_node_pos(adjacent_matrix, span)
        return {'code': 1, 'msg': '拓扑节点的位置自动化计算成功', 'position': pos_int_normal}
    
def adjacent_matrix_if_legal(origin_matrix):
    """对于拓扑邻接矩阵的检查
    Args:
        'origin_matrix'(list(list)): 嵌套字典格式的邻接矩阵
    """
    try:
        np_matrix = np.array(origin_matrix)
        # 是否方阵
        if not np_matrix.shape[0] == np_matrix.shape[1]:
            return False
        # 值有效
        is_integer = np.issubdtype(np_matrix.dtype, np.integer)
        is_non_negative = np.all(np_matrix >= 0)
        if not is_integer:
            is_integer = np.all(np_matrix == np_matrix.astype(int))
        if not (is_integer and is_non_negative):
            return False
        # 是否对角为0
        diagonal = np.diag(np_matrix)
        if not np.all(diagonal == 0):
            return False
        # 是否对称
        if not np.array_equal(np_matrix, np_matrix.T):
            return False
    except:
        return False
    return True

def cal_node_pos(origin_matrix, span):
    """根据邻接矩阵计算每个节点的位置
    Args:
        'origin_matrix'(list(list)): 嵌套字典格式的邻接矩阵
    """
    np_matrix = np.array(origin_matrix)
    # 创建 NetworkX 图
    G = nx.from_numpy_array(np_matrix)
    # 使用力导向布局spring_layout
    pos = nx.spring_layout(G, seed=SEED)  # seed用于结果可重复
    
    # 提取所有 x 和 y 坐标
    x_vals = np.array([p[0] for p in pos.values()])
    y_vals = np.array([p[1] for p in pos.values()])
    
     # 平移坐标，使所有值为非负
    x_min, y_min = x_vals.min(), y_vals.min()
    x_shift = -x_min if x_min < 0 else 0
    y_shift = -y_min if y_min < 0 else 0
    x_vals_shifted = x_vals + x_shift
    y_vals_shifted = y_vals + y_shift
    
    # 计算当前的最大值
    x_max, y_max = x_vals_shifted.max(), y_vals_shifted.max()
    current_max = max(x_max, y_max)
    
    # 计算缩放比例
    desired_span = span
    scale = desired_span / current_max if current_max != 0 else 1
    x_scaled = x_vals_shifted * scale
    y_scaled = y_vals_shifted * scale
    
    # 转换为整数
    init_offset = OFFSET
    x_int = np.round(x_scaled).astype(int) + init_offset
    y_int = np.round(y_scaled).astype(int) + init_offset
    
    # 创建新的位置字典
    pos_int = {node: (x, y) for node, x, y in zip(pos.keys(), x_int, y_int)}
    # for node, (x, y) in pos_int.items():
    #     print(f"节点 {node}: x = {x}, y = {y}")
        
    # 将元组中的numpy.int64类型转换为标准int，这样后续转json才能不报错
    pos_int_normal = {k: tuple(map(int, v)) for k, v in pos_int.items()}
    
    return pos_int_normal