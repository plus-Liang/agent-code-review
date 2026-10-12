"""测试用的临时目录工具。

系统临时目录在本机对沙箱进程不可写（git init 会被拒），因此优先用仓库内的
``.tmp/``；不可用时再退回系统临时目录。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def make_tmpdir(prefix: str = "acr-") -> str:
    """建一个本机沙箱可写的临时目录。

    不用 ``tempfile.mkdtemp``：它会用 0o700 建目录，在本机沙箱下新目录反而不可写。
    """
    base = ROOT / ".tmp"
    try:
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{prefix}{uuid.uuid4().hex[:8]}"
        os.makedirs(path)
        return str(path)
    except OSError:
        return tempfile.mkdtemp(prefix=prefix)


def cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
