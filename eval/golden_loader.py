"""本阶段占位：golden findings 加载器。

约定（阶段二）：golden 文件与 findings.json 同构（见 findings/schema.py 的 Finding），
额外可带 ``id`` / ``tags`` 字段，加载时忽略未知键。
"""

from __future__ import annotations

from typing import List

from findings.schema import Finding


def load_golden(path: str) -> List[Finding]:
    raise NotImplementedError("阶段二实现：本阶段不做评估，也不接 ReviewBench。")
