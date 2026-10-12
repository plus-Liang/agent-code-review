"""后处理测试：去重 / severity 过滤 / 上限。"""

from __future__ import annotations

import unittest

from findings.postprocess import (
    cap_findings,
    dedupe_findings,
    filter_by_severity,
    postprocess,
)
from findings.schema import Finding


def f(file: str, start: int, end: int, severity: str = "high", message: str = "m") -> Finding:
    return Finding(
        file=file, start_line=start, end_line=end, message=message, severity=severity,
        category="bug",
    )


class DedupeTest(unittest.TestCase):
    def test_overlapping_same_file_merge_keeps_most_severe(self):
        findings = [f("a.py", 10, 12, "medium"), f("a.py", 11, 13, "high")]
        kept, merged = dedupe_findings(findings)
        self.assertEqual(merged, 1)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].severity, "high")

    def test_different_files_do_not_merge(self):
        kept, merged = dedupe_findings([f("a.py", 10, 12), f("b.py", 10, 12)])
        self.assertEqual((len(kept), merged), (2, 0))

    def test_non_overlapping_same_file_survive(self):
        kept, merged = dedupe_findings([f("a.py", 10, 12), f("a.py", 20, 22)])
        self.assertEqual((len(kept), merged), (2, 0))

    def test_three_in_a_row_collapse_to_one(self):
        findings = [f("a.py", 10, 14), f("a.py", 12, 16), f("a.py", 14, 18)]
        kept, merged = dedupe_findings(findings)
        self.assertEqual(len(kept), 1)
        self.assertEqual(merged, 2)

    def test_transitive_chain_is_not_fully_collapsed(self):
        # 贪心只跟"已保留"的比较：10-12 与 14-16 不重叠，所以两条都留下
        findings = [f("a.py", 10, 12), f("a.py", 12, 14), f("a.py", 14, 16)]
        kept, merged = dedupe_findings(findings)
        self.assertEqual((len(kept), merged), (2, 1))


class SeverityTest(unittest.TestCase):
    def test_low_and_info_dropped(self):
        findings = [f("a.py", 1, 1, "low"), f("a.py", 2, 2, "info"), f("a.py", 3, 3, "medium")]
        kept, dropped = filter_by_severity(findings)
        self.assertEqual([item.severity for item in kept], ["medium"])
        self.assertEqual(dropped, 2)


class CapTest(unittest.TestCase):
    def test_cap_keeps_most_severe_then_resorts_by_line(self):
        findings = [f("a.py", i, i, "medium") for i in range(1, 13)] + [f("a.py", 50, 50, "high")]
        kept, dropped = cap_findings(findings, max_findings=3)
        self.assertEqual(len(kept), 3)
        self.assertEqual(dropped, 10)
        self.assertIn("high", [item.severity for item in kept])
        self.assertEqual([item.start_line for item in kept], sorted(item.start_line for item in kept))

    def test_cap_of_zero_means_no_cap(self):
        findings = [f("a.py", i, i) for i in range(1, 5)]
        kept, dropped = cap_findings(findings, max_findings=0)
        self.assertEqual((len(kept), dropped), (4, 0))


class PostprocessTest(unittest.TestCase):
    def test_three_gates_in_order_and_stats(self):
        findings = [
            f("a.py", 10, 12, "low"),          # 与下一条重叠，合并时被 high 吃掉
            f("a.py", 11, 13, "high"),
            f("b.py", 20, 20, "medium"),
            f("c.py", 30, 30, "medium"),
        ]
        kept, stats = postprocess(findings, max_findings=2)
        self.assertEqual(stats.input, 4)
        self.assertEqual(stats.merged_duplicates, 1)   # a.py 两条合并成 high 那条
        self.assertEqual(stats.dropped_severity, 0)    # 幸存者里没有 low
        self.assertEqual(stats.dropped_over_cap, 1)    # 3 条截到 2 条
        self.assertEqual(stats.kept, 2)
        self.assertEqual([item.file for item in kept], ["a.py", "b.py"])

    def test_low_only_input_ends_empty(self):
        kept, stats = postprocess([f("a.py", 1, 1, "low")])
        self.assertEqual(kept, [])
        self.assertEqual(stats.dropped_severity, 1)

    def test_stats_to_dict_shape(self):
        _, stats = postprocess([f("a.py", 1, 1)])
        self.assertEqual(
            set(stats.to_dict()),
            {"input", "kept", "merged_duplicates", "dropped_severity", "dropped_over_cap"},
        )


if __name__ == "__main__":
    unittest.main()
