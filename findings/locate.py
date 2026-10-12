"""原始结论 + ChangeSet -> Finding（带行号）。

这一层是护城河的落点：模型永远不碰行号，行号只在这里由本地匹配算出来。
定位失败的结论会被丢弃并记入 dropped 列表——宁可少报，不报错行号。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

from core import resolver
from findings.schema import ChangeSet, Finding, normalize_category, normalize_severity


def locate_finding(
    raw: Dict[str, Any], changesets: Sequence[ChangeSet], level: int = 1
) -> Finding | None:
    """单条原始结论 -> Finding；定位失败返回 None。"""
    file = raw.get("file") or ""
    existing_code = raw.get("existing_code") or ""
    if not file or not existing_code.strip():
        return None

    located = resolver.resolve(changesets, file, existing_code, level=level)
    if located is None:
        return None

    start_line, end_line = located
    return Finding(
        file=file,
        start_line=start_line,
        end_line=end_line,
        message=(raw.get("message") or "").strip(),
        severity=normalize_severity(raw.get("severity")),
        category=normalize_category(raw.get("category")),
    )


def locate_findings(
    raw_findings: Iterable[Dict[str, Any]],
    changesets: Sequence[ChangeSet],
    level: int = 1,
) -> Tuple[List[Finding], List[Dict[str, Any]]]:
    """批量定位。返回 (findings, dropped)，dropped 里带丢弃原因，便于评估定位率。"""
    findings: List[Finding] = []
    dropped: List[Dict[str, Any]] = []
    for raw in raw_findings:
        finding = locate_finding(raw, changesets, level=level)
        if finding is None:
            dropped.append({**raw, "drop_reason": "locate_failed_level_%d" % level})
            continue
        findings.append(finding)

    findings.sort(key=lambda item: (item.file, item.start_line, item.end_line))
    return findings, dropped
