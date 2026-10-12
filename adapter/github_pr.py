"""GitHub PR 适配器 —— 占位。

本阶段不做远端 PR（见 README「MVP 的边界」）。契约已经定好：任何适配器最终都要
产出 ``List[ChangeSet]``，因此后续接入只需在这里实现同一签名：

    def get_change_sets(repo_full_name: str, pr_number: int, token: str | None = None,
                        max_files: int | None = None) -> list[ChangeSet]: ...
"""

from __future__ import annotations

from typing import List, Optional

from findings.schema import ChangeSet


def get_change_sets(
    repo_full_name: str,
    pr_number: int,
    token: Optional[str] = None,
    max_files: Optional[int] = None,
) -> List[ChangeSet]:
    raise NotImplementedError("阶段二才实现 GitHub PR 适配器；本阶段只用本地 diff。")
