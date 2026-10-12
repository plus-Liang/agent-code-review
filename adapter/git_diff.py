"""本地 git diff 适配器：base...head -> ChangeSet 列表。"""

from __future__ import annotations

import subprocess
from typing import List, Optional

from core.diff_parser import parse_unified_diff
from findings.schema import ChangeSet

# 逐字读取文件内容时用的哨兵，避免把"文件不存在"和"空文件"混为一谈。
_MISSING = "\x00__MISSING__\x00"


def _git(args: List[str], repo: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout


def get_diff_text(base: str, head: str, repo: str = ".", three_dot: bool = True) -> str:
    """取 ``git diff base...head``（三点：相对共同祖先）的原始文本。"""
    spec = f"{base}...{head}" if three_dot else f"{base}..{head}"
    return _git(["diff", "--no-color", "--no-ext-diff", spec], repo)


def read_file_at(rev: str, path: str, repo: str = ".") -> str:
    """取某个 revision 下的文件全文；文件不存在时返回空串。"""
    result = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def get_change_sets(
    base: str,
    head: str,
    repo: str = ".",
    three_dot: bool = True,
    max_files: Optional[int] = None,
) -> List[ChangeSet]:
    """本阶段的主入口：读本地 diff，产出带新旧全文的 ChangeSet 列表。

    二进制文件与空 diff 会被直接跳过，保证 ChangeSet 一定可被 JSON 序列化。
    """
    diff_text = get_diff_text(base, head, repo=repo, three_dot=three_dot)
    changesets = parse_unified_diff(diff_text)

    result: List[ChangeSet] = []
    for changeset in changesets:
        new_content = read_file_at(head, changeset.file, repo=repo)
        if _MISSING in new_content or "\x00" in new_content:
            continue  # 二进制/不可读文件，不参与审查
        changeset.new_content = new_content
        changeset.old_content = read_file_at(base, changeset.file, repo=repo)
        result.append(changeset)
        if max_files is not None and len(result) >= max_files:
            break
    return result
