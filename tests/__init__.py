"""测试包：统一在这里把仓库根和本地 .deps 加进 sys.path。"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _extra in (_ROOT / ".deps", _ROOT):
    if _extra.is_dir() and str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))
