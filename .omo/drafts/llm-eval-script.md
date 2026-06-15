# Draft: LLM Question Answering & Grading Script

## Requirements (Confirmed)
- **Goal**: Create a script that evaluates an LLM agent (opencode) by:
  1. Feeding questions from a file to the agent
  2. Getting the agent's answers
  3. Grading answers against a "gold standard" using the same agent as a judge
- **Current Discussion Focus**: Step 1 — How to get the LLM to answer questions

## Open Questions
- **LLM Access Constraint**: The LLM/agent MUST have full repo context (like an interactive OpenCode session), not just a stateless API call. This is the critical design challenge.
- **Invocation Method**: CLI-based (`opencode run`), terminal-startable
- **Execution Mode**: Interactive/manual validation first, batch unattended later

## Technical Findings

### Repository Context
- **Project**: Hyperswitch — composable open-source payments infrastructure (Rust, 40+ crates)
- **No existing opencode config** in repo
- **Build tools**: justfile, Makefile, Cargo workspace

### Opencode Invocation Options Discovered
1. **CLI `opencode run`**: Stateless, non-interactive. Supports `--agent`, `-m model`, `--format json`, `-f file` attachments, `--session` for persistence.
2. **`opencode serve` + `opencode run --attach`**: Persistent server mode. Eliminates cold-start but requires managing a daemon.
3. **TypeScript SDK (`@opencode-ai/sdk`)**: Programmatic, can create sessions and send prompts. More integration work.
4. **Sessions**: `opencode run --session ses_xxx` can continue a previous session that might have repo context loaded.

### Critical Uncertainty
Does `opencode run` from within a git repo **automatically index/load the codebase** into the agent's context, or does it require explicit file attachments (`-f`)? This determines the entire architecture.

## Open Questions
- [x] What is the format of the input file? → **JSONL with question, gold standard answer, and metadata**
- [x] What scripting language to use? → **To be decided**
- [x] How to invoke the opencode agent programmatically? → **CLI (`opencode run`) confirmed viable**
- [x] What domain/knowledge do the questions test? → **About THIS codebase (Hyperswitch)**
- [x] Should the script be interactive or batch/automated? → **Interactive first, batch later**
## File Inventory (Discovered)
- `llm-judge-test/run1.full.jsonl` — 25+ benchmark tasks (real Hyperswitch PRs)
- `llm-judge-test/run_opencode_benchmark.py` — Complete benchmark orchestration script
- `llm-judge-test/filter_benchmark_tasks.py` — Dataset cleaning/filtering preprocessor

## JSONL Schema
```json
{
  "task": "natural language coding task description",
  "gold_patch": "git diff patch (the expected fix)",
  "metadata": {
    "repo": "juspay/hyperswitch",
    "pr_number": 123,
    "base_sha": "abc123...",
    "head_sha": "def456...",
    "quality_flags": [{"code": "..."}]
  }
}
```

## Repo Context Problem — SOLVED
The benchmark script elegantly solves the "agent needs full repo context" problem:
1. Creates a **git worktree** at the historical `base_sha` commit (isolated from main repo)
2. Runs `opencode run --dir <worktree> --format json` from INSIDE the worktree
3. Agent operates directly on checked-out code — can search, read, edit files like a human dev
4. Captures result via `git diff HEAD` from the worktree

This means the agent has FULL access to the entire codebase at the correct historical state.

## Architecture Alignment
| Our Discussion | Script Implementation |
|----------------|----------------------|
| Read questions from file | JSONL loader with validation (`load_rows`) |
| Feed to LLM with repo context | `opencode run --dir <worktree>` in isolated git worktree |
| Agent generates answers | Agent edits files in worktree, diff captured |
| Grade against gold standard | Triple-layer: diff F1 + style rules + LLM judge |
| 2-step process | Agent step → deterministic scoring → optional judge step |
| CLI invocation | Full argparse CLI with filtering/selective execution |

## Technical Decisions
- **Scripting language**: Python 3 (already implemented)
- **Agent invocation**: `opencode run --dir <worktree> --format json --model <model>`
- **Repo isolation**: Git worktrees per task at historical `base_sha`
- **Deterministic scoring**: File/line precision-recall-F1 + Rust style rule checker
- **LLM judge**: Structured rubric (correctness, completeness, conventions, hygiene)

## How It Achieves Full Repo Context (Key Design Decision)

Your concern was that the agent needs access to the **entire codebase** like a human developer. The benchmark script solves this elegantly:

**Architecture:**
1. **Isolated Worktrees**: For each task, creates a git worktree at the historical `base_sha` commit
2. **Agent Runs In-Repo**: `opencode run --dir <worktree>` launches the agent directly inside the checked-out code
3. **Agent Has Full Access**: The agent can search, read, and edit files just like you do in an interactive session
4. **Patch Capture**: After the agent finishes, `git diff HEAD` captures all changes

This is much better than trying to feed file contents via prompts — the agent operates on real files.

## Scoring System (3 Layers)

1. **Deterministic Diff Comparison** (`compare_diffs()`):
   - File-level precision/recall/F1
   - Added/removed line precision/recall/F1
   - Shows exactly which files were missed or added unexpectedly

2. **Style Rule Checker** (`style_check()`):
   - Detects `.unwrap()`, `.expect()`, `panic!`, `todo!`, `unsafe`, `dbg!`, `println!`
   - Penalizes hard violations (-2.5 pts each) and soft violations (-0.5 pts each)
   - Checks for backup/temp/summary files

3. **LLM Judge** (`run_llm_judge()`):
   - Uses a separate model with a structured rubric
   - Scores: correctness (35%), completeness (30%), convention adherence (20%), hygiene (15%)
   - Returns JSON with detailed reasoning, bug reports, missing requirements, convention violations

## Filtering/Curation (`filter_benchmark_tasks.py`)

Before running, you can clean the dataset:
- Exclude rows with certain quality flags (e.g., `invalid_draft`, `many_changed_files`)
- Limit max changed files, diff size, task length
- Filter out noisy paths (lockfiles, snapshots, generated code, migrations)
- Limit max added/removed lines
- Control implementation leakage scores

This lets you create a "clean" subset for focused benchmarking.
