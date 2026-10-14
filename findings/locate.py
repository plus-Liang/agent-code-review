"""原始结论 + ChangeSet -> Finding（带行号）。

这一层是护城河的落点：模型永远不碰行号，行号只在这里由本地匹配算出来。
逐级兜底（hunk 内 → 全文滑窗 → LLM 重定位），三级都失败就丢弃并记入 dropped——
宁可少报，也不报错行号。同时统计每一级各命中多少，便于判断该往哪一级投入。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from core import resolver
from findings.schema import (
    ChangeSet,
    Finding,
    normalize_category,
    normalize_confidence,
    normalize_severity,
)

Relocator = Callable[[str, str, str], Optional[Tuple[int, int]]]

LEVEL_KEYS = ("1", "2", "3", "miss")


def locate_finding(
    raw: Dict[str, Any],
    changesets: Sequence[ChangeSet],
    max_level: int = 3,
    llm_relocate: Optional[Relocator] = None,
) -> Tuple[Optional[Finding], Optional[int]]:
    """单条原始结论 -> (Finding, 命中等级)；定位失败返回 (None, None)。"""
    file = raw.get("file") or ""
    existing_code = raw.get("existing_code") or ""
    if not file or not existing_code.strip():
        return None, None

    hit = resolver.resolve_hit(
        changesets, file, existing_code, max_level=max_level, llm_relocate=llm_relocate
    )
    if hit is None:
        return None, None

    finding = Finding(
        file=file,
        start_line=hit.start_line,
        end_line=hit.end_line,
        message=(raw.get("message") or "").strip(),
        severity=normalize_severity(raw.get("severity")),
        category=normalize_category(raw.get("category")),
        confidence=normalize_confidence(raw.get("confidence")),
        needs_review=bool(raw.get("needs_review", False)),
    )
    return finding, hit.level


def empty_level_counts() -> Dict[str, int]:
    return {key: 0 for key in LEVEL_KEYS}


def locate_findings(
    raw_findings: Iterable[Dict[str, Any]],
    changesets: Sequence[ChangeSet],
    max_level: int = 3,
    llm_relocate: Optional[Relocator] = None,
) -> Tuple[List[Finding], List[Dict[str, Any]], Dict[str, int]]:
    """批量定位。

    返回 ``(findings, dropped, level_counts)``；dropped 里带丢弃原因，
    level_counts 记录每一级命中多少条（含 miss）。
    """
    findings: List[Finding] = []
    dropped: List[Dict[str, Any]] = []
    counts = empty_level_counts()

    for raw in raw_findings:
        finding, level = locate_finding(
            raw, changesets, max_level=max_level, llm_relocate=llm_relocate
        )
        if finding is None:
            counts["miss"] += 1
            dropped.append({**raw, "drop_reason": f"locate_failed_max_level_{max_level}"})
            continue
        counts[str(level)] += 1
        findings.append(finding)

    findings.sort(key=lambda item: (item.file, item.start_line, item.end_line))
    return findings, dropped, counts
