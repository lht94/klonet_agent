"""仓库根目录运行时的兼容包。

当前仓库目录本身就是 `klonet_agent` 包目录。为了让用户在仓库根目录也能运行
`python -m klonet_agent.agent`，这里把外层源码目录加入包搜索路径。
"""

from pathlib import Path
import sys


OUTER_PACKAGE_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = OUTER_PACKAGE_DIR.parent

if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

outer_path = str(OUTER_PACKAGE_DIR)
if outer_path not in __path__:
    __path__.append(outer_path)

__version__ = "0.4.0"
