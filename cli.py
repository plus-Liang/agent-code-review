#!/usr/bin/env python
"""命令行入口。

    python cli.py --base main --head test-branch

流程：本地 diff -> LangGraph(read_diff -> review) -> locate -> findings.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from core.filter import (
    DEFAULT_TYPE_MODE,
    cluster_findings,
    filter_by_confidence,
    filter_by_type,
)
from core.graph import build_graph, make_relocator
from core.logger import step_log
from findings.locate import locate_findings
from findings.postprocess import postprocess
from findings.schema import findings_to_json


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
        "--locate-level", type=int, default=3, help="定位最高等级：1/2/3（默认 3）"
    )
    parser.add_argument(
        "--max-findings", type=int, default=20, help="后处理上限：每个 PR 最多输出几条（默认 20）"
    )
    parser.add_argument(
        "--dump-dropped", action="store_true", help="把定位失败的原始结论写到 <out>.dropped.json"
    )
    parser.add_argument(
        "--type-mode",
        choices=("blacklist", "strict"),
        default=DEFAULT_TYPE_MODE,
        help="类型白名单口径：blacklist=只丢命名/注释/CSS/文档/格式/测试/重构（默认）；"
        "strict=必须命中 SQL注入/路径穿越/XSS/空指针/越界/资源泄漏 六类之一",
    )
    parser.add_argument(
        "--file-score-threshold",
        type=float,
        default=None,
        help="文件级评分阈值，低于它的文件不送 LLM（默认 0.5，等价 ACR_FILE_SCORE_THRESHOLD）",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # 文件级评分阈值的优先级：命令行 > 环境变量 > 默认值（默认在 core/filter.py）
    if args.file_score_threshold is not None:
        os.environ["ACR_FILE_SCORE_THRESHOLD"] = str(args.file_score_threshold)

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
    skipped_files = state.get("skipped_files") or []
    review_files = state.get("review_changesets") or []
    raw_findings = state.get("raw_findings") or []

    # 3 级定位要 LLM；没配 key 就退化成只跑 1、2 级
    relocator = None
    if args.locate_level >= 3:
        try:
            relocator = make_relocator(
                api_key=args.api_key, base_url=args.base_url, model=args.model
            )
        except RuntimeError:
            relocator = None
    # 闸 0（类型白名单）：只放行"必报"的六类漏洞/缺陷，丢掉命名/注释/CSS/文档/格式/重构。
    # 纯字符串匹配，在定位前做——低价值条目不必再花定位预算。
    raw_findings, type_stats = filter_by_type(raw_findings, mode=args.type_mode)
    # 闸 1（置信度）：模型自评把握不足的先丢掉，省下低置信度条目的定位预算
    raw_findings, conf_stats = filter_by_confidence(raw_findings)
    with step_log("locate", files=len(changesets)) as log:
        findings, dropped, level_counts = locate_findings(
            raw_findings,
            changesets,
            max_level=args.locate_level,
            llm_relocate=relocator,
        )
        log["hits"] = len(findings)
        # 定位失败不是致命错误（三级都找不到就丢弃），但必须可见——不静默丢结论
        if level_counts.get("miss"):
            log["error"] = f"locate_failed:{level_counts['miss']}"
    # 闸 2（聚类去重）：同文件里换着说法重复报的同一处问题，每组只留最具体的一条
    findings, cluster_stats = cluster_findings(findings)
    with step_log("postprocess", files=len(changesets)) as log:
        findings, stats = postprocess(findings, max_findings=args.max_findings)
        log["hits"] = stats.kept

    out_path = Path(args.out)
    out_path.write_text(findings_to_json(findings), encoding="utf-8")

    if args.dump_dropped and dropped:
        Path(str(out_path) + ".dropped.json").write_text(
            json.dumps(dropped, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(
        f"[agent-code-review] {args.base}...{args.head}: "
        f"{len(changesets)} 个变更文件, 分诊+文件评分后深审 {len(review_files)} 个（跳过 {len(skipped_files)}）, "
        f"模型原始结论 {len(raw_findings)} 条, 定位 1级 {level_counts['1']} / 2级 {level_counts['2']} / "
        f"3级 {level_counts['3']} / 失败 {level_counts['miss']}, "
        f"类型白名单丢弃 {type_stats.dropped} 条（{type_stats.per_type}）, "
        f"置信度丢弃 {conf_stats.dropped_low_confidence} / 待复核 {conf_stats.needs_review} 条, "
        f"聚类合并 {cluster_stats.merged} 条（{cluster_stats.clusters} 组）, "
        f"后处理合并 {stats.merged_duplicates} / 低severity {stats.dropped_severity} / "
        f"超限 {stats.dropped_over_cap} 条, 最终 {stats.kept} 条 -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
