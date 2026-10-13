"""上下文供给（三层）。

模型不是不够聪明，是没看到足够上下文——本模块只做"往 prompt 里补上下文"这一件事，
不调 LLM、不改任何结论，纯本地确定性计算。

* **第一层 同文件上下文**：被改文件的**非 diff 部分**（import / 类型与类定义 / 其它函数），
  超过 ``SAME_FILE_MAX_LINES`` 行时按相关性（import > 类型 > 离改动近的函数）截断。
* **第二层 调用链上下文**：被改函数的**调用方**与**被调用方**片段，各取前后
  ``SNIPPET_RADIUS`` 行；Python 走 ``ast``，其它语言走正则 + 缩进/花括号配平。
  整层文本上限 ``CALLCHAIN_MAX_CHARS``（≈8k token），超了按相关度截断。
* **第三层 框架文档**：从 import 与 API 指纹检测框架/库，从内置文档片段里检索 top-3
  注入。优先用 ``fastembed`` 做向量检索；装不上/下载不了时**自动降级**为词法检索
  （IDF 加权 token 重叠），不报错、不阻塞。检测不到框架就整层跳过。

开关：环境变量 ``ACR_CONTEXT_LAYERS``，逗号分隔，默认 ``"1,2,3"``。
"""

from __future__ import annotations

import ast
import math
import os
import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from findings.schema import ChangeSet

# ---------------------------------------------------------------- 可调参数

SAME_FILE_MAX_LINES = 100  # 第一层：同文件非 diff 内容上限（行）
SAME_FILE_IMPORT_CAP = 30  # 第一层：import 段最多占多少行
SAME_FILE_DEF_CAP = 45  # 第一层：单个函数/类块最多占多少行
SNIPPET_RADIUS = 20  # 第二层：调用点/定义前后取多少行
CALLCHAIN_MAX_CHARS = 24000  # 第二层：整层文本上限（≈8k token）
CALLCHAIN_MAX_CALLEES = 3  # 第二层：每个文件最多追几个被调用函数
CALLCHAIN_MAX_CALLERS = 3  # 第二层：每个文件最多追几个调用方
DOCS_TOP_K = 3  # 第三层：注入几条文档

_DEFAULT_LAYERS = "1,2,3"


def enabled_layers() -> Tuple[str, ...]:
    """读环境变量决定启用哪几层（未识别的值忽略）。"""
    raw = os.environ.get("ACR_CONTEXT_LAYERS", _DEFAULT_LAYERS)
    picked = [item.strip() for item in raw.split(",")]
    return tuple(item for item in ("1", "2", "3") if item in picked)


# ---------------------------------------------------------------- 小工具

_ENCODING_FALLBACK = "utf-8"


def _changed_new_lines(changeset: ChangeSet) -> Set[int]:
    """被改动的新侧行号集合（只看新增行；删除行没有新行号）。"""
    return {
        line.new_line
        for line in changeset.added_lines()
        if isinstance(line.new_line, int)
    }


def _numbered(title: str, lines: Sequence[str], start: int) -> str:
    """把一段代码渲染成 ``L12: code`` 的带行号块（行号对模型理解上下文很关键）。"""
    body = "\n".join(f"L{start + offset}: {text}" for offset, text in enumerate(lines))
    return f"--- {title} ---\n{body}"


def _clip_block(lines: Sequence[str], start: int, limit: int) -> List[str]:
    """超长块裁剪：保留前 2/3 与后 1/3，中间用省略号占位。"""
    if len(lines) <= limit:
        return list(lines)
    head = max(1, (limit * 2) // 3)
    tail = max(0, limit - head - 1)
    clipped = list(lines[:head])
    clipped.append(f"...（此处省略 {len(lines) - head - tail} 行）...")
    if tail:
        clipped.extend(lines[len(lines) - tail :])
    return clipped


# ================================================================ 第一层


_GENERIC_IMPORT_RE = re.compile(
    r"^\s*(?:#\s*)?(?:import|from|use|using|require|include|#include|extern\s+crate|"
    r"package|pub\s+use|mod|export\s+\*|from\s+\S+\s+import)\b",
    re.I,
)


def _python_blocks(lines: Sequence[str]) -> Optional[List[Tuple[str, str, int, int]]]:
    """用 ast 切出 (kind, name, start, end)（1 起算、含端点）；语法错误返回 None。"""
    source = "\n".join(lines)
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    blocks: List[Tuple[str, str, int, int]] = []
    import_start: Optional[int] = None
    import_end: Optional[int] = None

    for node in tree.body:
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start)
        decorators = getattr(node, "decorator_list", None) or []
        if decorators:
            start = min(start, min(item.lineno for item in decorators))

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_start = start if import_start is None else min(import_start, start)
            import_end = end if import_end is None else max(import_end, end)
        elif isinstance(node, ast.ClassDef):
            blocks.append(("class", node.name, start, end))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            blocks.append(("func", node.name, start, end))
        elif isinstance(node, ast.Assign) and start <= 60:
            names = [
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            ]
            if names and all(name.isupper() or name[:1].isupper() for name in names):
                blocks.append(("const", ", ".join(names), start, end))
        elif isinstance(node, ast.AnnAssign) and start <= 60:
            target = node.target
            if isinstance(target, ast.Name):
                blocks.append(("type", target.id, start, end))

    if import_start is not None and import_end is not None:
        blocks.insert(0, ("imports", "import", import_start, import_end))
    return blocks


def _generic_signature_lines(lines: Sequence[str]) -> List[int]:
    """非 Python：挑出看起来像定义的签名行（1 起算）。"""
    pattern = re.compile(
        r"^\s*(?:export\s+|pub\s+|public\s+|private\s+|protected\s+|internal\s+|"
        r"static\s+|async\s+|final\s+|override\s+|virtual\s+|abstract\s+|sealed\s+|"
        r"def\s+|func\s+|function\s+|fn\s+|sub\s+|fun\s+|class\s+|struct\s+|"
        r"interface\s+|enum\s+|trait\s+|impl\s+|type\s+|module\s+|namespace\s+)*"
        r"[\w<>\[\],\.\?&\*\s]*\b\w+\s*\([^;{}]*\)\s*(?:->\s*[^;{]+)?"
        r"(?::\s*[^;{]+)?\s*[{:]?\s*$"
    )
    keyword = re.compile(
        r"^\s*(?:export\s+|pub\s+|public\s+|private\s+|protected\s+|internal\s+|"
        r"static\s+|async\s+)*(?:def|func|function|fn|sub|fun|class|struct|interface|"
        r"enum|trait|impl|module|namespace|type)\b"
    )
    hits: List[int] = []
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "#", "*", "/*")):
            continue
        if keyword.match(line) or pattern.match(line):
            hits.append(index)
    return hits


def same_file_context(changeset: ChangeSet, max_lines: int = SAME_FILE_MAX_LINES) -> str:
    """第一层：被改文件的非 diff 部分，按 import > 类型/类 > 邻近函数排序。"""
    lines = (changeset.new_content or "").splitlines()
    if not lines:
        return ""

    changed = _changed_new_lines(changeset)
    blocks = _python_blocks(lines)

    picked: List[str] = []
    budget = max_lines

    if blocks:
        imports = next((item for item in blocks if item[0] == "imports"), None)
        ordered: List[Tuple[str, str, int, int]] = []
        if imports:
            ordered.append(imports)
        ordered.extend(item for item in blocks if item[0] in ("class", "const", "type"))
        # 改动本身所在的函数：只给签名（函数体已经在 diff 里了）
        changed_funcs = [
            item
            for item in blocks
            if item[0] == "func"
            and any(item[2] <= changed_line <= item[3] for changed_line in changed)
        ]
        # 其它函数：按"离改动区间最近"排序
        nearby = []
        anchor = min(changed) if changed else 1
        for kind, name, start, end in blocks:
            if kind != "func":
                continue
            if any(start <= changed_line <= end for changed_line in changed):
                continue
            nearby.append((abs(start - anchor), kind, name, start, end))
        nearby.sort()
        ordered.extend((kind, name, start, end) for _, kind, name, start, end in nearby)

        seen: Set[Tuple[int, int]] = set()
        for kind, name, start, end in ordered:
            if budget <= 0:
                break
            key = (start, end)
            if key in seen:
                continue
            seen.add(key)
            cap = SAME_FILE_IMPORT_CAP if kind == "imports" else SAME_FILE_DEF_CAP
            cap = min(cap, budget)
            snippet = _clip_block(lines[start - 1 : end], start, cap + 1)
            title = f"[非 diff] {name} (L{start}-L{end})"
            picked.append(_numbered(title, snippet, start))
            budget -= len(snippet) + 1

        for kind, name, start, end in changed_funcs:
            if budget <= 0:
                break
            signature = lines[start - 1].strip()
            picked.append(
                _numbered(f"[非 diff] 被改函数签名 {name} (L{start})", [signature], start)
            )
            budget -= 2
    else:
        import_lines = [
            index
            for index, line in enumerate(lines, start=1)
            if _GENERIC_IMPORT_RE.match(line)
        ][:SAME_FILE_IMPORT_CAP]
        for index in import_lines:
            if budget <= 0:
                break
            picked.append(_numbered("[非 diff] import", [lines[index - 1]], index))
            budget -= 1

        signatures = _generic_signature_lines(lines)
        emitted: Set[int] = set()
        for index in signatures:
            if budget <= 0:
                break
            if any(start <= index <= end for start, end in _changed_spans(changeset)):
                continue
            if index in emitted:
                continue
            emitted.add(index)
            picked.append(_numbered("[非 diff] 定义索引", [lines[index - 1]], index))
            budget -= 1

        if budget > 0:
            head = _clip_block(lines[:60], 1, budget)
            picked.append(_numbered("[非 diff] 文件头部", head, 1))

    if not picked:
        return ""
    return "\n".join(picked)


def _changed_spans(changeset: ChangeSet) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    for hunk in changeset.hunks:
        numbers = [line.new_line for line in hunk.lines if isinstance(line.new_line, int)]
        if numbers:
            spans.append((min(numbers), max(numbers)))
    return spans


# ================================================================ 第二层


_PY_KEYWORDS = {
    "if", "for", "while", "return", "print", "len", "range", "int", "str", "list",
    "dict", "set", "tuple", "float", "bool", "super", "self", "cls", "isinstance",
    "getattr", "setattr", "open", "eval", "exec", "type", "map", "filter", "sorted",
    "min", "max", "sum", "any", "all", "zip", "enumerate", "except", "with", "assert",
    "not", "and", "or", "in", "is", "del", "raise", "lambda", "yield", "await",
}

_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")

_GENERIC_DEF_KEYWORD_RE = re.compile(
    r"^\s*(?:export\s+|pub\s+|public\s+|private\s+|protected\s+|internal\s+|static\s+|"
    r"async\s+|final\s+|override\s+|virtual\s+)*(?:def|func|function|fn|sub|fun|"
    r"class|struct|interface|method)\s+([A-Za-z_]\w*)"
)


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _block_end(lines: Sequence[str], start: int) -> int:
    """从 1 起算的 start 行出发，估算块结束行（1 起算，含端点）。

    缩进语言按缩进回退，C 系按花括号配平；都不成立就退化为固定窗口。
    """
    base_indent = _indent_of(lines[start - 1])
    text = lines[start - 1]
    uses_braces = "{" in text

    if uses_braces:
        depth = text.count("{") - text.count("}")
        if depth > 0:
            for index in range(start, min(len(lines), start + 400)):
                depth += lines[index].count("{") - lines[index].count("}")
                if depth <= 0:
                    return index + 1
            return min(len(lines), start + 400)

    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if _indent_of(line) <= base_indent:
            break
        index += 1
    return max(start, index)


def _python_enclosing(tree: ast.AST, target: int) -> List[ast.AST]:
    """包住 target 行的函数节点，从外到内。"""
    found: List[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            if node.lineno <= target <= end:
                found.append(node)
    found.sort(key=lambda item: item.lineno)
    return found


def _python_callees(node: ast.AST) -> List[str]:
    names: List[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name and name not in _PY_KEYWORDS and name not in names:
                names.append(name)
    return names


def _python_defs(tree: ast.AST) -> Dict[str, ast.AST]:
    defs: Dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.setdefault(node.name, node)
    return defs


def _resolve_module(repo: str, source_file: str, module: str) -> Optional[str]:
    """把 ``a.b.c`` 形式的模块名解析成仓库内相对路径。"""
    parts = module.split(".")
    base_dir = os.path.dirname(source_file)
    candidates: List[str] = []
    for root in (repo, os.path.join(repo, base_dir)):
        candidates.append(os.path.join(root, *parts) + ".py")
        candidates.append(os.path.join(root, *parts, "__init__.py"))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.relpath(candidate, repo).replace("\\", "/")
    return None


def _python_import_map(tree: ast.AST, repo: str, source_file: str) -> Dict[str, str]:
    """名字 -> 仓库内相对路径（只解析本地模块）。"""
    mapping: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            path = _resolve_module(repo, source_file, node.module)
            if not path:
                continue
            for alias in node.names:
                mapping[alias.asname or alias.name] = path
        elif isinstance(node, ast.Import):
            for alias in node.names:
                path = _resolve_module(repo, source_file, alias.name)
                if path:
                    mapping[alias.asname or alias.name.split(".")[0]] = path
    return mapping


def _read_repo_file(repo: str, rel_path: str) -> Optional[str]:
    full = os.path.join(repo, rel_path)
    if not os.path.isfile(full):
        return None
    try:
        with open(full, "r", encoding=_ENCODING_FALLBACK, errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _snippet(lines: Sequence[str], start: int, end: int, radius: int = SNIPPET_RADIUS) -> List[str]:
    lo = max(1, start - radius)
    hi = min(len(lines), end + radius)
    return list(lines[lo - 1 : hi])


def _python_callchain(changeset: ChangeSet, repo: str) -> List[str]:
    lines = (changeset.new_content or "").splitlines()
    try:
        tree = ast.parse("\n".join(lines))
    except (SyntaxError, ValueError):
        return []

    changed = _changed_new_lines(changeset)
    if not changed:
        return []

    defs = _python_defs(tree)
    import_map = _python_import_map(tree, repo, changeset.file)
    out: List[str] = []
    seen: Set[Tuple[str, int, int]] = set()

    for target in sorted(changed):
        holders = _python_enclosing(tree, target)
        funcs = [node for node in holders if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if not funcs:
            continue
        owner = funcs[-1]

        # 被调用方：优先同文件定义，其次按 import 解析到别的文件
        callee_count = 0
        for callee in _python_callees(owner):
            if callee_count >= CALLCHAIN_MAX_CALLEES:
                break
            node = defs.get(callee)
            if node is not None:
                end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
                key = (changeset.file, node.lineno, end_line)
                if key in seen:
                    continue
                seen.add(key)
                callee_count += 1
                block = _snippet(lines, node.lineno, end_line)
                out.append(
                    _numbered(
                        f"[调用链·被调用] {callee}() 定义（同文件 L{node.lineno}）",
                        block,
                        max(1, node.lineno - SNIPPET_RADIUS),
                    )
                )
                continue
            path = import_map.get(callee)
            if not path:
                continue
            text = _read_repo_file(repo, path)
            if not text:
                continue
            try:
                other = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            target_node = _python_defs(other).get(callee)
            if target_node is None:
                continue
            end_line = getattr(target_node, "end_lineno", target_node.lineno) or target_node.lineno
            key = (path, target_node.lineno, end_line)
            if key in seen:
                continue
            seen.add(key)
            callee_count += 1
            other_lines = text.splitlines()
            block = _snippet(other_lines, target_node.lineno, end_line)
            out.append(
                _numbered(
                    f"[调用链·被调用] {callee}() 定义（跨文件 {path} L{target_node.lineno}）",
                    block,
                    max(1, target_node.lineno - SNIPPET_RADIUS),
                )
            )

        # 调用方：同文件里所有调用了本函数的位置
        caller_count = 0
        for caller in ast.walk(tree):
            if caller_count >= CALLCHAIN_MAX_CALLERS:
                break
            if not isinstance(caller, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if caller.name == owner.name:
                continue
            call_lines = [
                child.lineno
                for child in ast.walk(caller)
                if isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == owner.name
            ]
            if not call_lines:
                continue
            key = (changeset.file, caller.lineno, 0)
            if key in seen:
                continue
            seen.add(key)
            caller_count += 1
            anchor = min(call_lines)
            block = _snippet(lines, anchor, max(call_lines))
            out.append(
                _numbered(
                    f"[调用链·调用方] {caller.name}() 调用了 {owner.name}()（L{anchor} 附近）",
                    block,
                    max(1, anchor - SNIPPET_RADIUS),
                )
            )
    return out


def _generic_owner(lines: Sequence[str], target: int) -> Optional[int]:
    """非 Python：向上找最近的、看起来像被改函数的定义起始行（1 起算）。"""
    best: Optional[int] = None
    for index in range(target, max(0, target - 300), -1):
        line = lines[index - 1]
        if _GENERIC_DEF_KEYWORD_RE.match(line):
            best = index
            break
    return best


def _generic_callchain(changeset: ChangeSet) -> List[str]:
    lines = (changeset.new_content or "").splitlines()
    changed = _changed_new_lines(changeset)
    if not lines or not changed:
        return []

    out: List[str] = []
    owners: Set[Tuple[int, int]] = set()
    for target in sorted(changed):
        owner = _generic_owner(lines, target)
        if owner is None:
            continue
        end = _block_end(lines, owner)
        if (owner, end) in owners:
            continue
        owners.add((owner, end))
        block = _clip_block(lines[owner - 1 : end], owner, SNIPPET_RADIUS * 2)
        out.append(
            _numbered(f"[调用链·改动所在函数] L{owner}-L{end}", block, owner)
        )

        body = "\n".join(lines[owner - 1 : end])
        names: List[str] = []
        for name in _CALL_RE.findall(body):
            if name in _PY_KEYWORDS or name in names:
                continue
            names.append(name)
        for name in names:
            if len(out) >= 1 + CALLCHAIN_MAX_CALLEES:
                break
            # 同文件里的定义
            for index in range(1, len(lines) + 1):
                match = _GENERIC_DEF_KEYWORD_RE.match(lines[index - 1])
                if match and match.group(1) == name and not (owner <= index <= end):
                    stop = _block_end(lines, index)
                    snippet = _clip_block(
                        lines[index - 1 : stop], index, SNIPPET_RADIUS * 2
                    )
                    out.append(
                        _numbered(f"[调用链·被调用] {name}() 定义（L{index}）", snippet, index)
                    )
                    break
    return out


def callchain_context(changeset: ChangeSet, repo: str = ".") -> str:
    """第二层：被改函数的调用方 / 被调用方片段，整体受 ``CALLCHAIN_MAX_CHARS`` 限制。"""
    blocks = (
        _python_callchain(changeset, repo)
        if changeset.file.endswith(".py")
        else _generic_callchain(changeset)
    )
    if not blocks:
        return ""

    out: List[str] = []
    used = 0
    for block in blocks:
        if used + len(block) > CALLCHAIN_MAX_CHARS:
            out.append("...（调用链上下文已达长度上限，其余片段省略）...")
            break
        out.append(block)
        used += len(block) + 1
    return "\n".join(out)


# ================================================================ 第三层


DOCS: List[Dict[str, str]] = [
    # ---- reatom v2 ----
    {
        "framework": "reatom",
        "title": "reatom v2：action 必须通过 store.dispatch 派发",
        "text": "reatom v2 里 atom/action 不是可直接调用的函数。派发 action 要写 "
                "store.dispatch(action())，多个用数组 store.dispatch([a(), b()])。"
                "atom 的读值要写 store.getState(atom)。写成 atom('disable') 或 "
                "layer('enable') 这类「把 atom 当函数调用」是 v1 的写法，v2 下不会生效。",
    },
    {
        "framework": "reatom",
        "title": "reatom v2：ctx / 副作用与 onConnect",
        "text": "v2 的 action/atom 回调接收 ctx，副作用必须 ctx.schedule(...)，"
                "订阅用 atom.onChange 或 store.subscribe；副作用不能在 atom 求值时直接跑，"
                "否则每次 getState 都会重复触发。",
    },
    # ---- MapLibre GL JS ----
    {
        "framework": "maplibre-gl",
        "title": "MapLibre/Mapbox GL：样式与图层事件时序",
        "text": "setStyle() 之后地图会重新加载样式，load 事件**不会**再次触发；"
                "要等 styledata / idle，或先判断 map.isStyleLoaded()。"
                "要在样式就绪后加图层，用 map.on('load', ...) 只在首帧有效，"
                "换样式后必须重新注册或用 map.once('idle', ...)。",
    },
    {
        "framework": "maplibre-gl",
        "title": "MapLibre：图层可见性开关",
        "text": "控制图层显隐用 setLayoutProperty(id, 'visibility', 'visible'|'none')，"
                "或 removeLayer/addLayer。没有 layer('disable') 这种开关接口。",
    },
    # ---- React ----
    {
        "framework": "react",
        "title": "React：useEffect 依赖与闭包陷阱",
        "text": "useEffect 的回调闭包捕获的是当次渲染的 props/state；依赖数组漏写会导致读到过期值，"
                "写 [] 只在挂载时执行一次。订阅/定时器/事件监听必须在返回的清理函数里解绑，否则泄漏。",
    },
    {
        "framework": "react",
        "title": "React：state 更新是异步批处理的",
        "text": "setState 不会立即改变当前闭包里的变量；连续多次基于旧值计算会被合并。"
                "需要基于前值更新时用函数式写法 setX(prev => ...)。读写 ref 不触发重渲染，"
                "可在渲染期读到未提交的值。",
    },
    # ---- FastAPI ----
    {
        "framework": "fastapi",
        "title": "FastAPI：Depends 与默认值",
        "text": "依赖注入用参数默认值 Depends(...)；把依赖写成 Depends(func) 但给了别的默认值会让 FastAPI "
                "当成普通参数。dependency 默认在同一个请求内被缓存（use_cache=True），"
                "跨请求不会共享；要共享资源用 app.state 或 lru_cache。",
    },
    {
        "framework": "fastapi",
        "title": "FastAPI：请求体校验与 BackgroundTasks",
        "text": "路径/查询参数的运行时校验来自 Pydantic 模型与类型注解，绕过模型直接读 request.json() "
                "就没有校验。BackgroundTasks 在响应返回后执行，异常不会回传到客户端。",
    },
    # ---- Express ----
    {
        "framework": "express",
        "title": "Express：错误处理中间件必须四个参数",
        "text": "错误处理中间件签名必须是 (err, req, res, next) 四个参数，少一个 Express 就当成普通中间件，"
                "错误不会被它接住。async 处理函数里抛出的异常不会自动进错误链路，"
                "需要 try/catch 后 next(err) 或用 express-async-handler 之类的包装。",
    },
    {
        "framework": "express",
        "title": "Express：req.query / req.body 的运行时类型",
        "text": "TypeScript 里 req.query 的类型只是断言，运行时仍是字符串或数组。"
                "把 query 参数直接 as 成某个枚举/联合类型而不做校验，非法值会一路传到下游。",
    },
    # ---- pytest-mock ----
    {
        "framework": "pytest-mock",
        "title": "mocker.patch 要打在使用处，不是定义处",
        "text": "patch 的目标字符串必须指向「被测试模块里查找该名字的位置」。"
                "如果模块里写的是 from pkg import fn，就该 patch 'module_under_test.fn'，"
                "patch 'pkg.fn' 对已经绑定到模块命名空间的引用无效。",
    },
    {
        "framework": "pytest-mock",
        "title": "异步测试需要 pytest-asyncio 与标记",
        "text": "async def 测试函数必须由 pytest-asyncio（或 anyio）插件驱动，并给上 "
                "@pytest.mark.asyncio 或配置 asyncio_mode；否则测试会被直接跳过/报 warning 而不真正执行。"
                "AsyncMock 用于 async 函数，普通 Mock 返回的不是 awaitable。",
    },
    # ---- node-postgres / npm 发布 ----
    {
        "framework": "node-postgres",
        "title": "pg 包发布：files 字段决定 tarball 内容",
        "text": "package.json 的 files 字段是发布 tarball 的白名单，本地 require 成功不代表发布后可用。"
                "新增子模块（如 ./diagnostics）时必须同步加进 files，否则消费者 require 会 MODULE_NOT_FOUND。",
    },
    {
        "framework": "node-postgres",
        "title": "pg Pool 的错误与释放",
        "text": "Pool 会发出 'error' 事件，未监听会以未处理异常结束进程。"
                "用 pool.connect() 手动取连接必须在 finally 里 release，否则连接池耗尽。",
    },
    # ---- Go context ----
    {
        "framework": "go-context",
        "title": "Go：把 request context 传下去，别新建 Background",
        "text": "处理函数里应当把 r.Context()（或上游 ctx）继续往下传；"
                "用 context.Background()/TODO() 会切断取消与 deadline 传播，"
                "客户端断开或超时后下游仍会继续跑。同包内其它方法如果传了 ctx，就应该保持一致。",
    },
    {
        "framework": "go-context",
        "title": "Go：defer 与错误处理",
        "text": "defer 在函数返回时执行，闭包里引用循环变量要注意取值时机；"
                "命名返回值可以被 defer 修改。忽略 Close/Flush 返回的错误会静默丢数据。",
    },
    # ---- .NET ----
    {
        "framework": "dotnet",
        "title": ".NET：CancellationToken 与 OperationCanceledException",
        "text": "取消是正常控制流：catch (Exception) 会把 OperationCanceledException 一起吞掉，"
                "调用方再也感知不到取消。应当单独 catch (OperationCanceledException) 并 rethrow，"
                "或者用 when (ex is not OperationCanceledException) 过滤。",
    },
    {
        "framework": "dotnet",
        "title": ".NET：Ping 需要原始 ICMP 权限",
        "text": "System.Net.NetworkInformation.Ping 在 Linux/macOS 上需要 CAP_NET_RAW 或 root 才能创建原始 socket，"
                "装 iputils-ping 只是装了命令行工具，对 .NET 的托管实现没有帮助。",
    },
    # ---- 生态/工具链 ----
    {
        "framework": "coding-agent-cli",
        "title": "编码 Agent 的指令文件格式并不通用",
        "text": "Claude Code 的 CLAUDE.md 支持 @path 引用语法把文件内容内联；"
                "Codex CLI 读 AGENTS.md、Gemini CLI 读 GEMINI.md，它们把这些记号当普通文本，"
                "不会展开 @path。跨工具复用模板时必须改写这些工具特有的语法。",
    },
    {
        "framework": "ruby-gemspec",
        "title": "Ruby：gemspec 的 required_ruby_version 与语法",
        "text": "gemspec 里的 required_ruby_version 是给用户看的兼容声明，不会阻止安装。"
                "如果新代码用了关键字参数（2.0+）或 &. 安全导航（2.3+），"
                "而声明还停在旧版本，在老 Ruby 上会直接语法错误。",
    },
    {
        "framework": "github-actions",
        "title": "GitHub Actions：concurrency 会取消在跑的任务",
        "text": "concurrency.cancel-in-progress: true 会在新任务入队时**取消正在进行**的同一组任务，"
                "部署类工作流被中途取消可能留下半完成的发布状态。"
                "paths/paths-ignore 过滤器决定触发条件，收窄后清单文件变更将不再触发发布。",
    },
]


class DocIndex:
    """内置文档片段的小检索器：优先 fastembed 向量检索，失败降级为 IDF 词法检索。"""

    def __init__(self, docs: Sequence[Dict[str, str]]):
        self.docs = list(docs)
        self._vectors = None
        self._embedder = None
        self._mode = "lexical"
        self._tried_embedding = False  # 每进程只尝试一次，避免模型不可用时反复等下载超时
        self._idf = self._build_idf()

    # ---- 词法 ----
    @staticmethod
    def _tokens(text: str) -> List[str]:
        return [item for item in re.findall(r"[A-Za-z_][A-Za-z0-9_\.]{2,}", text.lower())]

    def _build_idf(self) -> Dict[str, float]:
        total = len(self.docs)
        counts: Dict[str, int] = {}
        for doc in self.docs:
            for token in set(self._tokens(doc["title"] + " " + doc["text"] + " " + doc["framework"])):
                counts[token] = counts.get(token, 0) + 1
        return {
            token: math.log((total + 1) / (count + 1)) + 1.0
            for token, count in counts.items()
        }

    def _lexical_scores(self, query: str) -> List[float]:
        tokens = set(self._tokens(query))
        scores: List[float] = []
        for doc in self.docs:
            doc_tokens = set(
                self._tokens(doc["title"] + " " + doc["text"] + " " + doc["framework"])
            )
            score = sum(self._idf.get(token, 1.0) for token in tokens & doc_tokens)
            scores.append(score)
        return scores

    # ---- 向量 ----
    def _ensure_vectors(self) -> bool:
        if self._vectors is not None:
            return True
        if self._tried_embedding:
            return False
        self._tried_embedding = True
        try:
            from fastembed import TextEmbedding  # 延迟导入：没装就不影响主链路
        except Exception:
            return False
        try:
            self._embedder = TextEmbedding()
            corpus = [doc["title"] + "。" + doc["text"] for doc in self.docs]
            self._vectors = list(self._embedder.embed(corpus))
            self._mode = "fastembed"
            return True
        except Exception:
            self._embedder = None
            self._vectors = None
            return False

    def _vector_scores(self, query: str) -> Optional[List[float]]:
        if not self._ensure_vectors():
            return None
        try:
            import numpy as np

            query_vector = list(self._embedder.embed([query]))[0]
            matrix = np.array(self._vectors)
            query_array = np.array(query_vector)
            denom = (np.linalg.norm(matrix, axis=1) * np.linalg.norm(query_array)) + 1e-9
            return list(matrix.dot(query_array) / denom)
        except Exception:
            return None

    def search(self, query: str, top_k: int = DOCS_TOP_K) -> List[Tuple[Dict[str, str], float]]:
        scores = self._vector_scores(query)
        if scores is None:
            scores = self._lexical_scores(query)
        ranked = sorted(zip(self.docs, scores), key=lambda item: item[1], reverse=True)
        return [(doc, float(score)) for doc, score in ranked[:top_k] if score > 0]

    @property
    def mode(self) -> str:
        return "fastembed" if self._vectors is not None else self._mode


_INDEX: Optional[DocIndex] = None


def _doc_index() -> DocIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = DocIndex(DOCS)
    return _INDEX


# 检测用的框架指纹：名称 -> (import/路径关键字, API 片段)。
# 一组命中的权重是 2/1，总分要 >= _FRAMEWORK_MIN_SCORE 才算命中，避免单个
# 通用词（比如文件里出现一个 "context"）就把整层文档拉进来。
_FRAMEWORK_HINTS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "reatom": (("reatom",), ("store.dispatch", "@reatom", "store.getState")),
    "maplibre-gl": (
        ("maplibre", "mapbox-gl"),
        ("map.on(", "map.once(", "setStyle(", "isStyleLoaded", "addLayer(", "setLayoutProperty"),
    ),
    "react": (
        ("from 'react'", 'from "react"', "react-dom", "from 'react/"),
        ("useEffect(", "useState(", "useMemo(", "useCallback("),
    ),
    "fastapi": (("fastapi",), ("Depends(", "BackgroundTasks", "@app.get", "@router.post")),
    "express": (
        ("from 'express'", 'from "express"', "require('express')", 'require("express")'),
        ("app.use(", "router.get(", "req.query", "next(err"),
    ),
    "pytest-mock": (
        ("pytest", "mocker"),
        ("mocker.patch", "pytest.mark.asyncio", "AsyncMock", "pytest.raises"),
    ),
    "node-postgres": (
        ("pg-pool", "node-postgres", "from 'pg'", "from 'pg-pool'"),
        ("diagnostics_channel", "new Pool(", "pool.connect()"),
    ),
    "go-context": (
        ('"context"', "context.Context", "context.WithTimeout", "context.WithCancel"),
        ("context.Background()", "context.TODO()", "r.Context()", "ctx.Done()"),
    ),
    "dotnet": (
        ("using System;", "System.Threading", "System.Net.NetworkInformation"),
        ("OperationCanceledException", "SendPingAsync", "CancellationToken", "catch (Exception"),
    ),
    "coding-agent-cli": (
        ("agents.md", "claude.md", "gemini.md", ".codex/", ".gemini/"),
        ("@filename", "@AGENTS"),
    ),
    "ruby-gemspec": (
        ("required_ruby_version", "gemspec", "Gem::Specification"),
        ("keyword_init", "safe navigation"),
    ),
    "github-actions": (
        (".github/workflows", "concurrency:", "cancel-in-progress"),
        ("paths-ignore", "paths:", "needs:"),
    ),
}

_FRAMEWORK_MIN_SCORE = 2


def detect_frameworks(text: str, file: str = "") -> List[str]:
    """从 diff/文件内容里检测框架，返回命中的框架名（按提示强度排序）。"""
    haystack = f"{file}\n{text}"
    lowered = haystack.lower()
    hits: List[Tuple[int, str]] = []
    for name, (imports, apis) in _FRAMEWORK_HINTS.items():
        score = 0
        for needle in imports:
            if needle.lower() in lowered:
                score += 2
        for needle in apis:
            if needle.lower() in lowered:
                score += 1
        if score >= _FRAMEWORK_MIN_SCORE:
            hits.append((score, name))
    hits.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in hits]


def framework_docs(changeset: ChangeSet, source_text: str = "", top_k: int = DOCS_TOP_K) -> str:
    """第三层：检测框架 -> 检索文档片段；检测不到就返回空串（不报错）。"""
    probe = f"{changeset.file}\n{changeset.diff_text()}\n{(changeset.new_content or '')[:4000]}"
    frameworks = detect_frameworks(probe)
    if not frameworks:
        return ""

    query = f"{' '.join(frameworks)} 框架 API 语义与常见误用\n{source_text[:2000]}"
    results = _doc_index().search(query, top_k=top_k)
    picked = [doc for doc, _ in results if doc["framework"] in set(frameworks)]
    if not picked:  # 向量检索没落在命中的框架上时，退化为该框架的前几条
        picked = [doc for doc in DOCS if doc["framework"] in set(frameworks)][:top_k]
    if not picked:
        return ""

    lines = [f"（检测到框架/库：{', '.join(frameworks[:4])}；检索方式：{_doc_index().mode}）"]
    for doc in picked:
        lines.append(f"--- [{doc['framework']}] {doc['title']} ---\n{doc['text']}")
    return "\n".join(lines)


# ================================================================ 组装


def build_context(changeset: ChangeSet, repo: str = ".", layers: Optional[Iterable[str]] = None) -> str:
    """把一个 ChangeSet 的三层上下文拼成一段文本，供深审 prompt 使用。

    任何一层算不出来都只是少一段文本，不影响主链路。
    """
    active = tuple(layers) if layers is not None else enabled_layers()
    sections: List[str] = []

    layer1 = ""
    if "1" in active:
        layer1 = same_file_context(changeset)
        if layer1:
            sections.append("【同文件上下文（非 diff 部分，已按相关性截断）】\n" + layer1)

    if "2" in active:
        layer2 = callchain_context(changeset, repo=repo)
        if layer2:
            sections.append("【调用链上下文（调用方 / 被调用方片段）】\n" + layer2)

    if "3" in active:
        layer3 = framework_docs(changeset, source_text=layer1)
        if layer3:
            sections.append("【框架/库文档（检索片段，供判断 API 语义）】\n" + layer3)

    return "\n\n".join(sections)
