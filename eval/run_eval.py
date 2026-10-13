"""评估驱动：把预测的 findings.json 和 ReviewBench golden 对起来打分。

用法（单题冒烟）：

    python -m eval.run_eval \
        --corpus D:\\reviewbench-data\\corpus\\test \
        --pred findings.json \
        --task PierreJanineh_TechDebtMCP_135-16a54f0f

用法（批量，预测文件按 ``<pr_key>.json`` 命名放在目录里）：

    python -m eval.run_eval --corpus D:\\reviewbench-data\\corpus\\test --pred-dir out/ --out report.json
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Dict, List, Optional

from eval.golden_loader import GoldenTask, identify_task, load_predictions, load_test_set
from eval.metrics import aggregate, match_findings
from core.triage import skip_reason

LAYER_PRODUCTION = "production"
LAYER_SKIPPED = "skipped"


def layer_of(file: str) -> str:
    """golden 落在哪一层：分诊会深审的文件 = production，会被跳过的 = skipped。"""
    return LAYER_SKIPPED if skip_reason(file) else LAYER_PRODUCTION


def split_by_layer(findings):
    buckets = {LAYER_PRODUCTION: [], LAYER_SKIPPED: []}
    for item in findings:
        buckets[layer_of(item.file)].append(item)
    return buckets


def _score_task(task: GoldenTask, pred_path: str, tolerance: int) -> Dict[str, object]:
    _, predictions = load_predictions(pred_path)
    score = match_findings(predictions, task.findings, tolerance=tolerance)

    # 分层：预测按文件归层，golden 按文件归层，各层各算一次（层内互不干扰）
    pred_layers = split_by_layer(predictions)
    golden_layers = split_by_layer(task.findings)
    layered = {}
    for layer in (LAYER_PRODUCTION, LAYER_SKIPPED):
        layered[layer] = match_findings(
            pred_layers[layer], golden_layers[layer], tolerance=tolerance
        )

    return {
        "pr_key": task.pr_key,
        "nwo": task.nwo,
        "predictions": len(predictions),
        "golden": len(task.findings),
        "score": score.to_dict(with_details=False),
        "layers": {
            layer: {
                "predictions": len(pred_layers[layer]),
                "golden": len(golden_layers[layer]),
                "score": layered[layer].to_dict(with_details=False),
            }
            for layer in (LAYER_PRODUCTION, LAYER_SKIPPED)
        },
        "_score_obj": score,
        "_layer_objs": layered,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval.run_eval",
        description="ReviewBench 确定性评估（同文件 + 行区间重叠），非官方 LLM judge",
    )
    parser.add_argument("--corpus", required=True, help="corpus/test 目录（含 test.json / findings.json）")
    parser.add_argument("--pred", help="单个预测文件（裸数组或官方 {pr, findings} 格式）")
    parser.add_argument("--pred-dir", help="批量模式：目录下按 <pr_key>.json 命名")
    parser.add_argument("--task", help="指定 pr_key（单文件模式不填则按 head/pr 自动识别）")
    parser.add_argument("--tolerance", type=int, default=0, help="行区间容差（默认 0）")
    parser.add_argument("--out", help="把报告写成 JSON")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    tasks = load_test_set(args.corpus)
    by_key = {task.pr_key: task for task in tasks}

    results: List[Dict[str, object]] = []
    scores = []

    if args.pred:
        task = by_key.get(args.task) if args.task else None
        if task is None:
            pr_meta, _ = load_predictions(args.pred)
            task = identify_task(pr_meta, tasks)
        if task is None:
            print(f"[eval] 无法把 {args.pred} 对到测试集的任何一题；请用 --task 指定 pr_key")
            return 2
        row = _score_task(task, args.pred, args.tolerance)
        results.append(row)
        scores.append(row["_score_obj"])
    elif args.pred_dir:
        for task in tasks:
            pred_path = os.path.join(args.pred_dir, f"{task.pr_key}.json")
            if not os.path.isfile(pred_path):
                continue
            row = _score_task(task, pred_path, args.tolerance)
            results.append(row)
            scores.append(row["_score_obj"])
        if not results:
            print(f"[eval] {args.pred_dir} 下没有找到任何 <pr_key>.json")
            return 2
    else:
        print("[eval] 需要 --pred 或 --pred-dir")
        return 2

    total = aggregate(scores)
    for row in results:
        score = row["score"]
        print(
            f"{row['pr_key']:<46} pred={row['predictions']:<3} golden={row['golden']:<3} "
            f"P={score['precision']:.3f} R={score['recall']:.3f} F1={score['f1']:.3f} "
            f"(tp={score['tp']} fp={score['fp']} fn={score['fn']})"
        )
    summary = total.to_dict(with_details=False)
    print(
        f"{'TOTAL':<46} {'':<11} {'':<10} "
        f"P={summary['precision']:.3f} R={summary['recall']:.3f} F1={summary['f1']:.3f} "
        f"(tp={summary['tp']} fp={summary['fp']} fn={summary['fn']})"
    )

    layer_totals = {
        layer: aggregate([row["_layer_objs"][layer] for row in results])
        for layer in (LAYER_PRODUCTION, LAYER_SKIPPED)
    }
    layer_summary = {}
    for layer, layer_score in layer_totals.items():
        payload = layer_score.to_dict(with_details=False)
        layer_summary[layer] = payload
        print(
            f"  层[{layer:<10}] P={payload['precision']:.3f} R={payload['recall']:.3f} "
            f"F1={payload['f1']:.3f} (tp={payload['tp']} fp={payload['fp']} fn={payload['fn']})"
        )

    report = {
        "matcher": "deterministic: same-file + line-range overlap",
        "tolerance": args.tolerance,
        "tasks": [
            {k: v for k, v in row.items() if not k.startswith("_")} for row in results
        ],
        "micro": summary,
        "layers": layer_summary,
        "details": [row["_score_obj"].to_dict() for row in results],
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(f"[eval] 报告写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
