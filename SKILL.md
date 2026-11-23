---
name: pr-test-receipt
description: Pre-PR verification and paste-ready PR Test Receipt generator for local Git checkouts. Use when a developer asks "am I ready to open a PR?", "pre-PR check", "verify my branch before pushing", "write the PR testing section", or wants evidence-backed test proof. Compares branch vs base, runs targeted local checks, separates dirty working-tree runs from committed HEAD (using a safe isolated temp worktree when feasible), catches 0-test false passes and mid-run tree changes, and outputs a copy-paste GitHub/GitLab Markdown receipt.
summary: Stop shipping "works on my machine" PRs—run targeted local checks and generate a paste-ready PR test receipt that catches uncommitted fixes, 0-test false passes, and CI gaps before you push.
version: "1.1"
tags:
  - pull-requests
  - code-review
  - github
  - git
  - testing
  - ci
  - preflight
  - verification
use_cases:
  - Run a pre-PR check before pushing or opening a pull request
  - Generate a paste-ready Markdown Testing section backed by fresh local runs
  - Catch uncommitted fixes that make local tests pass while committed HEAD fails
  - Detect 0-test false passes, skipped cases, stale bases, and unverified CI gates
---

# PR Test Receipt

Use this skill when a developer asks whether current changes are ready for a pull request, what still needs testing, or for a PR `Testing` section backed by fresh evidence.

## Safety & Integrity Rules

- **Non-destructive by default:** Keep tracked files, the Git index, branches, and remotes intact. Never stage, commit, stash, reset, push, or post to a remote PR unless the user explicitly asks.
- **Inspect before executing:** Read command/script definitions (`package.json`, `Makefile`, CI workflows, etc.) before running them. Leave deployment, publication, destructive DB resets, and checks requiring unavailable credentials or external infrastructure to the user.
- **Fresh, session-scoped evidence only:** Report only checks run in this session. Summarize failure output without reproducing credentials, tokens, or private `.env` values.

## Step 1: Capture the Pre-Check Snapshot

Run the bundled read-only snapshot script from this skill's directory (replacing `<skill_dir>` with the installed path of this skill):

```bash
python3 "<skill_dir>/scripts/preflight_snapshot.py" --repo . --format json
```

Pass `--base <ref>` if the user specifies a target base branch. If `python3` is unavailable, collect the equivalent state with read-only `git` commands (`rev-parse`, `symbolic-ref`, `status --porcelain=v1`, `merge-base`, `diff --name-status`, `diff --check`).

Record from the snapshot:
- `head.branch` (or detached HEAD), full `head.sha`, and `working_tree.state_hash`.
- `base.status`, `base.resolved_ref`, `base.resolved_short`, `base.merge_base_short`, `base.commits_behind`, and whether the base is a local remote-tracking snapshot.
- If `base.status` is `missing`, `ambiguous`, or `uncertain`: show the candidate or missing ref, still run checks that can run independently, and mark PR scope unverified.
- `working_tree` (`staged`, `unstaged`, `untracked`) and `risk_signals` (`dirty_tree`, `overlap_files`, `dirty_test_files`, `has_committed_whitespace_issues`).
- `repo_context` (`ci_files`, `pr_templates`, `instruction_files`, `task_files`).

## Step 2: Inspect the Diff & Select Checks

1. Inspect the committed diff from `merge_base` to `HEAD` (`git diff <merge_base>..HEAD`) and understand the affected behavior. Track staged, unstaged, and untracked work separately.
   - Check the committed diff for concrete correctness bugs, changed input boundaries, missing regression coverage, and whitespace/conflict-marker issues (`git diff --check`). Follow changed behavior into callers and tests where useful, citing `file:line` evidence.
   - Treat a test gap as uncertainty unless repository policy or the changed behavior makes it a concrete blocker.
2. Read applicable files found in `repo_context` (`AGENTS.md`, `CONTRIBUTING.md`, CI workflows, task definitions, and PR templates).
3. Select the smallest local checks that exercise the changed behavior, and identify broader required CI gates separately. Run focused checks plus locally runnable required gates when practical. If targeted checks could miss shared code or a repo-wide contract, run the appropriate broader check.

## Step 3: Run Checks & Separate Dirty Tree vs. Committed HEAD

- For every run, capture the exact command, working directory when relevant, exit code, duration, and observed counts/result.
- **Guard against false passes:** Treat a command that exited `0` while matching `0` tests or skipping the relevant test case as **INCOMPLETE**, not `PASS`. Record skipped or blocked checks with the concrete reason.
- **When `working_tree.clean` is `false`:**
  - If the user explicitly asks to verify uncommitted work for a future commit, label the check clearly as a **working-tree check**.
  - Otherwise, local checks in the main checkout cover the dirty working tree and **cannot** prove committed `HEAD` passes. When `risk_signals.overlap_files` or `risk_signals.dirty_test_files` is non-empty (or the dirty edits may affect the check), also verify the key focused check against committed `HEAD` using the bundled isolated temporary worktree runner:

```bash
python3 "<skill_dir>/scripts/verify_head_worktree.py" --repo . --rev HEAD -- <exact command>
```

  - `verify_head_worktree.py` checks out committed `HEAD` into a temp directory with Git hooks disabled, redacts common secrets, detects `0`-test runs and missing gitignored dependencies (`blocked_missing_deps`), and always removes the temporary worktree on exit without touching the main checkout.
  - **Honest fallback:** If `result_status` is `blocked_missing_deps` (for example, `node_modules` or `.venv` is gitignored and absent in the isolated worktree), do **not** report a test failure for `HEAD`—report committed `HEAD` as `NOT RUN` with the exact missing-dependency reason and recommend committing or stashing to verify cleanly.

## Step 4: Post-Check State Verification & Receipt Output

1. Re-run `preflight_snapshot.py` (or recheck `HEAD` and `git status --porcelain`) after running checks. Compare `head.sha` and `working_tree.state_hash` before vs. after. If a check, formatter, or code generator mutated the working tree or `HEAD` mid-run, qualify the evidence and rerun affected checks when the intended snapshot is clear.
2. Choose one verdict:
   - **Ready for review** — intended PR scope is verified, needed local checks passed on that exact snapshot, and no concrete blockers exist.
   - **Changes needed** — a concrete blocker or a required check failure is established for the intended scope (including when committed `HEAD` fails while the dirty working tree passes).
   - **Incomplete verification** — base or tested snapshot is uncertain, a check matched `0` tests / skipped the target case, or required verification could not run. If a check failed on a different snapshot, report that failure against the snapshot actually tested.
3. Format the response using [references/receipt-template.md](references/receipt-template.md):
   - **Part 1 (Developer Preflight Summary):** Verdict + reason, concrete blockers (`file:line`, impact, smallest next step), and any scope/CI gaps. Keep clean runs short.
   - **Part 2 (Paste-Ready PR Test Receipt):** A single fenced ` ```markdown ` code block ready to copy straight into GitHub or GitLab (using the Clean, Dirty-Tree Split, or Incomplete template as appropriate). If a repository PR template exists in `repo_context.pr_templates`, match its Testing/Verification section and check only boxes verified in this session.
   - Respond in the user's language.
