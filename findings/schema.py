"""核心数据契约（本阶段唯一权威定义）。

两个契约：
  * ChangeSet —— 一次变更的"可定位上下文"：文件 + 变更后的行号范围 + hunk 明细 + 新旧全文。
  * Finding   —— 一条审查结论：文件 + 行号区间 + 说明 + 严重级 + 类别。

两者都必须能被 JSON 序列化，Finding 的字段名直接对齐 ReviewBench 的
``findings.json``（``file`` / ``start_line`` / ``end_line`` / ``message`` /
``severity`` / ``category``），因此不需要额外的转换层。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 严重级：从高到低。模型必须从这里取值，否则 normalize 后落到 "medium"。
SEVERITY_LEVELS = ("critical", "high", "medium", "low", "info")
# 类别：留给模型的自由分类，未知值统一归到 "other"。
CATEGORY_LEVELS = ("bug", "security", "performance", "style", "maintainability", "other")
# 模型没给 / 给不出置信度时的兜底值：不淘汰，但标成待复核。
DEFAULT_CONFIDENCE = 0.5

_ADDED = "+"
_REMOVED = "-"
_CONTEXT = " "


@dataclass
class HunkLine:
    """hunk 内的一行。

    ``new_line`` 是该行在**变更后文件**中的行号；删除行没有新行号，为 None。
    resolver 只依赖这个字段，所以它是整条定位链路的骨架。
    """

    type: str  # "+" | "-" | " "
    content: str
    new_line: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "content": self.content, "new_line": self.new_line}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HunkLine":
        return cls(
            type=data["type"],
            content=data["content"],
            new_line=data.get("new_line"),
        )


@dataclass
class Hunk:
    """unidiff 语义下的一个 hunk，保留两侧行号区间。"""

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: List[HunkLine] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "old_start": self.old_start,
            "old_lines": self.old_lines,
            "new_start": self.new_start,
            "new_lines": self.new_lines,
            "lines": [line.to_dict() for line in self.lines],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Hunk":
        return cls(
            old_start=data["old_start"],
            old_lines=data["old_lines"],
            new_start=data["new_start"],
            new_lines=data["new_lines"],
            lines=[HunkLine.from_dict(item) for item in data.get("lines", [])],
        )


@dataclass
class ChangeSet:
    """一个文件在 base...head 之间的全部变更。

    ``head_start`` / ``head_end`` 是该文件所有新增行在变更后文件中的最小 / 最大行号，
    是"这个文件改了哪一段"的粗粒度范围；精确定位仍以 ``hunks`` 为准。
    """

    file: str
    head_start: int
    head_end: int
    hunks: List[Hunk] = field(default_factory=list)
    old_content: str = ""
    new_content: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "head_start": self.head_start,
            "head_end": self.head_end,
            "hunks": [hunk.to_dict() for hunk in self.hunks],
            "old_content": self.old_content,
            "new_content": self.new_content,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChangeSet":
        return cls(
            file=data["file"],
            head_start=data["head_start"],
            head_end=data["head_end"],
            hunks=[Hunk.from_dict(item) for item in data.get("hunks", [])],
            old_content=data.get("old_content", ""),
            new_content=data.get("new_content", ""),
        )

    def added_lines(self) -> List[HunkLine]:
        """按行号升序返回所有新增行。"""
        return sorted(
            (line for hunk in self.hunks for line in hunk.lines if line.type == _ADDED),
            key=lambda line: line.new_line or 0,
        )

    def diff_text(self) -> str:
        """还原成统一的 diff 文本，用来喂给模型当上下文。"""
        chunks: List[str] = [f"--- {self.file}"]
        for hunk in self.hunks:
            chunks.append(
                f"@@ -{hunk.old_start},{hunk.old_lines} "
                f"+{hunk.new_start},{hunk.new_lines} @@"
            )
            for line in hunk.lines:
                chunks.append(f"{line.type}{line.content}")
        return "\n".join(chunks)


@dataclass
class Finding:
    """一条审查结论。字段名即 ReviewBench findings.json 的字段名。"""

    file: str
    start_line: int
    end_line: int
    message: str
    severity: str = "medium"
    category: str = "other"
    # 模型自评的置信度（0~1）：越高越确信这是真实缺陷而不是误报。
    confidence: float = 1.0
    # 0.5 ~ 0.8 的结论不淘汰，但标出来让人看一眼（见 core/filter.py）。
    needs_review: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "message": self.message,
            "severity": self.severity,
            "category": self.category,
            "confidence": self.confidence,
            "needs_review": self.needs_review,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Finding":
        return cls(
            file=data["file"],
            start_line=int(data["start_line"]),
            end_line=int(data.get("end_line", data["start_line"])),
            message=data.get("message", ""),
            severity=normalize_severity(data.get("severity")),
            category=normalize_category(data.get("category")),
            confidence=normalize_confidence(data.get("confidence", 1.0)),
            needs_review=bool(data.get("needs_review", False)),
        )


def normalize_severity(value: Optional[str]) -> str:
    text = (value or "").strip().lower()
    return text if text in SEVERITY_LEVELS else "medium"


def normalize_category(value: Optional[str]) -> str:
    text = (value or "").strip().lower()
    return text if text in CATEGORY_LEVELS else "other"


def normalize_confidence(value: Any) -> float:
    """把模型给的置信度收敛到 0~1。

    容忍 ``None`` / ``"0.9"`` / ``90`` / ``"85%"`` 这几种写法；实在解析不出来
    （缺失、乱码、布尔值）就返回 :data:`DEFAULT_CONFIDENCE`——按"待复核"处理，
    既不误杀也不信任。
    """
    if isinstance(value, bool):
        return DEFAULT_CONFIDENCE
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value or "").strip().rstrip("%")
        try:
            number = float(text)
        except ValueError:
            return DEFAULT_CONFIDENCE
    if number > 1.0:
        # 模型有时给百分数（90 而不是 0.9）
        number = number / 100.0 if number <= 100.0 else 1.0
    return min(1.0, max(0.0, number))


def findings_to_json(findings: List[Finding], indent: int = 2) -> str:
    """导出为 findings.json 文本。"""
    return json.dumps([item.to_dict() for item in findings], ensure_ascii=False, indent=indent)


def load_findings(path: str) -> List[Finding]:
    """读回 findings.json。"""
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if isinstance(raw, dict):  # 兼容 {"findings": [...]}
        raw = raw.get("findings", [])
    return [Finding.from_dict(item) for item in raw]
