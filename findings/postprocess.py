"""审查结果后处理（治精度失控）。

三道闸，顺序固定：

  1. **去重**：同文件 + 行区间重叠的 findings 合成一条（保留最严重、区间最长的那条）。
     模型很爱为相邻几行重复报同一个问题。
  2. **严重级过滤**：low / info 直接丢，只要 high / medium。
  3. **数量上限**：整次审查最多 ``max_findings`` 条，超出按严重级排序截断。

只做"减法"：不改行号、不动 resolver 的匹配结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from findings.schema import Finding

SEVERITY_RANK: Dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "info": 0,
}

DEFAULT_KEEP_SEVERITIES: Tuple[str, ...] = ("critical", "high", "medium")
DEFAULT_MAX_FINDINGS = 10


@dataclass
class PostprocessStats:
    """每一步砍掉了多少，便于在全量评估里对比。"""

    input: int = 0
    kept: int = 0
    merged_duplicates: int = 0
    dropped_severity: int = 0
    dropped_over_cap: int = 0
    per_reason: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, int]:
        return {
            "input": self.input,
            "kept": self.kept,
            "merged_duplicates": self.merged_duplicates,
            "dropped_severity": self.dropped_severity,
            "dropped_over_cap": self.dropped_over_cap,
        }


def severity_rank(severity: str) -> int:
    return SEVERITY_RANK.get((severity or "").strip().lower(), 0)


def _overlaps(a: Finding, b: Finding) -> bool:
    """同文件且行区间相交。这里刻意不复用 resolver / eval 的代码，保持单向依赖。"""
    if a.file != b.file:
        return False
    a_lo, a_hi = sorted((a.start_line, a.end_line))
    b_lo, b_hi = sorted((b.start_line, b.end_line))
    return not (a_hi < b_lo or b_hi < a_lo)


def _span(finding: Finding) -> int:
    return abs(finding.end_line - finding.start_line)


def _sort_key(finding: Finding) -> Tuple[int, int, int, str]:
    # 严重的优先；同级别时区间长的优先；再按行号、文件保持稳定
    return (-severity_rank(finding.severity), -_span(finding), finding.start_line, finding.file)


def dedupe_findings(findings: Sequence[Finding]) -> Tuple[List[Finding], int]:
    """同文件 + 行区间重叠的合并。返回 (保留的 findings, 合并掉的数量)。"""
    kept: List[Finding] = []
    merged = 0
    for finding in sorted(findings, key=_sort_key):
        if any(_overlaps(finding, existing) for existing in kept):
            merged += 1
            continue
        kept.append(finding)
    return kept, merged


def filter_by_severity(
    findings: Sequence[Finding], keep: Sequence[str] = DEFAULT_KEEP_SEVERITIES
) -> Tuple[List[Finding], int]:
    """丢掉 low / info。"""
    allowed = {item.lower() for item in keep}
    kept = [item for item in findings if (item.severity or "").lower() in allowed]
    return kept, len(findings) - len(kept)


def cap_findings(
    findings: Sequence[Finding], max_findings: int = DEFAULT_MAX_FINDINGS
) -> Tuple[List[Finding], int]:
    """超过上限时，按严重级保留最严重的 ``max_findings`` 条。"""
    if max_findings is None or max_findings <= 0 or len(findings) <= max_findings:
        return list(findings), 0
    ordered = sorted(findings, key=_sort_key)[:max_findings]
    ordered.sort(key=lambda item: (item.file, item.start_line, item.end_line))
    return ordered, len(findings) - len(ordered)


def postprocess(
    findings: Sequence[Finding],
    max_findings: int = DEFAULT_MAX_FINDINGS,
    keep_severities: Sequence[str] = DEFAULT_KEEP_SEVERITIES,
) -> Tuple[List[Finding], PostprocessStats]:
    """跑完三道闸，返回 (最终 findings, 统计)。"""
    stats = PostprocessStats(input=len(findings))

    deduped, merged = dedupe_findings(findings)
    stats.merged_duplicates = merged

    filtered, dropped_severity = filter_by_severity(deduped, keep=keep_severities)
    stats.dropped_severity = dropped_severity

    capped, dropped_over_cap = cap_findings(filtered, max_findings=max_findings)
    stats.dropped_over_cap = dropped_over_cap

    capped.sort(key=lambda item: (item.file, item.start_line, item.end_line))
    stats.kept = len(capped)
    return capped, stats
