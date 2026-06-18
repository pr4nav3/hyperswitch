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
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aggregate_judges import aggregate_task_judges as aggregate_judge_scores


TEST_FILE_RE = re.compile(r"tests?[/\\]|_test\.rs$|_tests\.rs$", re.IGNORECASE)
DIFF_FILE_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.MULTILINE)

_ACTIVE_WORKTREES: list[Path] = []
_SOURCE_REPO: Path | None = None
_SHUTDOWN_REQUESTED: threading.Event = threading.Event()
_WRITE_LOCK: threading.Lock = threading.Lock()
_PRINT_LOCK: threading.Lock = threading.Lock()


def _register_worktree(path: Path) -> None:
    if path not in _ACTIVE_WORKTREES:
        _ACTIVE_WORKTREES.append(path)


def _unregister_worktree(path: Path) -> None:
    if path in _ACTIVE_WORKTREES:
        _ACTIVE_WORKTREES.remove(path)


def _cleanup_on_signal(signum: int, frame: Any) -> None:
    sig_name = signal.Signals(signum).name
    count = len(_ACTIVE_WORKTREES)
    with _PRINT_LOCK:
        print(f"\nReceived {sig_name}, shutting down... Cleaning up {count} active worktree(s).", flush=True)
    _SHUTDOWN_REQUESTED.set()
    for wt in list(_ACTIVE_WORKTREES):
        try:
            if _SOURCE_REPO is not None and _SOURCE_REPO.exists():
                run_command(
                    ["git", "worktree", "remove", "--force", str(wt)],
                    cwd=_SOURCE_REPO,
                    check=False,
                )
            if wt.exists():
                shutil.rmtree(wt, ignore_errors=True)
        except Exception:
            pass


try:
    signal.signal(signal.SIGINT, _cleanup_on_signal)
    signal.signal(signal.SIGTERM, _cleanup_on_signal)
except ValueError:
    pass  # Signals not available on this platform

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


def _ensure_cargo_on_path(env: dict[str, str] | None) -> dict[str, str]:
    merged = dict(os.environ) if env is None else {**os.environ, **env}
    path = merged.get("PATH", "")
    if shutil.which("cargo", path=path):
        return merged
    candidates: list[str] = []
    cargo_home = os.environ.get("CARGO_HOME")
    if cargo_home:
        candidates.append(str(Path(cargo_home) / "bin"))
    candidates.append(str(Path.home() / ".cargo" / "bin"))
    if sys.platform == "darwin":
        candidates.extend(["/opt/homebrew/bin", "/usr/local/bin"])
    for candidate in candidates:
        if shutil.which("cargo", path=candidate):
            merged["PATH"] = candidate + os.pathsep + path
            return merged
    return merged


def run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
    input: str | None = None,
    retries_on_timeout: int = 0,
) -> CommandResult:
    started = time.time()
    last_exception = None
    for attempt in range(retries_on_timeout + 1):
        try:
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
        except subprocess.TimeoutExpired as exc:
            last_exception = exc
            if attempt < retries_on_timeout:
                print(f"  Timeout on attempt {attempt + 1}/{retries_on_timeout + 1}, retrying...", flush=True)
                continue
            raise BenchmarkError(f"Command timed out after {retries_on_timeout + 1} attempts: {' '.join(cmd)}") from exc


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
        run_command(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=source_repo,
            check=False,
        )
        add = run_command(
            ["git", "worktree", "add", "--detach", str(worktree), row.base_sha],
            cwd=source_repo,
            timeout=600,
        )
    if add.returncode != 0:
        if worktree.exists() and not (worktree / ".git").exists():
            shutil.rmtree(worktree, ignore_errors=True)
        stderr = add.stderr[-2000:] if add.stderr else "(no stderr)"
        raise BenchmarkError(
            f"FATAL: git worktree add failed twice for {row.base_sha}. "
            f"The worktree could not be created. Error: {stderr}"
        )
    _register_worktree(worktree)
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
            files[current]["added"].append(line[1:])
        elif line.startswith("-"):
            files[current]["removed"].append(line[1:])
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
    current_file: str = ""
    for line in agent_diff.splitlines():
        match = DIFF_FILE_RE.match(line)
        if match:
            current_file = match.group(2)
            changed_files.add(current_file)
            continue
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added_lines.append((current_file, line[1:]))

    violations: list[dict[str, Any]] = []

    for key, description, pattern, severity in STYLE_RULES_ADDED:
        count = 0
        for filepath, content in added_lines:
            if TEST_FILE_RE.search(filepath):
                continue
            count += len(re.findall(pattern, content))
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
    return run_command(cmd, cwd=worktree, timeout=timeout, env=env, retries_on_timeout=1), prompt


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

    candidates: list[str] = [text.strip()]
    if "```" in text:
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE | re.DOTALL)
        candidates.append(stripped)

    def _balanced_braces(s: str) -> list[str]:
        blocks: list[str] = []
        depth = 0
        start = -1
        for i, ch in enumerate(s):
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start != -1:
                    blocks.append(s[start:i + 1])
                    start = -1
        return blocks

    for source in list(candidates):
        for block in sorted(_balanced_braces(source), key=len, reverse=True):
            candidates.append(block)

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
    cargo_env = _ensure_cargo_on_path(None)
    checks["rustfmt"] = run_command(
        ["cargo", "+nightly", "fmt", "--", "--check"],
        cwd=worktree,
        timeout=min(timeout_per_check, 120),
        env=cargo_env,
    )
    checks["cargo_check_affected"] = run_command(
        check_cmd,
        cwd=worktree,
        timeout=timeout_per_check,
        env=cargo_env,
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
        run_command(
            ["git", "worktree", "remove", "--force", str(judge_dir)],
            cwd=source_repo,
            check=False,
        )
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
    _register_worktree(judge_dir)

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
            expected_diff=row.gold_patch,
            agent_diff=agent_diff,
            review_assessment=_format_review_for_judge(review),
            compilation_summary=compilation_summary,
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

        proc = run_command(cmd, cwd=judge_dir, timeout=timeout, retries_on_timeout=1)
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
                parsed[key] = max(0.0, min(10.0, float(parsed[key])))
        has_dimensions = any(isinstance(parsed.get(k), (int, float)) for k in ("correctness", "completeness", "convention_adherence", "hygiene"))
        has_overall = isinstance(parsed.get("overall"), (int, float))
        
        if has_dimensions:
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
        elif has_overall:
            print(f"  [Judge {judge_id}] WARNING: Judge returned overall={parsed['overall']} but no individual dimension scores. Preserving judge's overall.", flush=True)
        parsed["parse_error"] = False
        parsed["returncode"] = proc.returncode
        parsed["judge_id"] = judge_id
        return parsed
    finally:
        teardown_judge_worktree(source_repo, judge_dir)
        _unregister_worktree(judge_dir)


def _json_safe_value(obj: Any) -> Any:
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_json_safe_value(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): _json_safe_value(v) for k, v in obj.items()}
    return str(obj)


def _format_review_for_judge(review: dict[str, Any] | None) -> str:
    if review is None:
        return "(No prior review was produced for this task.)"
    if review.get("parse_error"):
        return "(The prior review could not be parsed.)"

    actionable_keys = [
        "executive_summary",
        "approach_analysis",
        "breaking_changes",
        "logic_assessment",
        "convention_compliance",
        "compilation_assessment",
    ]
    trimmed = {k: review[k] for k in actionable_keys if k in review}
    if review.get("_auditor_corrections"):
        trimmed["_auditor_corrections"] = review["_auditor_corrections"]
    safe = _json_safe_value(trimmed)
    try:
        return json.dumps(safe, ensure_ascii=False)
    except (TypeError, ValueError):
        return "(The review contained data that could not be serialized.)"


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


def _clean_opencode_artifacts(worktree: Path) -> None:
    omo_dir = worktree / ".omo"
    if omo_dir.exists():
        shutil.rmtree(omo_dir, ignore_errors=True)


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
        gold_patch=row.gold_patch,
        agent_diff=agent_diff,
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
    proc = run_command(cmd, cwd=worktree, timeout=timeout, retries_on_timeout=1)
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


def run_review_auditor(
    *,
    worktree: Path,
    row: TaskRow,
    agent_diff: str,
    review: dict[str, Any],
    compilation_results: CompilationResults,
    opencode_bin: str,
    auditor_model: str,
    timeout: int,
    skip_permissions: bool,
) -> dict[str, Any]:
    compilation_summary = format_compilation_results(compilation_results)
    compilation_assessment = json.dumps(review.get("compilation_assessment", {}), ensure_ascii=False)
    prompt = _load_prompt("auditor").format(
        task=row.task,
        agent_diff=agent_diff,
        compilation_assessment=compilation_assessment,
        compilation_results=compilation_summary,
    )
    cmd = [
        opencode_bin,
        "run",
        "--dir",
        str(worktree),
        "--format",
        "json",
        "--model",
        auditor_model,
    ]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd.append(prompt)
    proc = run_command(cmd, cwd=worktree, timeout=timeout, retries_on_timeout=1)
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


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _find_completed_row_indices(results_dir: Path) -> set[int]:
    """Return indices of rows that completed successfully (have result.json with status='ok')."""
    completed: set[int] = set()
    if not results_dir.exists():
        return completed
    for entry in results_dir.iterdir():
        if entry.is_dir() and entry.name.startswith("row_"):
            try:
                idx = int(entry.name.split("_")[1])
                result_file = entry / "result.json"
                if result_file.exists():
                    try:
                        data = json.loads(result_file.read_text(encoding="utf-8"))
                        if data.get("status") == "ok":
                            completed.add(idx)
                    except (json.JSONDecodeError, OSError):
                        # Corrupt or unreadable result.json — treat as incomplete
                        pass
            except (ValueError, IndexError):
                continue
    return completed


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
    global _SOURCE_REPO
    _SOURCE_REPO = source_repo
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

        MAX_AGENT_ATTEMPTS = 2
        agent_stdout_parts: list[str] = []
        agent_stderr_parts: list[str] = []
        last_agent_proc: CommandResult | None = None
        last_prompt: str = ""

        for attempt in range(1, MAX_AGENT_ATTEMPTS + 1):
            if _SHUTDOWN_REQUESTED.is_set():
                break

            if attempt > 1:
                with _PRINT_LOCK:
                    print(f"  [{row.index}] Agent attempt {attempt - 1} produced empty patch. Retrying...", flush=True)
                run_command(["git", "checkout", "-f", row.base_sha], cwd=worktree, check=True)
                run_command(["git", "clean", "-fd"], cwd=worktree, check=True)
                _clean_opencode_artifacts(worktree)

            effective_task = row.task
            if attempt > 1:
                effective_task = (
                    row.task
                    + "\n\n---\n"
                    + "IMPORTANT: You previously explored the codebase but did not make any code changes. "
                    + "This benchmark measures your ability to produce working patches. "
                    + "Please identify the exact files that need modification and make the minimal correct changes. "
                    + "Do not stop after research—complete the implementation."
                )

            temp_row = TaskRow(
                index=row.index,
                task=effective_task,
                gold_patch=row.gold_patch,
                repo=row.repo,
                pr_number=row.pr_number,
                base_sha=row.base_sha,
                head_sha=row.head_sha,
                metadata=row.metadata,
            )

            proc, prompt = run_opencode_agent(
                worktree=worktree,
                row=temp_row,
                opencode_bin=args.opencode_bin,
                model=args.model,
                prompt_file=args.prompt_file,
                timeout=args.timeout_seconds,
                skip_permissions=not args.no_skip_permissions,
                extra_env=parse_env_overrides(args.env),
            )
            last_agent_proc = proc
            last_prompt = prompt
            agent_stdout_parts.append(proc.stdout)
            agent_stderr_parts.append(proc.stderr)

            _clean_opencode_artifacts(worktree)

            agent_diff = capture_diff(worktree)
            has_patch = bool(agent_diff.strip())

            if has_patch or attempt == MAX_AGENT_ATTEMPTS:
                break

            with _PRINT_LOCK:
                print(f"  [{row.index}] Agent attempt {attempt} complete but empty patch. Will retry.", flush=True)

        if last_agent_proc is None:
            raise BenchmarkError("No agent attempts were executed")

        changed_files = sorted(changed_files_from_patch(agent_diff))
        comparison = compare_diffs(agent_diff, row.gold_patch)
        style = style_check(agent_diff)

        write_text(out_dir / "prompt.txt", last_prompt)
        write_text(out_dir / "opencode.stdout.jsonl", "\n".join(agent_stdout_parts))
        write_text(out_dir / "opencode.stderr.txt", "\n".join(agent_stderr_parts))
        write_text(out_dir / "agent.patch", agent_diff)
        write_text(out_dir / "gold.patch", row.gold_patch)

        result["agent_attempts"] = len(agent_stdout_parts)
        result["agent_empty_patch_retries"] = len(agent_stdout_parts) - 1

        compilation_results = CompilationResults(checks={}, scope="skipped")
        if args.run_compilation_check:
            try:
                print(f"  [{row.index}] Running compilation checks...", flush=True)
                compilation_results = run_compilation_checks(
                    worktree=worktree,
                    changed_files=changed_files,
                    timeout_per_check=args.compilation_timeout_seconds,
                )
            except Exception as exc:
                print(f"  [{row.index}] Compilation checks FAILED: {exc}", flush=True)
                compilation_results = CompilationResults(
                    checks={"cargo_check_affected": CommandResult(
                        returncode=-1,
                        stdout="",
                        stderr=f"Compilation check failed with exception: {exc}",
                        elapsed_seconds=0.0,
                    )},
                    scope="error",
                )

        review = None
        try:
            reviewer_model = args.reviewer_model or args.model
            if not reviewer_model:
                raise BenchmarkError("No reviewer model specified and no fallback --model provided")
            print(f"  [{row.index}] Running PR reviewer...", flush=True)
            review = run_pr_reviewer(
                worktree=worktree,
                row=row,
                agent_diff=agent_diff,
                compilation_results=compilation_results,
                opencode_bin=args.opencode_bin,
                reviewer_model=reviewer_model,
                timeout=args.reviewer_timeout_seconds,
                skip_permissions=not args.no_skip_permissions,
            )
            write_json(out_dir / "review.json", review)
        except Exception as exc:
            print(f"  [{row.index}] PR reviewer FAILED: {exc}", flush=True)
            review = {
                "parse_error": True,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "executive_summary": f"Reviewer failed with error: {exc}",
            }
            write_json(out_dir / "review.json", review)

        audit = None
        try:
            auditor_model = args.auditor_model or reviewer_model
            if review is not None:
                print(f"  [{row.index}] Running compilation auditor...", flush=True)
                audit = run_review_auditor(
                    worktree=worktree,
                    row=row,
                    agent_diff=agent_diff,
                    review=review,
                    compilation_results=compilation_results,
                    opencode_bin=args.opencode_bin,
                    auditor_model=auditor_model,
                    timeout=args.auditor_timeout_seconds,
                    skip_permissions=not args.no_skip_permissions,
                )
                if not audit.get("parse_error") and audit.get("verdict") == "warn":
                    issues = audit.get("issues_found", [])
                    if issues:
                        corrections = "\n".join(
                            f"- [{i.get('severity', 'unknown').upper()}] {i.get('description', '')}\n  Recommendation: {i.get('recommendation', '')}"
                            for i in issues
                        )
                        corrected = audit.get("corrected_assessment", "")
                        if corrected:
                            corrections = f"CORRECTED COMPILATION ASSESSMENT: {corrected}\n\n" + corrections
                        review["_auditor_corrections"] = corrections
                        print(f"  [{row.index}] AUDITOR WARNING: {len(issues)} issue(s) found in review", flush=True)
                write_json(out_dir / "audit.json", audit)
            else:
                print(f"  [{row.index}] Skipping auditor: no review available", flush=True)
        except Exception as exc:
            print(f"  [{row.index}] Compilation auditor FAILED: {exc}", flush=True)
            audit = {
                "parse_error": True,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "verdict": "pass",
                "notes": "Auditor failed to run, so no corrections applied.",
            }
            write_json(out_dir / "audit.json", audit)

        judges: list[dict[str, Any]] = []
        try:
            judge_model = args.judge_model or reviewer_model
            judge_count = args.judge_count if args.judge_count > 0 else 1
            if judge_model:
                compilation_summary = format_compilation_results(compilation_results)
                review_for_judges = review or {}

                with ThreadPoolExecutor(max_workers=judge_count) as pool:
                    futures = {}
                    for judge_id in range(judge_count):
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
                            judge_model=judge_model,
                            timeout=args.judge_timeout_seconds,
                            skip_permissions=not args.no_skip_permissions,
                        )
                        futures[future] = judge_id

                    for future in as_completed(futures):
                        judge_id = futures[future]
                        try:
                            judge_result = future.result()
                            judges.append(judge_result)
                            print(f"  [{row.index}] Judge {judge_id + 1}/{judge_count} completed", flush=True)
                        except Exception as exc:
                            error_judge = {
                                "judge_id": judge_id,
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                            }
                            judges.append(error_judge)
                            print(f"  [{row.index}] Judge {judge_id + 1}/{judge_count} FAILED: {exc}", flush=True)

                write_json(out_dir / "judges.json", judges)
            else:
                print(f"  [{row.index}] Skipping judges: no judge model specified", flush=True)
        except Exception as exc:
            print(f"  [{row.index}] Judge execution FAILED: {exc}", flush=True)
            judges.append({
                "error": str(exc),
                "error_type": type(exc).__name__,
            })

        result.update(
            {
                "status": "ok",
                "agent_returncode": last_agent_proc.returncode,
                "agent_elapsed_seconds": round(last_agent_proc.elapsed_seconds, 2),
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
            "results_dir": str(out_dir),
            "agent_returncode": last_agent_proc.returncode,
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
            with _WRITE_LOCK:
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
            _unregister_worktree(worktree)

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
    if args.run_compilation_check and not shutil.which("cargo"):
        raise BenchmarkError("'cargo' not found on PATH but --run-compilation-check was requested. Ensure rustup/cargo is installed and on PATH.")
    if args.timeout_seconds <= 0:
        raise BenchmarkError("--timeout-seconds must be positive")
    if args.reviewer_model and args.reviewer_timeout_seconds <= 0:
        raise BenchmarkError("--reviewer-timeout-seconds must be positive")
    if args.auditor_model and args.auditor_timeout_seconds <= 0:
        raise BenchmarkError("--auditor-timeout-seconds must be positive")
    if args.judge_model and args.judge_timeout_seconds <= 0:
        raise BenchmarkError("--judge-timeout-seconds must be positive")
    if args.run_compilation_check and args.compilation_timeout_seconds <= 0:
        raise BenchmarkError("--compilation-timeout-seconds must be positive")
    if args.parallel_workers <= 0:
        raise BenchmarkError("--parallel-workers must be >= 1")


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
    parser.add_argument("--judge-timeout-seconds", type=int, default=900)
    parser.add_argument("--judge-count", type=int, default=1, help="Number of independent judges to run per task")
    parser.add_argument("--reviewer-model", default=None, help="Optional opencode model string for PR reviewer")
    parser.add_argument("--reviewer-timeout-seconds", type=int, default=1200)
    parser.add_argument("--auditor-model", default=None, help="Optional opencode model string for review auditor (validates compilation assessment)")
    parser.add_argument("--auditor-timeout-seconds", type=int, default=600, help="Timeout for compilation auditor (default: 600s = 10 min)")
    parser.add_argument("--run-compilation-check", action="store_true", help="Run cargo check on the agent's worktree before judging")
    parser.add_argument("--compilation-timeout-seconds", type=int, default=900)
    parser.add_argument("--judges-worktrees-dir", type=Path, default=None, help="Directory for judge worktrees (defaults to worktrees-dir)")
    parser.add_argument("--runs-dir", type=Path, default=Path(__file__).parent / "runs", help="Directory for run JSONL records")
    parser.add_argument("--run-id", type=str, default=None, help="Unique run ID (auto-generated if not provided)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--start-index", type=int, default=None)
    parser.add_argument("--row-index", action="append", help="Run only this original JSONL row index. May be repeated.")
    parser.add_argument("--pr-number", action="append", help="Run only this PR number. May be repeated.")
    parser.add_argument("--keep-existing-worktrees", action="store_true", help="Reuse/reset existing worktree dirs instead of deleting first")
    parser.add_argument("--no-skip-permissions", action="store_true", help="Do not pass --dangerously-skip-permissions to opencode")
    parser.add_argument("--resume", action="store_true", help="Skip rows that completed successfully (result.json with status=ok) and continue with remaining rows")
    parser.add_argument("--env", action="append", help="Extra environment variable for opencode, KEY=VALUE. May be repeated.")
    parser.add_argument("--parallel-workers", type=int, default=1, help="Number of tasks to process concurrently (default: 1)")
    args = parser.parse_args()

    if args.run_id is None:
        args.run_id = uuid.uuid4().hex[:12]
    if args.judges_worktrees_dir is None:
        args.judges_worktrees_dir = args.worktrees_dir

    try:
        validate_args(args)
        rows = select_rows(load_rows(args.dataset), args)
        
        if args.resume:
            completed = _find_completed_row_indices(args.results_dir)
            if completed:
                rows = [row for row in rows if row.index not in completed]
                print(f"[resume] Found {len(completed)} completed rows. Re-running {len(rows)} remaining row(s).", flush=True)
        
        args.results_dir.mkdir(parents=True, exist_ok=True)
        if not rows:
            raise BenchmarkError("No rows selected")

        summary_path = args.results_dir / "summary.jsonl"
        aggregate: list[dict[str, Any]] = []
        with summary_path.open("a", encoding="utf-8") as summary_f:
            completed_count: int = 0
            total_rows: int = len(rows)

            def _log_result(result: dict[str, Any]) -> None:
                nonlocal completed_count
                completed_count += 1
                status = result.get("status")
                judge_agg = result.get("judge_aggregate", {})
                judge = judge_agg.get("final_score", {}).get("overall")
                with _PRINT_LOCK:
                    print(
                        f"[{completed_count}/{total_rows}] row={result.get('row_index')} "
                        f"pr={result.get('pr_number')} base={result.get('base_sha', '')[:12]} "
                        f"status={status} patch={result.get('has_patch')} "
                        f"file_f1={result.get('file_f1')} style={result.get('style_score')} judge={judge}",
                        flush=True,
                    )

            if args.parallel_workers <= 1:
                for pos, row in enumerate(rows, start=1):
                    with _PRINT_LOCK:
                        print(
                            f"[{pos}/{total_rows}] row={row.index} pr={row.pr_number} base={row.base_sha[:12]}",
                            flush=True,
                        )
                    result = run_one(row, args)
                    aggregate.append(result)
                    summary_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                    summary_f.flush()
                    _log_result(result)
            else:
                cancelled = []
                to_submit = []
                for row in rows:
                    if _SHUTDOWN_REQUESTED.is_set():
                        cancelled.append(row)
                    else:
                        to_submit.append(row)

                with ThreadPoolExecutor(max_workers=args.parallel_workers) as pool:
                    futures = {pool.submit(run_one, row, args): row for row in to_submit}
                    for future in as_completed(futures):
                        row = futures[future]
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = {
                                "row_index": row.index,
                                "repo": row.repo,
                                "pr_number": row.pr_number,
                                "base_sha": row.base_sha,
                                "head_sha": row.head_sha,
                                "model": args.model,
                                "status": "error",
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                "output_dir": str(
                                    args.results_dir
                                    / f"row_{row.index:04d}_pr_{safe_slug(row.pr_number, str(row.index))}"
                                ),
                            }
                            with _PRINT_LOCK:
                                print(f"[ERROR] row={row.index}: {exc}", flush=True)

                        aggregate.append(result)
                        with _WRITE_LOCK:
                            summary_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                            summary_f.flush()
                        _log_result(result)

                for row in cancelled:
                    result = {
                        "row_index": row.index,
                        "repo": row.repo,
                        "pr_number": row.pr_number,
                        "base_sha": row.base_sha,
                        "head_sha": row.head_sha,
                        "model": args.model,
                        "status": "cancelled",
                        "error": "Shutdown requested before task started",
                        "output_dir": str(
                            args.results_dir
                            / f"row_{row.index:04d}_pr_{safe_slug(row.pr_number, str(row.index))}"
                        ),
                    }
                    aggregate.append(result)
                    with _WRITE_LOCK:
                        summary_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                        summary_f.flush()
                    _log_result(result)

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
            "mean_judge_overall": (
                round(
                    sum(
                        float(r.get("judge_aggregate", {}).get("final_score", {}).get("overall") or 0)
                        for r in ok
                    )
                    / sum(
                        1
                        for r in ok
                        if r.get("judge_aggregate", {}).get("final_score", {}).get("overall") is not None
                    ),
                    3,
                )
                if any(
                    r.get("judge_aggregate", {}).get("final_score", {}).get("overall") is not None
                    for r in ok
                )
                else None
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
