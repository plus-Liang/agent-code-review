#!/usr/bin/env python
"""命令行入口。

    python cli.py --base main --head test-branch

流程：本地 diff -> LangGraph(read_diff -> review) -> locate -> findings.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 本机无法写入 site-packages，unidiff 以仓库本地 .deps 形式提供（见 README）。
for extra in (ROOT / ".deps", ROOT):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from core.graph import build_graph  # noqa: E402
from findings.locate import locate_findings  # noqa: E402
from findings.schema import findings_to_json  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="本地 diff -> LLM 审查 -> findings.json（MVP：2 个节点，只支持本地 diff）",
    )
    parser.add_argument("--base", required=True, help="基线 revision，例如 main")
    parser.add_argument("--head", required=True, help="目标 revision，例如 feature / test-branch")
    parser.add_argument("--repo", default=".", help="仓库路径（默认当前目录）")
    parser.add_argument("--out", default="findings.json", help="输出文件（默认 findings.json）")
    parser.add_argument("--model", default=None, help="覆盖 LLM_MODEL")
    parser.add_argument("--base-url", default=None, help="覆盖 LLM_BASE_URL（OpenAI 兼容）")
    parser.add_argument("--api-key", default=None, help="覆盖 DEEPSEEK_API_KEY / LLM_API_KEY")
    parser.add_argument("--max-files", type=int, default=None, help="最多审查多少个文件")
    parser.add_argument(
        "--locate-level", type=int, default=1, help="定位等级，本阶段只支持 1"
    )
    parser.add_argument(
        "--dump-dropped", action="store_true", help="把定位失败的原始结论写到 <out>.dropped.json"
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    graph = build_graph()
    state = graph.invoke(
        {
            "repo": args.repo,
            "base": args.base,
            "head": args.head,
            "max_files": args.max_files,
            "api_key": args.api_key,
            "base_url": args.base_url,
            "model": args.model,
        }
    )

    changesets = state.get("changesets") or []
    raw_findings = state.get("raw_findings") or []
    findings, dropped = locate_findings(raw_findings, changesets, level=args.locate_level)

    out_path = Path(args.out)
    out_path.write_text(findings_to_json(findings), encoding="utf-8")

    if args.dump_dropped and dropped:
        Path(str(out_path) + ".dropped.json").write_text(
            json.dumps(dropped, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(
        f"[agent-code-review] {args.base}...{args.head}: "
        f"{len(changesets)} 个变更文件, 模型原始结论 {len(raw_findings)} 条, "
        f"定位成功 {len(findings)} 条, 丢弃 {len(dropped)} 条 -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
