"""确定性评估指标测试。"""

from __future__ import annotations

import unittest

from eval.metrics import aggregate, match_findings, ranges_overlap
from findings.schema import Finding


def f(file: str, start: int, end: int, message: str = "m") -> Finding:
    return Finding(file=file, start_line=start, end_line=end, message=message)


class RangesTest(unittest.TestCase):
    def test_overlap(self):
        self.assertTrue(ranges_overlap(10, 12, 12, 15))
        self.assertFalse(ranges_overlap(10, 12, 13, 15))
        self.assertTrue(ranges_overlap(10, 12, 13, 15, tolerance=1))
        self.assertTrue(ranges_overlap(10, 12, 9, 9, tolerance=1))

    def test_reversed_bounds_are_tolerated(self):
        self.assertTrue(ranges_overlap(12, 10, 10, 12))


class MatchTest(unittest.TestCase):
    def test_exact_hit(self):
        score = match_findings([f("a.py", 5, 5)], [f("a.py", 5, 5)])
        self.assertEqual((score.tp, score.fp, score.fn), (1, 0, 0))
        self.assertAlmostEqual(score.f1, 1.0)

    def test_file_mismatch_counts_fp_and_fn(self):
        score = match_findings([f("a.py", 5, 5)], [f("b.py", 5, 5)])
        self.assertEqual((score.tp, score.fp, score.fn), (0, 1, 1))
        self.assertEqual(score.precision, 0.0)
        self.assertEqual(score.recall, 0.0)

    def test_one_to_one_greedy(self):
        score = match_findings([f("a.py", 10, 12), f("a.py", 11, 11)], [f("a.py", 10, 12)])
        self.assertEqual((score.tp, score.fp, score.fn), (1, 1, 0))
        # 重叠更多的那条先占坑
        self.assertEqual(score.matches, [(0, 0)])

    def test_tolerance_recovers_near_miss(self):
        score = match_findings([f("a.py", 10, 10)], [f("a.py", 12, 12)], tolerance=2)
        self.assertEqual(score.tp, 1)
        score_strict = match_findings([f("a.py", 10, 10)], [f("a.py", 12, 12)])
        self.assertEqual(score_strict.tp, 0)

    def test_empty_predictions(self):
        score = match_findings([], [f("a.py", 1, 1)])
        self.assertEqual((score.tp, score.fp, score.fn), (0, 0, 1))
        self.assertEqual(score.precision, 0.0)  # 无预测时 precision 定义为 0
        self.assertEqual(score.recall, 0.0)

    def test_empty_golden(self):
        score = match_findings([f("a.py", 1, 1)], [])
        self.assertEqual((score.tp, score.fp, score.fn), (0, 1, 0))

    def test_aggregate_is_micro(self):
        s1 = match_findings([f("a.py", 1, 1)], [f("a.py", 1, 1)])
        s2 = match_findings([f("a.py", 1, 1)], [f("a.py", 9, 9)])
        total = aggregate([s1, s2])
        self.assertEqual((total.tp, total.fp, total.fn), (1, 1, 1))
        self.assertAlmostEqual(total.precision, 0.5)
        self.assertAlmostEqual(total.recall, 0.5)
        self.assertAlmostEqual(total.f1, 0.5)

    def test_to_dict_shape(self):
        payload = match_findings([f("a.py", 1, 1)], [f("a.py", 1, 1)]).to_dict()
        self.assertEqual(
            set(payload), {"tp", "fp", "fn", "precision", "recall", "f1", "matches",
                           "unmatched_predictions", "unmatched_golden"}
        )
        self.assertEqual(payload["matches"], [{"prediction": 0, "golden": 0}])


if __name__ == "__main__":
    unittest.main()
