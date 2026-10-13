"""分诊规则测试（确定性，不联网）。"""

from __future__ import annotations

import unittest

from core.graph import triage_node
from core.triage import skip_reason, triage
from findings.schema import ChangeSet


def cs(path: str) -> ChangeSet:
    return ChangeSet(file=path, head_start=1, head_end=1)


class SkipReasonTest(unittest.TestCase):
    def test_tests_are_skipped(self):
        for path in (
            "src/__tests__/a.ts",
            "src/tests/b.py",
            "test/c.go",
            "src/parser.test.ts",
            "src/parser.spec.ts",
            "pkg/test_helper.py",
            "pkg/helper_test.py",
        ):
            self.assertIsNotNone(skip_reason(path), path)

    def test_styles_and_docs_skipped(self):
        self.assertEqual(skip_reason("assets/main.css"), "style")
        self.assertEqual(skip_reason("styles/theme.scss"), "style")
        self.assertEqual(skip_reason("CHANGELOG.md"), "docs")

    def test_dependency_manifests_and_locks_skipped(self):
        for path in ("package.json", "requirements.txt", "go.mod", "yarn.lock", "poetry.lock"):
            self.assertIsNotNone(skip_reason(path), path)

    def test_production_code_is_kept(self):
        for path in (
            "src/server/handlers.ts",
            "lib/core/engine.py",
            "app/api/route.ts",
            "server/main.go",
            "core/resolver.py",
        ):
            self.assertIsNone(skip_reason(path), path)

    def test_windows_separators_and_dot_slash(self):
        self.assertIsNotNone(skip_reason(".\\src\\__tests__\\a.ts"))
        self.assertIsNone(skip_reason("./src/server/handlers.ts"))


class TriageTest(unittest.TestCase):
    def test_split_keeps_production_only(self):
        result = triage(
            [
                cs("src/server/handlers.ts"),
                cs("src/__tests__/handlers.test.ts"),
                cs("public/main.css"),
                cs("package.json"),
                cs("README.md"),
            ]
        )
        self.assertEqual([item.file for item in result.review], ["src/server/handlers.ts"])
        self.assertEqual(len(result.skipped), 4)
        reasons = {item.file: item.reason for item in result.skipped}
        self.assertEqual(reasons["package.json"], "dependency-manifest")
        self.assertEqual(reasons["public/main.css"], "style")

    def test_node_keeps_changesets_for_locating(self):
        state = {"changesets": [cs("src/a.ts"), cs("src/b.test.ts")]}
        updated = triage_node(state)
        # 原始 changesets 不动，定位阶段还要用它
        self.assertEqual(len(state["changesets"]), 2)
        self.assertEqual([item.file for item in updated["review_changesets"]], ["src/a.ts"])
        self.assertEqual(len(updated["skipped_files"]), 1)


if __name__ == "__main__":
    unittest.main()
