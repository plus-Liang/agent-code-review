"""把模型给出的 existing_code 片段解析成行号。

分级策略（逐级兜底，越往后越贵）：

  1 级 —— hunk 内逐行匹配：在**同一个 hunk** 的上下文行/新增行里找连续片段。便宜、精确。
  2 级 —— 全文滑动窗口：在**变更后整个文件**上做规范化（CRLF、行尾空白）后的滑窗匹配。
  3 级 —— LLM 重定位：把 existing_code + 带行号的文件内容喂给模型，让它指行号；
           拿到行号后**本地校验**该区间确实包含这段代码，否则拒绝。

匹配一律先做空白归一化，这样模型缩进抄错也不会失手；但**不做**语义改写，
本地校验不过就是不过——宁可不报，也不猜行号。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from findings.schema import ChangeSet, Hunk

# 行号区间（闭区间），(start, end)
LineRange = Tuple[int, int]

# 3 级最多把文件前多少行喂给模型（控制 token）
MAX_NUMBERED_LINES = 800

_WS = re.compile(r"\s+")


@dataclass
class LocateHit:
    """一次定位结果：行号范围 + 由哪一级命中。"""

    start_line: int
    end_line: int
    level: int

    @property
    def range(self) -> LineRange:
        return (self.start_line, self.end_line)


def normalize_line(line: str) -> str:
    return _WS.sub(" ", line.strip())


def normalize_soft(line: str) -> str:
    """2 级用：只处理 CRLF 与行尾空白，保留缩进（缩进在 Python/YAML 里有语义）。"""
    return line.replace("\r", "").rstrip()


def _snippet_lines(existing_code: str) -> List[str]:
    """把片段切成归一化后的非空行序列，忽略首尾空行。"""
    raw = existing_code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while raw and not raw[0].strip():
        raw.pop(0)
    while raw and not raw[-1].strip():
        raw.pop()
    return [normalize_line(line) for line in raw]


def _snippet_lines_soft(existing_code: str) -> List[str]:
    raw = existing_code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while raw and not raw[0].strip():
        raw.pop(0)
    while raw and not raw[-1].strip():
        raw.pop()
    return [normalize_soft(line) for line in raw]


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


def resolve_level1(changeset: ChangeSet, existing_code: str) -> Optional[LineRange]:
    """第 1 级：hunk 内逐行匹配。"""
    snippet = _snippet_lines(existing_code)
    if not snippet:
        return None
    for hunk in changeset.hunks:
        hit = resolve_in_hunk(hunk, snippet)
        if hit is not None:
            return hit
    return None


def _sliding_window(lines: Sequence[str], snippet: Sequence[str]) -> Optional[LineRange]:
    if not snippet or not lines or len(snippet) > len(lines):
        return None
    width = len(snippet)
    for start in range(0, len(lines) - width + 1):
        if all(lines[start + i] == snippet[i] for i in range(width)):
            return start + 1, start + width  # 1-based 行号
    return None


def resolve_level2(changeset: ChangeSet, existing_code: str) -> Optional[LineRange]:
    """第 2 级：变更后文件全文滑动窗口（先严格，再忽略缩进）。"""
    snippet = _snippet_lines_soft(existing_code)
    if not snippet:
        return None
    lines = [normalize_soft(line) for line in changeset.new_content.split("\n")]
    hit = _sliding_window(lines, snippet)
    if hit is not None:
        return hit
    return _sliding_window([line.strip() for line in lines], [line.strip() for line in snippet])


def _verify_range(changeset: ChangeSet, existing_code: str, start: int, end: int) -> bool:
    """3 级的本地校验：模型给的行区间必须真的包含这段代码。"""
    lines = [normalize_line(line) for line in changeset.new_content.split("\n")]
    snippet = _snippet_lines(existing_code)
    if not snippet or start < 1 or end < start or end > len(lines):
        return False
    window = lines[start - 1 : end]
    joined = " ".join(window)
    return all(part in joined for part in snippet) and any(part in joined for part in snippet)


def resolve_level3(
    changeset: ChangeSet,
    existing_code: str,
    llm_relocate: Optional[Callable[[str, str, str], Optional[LineRange]]],
) -> Optional[LineRange]:
    """第 3 级：LLM 重定位 + 本地校验。没有 relocator 时直接放弃。"""
    if llm_relocate is None:
        return None
    raw_lines = changeset.new_content.split("\n")
    if not raw_lines or not changeset.new_content.strip():
        return None
    numbered = "\n".join(
        f"{index}: {line}" for index, line in enumerate(raw_lines[:MAX_NUMBERED_LINES], start=1)
    )
    try:
        proposed = llm_relocate(changeset.file, existing_code, numbered)
    except Exception:
        return None
    if not proposed:
        return None
    start, end = proposed
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    start, end = min(start, end), max(start, end)
    if not _verify_range(changeset, existing_code, start, end):
        return None
    return start, end


def resolve_in_changeset(
    changeset: ChangeSet,
    existing_code: str,
    max_level: int = 3,
    llm_relocate: Optional[Callable[[str, str, str], Optional[LineRange]]] = None,
) -> Optional[LocateHit]:
    """在一个 ChangeSet 上逐级尝试，返回第一次命中的结果。"""
    for level in range(1, max_level + 1):
        if level == 1:
            hit = resolve_level1(changeset, existing_code)
        elif level == 2:
            hit = resolve_level2(changeset, existing_code)
        elif level == 3:
            hit = resolve_level3(changeset, existing_code, llm_relocate)
        else:
            raise ValueError(f"未知定位等级 {level}，本阶段只支持 1~3")
        if hit is not None:
            return LocateHit(start_line=hit[0], end_line=hit[1], level=level)
    return None


def resolve_hit(
    changesets: Sequence[ChangeSet],
    file: str,
    existing_code: str,
    max_level: int = 3,
    llm_relocate: Optional[Callable[[str, str, str], Optional[LineRange]]] = None,
) -> Optional[LocateHit]:
    """跨 ChangeSet 定位（带命中等级）。"""
    for changeset in changesets:
        if changeset.file != file:
            continue
        return resolve_in_changeset(
            changeset, existing_code, max_level=max_level, llm_relocate=llm_relocate
        )
    return None


def resolve(
    changesets: Sequence[ChangeSet], file: str, existing_code: str, level: int = 3
) -> Optional[LineRange]:
    """兼容旧签名的薄封装：只返回行号区间，不返回命中等级。"""
    hit = resolve_hit(changesets, file, existing_code, max_level=level)
    return hit.range if hit else None
