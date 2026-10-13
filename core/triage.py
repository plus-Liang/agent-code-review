"""分诊：在花钱深审之前，先用确定性规则把不值得审的文件筛掉。

**不调 LLM**，纯路径规则，因此可复现、零成本、可单测。它只回答一个问题：
"这个文件值不值得额外花一次模型调用？"

跳过（suffix / 目录 / 文件名任一中即跳过）：
  * 测试：``__tests__/``、``tests/``、``test/``、``spec/``、``*.test.*``、``*.spec.*``、``test_*``、``*_test.py``
  * 样式：``.css``/``.scss``/``.sass``/``.less``/``.styl``
  * 文档：``.md``/``.mdx``/``.rst``
  * 锁与依赖清单：``*.lock``、``package.json``、``requirements*.txt``、``go.mod``、``Cargo.toml`` …
  * 快照与生成物：``.snap``、``.min.js``、``dist/``、``build/``、``vendor/``

保留：其余一切（``src/``/``lib/``/``app/`` 这类 production 路径天然落在保留侧）。

注意：分诊只影响"要不要深审"，不改变 ``changesets`` 本身——定位仍然在全部变更文件上做，
所以不会因为跳过某个文件而让已有的 finding 定位失败。
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from findings.schema import ChangeSet

SKIP_DIR_MARKERS: Tuple[str, ...] = (
    "__tests__/",
    "tests/",
    "test/",
    "spec/",
    "__mocks__/",
    "e2e/",
    "fixtures/",
    "dist/",
    "build/",
    "vendor/",
    "node_modules/",
)

SKIP_SUFFIXES: Tuple[str, ...] = (
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".styl",
    ".md",
    ".mdx",
    ".rst",
    ".snap",
    ".lock",
    ".min.js",
    ".min.css",
    ".map",
)

SKIP_FILENAMES: Tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "requirements.txt",
    "requirements-dev.txt",
    "poetry.lock",
    "pipfile",
    "pipfile.lock",
    "go.mod",
    "go.sum",
    "cargo.toml",
    "cargo.lock",
    "composer.lock",
    "gemfile.lock",
    "nuget.config",
)

TEST_NAME_MARKERS: Tuple[str, ...] = (".test.", ".spec.", "_test.", ".d.ts")


@dataclass
class TriageItem:
    """一个被跳过的文件及其原因。"""

    file: str
    reason: str

    def to_dict(self) -> Dict[str, str]:
        return {"file": self.file, "reason": self.reason}


@dataclass
class TriageResult:
    review: List[ChangeSet] = field(default_factory=list)
    skipped: List[TriageItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "review": [item.file for item in self.review],
            "skipped": [item.to_dict() for item in self.skipped],
        }


def skip_reason(path: str) -> str | None:
    """返回跳过原因；返回 None 表示保留（值得深审）。"""
    normalized = (path or "").replace("\\", "/").lstrip("./").lower()
    if not normalized:
        return "empty-path"

    name = posixpath.basename(normalized)

    for marker in SKIP_DIR_MARKERS:
        if marker in normalized:
            return "test-or-generated-dir" if marker not in ("dist/", "build/", "vendor/", "node_modules/") else "generated"

    if name in SKIP_FILENAMES:
        return "dependency-manifest"
    if name.endswith(".lock") or name.endswith("-lock.json") or name.endswith("lock.yaml"):
        return "lockfile"

    for marker in TEST_NAME_MARKERS:
        if marker in name:
            return "test-file"
    if name.startswith("test_") or name.startswith("spec_") or "_test." in name or "_spec." in name:
        return "test-file"

    for suffix in SKIP_SUFFIXES:
        if name.endswith(suffix):
            if suffix in (".md", ".mdx", ".rst"):
                return "docs"
            if suffix in (".css", ".scss", ".sass", ".less", ".styl", ".min.css"):
                return "style"
            if suffix in (".snap", ".map", ".min.js"):
                return "generated"
            return "lockfile"
    return None


def triage(changesets: Sequence[ChangeSet]) -> TriageResult:
    """按规则切分：要深审的 / 跳过的。"""
    result = TriageResult()
    for changeset in changesets:
        reason = skip_reason(changeset.file)
        if reason is None:
            result.review.append(changeset)
        else:
            result.skipped.append(TriageItem(file=changeset.file, reason=reason))
    return result
