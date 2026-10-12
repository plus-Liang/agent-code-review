"""评估指标：确定性匹配 + P/R/F1。

**和官方口径的差别（必须知道）**：ReviewBench 官方的 matcher 只做「同一文件内、语义上
是不是同一个问题」的判断，由 LLM 完成（``scripts/eval/matcher.ts``）。本模块是**确定性
代理**：同文件 + 行区间重叠即判为命中。它便宜、可复现、能当天跑通，但会低估 recall
（同一问题的两个不同行区间会被判成没匹配上），也会高估 precision（行区间撞上但问题不同）。
等接入官方 judge 前，这个数字只用来做相对比较和回归，不要当真值对外报。

匹配规则：
  * 只在同一文件内比较（路径统一成「/」分隔的仓库相对路径）；
  * 行区间在 ``tolerance`` 行内相交即视为命中（0 表示必须相交）；
  * 一对一贪心：每个预测取重叠最多的未占用 golden，平手时取下标小的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from findings.schema import Finding

MatchPair = Tuple[int, int]


def ranges_overlap(
    a_start: int, a_end: int, b_start: int, b_end: int, tolerance: int = 0
) -> bool:
    """两个闭区间是否在 ``tolerance`` 行容差内相交。"""
    a_lo, a_hi = min(a_start, a_end), max(a_start, a_end)
    b_lo, b_hi = min(b_start, b_end), max(b_start, b_end)
    return not (a_hi + tolerance < b_lo or b_hi + tolerance < a_lo)


def _overlap_len(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    a_lo, a_hi = min(a_start, a_end), max(a_start, a_end)
    b_lo, b_hi = min(b_start, b_end), max(b_start, b_end)
    return max(0, min(a_hi, b_hi) - max(a_lo, b_lo) + 1)


@dataclass
class Score:
    """一题（或整体）的分数与明细。"""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    matches: List[MatchPair] = field(default_factory=list)
    unmatched_predictions: List[int] = field(default_factory=list)
    unmatched_golden: List[int] = field(default_factory=list)

    @property
    def precision(self) -> float:
        denominator = self.tp + self.fp
        return self.tp / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.tp + self.fn
        return self.tp / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0

    def to_dict(self, with_details: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }
        if with_details:
            data["matches"] = [{"prediction": p, "golden": g} for p, g in self.matches]
            data["unmatched_predictions"] = list(self.unmatched_predictions)
            data["unmatched_golden"] = list(self.unmatched_golden)
        return data


def match_findings(
    predictions: Sequence[Finding],
    golden: Sequence[Finding],
    tolerance: int = 0,
) -> Score:
    """确定性一对一匹配，返回带明细的 :class:`Score`。"""
    score = Score()
    golden_taken = [False] * len(golden)

    for pred_index, prediction in enumerate(predictions):
        best_golden = -1
        best_overlap = -1  # 容差内命中时重叠长度可能是 0，所以初值不能是 0
        for golden_index, expected in enumerate(golden):
            if golden_taken[golden_index]:
                continue
            if prediction.file != expected.file:
                continue
            if not ranges_overlap(
                prediction.start_line,
                prediction.end_line,
                expected.start_line,
                expected.end_line,
                tolerance=tolerance,
            ):
                continue
            overlap = _overlap_len(
                prediction.start_line, prediction.end_line, expected.start_line, expected.end_line
            )
            if overlap > best_overlap:  # 平手时保留下标更小的那个
                best_overlap = overlap
                best_golden = golden_index

        if best_golden >= 0:
            golden_taken[best_golden] = True
            score.matches.append((pred_index, best_golden))
        else:
            score.unmatched_predictions.append(pred_index)

    score.tp = len(score.matches)
    score.fp = len(score.unmatched_predictions)
    score.unmatched_golden = [i for i, taken in enumerate(golden_taken) if not taken]
    score.fn = len(score.unmatched_golden)
    return score


def aggregate(scores: Sequence[Score]) -> Score:
    """按题汇总（micro：先加总 tp/fp/fn 再算比率）。"""
    total = Score()
    for score in scores:
        total.tp += score.tp
        total.fp += score.fp
        total.fn += score.fn
        total.matches.extend(score.matches)
        total.unmatched_predictions.extend(score.unmatched_predictions)
        total.unmatched_golden.extend(score.unmatched_golden)
    return total


def micro_average(scores_with_counts: Sequence[Tuple[int, int, int]]) -> Dict[str, float]:
    """给定若干 (tp, fp, fn)，算 micro P/R/F1（便于跨题汇总）。"""
    tp = sum(item[0] for item in scores_with_counts)
    fp = sum(item[1] for item in scores_with_counts)
    fn = sum(item[2] for item in scores_with_counts)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }
