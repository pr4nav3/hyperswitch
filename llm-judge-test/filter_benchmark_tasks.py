#!/usr/bin/env python3
"""
Filter a JSONL coding-agent benchmark dataset down to a clean starter subset.

Expected input row shape, but the script is intentionally tolerant:
  {
    "task": "...",
    "gold_patch": "..." OR "diff": "...",
    "metadata": {
      "repo": "juspay/hyperswitch",
      "pr_number": 123,
      "base_sha": "...",
      "head_sha": "...",
      "quality_flags": [{"code": "..."}, ...]
    }
  }

The script does not modify the input file. It writes:
  - filtered JSONL rows
  - a summary JSON file
  - an optional CSV report of all rows and rejection reasons
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable


DIFF_FILE_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.MULTILINE)

DEFAULT_EXCLUDED_FLAG_CODES = {
    "invalid_draft",
    "many_changed_files",
}

NOISY_PATH_SUFFIXES = (
    ".lock",
    ".snap",
)

NOISY_PATH_FRAGMENTS = (
    "/generated/",
    "/schema/",
    "/migrations/",
)


@dataclass(frozen=True)
class RowStats:
    index: int
    repo: str
    pr_number: str
    base_sha: str
    head_sha: str
    task_chars: int
    gold_patch_chars: int
    diff_lines: int
    changed_files: int
    added_lines: int
    removed_lines: int
    quality_flag_codes: list[str]
    max_implementation_leakage: int
    noisy_paths: list[str]
    keep: bool
    reasons: list[str]


def load_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {idx + 1}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"Line {idx + 1} is not a JSON object")
            yield idx, row


def get_metadata(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("metadata")
    return meta if isinstance(meta, dict) else {}


def get_gold_patch(row: dict[str, Any]) -> str:
    value = row.get("gold_patch", row.get("diff", ""))
    return value if isinstance(value, str) else ""


def get_quality_flags(row: dict[str, Any]) -> list[dict[str, Any]]:
    flags = get_metadata(row).get("quality_flags", [])
    if not isinstance(flags, list):
        return []
    normalized: list[dict[str, Any]] = []
    for flag in flags:
        if isinstance(flag, dict):
            normalized.append(flag)
        else:
            normalized.append({"code": str(flag)})
    return normalized


def changed_files_from_patch(patch: str) -> list[str]:
    files: list[str] = []
    for match in DIFF_FILE_RE.finditer(patch):
        # Prefer the destination path because renames/deletions are represented there.
        files.append(match.group(2))
    return sorted(set(files))


def count_added_removed_lines(patch: str) -> tuple[int, int]:
    added = 0
    removed = 0
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def is_noisy_path(path: str) -> bool:
    lower = path.lower()
    if lower.endswith(NOISY_PATH_SUFFIXES):
        return True
    return any(fragment in lower for fragment in NOISY_PATH_FRAGMENTS)


def analyze_row(
    idx: int,
    row: dict[str, Any],
    *,
    max_files: int,
    max_diff_lines: int,
    max_patch_chars: int,
    max_task_chars: int,
    max_added_lines: int,
    max_removed_lines: int,
    max_leakage: int,
    excluded_flag_codes: set[str],
    exclude_noisy_paths: bool,
) -> RowStats:
    meta = get_metadata(row)
    task = row.get("task", "")
    task_text = task if isinstance(task, str) else ""
    patch = get_gold_patch(row)
    files = changed_files_from_patch(patch)
    added, removed = count_added_removed_lines(patch)
    flags = get_quality_flags(row)
    flag_codes = sorted({str(flag.get("code", "")) for flag in flags if flag.get("code")})
    leakage_values = [
        int(flag.get("implementation_leakage", 0) or 0)
        for flag in flags
        if str(flag.get("code", "")) == "high_leakage_score"
    ]
    max_observed_leakage = max(leakage_values) if leakage_values else 0
    noisy_paths = [path for path in files if is_noisy_path(path)]

    reasons: list[str] = []

    if not task_text.strip():
        reasons.append("missing_task")
    if not patch.strip():
        reasons.append("missing_gold_patch")
    if not str(meta.get("base_sha", "")).strip():
        reasons.append("missing_base_sha")

    for code in flag_codes:
        if code in excluded_flag_codes:
            reasons.append(f"excluded_flag:{code}")

    if max_observed_leakage > max_leakage:
        reasons.append(f"implementation_leakage>{max_leakage}:{max_observed_leakage}")
    if len(files) > max_files:
        reasons.append(f"changed_files>{max_files}:{len(files)}")
    if len(patch.splitlines()) > max_diff_lines:
        reasons.append(f"diff_lines>{max_diff_lines}:{len(patch.splitlines())}")
    if len(patch) > max_patch_chars:
        reasons.append(f"patch_chars>{max_patch_chars}:{len(patch)}")
    if len(task_text) > max_task_chars:
        reasons.append(f"task_chars>{max_task_chars}:{len(task_text)}")
    if added > max_added_lines:
        reasons.append(f"added_lines>{max_added_lines}:{added}")
    if removed > max_removed_lines:
        reasons.append(f"removed_lines>{max_removed_lines}:{removed}")
    if exclude_noisy_paths and noisy_paths:
        reasons.append("noisy_paths:" + ",".join(noisy_paths[:5]))

    return RowStats(
        index=idx,
        repo=str(meta.get("repo", row.get("repo", ""))),
        pr_number=str(meta.get("pr_number", row.get("pr_number", ""))),
        base_sha=str(meta.get("base_sha", row.get("base_sha", ""))),
        head_sha=str(meta.get("head_sha", row.get("head_sha", ""))),
        task_chars=len(task_text),
        gold_patch_chars=len(patch),
        diff_lines=len(patch.splitlines()),
        changed_files=len(files),
        added_lines=added,
        removed_lines=removed,
        quality_flag_codes=flag_codes,
        max_implementation_leakage=max_observed_leakage,
        noisy_paths=noisy_paths,
        keep=not reasons,
        reasons=reasons,
    )


def write_report_csv(path: Path, stats: list[RowStats]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(stats[0]).keys()) if stats else list(RowStats.__dataclass_fields__.keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in stats:
            row = asdict(item)
            row["quality_flag_codes"] = ";".join(item.quality_flag_codes)
            row["noisy_paths"] = ";".join(item.noisy_paths)
            row["reasons"] = ";".join(item.reasons)
            writer.writerow(row)


_DEFAULT_OUTPUT_DIR = Path(__file__).parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Input JSONL dataset")
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT_DIR / "filtered.jsonl", help="Filtered JSONL output")
    parser.add_argument("--summary", type=Path, default=_DEFAULT_OUTPUT_DIR / "filtered.summary.json", help="Summary JSON output")
    parser.add_argument("--report-csv", type=Path, default=None, help="CSV report for every row")
    parser.add_argument("--max-files", type=int, default=8)
    parser.add_argument("--max-diff-lines", type=int, default=900)
    parser.add_argument("--max-patch-chars", type=int, default=80_000)
    parser.add_argument("--max-task-chars", type=int, default=2_500)
    parser.add_argument("--max-added-lines", type=int, default=450)
    parser.add_argument("--max-removed-lines", type=int, default=300)
    parser.add_argument("--max-leakage", type=int, default=3)
    parser.add_argument(
        "--exclude-flag",
        action="append",
        default=sorted(DEFAULT_EXCLUDED_FLAG_CODES),
        help="Quality flag code to exclude. May be repeated. Defaults exclude invalid_draft and many_changed_files.",
    )
    parser.add_argument(
        "--exclude-noisy-paths",
        action="store_true",
        help="Exclude rows touching lockfiles, snapshots, generated files, schemas, or migrations.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Keep at most this many rows after filtering")
    args = parser.parse_args()

    excluded_flag_codes = set(args.exclude_flag or [])
    rows: list[dict[str, Any]] = []
    stats: list[RowStats] = []
    kept = 0

    for idx, row in load_jsonl(args.input):
        stat = analyze_row(
            idx,
            row,
            max_files=args.max_files,
            max_diff_lines=args.max_diff_lines,
            max_patch_chars=args.max_patch_chars,
            max_task_chars=args.max_task_chars,
            max_added_lines=args.max_added_lines,
            max_removed_lines=args.max_removed_lines,
            max_leakage=args.max_leakage,
            excluded_flag_codes=excluded_flag_codes,
            exclude_noisy_paths=args.exclude_noisy_paths,
        )
        stats.append(stat)
        if stat.keep and (args.limit is None or kept < args.limit):
            rows.append(row)
            kept += 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    reason_counts: dict[str, int] = {}
    for stat in stats:
        for reason in stat.reasons:
            key = reason.split(":", 1)[0]
            reason_counts[key] = reason_counts.get(key, 0) + 1

    summary = {
        "input": str(args.input),
        "output": str(args.output),
        "total_rows": len(stats),
        "eligible_rows_before_limit": sum(1 for s in stats if s.keep),
        "written_rows": len(rows),
        "filters": {
            "max_files": args.max_files,
            "max_diff_lines": args.max_diff_lines,
            "max_patch_chars": args.max_patch_chars,
            "max_task_chars": args.max_task_chars,
            "max_added_lines": args.max_added_lines,
            "max_removed_lines": args.max_removed_lines,
            "max_leakage": args.max_leakage,
            "excluded_flag_codes": sorted(excluded_flag_codes),
            "exclude_noisy_paths": args.exclude_noisy_paths,
            "limit": args.limit,
        },
        "rejection_reason_counts": dict(sorted(reason_counts.items())),
    }

    summary_path = args.summary or args.output.with_suffix(".summary.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if args.report_csv:
        write_report_csv(args.report_csv, stats)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
