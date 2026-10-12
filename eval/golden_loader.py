"""ReviewBench golden 加载（路线 A）。

支持两种载体，都归一到 :class:`GoldenTask`：

  * ``corpus/test/test.json`` + ``corpus/test/findings.json``
    —— 25 题测试集：前者是 PR 元数据，后者按 ``pr_key`` 给 golden findings。
  * ``golden/<pr_key>.json``
    —— 全量 219 题，单文件含 PR 元数据 + findings。

``pr_key`` 的口径由官方仓库决定：``<owner>_<repo>_<pr_number>-<head 前 8 位>``。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from findings.schema import Finding


def pr_key_for(nwo: str, pr_number: int, head: str) -> str:
    """按官方口径拼 pr_key。nwo 形如 ``owner/repo``。"""
    return f"{nwo.replace('/', '_')}_{pr_number}-{head[:8]}"


@dataclass
class GoldenTask:
    """一题：PR 元数据 + 专家标注的 findings。"""

    pr_key: str
    repo: str
    pr_number: int
    base: str
    head: str
    findings: List[Finding] = field(default_factory=list)
    nwo: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pr_key": self.pr_key,
            "pr": {
                "repo": self.repo,
                "pr_number": self.pr_number,
                "base": self.base,
                "head": self.head,
            },
            "nwo": self.nwo,
            "findings": [item.to_dict() for item in self.findings],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GoldenTask":
        pr = data.get("pr") or {}
        return cls(
            pr_key=data.get("pr_key", ""),
            repo=pr.get("repo", ""),
            pr_number=int(pr.get("pr_number", 0)),
            base=pr.get("base", ""),
            head=pr.get("head", ""),
            findings=[_finding_from_golden(item) for item in data.get("findings", [])],
            nwo=data.get("nwo", ""),
        )


def _finding_from_golden(item: Dict[str, Any]) -> Finding:
    """golden 条目比 Finding 多若干标注字段，这里只取契约内的字段。"""
    return Finding(
        file=_normalize_path(item.get("file", "")),
        start_line=int(item.get("start_line", 0)),
        end_line=int(item.get("end_line", item.get("start_line", 0))),
        message=item.get("message", ""),
        severity=item.get("severity", "medium"),
        category=item.get("category", "other"),
    )


def _normalize_path(path: str) -> str:
    path = (path or "").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def load_task_file(path: str) -> GoldenTask:
    """读 ``golden/<pr_key>.json`` 这种单题文件。"""
    with open(path, "r", encoding="utf-8") as handle:
        return GoldenTask.from_dict(json.load(handle))


def load_test_set(corpus_dir: str) -> List[GoldenTask]:
    """读 25 题测试集目录（含 ``test.json`` 与 ``findings.json``）。"""
    meta_path = os.path.join(corpus_dir, "test.json")
    findings_path = os.path.join(corpus_dir, "findings.json")
    with open(meta_path, "r", encoding="utf-8") as handle:
        metas = json.load(handle)
    with open(findings_path, "r", encoding="utf-8") as handle:
        findings_by_key = json.load(handle)

    tasks: List[GoldenTask] = []
    for meta in metas:
        nwo = meta.get("nwo", "")
        head = meta.get("head", "")
        pr_number = int(meta.get("pr_number", 0))
        key = pr_key_for(nwo, pr_number, head)
        raw_findings = findings_by_key.get(key, [])
        tasks.append(
            GoldenTask(
                pr_key=key,
                repo=meta.get("repo", ""),
                pr_number=pr_number,
                base=meta.get("base", ""),
                head=head,
                findings=[_finding_from_golden(item) for item in raw_findings],
                nwo=nwo,
            )
        )
    return tasks


def load_predictions(path: str) -> Tuple[Dict[str, Any], List[Finding]]:
    """读预测结果。两种格式都吃：

      * 官方提交格式 ``{"pr": {...}, "agent": ..., "findings": [...]}``
      * 本项目 cli.py 直接输出的裸数组 ``[Finding, ...]``
    """
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)

    if isinstance(raw, dict):
        pr_meta = raw.get("pr") or {}
        items = raw.get("findings", [])
    else:
        pr_meta, items = {}, raw

    findings: List[Finding] = []
    for item in items:
        file = _normalize_path(item.get("file", ""))
        if not file:
            continue
        findings.append(
            Finding(
                file=file,
                start_line=int(item.get("start_line", 0)),
                end_line=int(item.get("end_line", item.get("start_line", 0))),
                message=item.get("message", ""),
                severity=item.get("severity", "medium"),
                category=item.get("category", "other"),
            )
        )
    return pr_meta, findings


def identify_task(pr_meta: Dict[str, Any], tasks: Sequence[GoldenTask]) -> Optional[GoldenTask]:
    """按 head + pr_number 把预测结果对到某一题上。"""
    head = pr_meta.get("head", "")
    pr_number = pr_meta.get("pr_number")
    for task in tasks:
        if head and task.head != head:
            continue
        if pr_number is not None and task.pr_number != int(pr_number):
            continue
        return task
    return None
