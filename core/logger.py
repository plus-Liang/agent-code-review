"""结构化日志（旁路，不参与业务逻辑）。

每一步审查动作写**一行 JSON**：

    {"ts": "...", "trace": "...", "step": "diff_parse|llm_review|locate|postprocess",
     "files": N, "hits": N, "latency_ms": N, "error": ""}

约定：

* 只记**计数 / 耗时 / 错误类型**，不记代码内容、diff 文本与模型输出——
  日志可以进 CI 共享，不会顺手把源码或用户数据带出去。
* 默认写 stdout（CI 里直接进 job log）；设 ``ACR_LOG_FILE`` 可改为追加到文件。
  CI 用 stdout，本地排查用文件，两边读的是同一份 schema。
* 同一次审查的多个步骤共享一个 ``trace`` id，用来把一次 run 的 4 行串起来。
* 日志自身出错（比如日志文件不可写）**不抛出**：旁路挂掉不能拖垮主流程。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

#: 关键步骤白名单，与 README「日志与异常处理」一节一一对应。
STEPS = ("diff_parse", "llm_review", "locate", "postprocess")

#: 进程级 trace id：一次 CLI 调用 = 一条 trace。
_TRACE_ID = uuid.uuid4().hex[:12]


def new_trace_id() -> str:
    """开一条新 trace（测试或一次 run 内需要分段时用）。"""
    global _TRACE_ID
    _TRACE_ID = uuid.uuid4().hex[:12]
    return _TRACE_ID


def current_trace_id() -> str:
    return _TRACE_ID


def _timestamp() -> str:
    now = time.time()
    millis = int((now - int(now)) * 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)) + f".{millis:03d}"


def _emit(record: Dict[str, Any], started: float, trace: Optional[str]) -> Dict[str, Any]:
    entry = {
        "ts": _timestamp(),
        "trace": trace or _TRACE_ID,
        "step": record.get("step", ""),
        "files": int(record.get("files") or 0),
        "hits": int(record.get("hits") or 0),
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "error": str(record.get("error") or "")[:200],
    }
    line = json.dumps(entry, ensure_ascii=False)
    path = os.environ.get("ACR_LOG_FILE")
    try:
        if path:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        else:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    except OSError:
        pass  # 日志失败不影响主流程
    return entry


@contextmanager
def step_log(step: str, files: int = 0, trace: Optional[str] = None) -> Iterator[Dict[str, Any]]:
    """``with step_log("locate", files=n) as log: log["hits"] = k``。

    正常退出、异常退出都写一行；异常会带上 ``error`` 后**原样抛出**，
    所以它只观测、不吞异常（不静默挂掉，也不改变失败行为）。
    """
    record: Dict[str, Any] = {"step": step, "files": files, "hits": 0, "error": ""}
    started = time.perf_counter()
    try:
        yield record
    except Exception as exc:  # noqa: BLE001 —— 记完必须重新抛出
        record["error"] = record["error"] or f"{type(exc).__name__}: {exc}"
        _emit(record, started, trace)
        raise
    _emit(record, started, trace)


def log_step(
    step: str,
    files: int = 0,
    hits: int = 0,
    error: str = "",
    trace: Optional[str] = None,
    started: Optional[float] = None,
) -> Dict[str, Any]:
    """单行写法（脚本 / 手工埋点用）：立刻写一行。"""
    return _emit(
        {"step": step, "files": files, "hits": hits, "error": error},
        started if started is not None else time.perf_counter(),
        trace,
    )
