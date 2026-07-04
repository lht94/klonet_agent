"""测试辅助函数。

这里不用 pytest 的 tmp_path fixture，是为了避免某些 Windows/OneDrive 环境下
pytest 默认临时目录创建失败，导致测试还没进入业务逻辑就报 setup error。
"""

from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import tempfile
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_TMP_ROOT = Path(
    os.environ.get(
        "KLONET_AGENT_TEST_TMP",
        str(Path(tempfile.gettempdir()) / "klonet_agent_test_tmp"),
    )
)


@contextmanager
def local_temp_dir():
    """在项目目录下创建一次性测试目录。"""

    path = TEST_TMP_ROOT / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
