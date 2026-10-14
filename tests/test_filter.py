"""降误报后处理测试：置信度闸 + 语义聚类去重。"""

from __future__ import annotations

import unittest

from core.filter import (
    bigram_similarity,
    cluster_findings,
    filter_by_confidence,
)
from findings.schema import Finding, normalize_confidence


def raw(message: str, confidence, file: str = "a.py") -> dict:
    return {
        "file": file,
        "existing_code": "x = 1",
        "message": message,
        "severity": "high",
        "category": "bug",
        "confidence": confidence,
    }


def f(file: str, start: int, end: int, message: str = "m", confidence: float = 0.9) -> Finding:
    return Finding(
        file=file,
        start_line=start,
        end_line=end,
        message=message,
        severity="high",
        category="bug",
        confidence=confidence,
    )


class NormalizeConfidenceTest(unittest.TestCase):
    def test_accepts_float_string_percent_and_junk(self):
        self.assertEqual(normalize_confidence(0.9), 0.9)
        self.assertEqual(normalize_confidence("0.75"), 0.75)
        self.assertEqual(normalize_confidence("85%"), 0.85)
        self.assertEqual(normalize_confidence(90), 0.9)
        self.assertEqual(normalize_confidence(200), 1.0)
        self.assertEqual(normalize_confidence(-1), 0.0)
        self.assertEqual(normalize_confidence(None), 0.5)   # 缺失 -> 待复核
        self.assertEqual(normalize_confidence(True), 0.5)   # 布尔不算数
        self.assertEqual(normalize_confidence("高"), 0.5)


class ConfidenceGateTest(unittest.TestCase):
    def test_three_buckets(self):
        kept, stats = filter_by_confidence(
            [raw("高把握", 0.9), raw("中等把握", 0.65), raw("瞎猜", 0.3)]
        )
        self.assertEqual([item["message"] for item in kept], ["高把握", "中等把握"])
        self.assertEqual(stats.dropped_low_confidence, 1)
        self.assertEqual(stats.needs_review, 1)
        self.assertEqual(stats.kept, 2)
        self.assertFalse(kept[0]["needs_review"])
        self.assertTrue(kept[1]["needs_review"])

    def test_boundaries_are_inclusive_on_the_keep_side(self):
        kept, stats = filter_by_confidence([raw("正好 0.8", 0.8), raw("正好 0.5", 0.5)])
        self.assertEqual(stats.dropped_low_confidence, 0)
        self.assertEqual(stats.needs_review, 1)
        self.assertFalse(kept[0]["needs_review"])

    def test_input_dicts_are_not_mutated(self):
        item = raw("x", 0.6)
        filter_by_confidence([item])
        self.assertNotIn("needs_review", item)


class BigramSimilarityTest(unittest.TestCase):
    def test_identical_and_unrelated(self):
        self.assertEqual(bigram_similarity("缺少长度校验", "缺少长度校验"), 1.0)
        self.assertLess(bigram_similarity("缺少长度校验", "数据库连接未关闭"), 0.6)
        self.assertEqual(bigram_similarity("", "x"), 0.0)


class ClusterTest(unittest.TestCase):
    def test_similar_messages_same_file_merge_to_one(self):
        findings = [
            f("a.py", 10, 14, "未对 code 参数做长度校验，存在 ReDoS 风险"),
            f("a.py", 12, 12, "未对 code 参数做长度校验，存在 ReDoS 风险。"),
        ]
        kept, stats = cluster_findings(findings)
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats.merged, 1)
        self.assertEqual(stats.clusters, 1)
        # 行区间最窄的那条胜出（行号最精确）
        self.assertEqual((kept[0].start_line, kept[0].end_line), (12, 12))

    def test_same_span_prefers_longer_message(self):
        findings = [
            f("a.py", 10, 10, "code 参数缺少长度校验"),
            f("a.py", 10, 10, "code 参数缺少长度校验，可被 ReDoS 拖死"),
        ]
        kept, _ = cluster_findings(findings)
        self.assertEqual(len(kept), 1)
        self.assertIn("ReDoS", kept[0].message)

    def test_same_message_different_file_does_not_merge(self):
        findings = [f("a.py", 10, 10, "缺少长度校验"), f("b.py", 10, 10, "缺少长度校验")]
        kept, stats = cluster_findings(findings)
        self.assertEqual((len(kept), stats.merged), (2, 0))

    def test_different_messages_same_file_do_not_merge(self):
        findings = [
            f("a.py", 10, 10, "未校验 code 参数长度，存在 ReDoS 风险"),
            f("a.py", 30, 30, "数据库连接未关闭，连接池会被耗尽"),
        ]
        kept, stats = cluster_findings(findings)
        self.assertEqual((len(kept), stats.merged), (2, 0))

    def test_transitive_closure(self):
        findings = [
            f("a.py", 10, 20, "未校验 code 参数长度，存在 ReDoS 风险"),
            f("a.py", 11, 19, "未校验 code 参数长度，存在 ReDoS 风险！"),
            f("a.py", 12, 18, "未校验 code 参数长度，存在 ReDoS 风险。"),
        ]
        kept, stats = cluster_findings(findings)
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats.merged, 2)
        self.assertEqual((kept[0].start_line, kept[0].end_line), (12, 18))

    def test_empty_and_single(self):
        self.assertEqual(cluster_findings([]), ([], cluster_findings([])[1]))
        kept, stats = cluster_findings([f("a.py", 1, 1)])
        self.assertEqual((len(kept), stats.merged), (1, 0))
        self.assertEqual(stats.input, 1)


if __name__ == "__main__":
    unittest.main()
