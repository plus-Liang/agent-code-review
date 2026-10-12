"""diff 解析测试：行号必须落在变更后文件上。"""

from __future__ import annotations

import unittest

from core.diff_parser import parse_unified_diff

DIFF = """diff --git a/demo.py b/demo.py
index 1111111..2222222 100644
--- a/demo.py
+++ b/demo.py
@@ -1,4 +1,5 @@
 import os
+x = 1
 def f():
-    pass
+    return 1 / 0
     return None
"""


class DiffParserTest(unittest.TestCase):
    def test_change_set_shape(self):
        changesets = parse_unified_diff(DIFF)
        self.assertEqual(len(changesets), 1)
        changeset = changesets[0]
        self.assertEqual(changeset.file, "demo.py")
        self.assertEqual((changeset.head_start, changeset.head_end), (2, 4))
        self.assertEqual(len(changeset.hunks), 1)

    def test_added_lines_have_new_numbers_and_removed_have_none(self):
        hunk = parse_unified_diff(DIFF)[0].hunks[0]
        added = [line for line in hunk.lines if line.type == "+"]
        removed = [line for line in hunk.lines if line.type == "-"]
        self.assertEqual([line.new_line for line in added], [2, 4])
        self.assertTrue(all(line.new_line is None for line in removed))
        self.assertEqual(added[1].content, "    return 1 / 0")

    def test_empty_diff(self):
        self.assertEqual(parse_unified_diff(""), [])


if __name__ == "__main__":
    unittest.main()
