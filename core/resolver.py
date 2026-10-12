"""把模型给出的 existing_code 片段解析成行号。

分级策略（本阶段只实现第 1 级）：

  1 级 —— hunk 内逐行匹配：在**同一个 hunk** 的上下文行/新增行里找连续片段。
  2 级 —— 跨 hunk / 全文件匹配（阶段二）。
  3 级 —— 模糊匹配 + 相似度阈值（阶段二）。

匹配一律先做空白归一化（去首尾空白、压缩内部空白），这样模型缩进抄错也不会失手；
但**不做**语义改写，找不到就是找不到——宁可不报，也不猜行号。
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from findings.schema import ChangeSet, Hunk

# 行号区间（闭区间），(start, end)
LineRange = Tuple[int, int]

_WS = re.compile(r"\s+")


def normalize_line(line: str) -> str:
    return _WS.sub(" ", line.strip())


def _snippet_lines(existing_code: str) -> List[str]:
    """把片段切成归一化后的非空行序列，忽略首尾空行。"""
    raw = existing_code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while raw and not raw[0].strip():
        raw.pop(0)
    while raw and not raw[-1].strip():
        raw.pop()
    return [normalize_line(line) for line in raw]


def _hunk_lines(hunk: Hunk) -> List[Tuple[int, str]]:
    """hunk 内可用于定位的行：(新行号, 归一化内容)，按新行号升序。"""
    items = [
        (line.new_line, normalize_line(line.content))
        for line in hunk.lines
        if line.new_line is not None and line.type in ("+", " ")
    ]
    items.sort(key=lambda item: item[0])
    return items


def resolve_in_hunk(hunk: Hunk, snippet: Sequence[str]) -> Optional[LineRange]:
    """第 1 级：在单个 hunk 内找连续匹配的行号区间。"""
    if not snippet:
        return None
    candidates = _hunk_lines(hunk)
    if not candidates:
        return None

    width = len(snippet)
    for start in range(0, len(candidates) - width + 1):
        window = candidates[start : start + width]
        if all(window[i][1] == snippet[i] for i in range(width)):
            return window[0][0], window[-1][0]
    return None


def resolve_in_changeset(
    changeset: ChangeSet, existing_code: str, level: int = 1
) -> Optional[LineRange]:
    """在单个 ChangeSet 上定位；level 目前只支持 1。"""
    if level != 1:
        raise NotImplementedError(f"定位等级 {level} 尚未实现，本阶段只支持 1 级。")

    snippet = _snippet_lines(existing_code)
    if not snippet:
        return None
    for hunk in changeset.hunks:
        hit = resolve_in_hunk(hunk, snippet)
        if hit is not None:
            return hit
    return None


def resolve(
    changesets: Sequence[ChangeSet], file: str, existing_code: str, level: int = 1
) -> Optional[LineRange]:
    """跨 ChangeSet 定位：先按文件命中，再走分级匹配。"""
    for changeset in changesets:
        if changeset.file != file:
            continue
        return resolve_in_changeset(changeset, existing_code, level=level)
    return None
