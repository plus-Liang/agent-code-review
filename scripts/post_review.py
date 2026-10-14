#!/usr/bin/env python
"""把 findings.json 汇总成一条 PR 评论，通过 GitHub API 发出。

用法：

    python scripts/post_review.py --findings findings.json          # 真发（需要 GITHUB_TOKEN / PR_NUMBER）
    python scripts/post_review.py --findings findings.json --dry-run # 只打印到 stdout

环境变量：
    GITHUB_TOKEN        必填（真发时）；GitHub Actions 里由 secrets.GITHUB_TOKEN 注入
    GITHUB_REPOSITORY   必填，"owner/repo"
    PR_NUMBER           必填，PR 编号
    GITHUB_API_URL      可选，默认 https://api.github.com（GitHub Enterprise 用）
    DRY_RUN             可选，置 1/true 等价于 --dry-run

只依赖标准库，不引入 requests。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List

# 严重级从高到低；未知值排最后。
SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")
SEVERITY_ICON = {
    "critical": "🟥",
    "high": "🟧",
    "medium": "🟨",
    "low": "🟦",
    "info": "⬜",
}
DEFAULT_API_URL = "https://api.github.com"
# GitHub 单条评论上限 65536 字符，留出余量。
MAX_BODY_CHARS = 60000


def _severity_rank(item: Dict[str, Any]) -> int:
    severity = str(item.get("severity") or "").strip().lower()
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else len(SEVERITY_ORDER)


def load_findings(path: str) -> List[Dict[str, Any]]:
    """读 findings.json；容忍 {"findings": [...]} 包装。"""
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if isinstance(raw, dict):
        raw = raw.get("findings", [])
    if not isinstance(raw, list):
        raise ValueError(f"{path} 顶层应为数组或 {{'findings': [...]}}，实际是 {type(raw).__name__}")
    return [item for item in raw if isinstance(item, dict)]


def _line_label(item: Dict[str, Any]) -> str:
    start = item.get("start_line")
    end = item.get("end_line", start)
    try:
        start_i = int(start)
        end_i = int(end)
    except (TypeError, ValueError):
        return "L?"
    return f"L{start_i}" if start_i == end_i else f"L{start_i}-{end_i}"


def render_comment(findings: List[Dict[str, Any]]) -> str:
    """按文件分组、按 severity 排序，渲染成一条 markdown 评论。"""
    if not findings:
        return "## 🔍 Agent Code Review\n\n未发现需要上报的问题 ✅"

    counts: Dict[str, int] = {}
    for item in findings:
        severity = str(item.get("severity") or "unknown").strip().lower() or "unknown"
        counts[severity] = counts.get(severity, 0) + 1

    parts = [f"{severity} {counts[severity]}" for severity in SEVERITY_ORDER if severity in counts]
    parts += [f"{key} {value}" for key, value in sorted(counts.items()) if key not in SEVERITY_ORDER]
    overview = f"发现 {len(findings)} 个问题（{' / '.join(parts)}）"

    lines: List[str] = ["## 🔍 Agent Code Review", "", f"**{overview}**", ""]

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in findings:
        grouped.setdefault(str(item.get("file") or "(unknown)"), []).append(item)

    for file_name in sorted(grouped):
        items = sorted(grouped[file_name], key=lambda entry: (_severity_rank(entry), _line_label(entry)))
        lines.append(f"### `{file_name}`")
        lines.append("")
        for item in items:
            severity = str(item.get("severity") or "unknown").strip().lower()
            icon = SEVERITY_ICON.get(severity, "⬜")
            message = " ".join(str(item.get("message") or "").split()) or "(无说明)"
            suffix = " ⚠️ 待复核" if item.get("needs_review") else ""
            lines.append(f"- {icon} **{_line_label(item)}** (`{severity}`) {message}{suffix}")
        lines.append("")

    lines.append("<sub>由 agent-code-review 自动生成</sub>")
    body = "\n".join(lines)

    if len(body) > MAX_BODY_CHARS:
        body = body[: MAX_BODY_CHARS - 200].rstrip() + "\n\n...（评论过长已截断）\n</sub>"
    return body


def post_comment(repo: str, pr_number: str, body: str, token: str, api_url: str) -> Dict[str, Any]:
    url = f"{api_url.rstrip('/')}/repos/{repo}/issues/{pr_number}/comments"
    payload = json.dumps({"body": body}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "agent-code-review",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:  # 把 GitHub 的错误正文带出来，方便排查权限问题
        detail = error.read().decode("utf-8", "replace")
        raise RuntimeError(f"GitHub API {error.code} {url}: {detail}") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="post_review.py", description="把 findings.json 汇总成一条 PR 评论并发到 GitHub"
    )
    parser.add_argument("--findings", default="findings.json", help="findings.json 路径")
    parser.add_argument("--repo", default=None, help="owner/repo（默认取 GITHUB_REPOSITORY）")
    parser.add_argument("--pr", default=None, help="PR 编号（默认取 PR_NUMBER）")
    parser.add_argument("--token", default=None, help="GitHub token（默认取 GITHUB_TOKEN）")
    parser.add_argument("--api-url", default=None, help="GitHub API 地址（默认 GITHUB_API_URL 或官方）")
    parser.add_argument("--dry-run", action="store_true", help="不真发，把评论打印到 stdout")
    return parser


def _force_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，会把评论里的 emoji 打印炸；切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv=None) -> int:
    _force_utf8_stdout()
    args = build_parser().parse_args(argv)

    dry_run = args.dry_run or str(os.environ.get("DRY_RUN", "")).strip().lower() in {"1", "true", "yes"}

    if not os.path.exists(args.findings):
        print(f"[post_review] 找不到 {args.findings}，跳过评论", file=sys.stderr)
        return 0

    findings = load_findings(args.findings)
    body = render_comment(findings)

    if dry_run:
        print(f"[post_review] dry-run：共 {len(findings)} 条，以下为将要发送的评论")
        print("-" * 60)
        print(body)
        print("-" * 60)
        return 0

    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = args.pr or os.environ.get("PR_NUMBER", "")
    token = args.token or os.environ.get("GITHUB_TOKEN", "")
    api_url = args.api_url or os.environ.get("GITHUB_API_URL") or DEFAULT_API_URL

    missing = [name for name, value in (("repo", repo), ("PR_NUMBER", pr_number), ("GITHUB_TOKEN", token)) if not value]
    if missing:
        print(f"[post_review] 缺少 {', '.join(missing)}；加 --dry-run 可以只打印", file=sys.stderr)
        return 1

    created = post_comment(repo, pr_number, body, token, api_url)
    print(f"[post_review] 已评论 {repo}#{pr_number}: {created.get('html_url', '(no url)')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
