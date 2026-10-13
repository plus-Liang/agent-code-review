"""LangGraph 主链路 —— 本阶段只有 2 个节点。

    [read_diff] --ChangeSet 列表--> [review] --原始结论(existing_code, message, severity)-->

刻意不做多智能体：一个节点读 diff，一个节点审代码。复杂度的位置留给
「定位」而不是「拆分角色」——见 README 的护城河一节。

关键约束：**模型不输出行号**。模型只被允许逐字引用代码片段（existing_code），
行号由 core/resolver.py 在本地算出来。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph
from openai import OpenAI

from adapter.git_diff import get_change_sets
from core.context import build_context
from core.triage import triage
from findings.schema import ChangeSet

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
# 单次请求超时与重试：不带超时的话，上游偶发的一次卡死会把整个 PR 拖到 SDK 默认的
# 600 秒上限（ReviewBench 官方每 PR 只有 15 分钟），有超时+重试才跑得完。
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_MAX_RETRIES = 3


def _llm_timeout() -> float:
    return float(os.environ.get("LLM_TIMEOUT") or DEFAULT_TIMEOUT_SECONDS)


def _llm_max_retries() -> int:
    return int(os.environ.get("LLM_MAX_RETRIES") or DEFAULT_MAX_RETRIES)


def _new_client(api_key: str, base_url: str) -> OpenAI:
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=_llm_timeout(),
        max_retries=_llm_max_retries(),
    )

SYSTEM_PROMPT = """你是一名严格但务实的资深代码审查员，审查的是一个真实 PR 里改动的 production 代码。

只报这三类问题：
- 安全漏洞：注入、路径穿越、鉴权缺失、敏感信息泄露、不安全反序列化/eval
- 正确性缺陷：逻辑错误、边界条件、空值/除零、并发竞态、状态不一致
- 可靠性问题：资源泄漏、异常被吞、缺少超时/重试、错误的平台假设

以下**不算问题，一律不要报**：
- 样式、格式、缩进、命名、注释措辞、代码优雅度、"建议重构"
- CSS 变量、颜色值、对比度、主题/视觉微调
- 测试文件里的任何内容（路径含 tests/、__tests__/，或文件名形如 test_* / *.test.* / *_test.*）
- 纯文档、CHANGELOG、README、模板文本

严重级：按真实影响标 high / medium / low。不确定严重程度时，按你认为最接近的一档标出来即可，
不要因为不确定就省略问题——漏报比多报一条更糟。

输出必须是 JSON，格式：
{"findings": [{"existing_code": "...", "message": "...", "severity": "high|medium|low", "category": "bug|security|performance|reliability|maintainability"}]}

硬性要求：
1. existing_code 必须**逐字照抄**变更后文件中的原始代码行（可以有 1~5 行），
   保留原始缩进和标点，不要改写、不要加行号、不要加 +/- 前缀。
2. 绝对不要输出行号、不要输出 "line 12" 这类文字——行号由外部工具根据
   existing_code 自行定位。
3. 同一处问题只报一次；相邻几行属于同一个问题的，合并成一条。
4. 每个文件最多 4 条，整次审查最多 20 条。
5. 没有问题就返回 {"findings": []}。
"""

USER_PROMPT_TEMPLATE = """文件：{file}

diff：
{diff}
{context}
请按系统提示的 JSON 格式输出审查结论。"""


class ReviewState(TypedDict, total=False):
    """图状态。除入参外全部由节点填充，可被 JSON 序列化（便于回放）。"""

    repo: str
    base: str
    head: str
    max_files: Optional[int]
    api_key: Optional[str]
    base_url: Optional[str]
    model: Optional[str]
    template: Optional[str]
    changesets: List[ChangeSet]
    review_changesets: List[ChangeSet]
    skipped_files: List[Dict[str, str]]
    raw_findings: List[Dict[str, Any]]
    error: str


def resolve_llm_config(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, str]:
    """按 参数 > 环境变量 > 默认值 的顺序解析 LLM 配置。"""
    key = (
        api_key
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    if not key:
        raise RuntimeError(
            "缺少 API key：请设置 DEEPSEEK_API_KEY（或 LLM_API_KEY / OPENAI_API_KEY），"
            "或使用 --api-key 传入。"
        )
    return {
        "api_key": key,
        "base_url": base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL,
        "model": model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL,
    }


def read_diff_node(state: ReviewState) -> ReviewState:
    """节点 1：读本地 diff，产出 ChangeSet 列表。"""
    changesets = get_change_sets(
        base=state["base"],
        head=state["head"],
        repo=state.get("repo") or ".",
        max_files=state.get("max_files"),
    )
    return {"changesets": changesets}


def triage_node(state: ReviewState) -> ReviewState:
    """节点 2（分诊）：确定性筛文件，只留下值得花模型调用深审的。

    ``changesets`` 保持原样不动——定位阶段仍然用它，避免被跳过的文件导致定位失败。
    """
    result = triage(state.get("changesets") or [])
    return {
        "review_changesets": result.review,
        "skipped_files": [item.to_dict() for item in result.skipped],
    }


def _extract_json(text: str) -> Dict[str, Any]:
    """容忍 ```json 围栏和前后废话的 JSON 提取。"""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return json.loads(cleaned[start : end + 1])
    raise ValueError(f"模型输出不是合法 JSON：{text[:200]}")


def review_node(state: ReviewState, client: Optional[OpenAI] = None) -> ReviewState:
    """节点 2：逐文件调 LLM，产出原始结论（含 existing_code，不含行号）。"""
    # client 参数是给测试注入假客户端用的；注入时不校验/不读取 API key。
    if client is None:
        config = resolve_llm_config(
            api_key=state.get("api_key"),
            base_url=state.get("base_url"),
            model=state.get("model"),
        )
        llm = _new_client(config["api_key"], config["base_url"])
        model = config["model"]
    else:
        llm = client
        model = state.get("model") or os.environ.get("LLM_MODEL") or DEFAULT_MODEL

    # 支持 {file} / {diff} 占位，便于测试注入假模板
    template = state.get("template") or USER_PROMPT_TEMPLATE

    raw_findings: List[Dict[str, Any]] = []
    # 只深审分诊后留下的文件；没跑过 triage 时（单测直接调本节点）退化为全部文件
    targets = state.get("review_changesets")
    if targets is None:
        targets = state.get("changesets") or []
    repo = state.get("repo") or "."
    for changeset in targets:
        # 三层上下文供给（同文件 / 调用链 / 框架文档）。整层算不出来就退化成纯 diff，
        # 由 ACR_CONTEXT_LAYERS 控制启用哪几层（默认 1,2,3）。
        context = build_context(changeset, repo=repo)
        context_block = (
            "\n\n补充上下文（来自同文件非 diff 区域 / 调用链 / 框架文档，"
            "用于判断这段改动的影响面；问题仍必须指向 diff 中的代码）：\n" + context
            if context
            else ""
        )
        try:
            prompt = template.format(
                file=changeset.file, diff=changeset.diff_text(), context=context_block
            )
        except (KeyError, IndexError):
            # 兼容不含 {context} 占位符的旧模板（例如测试注入的假模板）
            prompt = template.format(file=changeset.file, diff=changeset.diff_text())
        response = llm.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        payload = _extract_json(response.choices[0].message.content or "")
        items = payload.get("findings", payload) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or not item.get("existing_code"):
                continue
            raw_findings.append(
                {
                    "file": changeset.file,  # 文件由调用方确定，模型不参与
                    "existing_code": item["existing_code"],
                    "message": item.get("message", ""),
                    "severity": item.get("severity"),
                    "category": item.get("category"),
                }
            )
    return {"raw_findings": raw_findings}


RELOCATE_SYSTEM_PROMPT = """你在做代码定位。用户给你一段代码片段，以及某个文件带行号的内容。

找出这段片段在文件中的位置，只输出 JSON：
{"start_line": <起始行号>, "end_line": <结束行号>}

规则：
- 行号是该文件里 1 起算的真实行号，直接照抄你看到的那几行前面的数字。
- 只能给出**确实包含这段代码**的行区间；片段可能被改写或截断，按最接近的一段给。
- 完全找不到就输出 {"start_line": null, "end_line": null}。
- 只输出 JSON，不要解释。"""


def make_relocator(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    client: Optional[OpenAI] = None,
):
    """构造 3 级定位用的 relocator：``(file, existing_code, numbered_file) -> (start, end) | None``。

    只在 1、2 级都失败时才会被调用，所以它贵一点没关系。
    """
    if client is None:
        config = resolve_llm_config(api_key=api_key, base_url=base_url, model=model)
        llm = _new_client(config["api_key"], config["base_url"])
        target_model = config["model"]
    else:
        llm = client
        target_model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL

    def relocate(file: str, existing_code: str, numbered_file: str):
        user_prompt = (
            f"文件：{file}\n\n代码片段：\n{existing_code}\n\n"
            f"该文件带行号的内容：\n{numbered_file}\n\n请输出 JSON。"
        )
        response = llm.chat.completions.create(
            model=target_model,
            messages=[
                {"role": "system", "content": RELOCATE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        payload = _extract_json(response.choices[0].message.content or "")
        if not isinstance(payload, dict):
            return None
        start = payload.get("start_line")
        end = payload.get("end_line", start)
        if not isinstance(start, int) or not isinstance(end, int):
            return None
        return start, end

    return relocate


def build_graph():
    """编译 3 节点图：read_diff -> triage -> review（定位在 cli 层做）。"""
    graph = StateGraph(ReviewState)
    graph.add_node("read_diff", read_diff_node)
    graph.add_node("triage", triage_node)
    graph.add_node("review", review_node)
    graph.set_entry_point("read_diff")
    graph.add_edge("read_diff", "triage")
    graph.add_edge("triage", "review")
    graph.add_edge("review", END)
    return graph.compile()
