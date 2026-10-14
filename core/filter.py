"""降误报的确定性过滤层：类型白名单 + 文件级评分 + 置信度闸 + 语义聚类去重。

和 ``findings/postprocess.py`` 的分工：
  * 本模块管**语义 / 范围层面**的减法——该不该报这类问题（类型白名单）、
    这个文件值不值得花一次模型调用（文件级评分）、模型自己说有多大把握
    （confidence）、同文件里换着说法重复报的同一处问题（message bigram 重合）。
  * ``postprocess.py`` 管**结构层面**的减法——行区间重叠、severity、数量上限。

所有闸都只做减法，不改行号、不动 resolver。

四道闸：

1. ``filter_files_by_score``（**定位前、LLM 前**）：给每个被改文件打 0~1 分，
   ``< FILE_SCORE_THRESHOLD`` 的文件直接不送 LLM（省 token 的那道闸）。
   计分：production 逻辑 +0.4 / 改动 > 5 行 +0.3 / 敏感操作（DB·文件·网络·权限）+0.3。
2. ``filter_by_type``（定位前）：类型白名单，只放行"必报"的漏洞与缺陷类型。
3. ``filter_by_confidence``（定位前）：``>= 0.8`` 保留；``0.5 ~ 0.8`` 保留并标
   ``needs_review``；``< 0.5`` 丢弃。
4. ``cluster_findings``（定位后）：同文件 + message bigram 重合度（Dice）``>= 0.6``
   视为同一组（并查集传递闭包），每组只留"最具体"的一条——行区间最窄（行号最精确）
   → message 最长（描述最详细）→ 置信度最高 → severity 最高。
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from core.triage import skip_reason
from findings.postprocess import severity_rank
from findings.schema import ChangeSet, Finding, normalize_confidence

# 置信度分档阈值
CONFIDENCE_KEEP = 0.8
CONFIDENCE_REVIEW = 0.5

# 同文件 message bigram 重合度阈值（Dice 系数）
DEFAULT_SIMILARITY = 0.6

# 文件级评分：三项信号各占多少，以及低于多少分直接不送 LLM。
FILE_SCORE_PRODUCTION = 0.4
FILE_SCORE_CHANGED_LINES = 0.3
FILE_SCORE_SENSITIVE = 0.3
FILE_SCORE_THRESHOLD = 0.5
# "改动 > 5 行"的门槛：新增行 + 删除行
LARGE_CHANGE_LINES = 5

# 去掉空白与标点，保留字母/数字/下划线/CJK——bigram 只在"实义字符"上算
_NOISE = re.compile(r"[^\w]+", re.UNICODE)


# ---------------------------------------------------------------------------
# 手段 1：类型白名单
# ---------------------------------------------------------------------------

# 必报：只看这六类（安全漏洞 / 空值 / 越界 / 资源泄漏）
TYPE_SQL_INJECTION = "sql_injection"
TYPE_PATH_TRAVERSAL = "path_traversal"
TYPE_XSS = "xss"
TYPE_NULL_POINTER = "null_pointer"
TYPE_OUT_OF_BOUNDS = "out_of_bounds"
TYPE_RESOURCE_LEAK = "resource_leak"

# 不报：这些是"不算问题"的类别，命中即丢
DROP_NAMING = "naming"
DROP_COMMENT = "comment"
DROP_STYLE = "style"
DROP_DOCS = "docs"
DROP_FORMAT = "format"
DROP_TEST = "test"
DROP_REFACTOR = "refactor"

WHITELIST_TYPES: Tuple[str, ...] = (
    TYPE_SQL_INJECTION,
    TYPE_PATH_TRAVERSAL,
    TYPE_XSS,
    TYPE_NULL_POINTER,
    TYPE_OUT_OF_BOUNDS,
    TYPE_RESOURCE_LEAK,
)

# 命中即丢的类别（"不报"那一栏）
BLACKLIST_TYPES: Tuple[str, ...] = (
    DROP_NAMING,
    DROP_COMMENT,
    DROP_STYLE,
    DROP_DOCS,
    DROP_FORMAT,
    DROP_TEST,
    DROP_REFACTOR,
)

# 六类"必报"的关键词表。中英双语，因为模型的 message 两种语言都写。
WHITELIST_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        (
            TYPE_SQL_INJECTION,
            r"sql\s*(注入|injection)|注入\s*sql|sqli|"
            r"拼接\s*(sql|查询|语句)|(sql|query|查询|语句)[^。\n]{0,12}字符串(拼接|连接)|"
            r"参数化查询|parameteriz|prepared statement",
        ),
        (
            TYPE_PATH_TRAVERSAL,
            r"路径(穿越|遍历|注入)|目录(穿越|遍历)|path traversal|directory traversal|"
            r"\.\./|绝对路径|absolute path|未(校验|过滤|净化)[^。\n]{0,6}(路径|文件名)|"
            r"(路径|文件名|path|filename)[^。\n]{0,10}(校验不充分|缺少校验|未校验|未过滤)|"
            r"path[^。\n]{0,10}(sanitiz|validat|normaliz)",
        ),
        (
            TYPE_XSS,
            r"\bxss\b|跨站脚本|脚本注入|html\s*注入|innerhtml|dangerouslysetinnerhtml|"
            r"v-html|未(转义|escape)|unescaped|output encoding",
        ),
        (
            TYPE_NULL_POINTER,
            r"空指针|null\s*pointer|null\s*(dereference|check|safe)|npe\b|"
            r"(未|没有|缺少|不做)[^。\n]{0,8}(空值|null|none)[^。\n]{0,8}(检查|校验|判断)|"
            r"(可能|会)为\s*(null|none|nil|undefined)|"
            r"(null|none|nil|undefined)[^。\n]{0,10}(check|guard|assert)|属性为空|值为空|对象为空",
        ),
        (
            TYPE_OUT_OF_BOUNDS,
            r"越界|数组下标|索引越界|index\s*out\s*of\s*(bounds|range)|out[- ]of[- ]bounds|"
            r"off[- ]by[- ]one|(下标|索引|index)[^。\n]{0,8}(超出|越界|越出)|"
            r"(length|size|len)\b[^。\n]{0,10}(边界|越界)|边界条件[^。\n]{0,6}(数组|下标|索引)",
        ),
        (
            TYPE_RESOURCE_LEAK,
            r"资源(泄漏|泄露|未释放)|内存(泄漏|泄露)|句柄(泄漏|未释放)|存储泄漏|"
            r"(文件|流|连接|socket|cursor|transaction|句柄|handle)[^。\n]{0,8}"
            r"(未|没有|忘记|不)[^。\n]{0,4}(关闭|释放|回收)|"
            r"(泄漏|泄露|leak)|\bleak\b|"
            r"(finally|try[- ]with[- ]resources|dispose|close\(\))[^。\n]{0,12}"
            r"(close|release|dispose|释放|关闭)|unclosed|not (closed|released)|"
            r"(open|打开)[^。\n]{0,16}(没有|未|缺少)[^。\n]{0,8}(close|关闭|释放)",
        ),
    )
)

# "不报"那一栏的关键词表。顺序敏感：**先窄后宽**，宽规则（refactor）放最后，
# 否则"缺少注释"会被"建议补充"这类泛语吃掉。
BLACKLIST_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        (
            DROP_NAMING,
            r"命名|取名|拼写|[nm]aming|rename|变量名|函数名|类名|标识符|命名风格|camelcase|snake_case",
        ),
        (
            DROP_COMMENT,
            r"注释|文档字符串|docstring|缺少说明|补充说明|\bcomments?\b|注释(过时|不符|不一致)",
        ),
        (
            DROP_STYLE,
            r"\bcss\b|样式表|样式|主题|主题色|颜色值|配色|对比度|contrast|hover|字体|字号|"
            r"css\s*变量|css\s*variable|styling|视觉|图标|间距|圆角|布局(微调)?",
        ),
        (
            DROP_DOCS,
            r"\breadme\b|changelog|文档|documentation|\bdocs?\b|模板文本|使用说明|指南",
        ),
        (
            DROP_FORMAT,
            r"格式|缩进|换行符|尾随空格|分号|引号风格|格式化|\bformat(ting)?\b|lint|prettier|"
            r"行宽|排版|空行",
        ),
        (
            DROP_TEST,
            r"测试(用例|覆盖|文件|代码)|单元测试|\btests?\b|\bspec\b|用例覆盖|mock(数据)?|"
            r"断言|覆盖率|coverage",
        ),
        (
            DROP_REFACTOR,
            r"重构|可读性|可维护性|优雅|简洁|重复代码|提取(函数|方法|常量)|命名空间|"
            r"建议(改|用|拆|合并|提取|补充|调整)|不妨|更好的写法|refactor|readability|maintainab|"
            r"magic number|魔法数字|代码风格|dead code|未使用的(变量|导入|代码)|"
            r"unused|缓存(优化)?",
        ),
    )
)

# 白名单过滤的两种口径：
#   "blacklist" 只丢"不报"那一栏（宽松，保召回）
#   "strict"    必须命中六类必报之一（严，保精度）
TYPE_MODE_BLACKLIST = "blacklist"
TYPE_MODE_STRICT = "strict"
DEFAULT_TYPE_MODE = TYPE_MODE_BLACKLIST


def classify_type(message: Any, category: Any = None) -> str | None:
    """给一条结论归到类型白名单/黑名单的某一类；都不命中返回 ``None``。

    两段式匹配，避免宽规则吃掉窄规则：

      1. "不报"那栏的**窄规则**（命名 / 注释 / CSS / 文档 / 格式 / 测试）先扫；
      2. "必报"六类——它们比"泛泛建议重构"更具体，所以排在 refactor 之前；
      3. 最后才是 refactor 这类宽规则（"建议…"、"可读性"）。

    只看 message，外加一个 category 兜底：模型标了 ``style`` 就当样式。
    """
    text = str(message or "")
    hint = str(category or "").strip().lower()

    if hint == "style":
        return DROP_STYLE

    narrow = {DROP_NAMING, DROP_COMMENT, DROP_STYLE, DROP_DOCS, DROP_FORMAT, DROP_TEST}
    for name, pattern in BLACKLIST_PATTERNS:
        if name in narrow and pattern.search(text):
            return name
    for name, pattern in WHITELIST_PATTERNS:
        if pattern.search(text):
            return name
    for name, pattern in BLACKLIST_PATTERNS:
        if name not in narrow and pattern.search(text):
            return name
    return None


@dataclass
class TypeStats:
    """类型白名单砍掉了多少、按类别各多少。"""

    input: int = 0
    kept: int = 0
    dropped: int = 0
    per_type: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input": self.input,
            "kept": self.kept,
            "dropped": self.dropped,
            "per_type": dict(sorted(self.per_type.items())),
        }


def filter_by_type(
    findings: Sequence[Dict[str, Any]], mode: str = DEFAULT_TYPE_MODE
) -> Tuple[List[Dict[str, Any]], TypeStats]:
    """类型白名单（定位前的原始结论）。

    ``mode="blacklist"``：只丢"不报"类别（命名 / 注释 / CSS / 文档 / 格式 / 测试 / 重构）。
    ``mode="strict"``：必须命中"必报"六类之一，其余全丢。
    """
    stats = TypeStats(input=len(findings))
    kept: List[Dict[str, Any]] = []
    for item in findings:
        label = classify_type(item.get("message"), item.get("category"))
        drop = label in BLACKLIST_TYPES or (
            mode == TYPE_MODE_STRICT and label not in WHITELIST_TYPES
        )
        if drop:
            stats.dropped += 1
            key = label or "unclassified"
            stats.per_type[key] = stats.per_type.get(key, 0) + 1
            continue
        kept.append(item)
    stats.kept = len(kept)
    return kept, stats


# ---------------------------------------------------------------------------
# 手段 2：文件级评分
# ---------------------------------------------------------------------------

# 只有这些后缀算"代码/配置逻辑"；其余（.json/.md/图片…）拿不到 production 那 0.4 分。
# CI 工作流与基础设施配置也算逻辑——它们一样会改坏，也确实有人在 review 里报出来。
CODE_SUFFIXES: Tuple[str, ...] = (
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".java", ".kt", ".kts", ".scala", ".go", ".rs", ".rb", ".php", ".cs", ".fs",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".m", ".mm", ".swift", ".dart",
    ".ex", ".exs", ".erl", ".lua", ".pl", ".pm", ".sh", ".bash", ".zsh", ".ps1",
    ".sql", ".vue", ".svelte", ".astro",
    # 基础设施 / CI / 构建
    ".yml", ".yaml", ".tf", ".hcl", ".dockerfile", ".mk", ".cmake", ".gradle",
    ".toml", ".ini", ".cfg", ".conf", ".properties", ".proto",
)

# 没有后缀的构建/CI 文件（basename 精确匹配）
CODE_FILENAMES: Tuple[str, ...] = (
    "dockerfile",
    "makefile",
    "jenkinsfile",
    "vagrantfile",
    "procfile",
    "brewfile",
)

# 敏感操作：DB / 文件 / 网络 / 权限 / 序列化 / 进程
_SENSITIVE_PATTERN = re.compile(
    r"(?:\bselect\b|\binsert\b|\bupdate\b|\bdelete\s+from\b|\bexecute\b|executemany|\bcursor\b|"
    r"\bsql\b|sqlite|postgres|mysql|mongo|redis|\borm\b|\bquery\b|transaction|\bcommit\b|rollback|"
    r"\bconn(ection)?\b|\bopen\s*\(|readfile|writefile|fs\.|filesystem|file\s*(path|handle)|"
    r"path\.join|os\.path|shutil|tempfile|upload|download|"
    r"requests?\.|httpx|urllib|\bfetch\s*\(|axios|\bsocket\b|websocket|"
    r"chmod|chown|permission|权限|鉴权|\bauth\b|\btoken\b|\bjwt\b|\bsession\b|\bcookie\b|oauth|"
    r"pickle|yaml\.load|\beval\s*\(|\bexec\s*\(|subprocess|child_process|os\.system|\bshell\b)",
    re.IGNORECASE,
)


@dataclass
class FileScore:
    """一个被改文件的分数与理由。"""

    file: str
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    changed_lines: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "score": round(self.score, 3),
            "reasons": list(self.reasons),
            "changed_lines": self.changed_lines,
        }


def changed_line_count(changeset: ChangeSet) -> int:
    """新增行 + 删除行的条数（"改动 > 5 行"的分母）。"""
    return sum(1 for hunk in changeset.hunks for line in hunk.lines if line.type in ("+", "-"))


def is_production_path(path: str) -> bool:
    """代码 / 配置逻辑，且不是测试 / 文档 / 样式 / 依赖清单 / 生成物。"""
    normalized = (path or "").replace("\\", "/").lower()
    name = posixpath.basename(normalized)
    if name in CODE_FILENAMES:
        return skip_reason(path) is None
    if not name.endswith(CODE_SUFFIXES):
        return False
    return skip_reason(path) is None


def has_sensitive_operation(changeset: ChangeSet) -> bool:
    """改动里有没有 DB / 文件 / 网络 / 权限这类敏感操作。只看新增与删除行。"""
    for hunk in changeset.hunks:
        for line in hunk.lines:
            if line.type in ("+", "-") and _SENSITIVE_PATTERN.search(line.content or ""):
                return True
    return False


def score_changeset(changeset: ChangeSet) -> FileScore:
    """文件级评分（0~1）：production +0.4 / 改动 >5 行 +0.3 / 敏感操作 +0.3。"""
    score = FileScore(file=changeset.file, changed_lines=changed_line_count(changeset))
    if is_production_path(changeset.file):
        score.score += FILE_SCORE_PRODUCTION
        score.reasons.append(f"production+{FILE_SCORE_PRODUCTION}")
    if score.changed_lines > LARGE_CHANGE_LINES:
        score.score += FILE_SCORE_CHANGED_LINES
        score.reasons.append(f"changed_lines>{LARGE_CHANGE_LINES}+{FILE_SCORE_CHANGED_LINES}")
    if has_sensitive_operation(changeset):
        score.score += FILE_SCORE_SENSITIVE
        score.reasons.append(f"sensitive+{FILE_SCORE_SENSITIVE}")
    return score


def should_review_file(changeset: ChangeSet, threshold: float = FILE_SCORE_THRESHOLD) -> bool:
    return score_changeset(changeset).score >= threshold


@dataclass
class FileScoreStats:
    """文件级评分闸：跳过多少文件、少送多少条结论。"""

    input_files: int = 0
    input_findings: int = 0
    kept_files: int = 0
    skipped_files: int = 0
    skipped_findings: int = 0
    skipped_detail: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_files": self.input_files,
            "input_findings": self.input_findings,
            "kept_files": self.kept_files,
            "skipped_files": self.skipped_files,
            "skipped_findings": self.skipped_findings,
            "skipped_detail": list(self.skipped_detail),
        }


def filter_files_by_score(
    changesets: Sequence[ChangeSet],
    findings: Sequence[Dict[str, Any]] = (),
    threshold: float = FILE_SCORE_THRESHOLD,
) -> Tuple[List[ChangeSet], List[Dict[str, Any]], FileScoreStats]:
    """文件级评分闸（**在 LLM 调用之前**）。

    低于阈值的文件整份不送审——既省掉那次模型调用，也丢掉挂在它上面的原始结论。
    返回 ``(要审的 changesets, 要审的原始结论, 统计)``。
    """
    stats = FileScoreStats(input_files=len(changesets), input_findings=len(findings))
    kept_changesets: List[ChangeSet] = []
    for changeset in changesets:
        score = score_changeset(changeset)
        if score.score >= threshold:
            kept_changesets.append(changeset)
            continue
        stats.skipped_files += 1
        stats.skipped_detail.append(score.to_dict())

    kept_paths = {item.file for item in kept_changesets}
    kept_findings = [item for item in findings if item.get("file") in kept_paths]
    stats.kept_files = len(kept_changesets)
    stats.skipped_findings = len(findings) - len(kept_findings)
    return kept_changesets, kept_findings, stats


def normalize_text(text: Any) -> str:
    """小写 + 去标点空白，作为 bigram 的输入。"""
    return _NOISE.sub("", str(text or "").lower())


def bigrams(text: Any) -> frozenset:
    """字符二元组集合。中英文都能用，不需要分词器。"""
    cleaned = normalize_text(text)
    if len(cleaned) < 2:
        return frozenset({cleaned} if cleaned else [])
    return frozenset(cleaned[i : i + 2] for i in range(len(cleaned) - 1))


def bigram_similarity(left: Any, right: Any) -> float:
    """两条 message 的 bigram 重合度（Dice 系数，0~1）。"""
    a, b = bigrams(left), bigrams(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return 2 * len(a & b) / (len(a) + len(b))


@dataclass
class ConfidenceStats:
    """置信度闸砍掉了多少。"""

    input: int = 0
    kept: int = 0
    needs_review: int = 0
    dropped_low_confidence: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "input": self.input,
            "kept": self.kept,
            "needs_review": self.needs_review,
            "dropped_low_confidence": self.dropped_low_confidence,
        }


def filter_by_confidence(
    findings: Sequence[Dict[str, Any]],
    keep_threshold: float = CONFIDENCE_KEEP,
    review_threshold: float = CONFIDENCE_REVIEW,
) -> Tuple[List[Dict[str, Any]], ConfidenceStats]:
    """置信度闸：丢弃 < 0.5，0.5~0.8 标 ``needs_review``，>= 0.8 直接过。

    作用在**定位前**的原始结论上——低置信度的条目不必再花定位预算。
    """
    stats = ConfidenceStats(input=len(findings))
    kept: List[Dict[str, Any]] = []
    for item in findings:
        confidence = normalize_confidence(item.get("confidence"))
        if confidence < review_threshold:
            stats.dropped_low_confidence += 1
            continue
        if confidence < keep_threshold:
            stats.needs_review += 1
            kept.append({**item, "confidence": confidence, "needs_review": True})
            continue
        kept.append({**item, "confidence": confidence, "needs_review": False})
    stats.kept = len(kept)
    return kept, stats


@dataclass
class ClusterStats:
    """聚类去重前后对比。"""

    input: int = 0
    kept: int = 0
    merged: int = 0
    clusters: int = 0  # 成员 >= 2 的组数

    def to_dict(self) -> Dict[str, int]:
        return {
            "input": self.input,
            "kept": self.kept,
            "merged": self.merged,
            "clusters": self.clusters,
        }


def _specificity_key(finding: Finding) -> Tuple[int, int, float, int, int, int]:
    """"最具体"的排序键：行号最精确 → 描述最详细 → 置信度最高 → severity 最高。"""
    span = abs(finding.end_line - finding.start_line)
    return (
        span,
        -len((finding.message or "").strip()),
        -normalize_confidence(getattr(finding, "confidence", 1.0)),
        -severity_rank(finding.severity),
        finding.start_line,
        finding.end_line,
    )


def _union_find_groups(findings: Sequence[Finding], threshold: float) -> List[List[int]]:
    """同文件 + message bigram 重合度 >= threshold 的传递闭包分组。"""
    count = len(findings)
    parent = list(range(count))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    # 预先算好 bigram，避免 O(n^2) 次重复解析
    gram_cache = [bigrams(item.message) for item in findings]
    for i in range(count):
        for j in range(i + 1, count):
            if findings[i].file != findings[j].file:
                continue
            a, b = gram_cache[i], gram_cache[j]
            if not a or not b:
                continue
            similarity = 1.0 if a == b else 2 * len(a & b) / (len(a) + len(b))
            if similarity >= threshold:
                union(i, j)

    buckets: Dict[int, List[int]] = {}
    for index in range(count):
        buckets.setdefault(find(index), []).append(index)
    return list(buckets.values())


def cluster_findings(
    findings: Sequence[Finding], threshold: float = DEFAULT_SIMILARITY
) -> Tuple[List[Finding], ClusterStats]:
    """按"同文件 + message 语义相似"聚组，每组只保留最具体的一条。"""
    stats = ClusterStats(input=len(findings))
    if not findings:
        return [], stats

    kept: List[Finding] = []
    for group in _union_find_groups(findings, threshold):
        if len(group) > 1:
            stats.clusters += 1
        best = min((findings[index] for index in group), key=_specificity_key)
        kept.append(best)

    kept.sort(key=lambda item: (item.file, item.start_line, item.end_line))
    stats.kept = len(kept)
    stats.merged = stats.input - stats.kept
    return kept, stats
