"""unified diff 文本 -> ChangeSet 列表。

只做"解析"，不碰 git，也不碰 LLM：这一层是可单测的纯函数。
old_content / new_content 不在这里填（那是 adapter 的活），解析结果里留空。
"""

from __future__ import annotations

from typing import List

from unidiff import PatchSet

from findings.schema import ChangeSet, Hunk, HunkLine


def parse_unified_diff(diff_text: str) -> List[ChangeSet]:
    """把 ``git diff`` 的文本解析成 ChangeSet 列表。

    没有新增行的文件（纯删除）也会保留：head_start/head_end 记为 0。
    """
    if not diff_text or not diff_text.strip():
        return []

    patch = PatchSet(diff_text)
    changesets: List[ChangeSet] = []
    for patched_file in patch:
        hunks: List[Hunk] = []
        added_numbers: List[int] = []

        for hunk in patched_file:
            lines: List[HunkLine] = []
            for line in hunk:
                content = line.value.rstrip("\n").rstrip("\r")
                if line.is_added:
                    lines.append(HunkLine("+", content, line.target_line_no))
                    added_numbers.append(line.target_line_no)
                elif line.is_removed:
                    lines.append(HunkLine("-", content, None))
                else:
                    lines.append(HunkLine(" ", content, line.target_line_no))
            hunks.append(
                Hunk(
                    old_start=hunk.source_start,
                    old_lines=hunk.source_length,
                    new_start=hunk.target_start,
                    new_lines=hunk.target_length,
                    lines=lines,
                )
            )

        changesets.append(
            ChangeSet(
                file=_normalize_path(patched_file.path),
                head_start=min(added_numbers) if added_numbers else 0,
                head_end=max(added_numbers) if added_numbers else 0,
                hunks=hunks,
            )
        )
    return changesets


def _normalize_path(path: str) -> str:
    """去掉 diff 头里的 a/ b/ 前缀，统一成仓库相对路径。"""
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path
