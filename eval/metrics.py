"""本阶段占位：评估相关代码不在第一阶段范围内。

设计意图（阶段二实现）：ReviewBench 的 golden findings 与我们的 findings.json
按 (file, 行区间重叠) 匹配，算出 precision / recall / F1，以及定位命中率。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence


def precision_recall_f1(
    predicted: Sequence[Any], golden: Sequence[Any], tolerance: int = 0
) -> Dict[str, float]:
    raise NotImplementedError("阶段二实现：需要 golden findings 才谈得上指标。")
