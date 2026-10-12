"""ReviewBench golden / 预测结果加载测试（用合成数据，不依赖本地数据集）。"""

from __future__ import annotations

import json
import os
import unittest

from eval.golden_loader import (
    identify_task,
    load_predictions,
    load_task_file,
    load_test_set,
    pr_key_for,
)
from tests._tmp import cleanup, make_tmpdir

HEAD = "16a54f0f85a9091f220418850eac6b4122782be5"


def _task_json() -> dict:
    return {
        "pr_key": "PierreJanineh_TechDebtMCP_135-16a54f0f",
        "pr": {
            "repo": "https://github.com/PierreJanineh/TechDebtMCP",
            "pr_number": 135,
            "base": "ea0385c7349bbaee04456c57bf0ec3f217d63532",
            "head": HEAD,
        },
        "findings": [
            {
                "file": "src/core/a.ts",
                "start_line": 32,
                "end_line": 32,
                "message": "flag 漏了 d",
                "severity": "medium",
                "category": "correctness",
                "scope": "introduced-by-pr",  # golden 独有的标注字段，应被忽略
            }
        ],
    }


class GoldenLoaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmpdir(prefix="acr-golden-")
        self.addCleanup(cleanup, self.tmp)

    def _write(self, name: str, payload) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return path

    def test_pr_key_uses_nwo_and_short_head(self):
        self.assertEqual(
            pr_key_for("PierreJanineh/TechDebtMCP", 135, HEAD),
            "PierreJanineh_TechDebtMCP_135-16a54f0f",
        )

    def test_load_single_task_file_ignores_extra_labels(self):
        task = load_task_file(self._write("t.json", _task_json()))
        self.assertEqual(task.pr_number, 135)
        self.assertEqual(len(task.findings), 1)
        self.assertEqual(task.findings[0].category, "correctness")

    def test_load_test_set_joins_meta_and_findings(self):
        corpus = os.path.join(self.tmp, "corpus")
        os.makedirs(corpus)
        self._write(
            os.path.join("corpus", "test.json"),
            [
                {
                    "repo": "https://github.com/PierreJanineh/TechDebtMCP",
                    "pr_number": 135,
                    "base": "ea03",
                    "head": HEAD,
                    "nwo": "PierreJanineh/TechDebtMCP",
                }
            ],
        )
        self._write(
            os.path.join("corpus", "findings.json"),
            {"PierreJanineh_TechDebtMCP_135-16a54f0f": _task_json()["findings"]},
        )
        tasks = load_test_set(corpus)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].findings[0].start_line, 32)

    def test_load_predictions_accepts_bare_array_and_wrapped(self):
        bare = self._write(
            "bare.json",
            [{"file": "./a.py", "start_line": 3, "end_line": 4, "message": "m"}],
        )
        _, findings = load_predictions(bare)
        self.assertEqual(findings[0].file, "a.py")  # 去掉 ./ 前缀
        self.assertEqual(findings[0].severity, "medium")  # 缺字段时的默认值

        wrapped = self._write(
            "wrapped.json",
            {
                "pr": {"head": HEAD, "pr_number": 135},
                "agent": "agent-code-review",
                "findings": [{"file": "a.py", "start_line": 1, "end_line": 2, "message": "m"}],
            },
        )
        pr_meta, findings = load_predictions(wrapped)
        self.assertEqual(pr_meta["pr_number"], 135)
        self.assertEqual(len(findings), 1)

    def test_identify_task_by_head_and_pr_number(self):
        corpus = os.path.join(self.tmp, "corpus")
        os.makedirs(corpus)
        self._write(
            os.path.join("corpus", "test.json"),
            [
                {
                    "repo": "r",
                    "pr_number": 135,
                    "base": "b",
                    "head": HEAD,
                    "nwo": "PierreJanineh/TechDebtMCP",
                }
            ],
        )
        self._write(
            os.path.join("corpus", "findings.json"),
            {"PierreJanineh_TechDebtMCP_135-16a54f0f": []},
        )
        tasks = load_test_set(corpus)
        hit = identify_task({"head": HEAD, "pr_number": 135}, tasks)
        self.assertIsNotNone(hit)
        self.assertIsNone(identify_task({"head": "deadbeef", "pr_number": 1}, tasks))


if __name__ == "__main__":
    unittest.main()
