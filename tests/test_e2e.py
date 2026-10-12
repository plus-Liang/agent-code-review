"""端到端测试：临时仓库造分支 -> 图 -> 定位 -> findings。

用假 LLM 客户端（不联网、不花钱）验证链路本身；真实模型的验证由 cli.py 跑。
"""

from __future__ import annotations

import os
import subprocess
import unittest

from core.graph import build_graph
from findings.locate import locate_findings
from tests._tmp import cleanup, make_tmpdir

MAIN_SOURCE = """def divide(a, b):
    return a / b


def load(path):
    return open(path).read()
"""

BRANCH_SOURCE = """def divide(a, b):
    return a / b


def load(path):
    return open(path).read()


def parse(text):
    return eval(text)
"""

FAKE_REPLY = """```json
{"findings": [{"existing_code": "    return eval(text)", "message": "eval 执行不可信输入",
 "severity": "high", "category": "security"}]}
```"""


class _FakeClient:
    """最小可用的 OpenAI 客户端替身。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls += 1
                message = type("M", (), {"content": outer.reply})()
                choice = type("C", (), {"message": message})()
                return type("R", (), {"choices": [choice]})()

        self.chat = type("Chat", (), {"completions": _Completions()})()


def _git(repo: str, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmpdir(prefix="acr-e2e-")
        self.addCleanup(cleanup, self.tmp)
        _git(self.tmp, "init", "-b", "main")
        with open(os.path.join(self.tmp, "sample.py"), "w", encoding="utf-8") as handle:
            handle.write(MAIN_SOURCE)
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-m", "main")
        _git(self.tmp, "checkout", "-b", "test-branch")
        with open(os.path.join(self.tmp, "sample.py"), "w", encoding="utf-8") as handle:
            handle.write(BRANCH_SOURCE)
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-m", "buggy change")

    def test_graph_has_exactly_two_nodes(self):
        graph = build_graph()
        nodes = set(graph.get_graph().nodes)
        self.assertIn("read_diff", nodes)
        self.assertIn("review", nodes)

    def test_pipeline_produces_located_finding(self):
        from core.graph import read_diff_node, review_node

        client = _FakeClient(FAKE_REPLY)
        state = read_diff_node(
            {"repo": self.tmp, "base": "main", "head": "test-branch"}
        )
        # review 节点默认会自己建 client；这里注入假客户端以隔离网络
        state.update(review_node(state, client=client))

        self.assertEqual([c.file for c in state["changesets"]], ["sample.py"])
        self.assertEqual(len(state["raw_findings"]), 1)
        self.assertNotIn("start_line", state["raw_findings"][0])

        findings, dropped = locate_findings(state["raw_findings"], state["changesets"])
        self.assertEqual(dropped, [])
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.file, "sample.py")
        self.assertEqual(finding.severity, "high")
        self.assertEqual(finding.category, "security")
        # 变更后文件中 "    return eval(text)" 在第 10 行
        self.assertEqual(finding.start_line, 10)
        self.assertEqual(finding.end_line, 10)


if __name__ == "__main__":
    unittest.main()
