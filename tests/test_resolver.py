"""定位测试（第 1 级：hunk 内逐行匹配）。"""

from __future__ import annotations

import unittest

from core import resolver
from core.diff_parser import parse_unified_diff

DIFF = """diff --git a/demo.py b/demo.py
--- a/demo.py
+++ b/demo.py
@@ -1,4 +1,6 @@
 import os
+x = 1
 def f():
-    pass
+    y = 2
+    return x / y
     return None
@@ -20,2 +22,3 @@ def g():
     a = 1
+    b = 2
     return a
"""


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self.changesets = parse_unified_diff(DIFF)

    def test_single_line_exact_match(self):
        self.assertEqual(resolver.resolve(self.changesets, "demo.py", "    y = 2"), (4, 4))

    def test_multi_line_consecutive_match(self):
        snippet = "    y = 2\n    return x / y"
        self.assertEqual(resolver.resolve(self.changesets, "demo.py", snippet), (4, 5))

    def test_second_hunk(self):
        self.assertEqual(resolver.resolve(self.changesets, "demo.py", "    b = 2"), (23, 23))

    def test_whitespace_tolerant(self):
        # 模型少抄了缩进也要能命中
        self.assertEqual(resolver.resolve(self.changesets, "demo.py", "y = 2"), (4, 4))

    def test_miss_returns_none(self):
        self.assertIsNone(
            resolver.resolve(self.changesets, "demo.py", "    totally = 'not here'")
        )
        self.assertIsNone(resolver.resolve(self.changesets, "nope.py", "    y = 2"))

    def test_level_2_finds_match_outside_hunk(self):
        # 片段在文件里，但不在任何 hunk 的上下文/新增行里 → 1 级找不到，2 级全文滑窗能找到
        changeset = self.changesets[0]
        changeset.new_content = (
            "import os\nx = 1\ndef f():\n    y = 2\n    return x / y\n    return None\n"
            "\n\n# 一大段没被改动的代码\nUNCHANGED = 'only in file'\n"
        )
        hit = resolver.resolve_in_changeset(changeset, "UNCHANGED = 'only in file'", max_level=2)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.level, 2)
        self.assertEqual(hit.start_line, 10)

    def test_level_1_hit_reports_level_1(self):
        hit = resolver.resolve_in_changeset(self.changesets[0], "    y = 2", max_level=2)
        self.assertEqual(hit.level, 1)

    def test_unknown_level_raises(self):
        with self.assertRaises(ValueError):
            resolver.resolve_in_changeset(self.changesets[0], "    not-in-file", max_level=4)


if __name__ == "__main__":
    unittest.main()
