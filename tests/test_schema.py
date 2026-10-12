"""契约测试：两个数据类必须能无损地走一遍 JSON。"""

from __future__ import annotations

import json
import unittest

from findings.schema import (
    ChangeSet,
    Finding,
    Hunk,
    HunkLine,
    findings_to_json,
    load_findings,
    normalize_severity,
)


class SchemaTest(unittest.TestCase):
    def test_changeset_json_round_trip(self):
        changeset = ChangeSet(
            file="demo.py",
            head_start=2,
            head_end=3,
            hunks=[
                Hunk(
                    old_start=1,
                    old_lines=2,
                    new_start=1,
                    new_lines=3,
                    lines=[
                        HunkLine(" ", "def f():", 1),
                        HunkLine("+", "    return 1 / 0", 2),
                        HunkLine(" ", "    pass", 3),
                    ],
                )
            ],
            old_content="def f():\n    pass\n",
            new_content="def f():\n    return 1 / 0\n    pass\n",
        )
        restored = ChangeSet.from_dict(json.loads(json.dumps(changeset.to_dict())))
        self.assertEqual(restored, changeset)
        self.assertEqual([line.new_line for line in restored.added_lines()], [2])

    def test_finding_json_round_trip_and_keys(self):
        finding = Finding(
            file="demo.py",
            start_line=2,
            end_line=2,
            message="除零",
            severity="high",
            category="bug",
        )
        payload = finding.to_dict()
        self.assertEqual(
            set(payload),
            {"file", "start_line", "end_line", "message", "severity", "category"},
        )
        self.assertEqual(Finding.from_dict(payload), finding)

    def test_findings_to_json_is_loadable(self):
        text = findings_to_json([Finding("a.py", 1, 1, "x", "low", "style")])
        self.assertEqual(json.loads(text)[0]["file"], "a.py")

    def test_unknown_severity_falls_back(self):
        self.assertEqual(normalize_severity("BLOCKER"), "medium")
        self.assertEqual(normalize_severity(None), "medium")

    def test_load_findings_accepts_wrapped_dict(self):
        import os

        from tests._tmp import cleanup, make_tmpdir

        tmp = make_tmpdir(prefix="acr-schema-")
        self.addCleanup(cleanup, tmp)
        path = os.path.join(tmp, "f.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"findings": [Finding("a.py", 3, 4, "m").to_dict()]}, handle)
        loaded = load_findings(path)
        self.assertEqual(loaded[0].end_line, 4)


if __name__ == "__main__":
    unittest.main()
