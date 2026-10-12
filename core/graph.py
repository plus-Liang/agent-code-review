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
from findings.schema import ChangeSet

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM_PROMPT = """你是一名严格但务实的资深代码审查员。你会收到一个文件的 diff。

只报告**真实存在**的问题：逻辑错误、边界条件、异常处理缺失、安全问题、
并发/资源泄漏、明显的性能反模式。不要报告纯粹的命名/格式偏好。

输出必须是 JSON，格式：
{"findings": [{"existing_code": "...", "message": "...", "severity": "high|medium|low", "category": "bug|security|performance|style|maintainability|other"}]}

硬性要求：
1. existing_code 必须**逐字照抄**变更后文件中的原始代码行（可以有 1~5 行），
   保留原始缩进和标点，不要改写、不要加行号、不要加 +/- 前缀。
2. 绝对不要输出行号、不要输出 "line 12" 这类文字——行号由外部工具根据
   existing_code 自行定位。
3. 同一条问题不要拆成多条；没有把握就少报。
4. 没有问题就返回 {"findings": []}。
"""

USER_PROMPT_TEMPLATE = """文件：{file}

diff：
{diff}

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
        llm = OpenAI(api_key=config["api_key"], base_url=config["base_url"])
        model = config["model"]
    else:
        llm = client
        model = state.get("model") or os.environ.get("LLM_MODEL") or DEFAULT_MODEL

    # 支持 {file} / {diff} 占位，便于测试注入假模板
    template = state.get("template") or USER_PROMPT_TEMPLATE

    raw_findings: List[Dict[str, Any]] = []
    for changeset in state.get("changesets") or []:
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


def build_graph():
    """编译 2 节点图：read_diff -> review。"""
    graph = StateGraph(ReviewState)
    graph.add_node("read_diff", read_diff_node)
    graph.add_node("review", review_node)
    graph.set_entry_point("read_diff")
    graph.add_edge("read_diff", "review")
    graph.add_edge("review", END)
    return graph.compile()
