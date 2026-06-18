#!/usr/bin/env python3
"""
Aggregate judge scores across benchmark runs with IQR-based outlier detection.

Reads one or more run JSONL files produced by run_opencode_benchmark.py and
computes per-task and cross-run statistics.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

SCORE_KEYS = ("correctness", "completeness", "convention_adherence", "hygiene", "overall")
IQR_MULTIPLIER = 1.5


def compute_iqr_bounds(values: list[float]) -> tuple[float, float]:
    """Return (lower_bound, upper_bound) using the standard IQR rule."""
    if len(values) < 4:
        return (-math.inf, math.inf)

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    q1_idx = (n - 1) * 0.25
    q3_idx = (n - 1) * 0.75

    def _interp(idx: float) -> float:
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return sorted_vals[lo]
        frac = idx - lo
        return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])

    q1 = _interp(q1_idx)
    q3 = _interp(q3_idx)
    iqr = q3 - q1
    lower = q1 - IQR_MULTIPLIER * iqr
    upper = q3 + IQR_MULTIPLIER * iqr
    return lower, upper


def identify_outliers(
    scores: list[float],
    judge_ids: list[int],
) -> tuple[list[int], str]:
    """Return (outlier_indices, explanation) for scores deemed extreme by IQR."""
    if len(scores) < 4:
        return [], "Too few judges (< 4) to compute reliable IQR bounds; no outliers excluded."

    lower, upper = compute_iqr_bounds(scores)
    outlier_ids = []
    for jid, sc in zip(judge_ids, scores):
        if sc < lower or sc > upper:
            outlier_ids.append(jid)

    if not outlier_ids:
        return [], (
            f"No outliers detected. All scores within [Q1-1.5*IQR, Q3+1.5*IQR] = "
            f"[{round(lower, 2)}, {round(upper, 2)}]."
        )

    reasons = []
    score_map = dict(zip(judge_ids, scores))
    for jid in outlier_ids:
        sc = score_map[jid]
        direction = "low" if sc < lower else "high"
        reasons.append(f"judge {jid} scored {sc} ({direction})")

    explanation = (
        f"Excluded {len(outlier_ids)} outlier(s): {', '.join(reasons)}. "
        f"Bounds: [{round(lower, 2)}, {round(upper, 2)}] computed from IQR with multiplier {IQR_MULTIPLIER}."
    )
    return outlier_ids, explanation


def aggregate_task_judges(judges: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute statistics for one task's judge panel."""
    raw_scores: dict[str, list[float]] = {k: [] for k in SCORE_KEYS}
    judge_ids: list[int] = []

    for j in judges:
        if j.get("parse_error") or "error" in j:
            continue
        jid = j.get("judge_id", -1)
        judge_ids.append(jid)
        for k in SCORE_KEYS:
            v = j.get(k)
            if isinstance(v, (int, float)):
                raw_scores[k].append(float(v))

    result: dict[str, Any] = {
        "judge_count": len(judge_ids),
        "valid_judges": len(judge_ids),
        "raw_scores": {},
        "mean": {},
        "std_dev": {},
        "median": {},
        "outliers_excluded": {},
        "outlier_explanations": {},
        "final_score": {},
    }

    for k in SCORE_KEYS:
        vals = raw_scores[k]
        result["raw_scores"][k] = vals

        if not vals:
            result["mean"][k] = None
            result["std_dev"][k] = None
            result["median"][k] = None
            result["outliers_excluded"][k] = []
            result["outlier_explanations"][k] = "No valid scores."
            result["final_score"][k] = None
            continue

        mean = statistics.mean(vals)
        std_dev = statistics.stdev(vals) if len(vals) > 1 else 0.0
        median = statistics.median(vals)

        outlier_ids, explanation = identify_outliers(vals, judge_ids)
        result["outliers_excluded"][k] = outlier_ids
        result["outlier_explanations"][k] = explanation

        filtered = [v for jid, v in zip(judge_ids, vals) if jid not in outlier_ids]
        if filtered:
            final_mean = statistics.mean(filtered)
        else:
            final_mean = mean

        result["mean"][k] = round(mean, 3)
        result["std_dev"][k] = round(std_dev, 3)
        result["median"][k] = round(median, 3)
        result["final_score"][k] = round(final_mean, 3)

    return result


def process_run_file(path: Path) -> list[dict[str, Any]]:
    """Read a run JSONL and aggregate each task's judges."""
    tasks: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            task = json.loads(line)
            judges = task.get("judges", [])
            stats = aggregate_task_judges(judges)
            task["judge_aggregate"] = stats
            tasks.append(task)
    return tasks


def cross_run_summary(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate statistics across all tasks in a run."""
    overall_scores = []
    for t in tasks:
        fs = t.get("judge_aggregate", {}).get("final_score", {}).get("overall")
        if fs is not None:
            overall_scores.append(fs)

    if not overall_scores:
        return {"task_count": len(tasks), "note": "No valid overall scores found."}

    return {
        "task_count": len(tasks),
        "mean_final_overall": round(statistics.mean(overall_scores), 3),
        "median_final_overall": round(statistics.median(overall_scores), 3),
        "std_dev_overall": round(statistics.stdev(overall_scores), 3) if len(overall_scores) > 1 else 0.0,
        "min_overall": round(min(overall_scores), 3),
        "max_overall": round(max(overall_scores), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path, help="Run JSONL file(s) to aggregate")
    parser.add_argument("--output", type=Path, default=None, help="Output JSON file")
    parser.add_argument("--per-task", action="store_true", help="Also write per-task aggregates to stdout")
    args = parser.parse_args()

    all_tasks: list[dict[str, Any]] = []
    for run_path in args.runs:
        print(f"Processing {run_path}...")
        tasks = process_run_file(run_path)
        all_tasks.extend(tasks)
        summary = cross_run_summary(tasks)
        print(f"  Tasks: {summary['task_count']}, Mean overall: {summary.get('mean_final_overall', 'N/A')}")

    if not all_tasks:
        print("No tasks found.")
        return 1

    full_summary = cross_run_summary(all_tasks)

    report = {
        "runs_processed": [str(p) for p in args.runs],
        "total_tasks": len(all_tasks),
        "cross_run_summary": full_summary,
        "tasks": all_tasks if args.per_task else None,
    }

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nWrote aggregate report to {args.output}")
    else:
        print(json.dumps(full_summary, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
