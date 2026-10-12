"""locate 层测试：模型只给片段，这里才产生行号。"""

from __future__ import annotations

import unittest

from core.diff_parser import parse_unified_diff
from findings.locate import locate_finding, locate_findings

DIFF = """diff --git a/demo.py b/demo.py
--- a/demo.py
+++ b/demo.py
@@ -1,3 +1,4 @@
 import os
+    return 1 / 0
 def f():
     return None
"""


class LocateTest(unittest.TestCase):
    def setUp(self):
        self.changesets = parse_unified_diff(DIFF)

    def test_raw_finding_becomes_finding_with_line_numbers(self):
        raw = {
            "file": "demo.py",
            "existing_code": "    return 1 / 0",
            "message": "除零风险",
            "severity": "HIGH",
            "category": "bug",
        }
        finding = locate_finding(raw, self.changesets)
        self.assertIsNotNone(finding)
        self.assertEqual((finding.start_line, finding.end_line), (2, 2))
        self.assertEqual(finding.severity, "high")

    def test_unlocatable_finding_is_dropped_not_guessed(self):
        findings, dropped = locate_findings(
            [{"file": "demo.py", "existing_code": "    nope = 1", "message": "m"}],
            self.changesets,
        )
        self.assertEqual(findings, [])
        self.assertEqual(len(dropped), 1)
        self.assertIn("drop_reason", dropped[0])

    def test_results_sorted(self):
        raws = [
            {"file": "demo.py", "existing_code": "def f():", "message": "c"},
            {"file": "demo.py", "existing_code": "    return 1 / 0", "message": "a"},
            {"file": "demo.py", "existing_code": "import os", "message": "b"},
        ]
        findings, dropped = locate_findings(raws, self.changesets)
        self.assertEqual([f.start_line for f in findings], [1, 2, 3])
        self.assertEqual(dropped, [])


if __name__ == "__main__":
    unittest.main()
