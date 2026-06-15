#!/usr/bin/env python3
"""
Run an opencode-based coding-agent benchmark against JSONL rows containing tasks,
base commits, and gold/reference patches.

This script deliberately separates responsibilities:
  - git/worktree setup and cleanup
  - opencode invocation
  - patch capture
  - cheap deterministic patch/style scoring
  - optional LLM-as-judge scoring through opencode
  - artifact persistence

It does not mutate the source repository except to create/remove git worktrees through
`git worktree`. All task edits occur inside per-task worktree directories.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aggregate_judges import aggregate_task_judges as aggregate_judge_scores


DIFF_FILE_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.MULTILINE)

STYLE_RULES_ADDED = [
    ("unwrap", "uses .unwrap() in production code", r"\.unwrap\s*\(", "soft"),
    ("expect", "uses .expect() in production code", r"\.expect\s*\(", "soft"),
    ("panic", "uses panic!()", r"\bpanic!\s*\(", "hard"),
    ("todo", "uses todo!()", r"\btodo!\s*\(", "hard"),
    ("unimplemented", "uses unimplemented!()", r"\bunimplemented!\s*\(", "hard"),
    ("unreachable", "uses unreachable!()", r"\bunreachable!\s*\(", "hard"),
    ("dbg_macro", "uses dbg!()", r"\bdbg!\s*\(", "hard"),
    ("println", "uses print/println/eprint/eprintln macro", r"\b(?:e?)print(?:ln)?!\s*\(", "soft"),
    ("as_cast", "uses numeric `as` cast", r"\s+as\s+(?:u8|u16|u32|u64|usize|i8|i16|i32|i64|isize|f32|f64)\b", "soft"),
    ("unsafe_block", "uses unsafe block", r"\bunsafe\s*\{", "hard"),
]

STYLE_RULES_FILE = [
    ("backup_file", "created backup/temp file", r"\.(?:backup|bak|old|orig|tmp)\b", "hard"),
    ("summary_doc", "created summary/notes/solution file", r"(?:IMPLEMENTATION_SUMMARY|CHANGES|SOLUTION|NOTES|SUMMARY).*\.(?:md|txt)$", "hard"),
]

PROMPT_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


@dataclass(frozen=True)
class TaskRow:
    index: int
    task: str
    gold_patch: str
    repo: str
    pr_number: str
    base_sha: str
    head_sha: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float


@dataclass(frozen=True)
class CompilationResults:
    checks: dict[str, CommandResult]
    scope: str


class BenchmarkError(RuntimeError):
    pass


def run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
    input: str | None = None,
) -> CommandResult:
    started = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        env=env,
        text=True,
        input=input,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    elapsed = time.time() - started
    result = CommandResult(proc.returncode, proc.stdout, proc.stderr, elapsed)
    if check and proc.returncode != 0:
        rendered = " ".join(cmd)
        raise BenchmarkError(f"Command failed ({proc.returncode}): {rendered}\n{proc.stderr[-2000:]}")
    return result


def load_rows(path: Path) -> list[TaskRow]:
    rows: list[TaskRow] = []
    with path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise BenchmarkError(f"Dataset line {idx + 1} is not a JSON object")
            meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
            task = raw.get("task", "")
            gold_patch = raw.get("gold_patch", raw.get("diff", ""))
            if not isinstance(task, str) or not task.strip():
                raise BenchmarkError(f"Dataset line {idx + 1} has no non-empty task")
            if not isinstance(gold_patch, str) or not gold_patch.strip():
                raise BenchmarkError(f"Dataset line {idx + 1} has no non-empty gold_patch/diff")
            base_sha = str(meta.get("base_sha", raw.get("base_sha", ""))).strip()
            if not base_sha:
                raise BenchmarkError(f"Dataset line {idx + 1} has no base_sha")
            rows.append(
                TaskRow(
                    index=idx,
                    task=task,
                    gold_patch=gold_patch,
                    repo=str(meta.get("repo", raw.get("repo", ""))),
                    pr_number=str(meta.get("pr_number", raw.get("pr_number", idx))),
                    base_sha=base_sha,
                    head_sha=str(meta.get("head_sha", raw.get("head_sha", ""))),
                    metadata=meta,
                )
            )
    return rows


def safe_slug(value: str, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return slug[:120] if slug else fallback


def ensure_commit_available(repo_dir: Path, sha: str) -> None:
    check = run_command(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_dir)
    if check.returncode == 0:
        return
    fetch = run_command(["git", "fetch", "origin", sha, "--depth", "1"], cwd=repo_dir, timeout=600)
    if fetch.returncode != 0:
        raise BenchmarkError(f"Could not fetch base commit {sha}: {fetch.stderr[-2000:]}")
    check_again = run_command(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_dir)
    if check_again.returncode != 0:
        raise BenchmarkError(f"Base commit {sha} is unavailable after fetch")


def setup_worktree(source_repo: Path, worktrees_dir: Path, row: TaskRow, keep_existing: bool) -> Path:
    ensure_commit_available(source_repo, row.base_sha)
    name = f"task_{row.index:04d}_pr_{safe_slug(row.pr_number, str(row.index))}"
    worktree = worktrees_dir / name
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    if worktree.exists() and not keep_existing:
        # Only remove directories under the explicitly configured worktrees dir.
        resolved_root = worktrees_dir.resolve()
        resolved_worktree = worktree.resolve()
        if resolved_root not in resolved_worktree.parents:
            raise BenchmarkError(f"Refusing to remove path outside worktrees dir: {worktree}")
        shutil.rmtree(worktree)

    if worktree.exists():
        run_command(["git", "checkout", "-f", row.base_sha], cwd=worktree, check=True)
        run_command(["git", "clean", "-fd"], cwd=worktree, check=True)
        return worktree

    add = run_command(
        ["git", "worktree", "add", "--detach", str(worktree), row.base_sha],
        cwd=source_repo,
        timeout=600,
    )
    if add.returncode != 0:
        run_command(["git", "worktree", "prune"], cwd=source_repo)
        add = run_command(
            ["git", "worktree", "add", "--detach", str(worktree), row.base_sha],
            cwd=source_repo,
            timeout=600,
        )
    if add.returncode != 0:
        raise BenchmarkError(f"git worktree add failed for {row.base_sha}: {add.stderr[-2000:]}")
    return worktree


def capture_diff(repo_dir: Path) -> str:
    # Make untracked files visible in `git diff HEAD` without staging content.
    run_command(["git", "add", "-N", "--", "."], cwd=repo_dir)
    return run_command(["git", "diff", "HEAD"], cwd=repo_dir).stdout


def changed_files_from_patch(patch: str) -> set[str]:
    return {m.group(2) for m in DIFF_FILE_RE.finditer(patch)}


def parse_diff_lines(patch: str) -> dict[str, dict[str, list[str]]]:
    files: dict[str, dict[str, list[str]]] = {}
    current: str | None = None
    for line in patch.splitlines():
        match = DIFF_FILE_RE.match(line)
        if match:
            current = match.group(2)
            files[current] = {"added": [], "removed": []}
            continue
        if current is None:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            files[current]["added"].append(line[1:].strip())
        elif line.startswith("-"):
            files[current]["removed"].append(line[1:].strip())
    return files


def _score_prf(agent: set[str], expected: set[str]) -> dict[str, Any]:
    """Compute precision, recall, F1 for two sets."""
    if not expected:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0, "agent": len(agent), "expected": 0, "overlap": 0}
    overlap = len(agent & expected)
    precision = overlap / len(agent) if agent else 0.0
    recall = overlap / len(expected)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "agent": len(agent),
        "expected": len(expected),
        "overlap": overlap,
    }


def compare_diffs(agent_diff: str, gold_patch: str) -> dict[str, Any]:
    agent_files = parse_diff_lines(agent_diff)
    gold_files = parse_diff_lines(gold_patch)
    agent_file_set = set(agent_files)
    gold_file_set = set(gold_files)
    agent_added = {line for data in agent_files.values() for line in data["added"] if line}
    agent_removed = {line for data in agent_files.values() for line in data["removed"] if line}
    gold_added = {line for data in gold_files.values() for line in data["added"] if line}
    gold_removed = {line for data in gold_files.values() for line in data["removed"] if line}
    return {
        "file_scores": _score_prf(agent_file_set, gold_file_set),
        "added_line_scores": _score_prf(agent_added, gold_added),
        "removed_line_scores": _score_prf(agent_removed, gold_removed),
        "files": {
            "agent_only": sorted(agent_file_set - gold_file_set),
            "expected_only": sorted(gold_file_set - agent_file_set),
            "overlap": sorted(agent_file_set & gold_file_set),
        },
    }


def style_check(agent_diff: str) -> dict[str, Any]:
    if not agent_diff.strip():
        return {"score": 0.0, "violations": [], "added_lines": 0, "note": "empty diff"}

    added_lines: list[str] = []
    changed_files: set[str] = set()
    for line in agent_diff.splitlines():
        match = DIFF_FILE_RE.match(line)
        if match:
            changed_files.add(match.group(2))
            continue
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added_lines.append(line[1:])

    added_text = "\n".join(added_lines)
    violations: list[dict[str, Any]] = []

    for key, description, pattern, severity in STYLE_RULES_ADDED:
        count = len(re.findall(pattern, added_text))
        if count:
            violations.append({"rule": key, "description": description, "count": count, "severity": severity})

    for path in sorted(changed_files):
        for key, description, pattern, severity in STYLE_RULES_FILE:
            if re.search(pattern, path, re.IGNORECASE):
                violations.append({"rule": key, "description": description, "file": path, "count": 1, "severity": severity})

    hard = sum(int(v["count"]) for v in violations if v["severity"] == "hard")
    soft = sum(int(v["count"]) for v in violations if v["severity"] == "soft")
    score = max(0.0, min(10.0, 10.0 - 2.5 * hard - 0.5 * soft))
    return {
        "score": round(score, 2),
        "violations": violations,
        "added_lines": len(added_lines),
        "hard_penalty_units": hard,
        "soft_penalty_units": soft,
    }


def build_agent_prompt(task: str, prompt_file: Path | None) -> str:
    if prompt_file:
        template = prompt_file.read_text(encoding="utf-8")
    else:
        template = _load_prompt("agent")
    if "{task}" not in template:
        return template.rstrip() + "\n\nTask:\n" + task
    return template.format(task=task)


def run_opencode_agent(
    *,
    worktree: Path,
    row: TaskRow,
    opencode_bin: str,
    model: str | None,
    prompt_file: Path | None,
    timeout: int,
    skip_permissions: bool,
    extra_env: dict[str, str],
) -> tuple[CommandResult, str]:
    prompt = build_agent_prompt(row.task, prompt_file)
    cmd = [opencode_bin, "run", "--dir", str(worktree), "--format", "json"]
    if model:
        cmd.extend(["--model", model])
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd.append(prompt)
    env = os.environ.copy()
    env.update(extra_env)
    return run_command(cmd, cwd=worktree, timeout=timeout, env=env), prompt


def extract_text_from_opencode_json(stdout: str) -> str:
    parts: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "text":
            part = event.get("part", {})
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "".join(parts).strip()


def parse_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    candidates = [text.strip()]
    if "```" in text:
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE | re.DOTALL)
        candidates.append(stripped)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


GLOBAL_BUILD_FILES = {
    "Cargo.toml",
    "Cargo.lock",
    ".cargo/config.toml",
    ".cargo/config",
    "rust-toolchain",
    "rust-toolchain.toml",
}

GLOBAL_BUILD_FILE_PATTERNS = [
    re.compile(r"^\.cargo/"),
    re.compile(r"^build\.rs$"),
    re.compile(r"/build\.rs$"),
    re.compile(r"^\.github/workflows/"),
]


def _is_global_build_file(path: str) -> bool:
    if path in GLOBAL_BUILD_FILES:
        return True
    for pattern in GLOBAL_BUILD_FILE_PATTERNS:
        if pattern.search(path):
            return True
    return False


def _get_cargo_metadata(worktree: Path) -> dict[str, Any] | None:
    proc = run_command(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        cwd=worktree,
        timeout=60,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _build_reverse_dependency_graph(metadata: dict[str, Any]) -> dict[str, set[str]]:
    """Map each crate name to the set of workspace crates that depend on it."""
    reverse_deps: dict[str, set[str]] = {}
    packages = {pkg["name"]: pkg for pkg in metadata.get("packages", [])}
    for pkg_name, pkg in packages.items():
        for dep in pkg.get("dependencies", []):
            dep_name = dep["name"]
            if dep_name in packages:
                reverse_deps.setdefault(dep_name, set()).add(pkg_name)
    return reverse_deps


def _resolve_affected_crates(
    changed_files: list[str],
    worktree: Path,
    metadata: dict[str, Any],
) -> set[str] | None:
    """
    Return the set of workspace crate names that need checking.
    Returns None if a global/workspace-level file was changed, signalling a full check.
    """
    packages = metadata.get("packages", [])
    if not packages:
        return None

    pkg_by_name = {p["name"]: p for p in packages}
    pkg_by_root: dict[str, str] = {}
    for pkg in packages:
        manifest_path = pkg.get("manifest_path", "")
        if manifest_path:
            try:
                root_dir = str(Path(manifest_path).parent.resolve().relative_to(Path(worktree).resolve()))
                pkg_by_root[root_dir] = pkg["name"]
            except (ValueError, RuntimeError):
                pass

    directly_changed: set[str] = set()
    for filepath in changed_files:
        if _is_global_build_file(filepath):
            return None

        path_parts = Path(filepath).parts
        for depth in range(len(path_parts), 0, -1):
            candidate = "/".join(path_parts[:depth])
            if candidate in pkg_by_root:
                directly_changed.add(pkg_by_root[candidate])
                break

    if not directly_changed:
        return None

    reverse_deps = _build_reverse_dependency_graph(metadata)
    affected = set(directly_changed)
    queue = list(directly_changed)
    visited = set(directly_changed)

    while queue:
        current = queue.pop(0)
        for dependent in reverse_deps.get(current, set()):
            if dependent not in visited:
                visited.add(dependent)
                affected.add(dependent)
                queue.append(dependent)

    return affected


def run_compilation_checks(
    worktree: Path,
    changed_files: list[str],
    timeout_per_check: int,
) -> CompilationResults:
    metadata = _get_cargo_metadata(worktree)
    affected_crates: set[str] | None = None
    if metadata:
        affected_crates = _resolve_affected_crates(changed_files, worktree, metadata)

    if affected_crates is None:
        check_cmd = ["cargo", "check", "--workspace", "--all-targets"]
        scope_note = "full_workspace"
    else:
        check_cmd = ["cargo", "check"]
        for crate in sorted(affected_crates):
            check_cmd.extend(["-p", crate])
        scope_note = f"crates:{','.join(sorted(affected_crates))}"

    checks: dict[str, CommandResult] = {}
    checks["rustfmt"] = run_command(
        ["cargo", "+nightly", "fmt", "--", "--check"],
        cwd=worktree,
        timeout=min(timeout_per_check, 120),
    )
    checks["cargo_check_affected"] = run_command(
        check_cmd,
        cwd=worktree,
        timeout=timeout_per_check,
        env=os.environ.copy(),
    )

    return CompilationResults(checks=checks, scope=scope_note)


def setup_judge_worktree(
    source_repo: Path,
    worktrees_dir: Path,
    row: TaskRow,
    judge_id: int,
) -> Path:
    name = f"judge_{judge_id:02d}_row_{row.index:04d}_pr_{safe_slug(row.pr_number, str(row.index))}"
    judge_dir = worktrees_dir / name

    if judge_dir.exists():
        resolved_root = worktrees_dir.resolve()
        resolved_judge = judge_dir.resolve()
        if resolved_root not in resolved_judge.parents:
            raise BenchmarkError(f"Refusing to remove path outside worktrees dir: {judge_dir}")
        shutil.rmtree(judge_dir)

    add = run_command(
        ["git", "worktree", "add", "--detach", str(judge_dir), row.base_sha],
        cwd=source_repo,
        timeout=600,
    )
    if add.returncode != 0:
        run_command(["git", "worktree", "prune"], cwd=source_repo)
        add = run_command(
            ["git", "worktree", "add", "--detach", str(judge_dir), row.base_sha],
            cwd=source_repo,
            timeout=600,
        )
    if add.returncode != 0:
        raise BenchmarkError(f"git worktree add failed for judge {judge_id}: {add.stderr[-2000:]}")

    return judge_dir


def teardown_judge_worktree(source_repo: Path, judge_dir: Path) -> None:
    run_command(["git", "worktree", "remove", "--force", str(judge_dir)], cwd=source_repo)


def run_judge_in_worktree(
    *,
    judge_id: int,
    source_repo: Path,
    worktrees_dir: Path,
    row: TaskRow,
    agent_diff: str,
    review: dict[str, Any],
    compilation_summary: str,
    opencode_bin: str,
    judge_model: str,
    timeout: int,
    skip_permissions: bool,
) -> dict[str, Any]:
    judge_dir = setup_judge_worktree(source_repo, worktrees_dir, row, judge_id)

    try:
        benchmark_dir = judge_dir / ".benchmark"
        benchmark_dir.mkdir(parents=True, exist_ok=True)

        (benchmark_dir / "task.txt").write_text(row.task, encoding="utf-8")
        (benchmark_dir / "gold.patch").write_text(row.gold_patch, encoding="utf-8")
        (benchmark_dir / "agent.patch").write_text(agent_diff, encoding="utf-8")
        (benchmark_dir / "review.json").write_text(
            json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (benchmark_dir / "compilation.txt").write_text(compilation_summary, encoding="utf-8")

        run_command(["git", "apply", "--whitespace=nowarn"], cwd=judge_dir, input=agent_diff)

        prompt = _load_prompt("judge").format(
            task=row.task,
            expected_diff=truncate_middle(row.gold_patch, 40_000),
            agent_diff=truncate_middle(agent_diff, 40_000),
        )
        cmd = [
            opencode_bin,
            "run",
            "--dir",
            str(judge_dir),
            "--format",
            "json",
            "--model",
            judge_model,
        ]
        if skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        cmd.append(prompt)

        proc = run_command(cmd, cwd=judge_dir, timeout=timeout)
        text = extract_text_from_opencode_json(proc.stdout) or proc.stdout.strip()
        parsed = parse_json_object(text)
        if parsed is None:
            return {
                "parse_error": True,
                "returncode": proc.returncode,
                "raw_excerpt": text[:2000],
                "stderr_excerpt": proc.stderr[-2000:],
            }
        for key in ("correctness", "completeness", "convention_adherence", "hygiene"):
            if key in parsed and isinstance(parsed[key], (int, float)):
                parsed[key] = max(0, min(10, int(round(parsed[key]))))
        try:
            computed = round(
                0.35 * float(parsed.get("correctness", 0))
                + 0.30 * float(parsed.get("completeness", 0))
                + 0.20 * float(parsed.get("convention_adherence", 0))
                + 0.15 * float(parsed.get("hygiene", 0)),
                2,
            )
            parsed["overall"] = computed
        except (TypeError, ValueError):
            pass
        parsed["parse_error"] = False
        parsed["returncode"] = proc.returncode
        parsed["judge_id"] = judge_id
        return parsed
    finally:
        teardown_judge_worktree(source_repo, judge_dir)


def format_compilation_results(results: CompilationResults) -> str:
    """Format compilation check results into a human-readable summary for the PR reviewer."""
    lines: list[str] = []
    lines.append("=== Compilation & Quality Check Results ===")
    lines.append(f"Scope: {results.scope}")
    lines.append("")

    for check_name, result in results.checks.items():
        status = "PASS" if result.returncode == 0 else "FAIL"
        if result.returncode == -1:
            status = "SKIP"
        lines.append(f"[{status}] {check_name} (rc={result.returncode}, elapsed={round(result.elapsed_seconds, 1)}s)")
        if result.returncode != 0:
            err = result.stderr[-1500:] if result.stderr else "(no stderr)"
            lines.append(f"  STDERR excerpt:\n{err}")
        lines.append("")

    passed = sum(1 for r in results.checks.values() if r.returncode == 0)
    total = len(results.checks)
    lines.append(f"Summary: {passed}/{total} checks passed")
    return "\n".join(lines)


def run_pr_reviewer(
    *,
    worktree: Path,
    row: TaskRow,
    agent_diff: str,
    compilation_results: CompilationResults,
    opencode_bin: str,
    reviewer_model: str,
    timeout: int,
    skip_permissions: bool,
) -> dict[str, Any]:
    compilation_summary = format_compilation_results(compilation_results)
    prompt = _load_prompt("reviewer").format(
        task=row.task,
        gold_patch=truncate_middle(row.gold_patch, 40_000),
        agent_diff=truncate_middle(agent_diff, 40_000),
        compilation_result=compilation_summary,
    )
    cmd = [
        opencode_bin,
        "run",
        "--dir",
        str(worktree),
        "--format",
        "json",
        "--model",
        reviewer_model,
    ]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd.append(prompt)
    proc = run_command(cmd, cwd=worktree, timeout=timeout)
    text = extract_text_from_opencode_json(proc.stdout) or proc.stdout.strip()
    parsed = parse_json_object(text)
    if parsed is None:
        return {
            "parse_error": True,
            "returncode": proc.returncode,
            "raw_excerpt": text[:2000],
            "stderr_excerpt": proc.stderr[-2000:],
        }
    parsed["parse_error"] = False
    parsed["returncode"] = proc.returncode
    return parsed


def run_llm_judge(
    *,
    worktree: Path,
    row: TaskRow,
    agent_diff: str,
    opencode_bin: str,
    judge_model: str,
    timeout: int,
    skip_permissions: bool,
) -> dict[str, Any]:
    prompt = _load_prompt("judge").format(
        task=row.task,
        expected_diff=truncate_middle(row.gold_patch, 40_000),
        agent_diff=truncate_middle(agent_diff, 40_000),
    )
    cmd = [opencode_bin, "run", "--dir", str(worktree), "--format", "json", "--model", judge_model]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd.append(prompt)
    proc = run_command(cmd, cwd=worktree, timeout=timeout)
    text = extract_text_from_opencode_json(proc.stdout) or proc.stdout.strip()
    parsed = parse_json_object(text)
    if parsed is None:
        return {
            "parse_error": True,
            "returncode": proc.returncode,
            "raw_excerpt": text[:2000],
            "stderr_excerpt": proc.stderr[-2000:],
        }
    for key in ("correctness", "completeness", "convention_adherence", "hygiene"):
        if key in parsed and isinstance(parsed[key], (int, float)):
            parsed[key] = max(0, min(10, int(round(parsed[key]))))
    try:
        computed = round(
            0.35 * float(parsed.get("correctness", 0))
            + 0.30 * float(parsed.get("completeness", 0))
            + 0.20 * float(parsed.get("convention_adherence", 0))
            + 0.15 * float(parsed.get("hygiene", 0)),
            2,
        )
        parsed["overall"] = computed
    except (TypeError, ValueError):
        pass
    parsed["parse_error"] = False
    parsed["returncode"] = proc.returncode
    return parsed


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max((limit - 200) // 2, 1000)
    return text[:keep] + f"\n\n[truncated {len(text) - 2 * keep} chars]\n\n" + text[-keep:]


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def select_rows(rows: list[TaskRow], args: argparse.Namespace) -> list[TaskRow]:
    selected = rows
    if args.start_index is not None:
        selected = [row for row in selected if row.index >= args.start_index]
    if args.pr_number:
        wanted = {str(x) for x in args.pr_number}
        selected = [row for row in selected if row.pr_number in wanted]
    if args.row_index:
        wanted_idx = {int(x) for x in args.row_index}
        selected = [row for row in selected if row.index in wanted_idx]
    if args.limit is not None:
        selected = selected[: args.limit]
    return selected


def run_one(row: TaskRow, args: argparse.Namespace) -> dict[str, Any]:
    source_repo = args.repo.resolve()
    out_name = f"row_{row.index:04d}_pr_{safe_slug(row.pr_number, str(row.index))}"
    out_dir = (args.results_dir / out_name).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    result: dict[str, Any] = {
        "row_index": row.index,
        "repo": row.repo,
        "pr_number": row.pr_number,
        "base_sha": row.base_sha,
        "head_sha": row.head_sha,
        "model": args.model,
        "status": "started",
        "output_dir": str(out_dir),
    }
    worktree: Path | None = None

    try:
        worktree = setup_worktree(source_repo, args.worktrees_dir.resolve(), row, args.keep_existing_worktrees)
        result["worktree"] = str(worktree)

        proc, prompt = run_opencode_agent(
            worktree=worktree,
            row=row,
            opencode_bin=args.opencode_bin,
            model=args.model,
            prompt_file=args.prompt_file,
            timeout=args.timeout_seconds,
            skip_permissions=not args.no_skip_permissions,
            extra_env=parse_env_overrides(args.env),
        )

        agent_diff = capture_diff(worktree)
        changed_files = sorted(changed_files_from_patch(agent_diff))
        comparison = compare_diffs(agent_diff, row.gold_patch)
        style = style_check(agent_diff)

        write_text(out_dir / "prompt.txt", prompt)
        write_text(out_dir / "opencode.stdout.jsonl", proc.stdout)
        write_text(out_dir / "opencode.stderr.txt", proc.stderr)
        write_text(out_dir / "agent.patch", agent_diff)
        write_text(out_dir / "gold.patch", row.gold_patch)
        write_json(out_dir / "metadata.json", row.metadata)
        write_json(out_dir / "comparison.json", comparison)
        write_json(out_dir / "style_rule.json", style)
        write_json(out_dir / "changed_files.json", changed_files)

        compilation_results: dict[str, CommandResult] = {}
        if args.run_compilation_check:
            print(f"  [{row.index}] Running compilation checks...", flush=True)
            compilation_results = run_compilation_checks(
                worktree=worktree,
                changed_files=changed_files,
                timeout_per_check=args.compilation_timeout_seconds,
            )
            write_json(
                out_dir / "compilation.json",
                {
                    "checks": {
                        name: {
                            "returncode": res.returncode,
                            "elapsed_seconds": round(res.elapsed_seconds, 2),
                            "stdout_excerpt": res.stdout[:2000],
                            "stderr_excerpt": res.stderr[:2000],
                        }
                        for name, res in compilation_results.checks.items()
                    },
                    "summary": {
                        "passed": sum(1 for r in compilation_results.checks.values() if r.returncode == 0),
                        "total": len(compilation_results.checks),
                    },
                },
            )

        review = None
        if args.reviewer_model:
            print(f"  [{row.index}] Running PR reviewer...", flush=True)
            review = run_pr_reviewer(
                worktree=worktree,
                row=row,
                agent_diff=agent_diff,
                compilation_results=compilation_results,
                opencode_bin=args.opencode_bin,
                reviewer_model=args.reviewer_model,
                timeout=args.reviewer_timeout_seconds,
                skip_permissions=not args.no_skip_permissions,
            )
            write_json(out_dir / "review.json", review)

        judges: list[dict[str, Any]] = []
        if args.judge_model and args.judge_count > 0:
            compilation_summary = format_compilation_results(compilation_results)
            review_for_judges = review or {}

            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=args.judge_count) as pool:
                futures = {}
                for judge_id in range(args.judge_count):
                    future = pool.submit(
                        run_judge_in_worktree,
                        judge_id=judge_id,
                        source_repo=source_repo,
                        worktrees_dir=args.judges_worktrees_dir.resolve(),
                        row=row,
                        agent_diff=agent_diff,
                        review=review_for_judges,
                        compilation_summary=compilation_summary,
                        opencode_bin=args.opencode_bin,
                        judge_model=args.judge_model,
                        timeout=args.judge_timeout_seconds,
                        skip_permissions=not args.no_skip_permissions,
                    )
                    futures[future] = judge_id

                for future in as_completed(futures):
                    judge_id = futures[future]
                    try:
                        judge_result = future.result()
                        write_json(out_dir / f"judge_{judge_id:02d}.json", judge_result)
                        judges.append(judge_result)
                        print(f"  [{row.index}] Judge {judge_id + 1}/{args.judge_count} completed", flush=True)
                    except Exception as exc:
                        error_judge = {
                            "judge_id": judge_id,
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                        }
                        write_json(out_dir / f"judge_{judge_id:02d}.json", error_judge)
                        judges.append(error_judge)
                        print(f"  [{row.index}] Judge {judge_id + 1}/{args.judge_count} FAILED: {exc}", flush=True)

        result.update(
            {
                "status": "ok",
                "agent_returncode": proc.returncode,
                "agent_elapsed_seconds": round(proc.elapsed_seconds, 2),
                "has_patch": bool(agent_diff.strip()),
                "num_changed_files": len(changed_files),
                "changed_files": changed_files,
                "file_f1": comparison["file_scores"]["f1"],
                "file_recall": comparison["file_scores"]["recall"],
                "added_line_f1": comparison["added_line_scores"]["f1"],
                "removed_line_f1": comparison["removed_line_scores"]["f1"],
                "style_score": style["score"],
                "compilation_summary": {
                    "passed": sum(1 for r in compilation_results.checks.values() if r.returncode == 0),
                    "total": len(compilation_results.checks),
                    "all_passed": all(r.returncode == 0 for r in compilation_results.checks.values()) if compilation_results.checks else None,
                },
                "review_available": review is not None,
                "judge_count": len(judges),
                "judge_aggregate": aggregate_judge_scores(judges),
                "elapsed_seconds": round(time.time() - started, 2),
            }
        )

        run_record = {
            "run_id": args.run_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "row_index": row.index,
            "pr_number": row.pr_number,
            "base_sha": row.base_sha,
            "head_sha": row.head_sha,
            "task": row.task,
            "gold_patch": row.gold_patch,
            "agent_patch": agent_diff,
            "agent_returncode": proc.returncode,
            "file_f1": comparison["file_scores"]["f1"],
            "style_score": style["score"],
            "compilation": {
                name: {
                    "returncode": res.returncode,
                    "elapsed_seconds": round(res.elapsed_seconds, 2),
                    "stdout_excerpt": res.stdout[:2000],
                    "stderr_excerpt": res.stderr[:2000],
                }
                for name, res in compilation_results.checks.items()
            },
            "review": review,
            "judges": judges,
            "judge_aggregate": aggregate_judge_scores(judges),
        }
        run_line = json.dumps(run_record, ensure_ascii=False) + "\n"
        if args.runs_dir:
            args.runs_dir.mkdir(parents=True, exist_ok=True)
            run_path = args.runs_dir / f"run_{args.run_id}.jsonl"
            with run_path.open("a", encoding="utf-8") as f:
                f.write(run_line)

    except Exception as exc:
        result.update(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "elapsed_seconds": round(time.time() - started, 2),
            }
        )
        write_json(out_dir / "error.json", result)

    write_json(out_dir / "result.json", result)

    if not args.keep_existing_worktrees and worktree is not None and worktree.exists():
        resolved_root = args.worktrees_dir.resolve()
        resolved_worktree = worktree.resolve()
        if resolved_root in resolved_worktree.parents:
            run_command(["git", "worktree", "remove", "--force", str(worktree)], cwd=source_repo)

    return result


def parse_env_overrides(items: list[str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise BenchmarkError(f"--env must be KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        if not key:
            raise BenchmarkError(f"--env key is empty in {item!r}")
        env[key] = value
    return env


def validate_args(args: argparse.Namespace) -> None:
    if not args.dataset.is_file():
        raise BenchmarkError(f"Dataset not found: {args.dataset}")
    if not args.repo.exists():
        raise BenchmarkError(f"Repo path not found: {args.repo}")
    if not (args.repo / ".git").exists():
        raise BenchmarkError(f"Repo path does not look like a git checkout: {args.repo}")
    if not shutil.which(args.opencode_bin):
        raise BenchmarkError(f"opencode binary not found on PATH: {args.opencode_bin}")
    if args.timeout_seconds <= 0:
        raise BenchmarkError("--timeout-seconds must be positive")
    if args.reviewer_model and args.reviewer_timeout_seconds <= 0:
        raise BenchmarkError("--reviewer-timeout-seconds must be positive")
    if args.judge_model and args.judge_timeout_seconds <= 0:
        raise BenchmarkError("--judge-timeout-seconds must be positive")
    if args.run_compilation_check and args.compilation_timeout_seconds <= 0:
        raise BenchmarkError("--compilation-timeout-seconds must be positive")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path, help="Input JSONL dataset, preferably filtered")
    parser.add_argument("--repo", required=True, type=Path, help="Local canonical git checkout of the repo")
    parser.add_argument("--worktrees-dir", required=True, type=Path, help="Directory where per-task git worktrees are created")
    parser.add_argument("--results-dir", required=True, type=Path, help="Directory where artifacts are written")
    parser.add_argument("--model", default=None, help="opencode model string, e.g. provider/model")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--prompt-file", type=Path, default=None, help="Optional prompt template file containing {task}")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--judge-model", default=None, help="Optional opencode model string for LLM judging")
    parser.add_argument("--judge-timeout-seconds", type=int, default=600)
    parser.add_argument("--judge-count", type=int, default=1, help="Number of independent judges to run per task")
    parser.add_argument("--reviewer-model", default=None, help="Optional opencode model string for PR reviewer")
    parser.add_argument("--reviewer-timeout-seconds", type=int, default=600)
    parser.add_argument("--run-compilation-check", action="store_true", help="Run cargo check on the agent's worktree before judging")
    parser.add_argument("--compilation-timeout-seconds", type=int, default=300)
    parser.add_argument("--judges-worktrees-dir", type=Path, default=None, help="Directory for judge worktrees (defaults to worktrees-dir)")
    parser.add_argument("--runs-dir", type=Path, default=Path(__file__).parent / "runs", help="Directory for run JSONL records")
    parser.add_argument("--run-id", type=str, default=None, help="Unique run ID (auto-generated if not provided)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--start-index", type=int, default=None)
    parser.add_argument("--row-index", action="append", help="Run only this original JSONL row index. May be repeated.")
    parser.add_argument("--pr-number", action="append", help="Run only this PR number. May be repeated.")
    parser.add_argument("--keep-existing-worktrees", action="store_true", help="Reuse/reset existing worktree dirs instead of deleting first")
    parser.add_argument("--no-skip-permissions", action="store_true", help="Do not pass --dangerously-skip-permissions to opencode")
    parser.add_argument("--env", action="append", help="Extra environment variable for opencode, KEY=VALUE. May be repeated.")
    args = parser.parse_args()

    if args.run_id is None:
        args.run_id = uuid.uuid4().hex[:12]
    if args.judges_worktrees_dir is None:
        args.judges_worktrees_dir = args.worktrees_dir

    try:
        validate_args(args)
        rows = select_rows(load_rows(args.dataset), args)
        args.results_dir.mkdir(parents=True, exist_ok=True)
        if not rows:
            raise BenchmarkError("No rows selected")

        summary_path = args.results_dir / "summary.jsonl"
        aggregate: list[dict[str, Any]] = []
        with summary_path.open("a", encoding="utf-8") as summary_f:
            for pos, row in enumerate(rows, start=1):
                print(f"[{pos}/{len(rows)}] row={row.index} pr={row.pr_number} base={row.base_sha[:12]}", flush=True)
                result = run_one(row, args)
                aggregate.append(result)
                summary_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                summary_f.flush()
                status = result.get("status")
                judge_agg = result.get("judge_aggregate", {})
                judge = judge_agg.get("final_score", {}).get("overall")
                print(
                    f"  status={status} patch={result.get('has_patch')} "
                    f"file_f1={result.get('file_f1')} style={result.get('style_score')} judge={judge}",
                    flush=True,
                )

        ok = [r for r in aggregate if r.get("status") == "ok"]
        final = {
            "dataset": str(args.dataset),
            "model": args.model,
            "judge_model": args.judge_model,
            "total_selected": len(rows),
            "ok": len(ok),
            "errors": len(aggregate) - len(ok),
            "mean_file_f1": round(sum(float(r.get("file_f1") or 0) for r in ok) / len(ok), 3) if ok else None,
            "mean_style_score": round(sum(float(r.get("style_score") or 0) for r in ok) / len(ok), 3) if ok else None,
            "mean_judge_overall": round(
                sum(
                    float(r.get("judge_aggregate", {}).get("final_score", {}).get("overall") or 0)
                    for r in ok
                )
                / max(
                    1,
                    sum(
                        1
                        for r in ok
                        if r.get("judge_aggregate", {}).get("final_score", {}).get("overall") is not None
                    ),
                ),
                3,
            ) if args.judge_model else None,
            "summary_jsonl": str(summary_path),
        }
        write_json(args.results_dir / "aggregate.json", final)
        print(json.dumps(final, indent=2))
        return 0
    except BenchmarkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
