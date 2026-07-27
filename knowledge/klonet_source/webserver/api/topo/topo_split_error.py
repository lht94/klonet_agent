class NETypeError(ValueError):
    """切割拓扑时出现未知拓扑类型时，引发该异常"""


class TopoSplitFailError(RuntimeError):
    """因worker容量不足导致预计切割拓扑失败，引发该异常"""
