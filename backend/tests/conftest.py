import os
import tempfile
from pathlib import Path

# 必须在导入 app.* 之前固定数据目录与夹具目录（db.py 在导入期解析路径）。
_TMP = tempfile.mkdtemp(prefix="clickspan-test-")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ.setdefault(
    "FIXTURES_DIR", str(Path(__file__).resolve().parents[2] / "fixtures")
)
