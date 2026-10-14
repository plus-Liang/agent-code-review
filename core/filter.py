"""降误报的两道后处理闸：置信度闸 + 语义聚类去重。

和 ``findings/postprocess.py`` 的分工：
  * 本模块管**语义层面**的减法——模型自己说有多大把握（confidence），以及
    同文件里换着说法重复报的同一处问题（message bigram 重合）。
  * ``postprocess.py`` 管**结构层面**的减法——行区间重叠、severity、数量上限。

两道闸都在定位前后只做减法，不改行号、不动 resolver。

置信度分档（``filter_by_confidence``，作用在定位前的原始结论上）：
  * ``>= 0.8``        → 保留
  * ``0.5 ~ 0.8``     → 保留，但打上 ``needs_review``
  * ``< 0.5``         → 丢弃

聚类去重（``cluster_findings``，作用在定位后的 Finding 上）：
  * 同文件 + message bigram 重合度（Dice）``>= 0.6`` → 同一组（并查集传递闭包）
  * 每组只留一条"最具体"的：行区间最窄（行号最精确）→ message 最长（描述最详细）
    → 置信度最高 → severity 最高
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

from findings.postprocess import severity_rank
from findings.schema import Finding, normalize_confidence

# 置信度分档阈值
CONFIDENCE_KEEP = 0.8
CONFIDENCE_REVIEW = 0.5

# 同文件 message bigram 重合度阈值（Dice 系数）
DEFAULT_SIMILARITY = 0.6

# 去掉空白与标点，保留字母/数字/下划线/CJK——bigram 只在"实义字符"上算
_NOISE = re.compile(r"[^\w]+", re.UNICODE)


def normalize_text(text: Any) -> str:
    """小写 + 去标点空白，作为 bigram 的输入。"""
    return _NOISE.sub("", str(text or "").lower())


def bigrams(text: Any) -> frozenset:
    """字符二元组集合。中英文都能用，不需要分词器。"""
    cleaned = normalize_text(text)
    if len(cleaned) < 2:
        return frozenset({cleaned} if cleaned else [])
    return frozenset(cleaned[i : i + 2] for i in range(len(cleaned) - 1))


def bigram_similarity(left: Any, right: Any) -> float:
    """两条 message 的 bigram 重合度（Dice 系数，0~1）。"""
    a, b = bigrams(left), bigrams(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return 2 * len(a & b) / (len(a) + len(b))


@dataclass
class ConfidenceStats:
    """置信度闸砍掉了多少。"""

    input: int = 0
    kept: int = 0
    needs_review: int = 0
    dropped_low_confidence: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "input": self.input,
            "kept": self.kept,
            "needs_review": self.needs_review,
            "dropped_low_confidence": self.dropped_low_confidence,
        }


def filter_by_confidence(
    findings: Sequence[Dict[str, Any]],
    keep_threshold: float = CONFIDENCE_KEEP,
    review_threshold: float = CONFIDENCE_REVIEW,
) -> Tuple[List[Dict[str, Any]], ConfidenceStats]:
    """置信度闸：丢弃 < 0.5，0.5~0.8 标 ``needs_review``，>= 0.8 直接过。

    作用在**定位前**的原始结论上——低置信度的条目不必再花定位预算。
    """
    stats = ConfidenceStats(input=len(findings))
    kept: List[Dict[str, Any]] = []
    for item in findings:
        confidence = normalize_confidence(item.get("confidence"))
        if confidence < review_threshold:
            stats.dropped_low_confidence += 1
            continue
        if confidence < keep_threshold:
            stats.needs_review += 1
            kept.append({**item, "confidence": confidence, "needs_review": True})
            continue
        kept.append({**item, "confidence": confidence, "needs_review": False})
    stats.kept = len(kept)
    return kept, stats


@dataclass
class ClusterStats:
    """聚类去重前后对比。"""

    input: int = 0
    kept: int = 0
    merged: int = 0
    clusters: int = 0  # 成员 >= 2 的组数

    def to_dict(self) -> Dict[str, int]:
        return {
            "input": self.input,
            "kept": self.kept,
            "merged": self.merged,
            "clusters": self.clusters,
        }


def _specificity_key(finding: Finding) -> Tuple[int, int, float, int, int, int]:
    """"最具体"的排序键：行号最精确 → 描述最详细 → 置信度最高 → severity 最高。"""
    span = abs(finding.end_line - finding.start_line)
    return (
        span,
        -len((finding.message or "").strip()),
        -normalize_confidence(getattr(finding, "confidence", 1.0)),
        -severity_rank(finding.severity),
        finding.start_line,
        finding.end_line,
    )


def _union_find_groups(findings: Sequence[Finding], threshold: float) -> List[List[int]]:
    """同文件 + message bigram 重合度 >= threshold 的传递闭包分组。"""
    count = len(findings)
    parent = list(range(count))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    # 预先算好 bigram，避免 O(n^2) 次重复解析
    gram_cache = [bigrams(item.message) for item in findings]
    for i in range(count):
        for j in range(i + 1, count):
            if findings[i].file != findings[j].file:
                continue
            a, b = gram_cache[i], gram_cache[j]
            if not a or not b:
                continue
            similarity = 1.0 if a == b else 2 * len(a & b) / (len(a) + len(b))
            if similarity >= threshold:
                union(i, j)

    buckets: Dict[int, List[int]] = {}
    for index in range(count):
        buckets.setdefault(find(index), []).append(index)
    return list(buckets.values())


def cluster_findings(
    findings: Sequence[Finding], threshold: float = DEFAULT_SIMILARITY
) -> Tuple[List[Finding], ClusterStats]:
    """按"同文件 + message 语义相似"聚组，每组只保留最具体的一条。"""
    stats = ClusterStats(input=len(findings))
    if not findings:
        return [], stats

    kept: List[Finding] = []
    for group in _union_find_groups(findings, threshold):
        if len(group) > 1:
            stats.clusters += 1
        best = min((findings[index] for index in group), key=_specificity_key)
        kept.append(best)

    kept.sort(key=lambda item: (item.file, item.start_line, item.end_line))
    stats.kept = len(kept)
    stats.merged = stats.input - stats.kept
    return kept, stats
