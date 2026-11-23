#!/usr/bin/env python3
"""Read-only Git and repository preflight snapshot for PR Test Receipt.

Captures deterministic branch, base, merge-base, committed diff, working-tree
state, risk signals (including overlap between committed PR changes and
uncommitted local edits), and repository check metadata without modifying
tracked files, the Git index, branches, or remotes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_BRANCH_CANDIDATES = ("main", "master", "develop", "trunk")
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|specs?)(/|$)|\.(test|spec)\.[^/]+$|_test\.[^/]+$|^test_[^/]+\.py$",
    re.IGNORECASE,
)
MAX_UNTRACKED_HASH_BYTES = 1_048_576  # 1 MiB
MAX_CHECK_LINES = 20


def run_git(
    repo: Path,
    args: list[str],
    *,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    cmd = ["git", "-C", str(repo), *args]
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        check=False,
    )


def git_rev_parse(repo: Path, rev: str) -> str | None:
    proc = run_git(repo, ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"])
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def git_ref_exists(repo: Path, full_ref: str) -> bool:
    proc = run_git(repo, ["show-ref", "--verify", "--quiet", full_ref])
    return proc.returncode == 0


def parse_porcelain_z(raw: bytes) -> dict[str, list[dict[str, str]] | list[str]]:
    staged: list[dict[str, str]] = []
    unstaged: list[dict[str, str]] = []
    untracked: list[str] = []

    tokens = raw.split(b"\x00")
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if not entry:
            continue
        if len(entry) < 3:
            continue
        xy = entry[:2].decode("utf-8", errors="replace")
        path = entry[3:].decode("utf-8", errors="replace")
        orig_path: str | None = None
        if xy[0] in ("R", "C") or xy[1] in ("R", "C"):
            if i < len(tokens) and tokens[i]:
                orig_path = tokens[i].decode("utf-8", errors="replace")
                i += 1

        if xy == "??":
            untracked.append(path)
            continue

        x, y = xy[0], xy[1]
        if x not in (" ", "?"):
            item: dict[str, str] = {"status": x, "path": path}
            if orig_path:
                item["orig_path"] = orig_path
            staged.append(item)
        if y not in (" ", "?"):
            item = {"status": y, "path": path}
            if orig_path:
                item["orig_path"] = orig_path
            unstaged.append(item)

    return {
        "staged": staged,
        "unstaged": unstaged,
        "untracked": sorted(untracked),
    }


def parse_name_status_z(raw: bytes) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    tokens = [t for t in raw.split(b"\x00") if t]
    i = 0
    while i < len(tokens):
        status = tokens[i].decode("utf-8", errors="replace")
        i += 1
        if status.startswith(("R", "C")):
            if i + 1 < len(tokens):
                old_path = tokens[i].decode("utf-8", errors="replace")
                new_path = tokens[i + 1].decode("utf-8", errors="replace")
                files.append(
                    {"status": status, "path": new_path, "orig_path": old_path}
                )
                i += 2
        else:
            if i < len(tokens):
                path = tokens[i].decode("utf-8", errors="replace")
                files.append({"status": status, "path": path})
                i += 1
    return files


def compute_state_hash(
    repo: Path,
    head_sha: str | None,
    porcelain_raw: bytes,
    untracked_paths: list[str],
) -> str:
    h = hashlib.sha256()
    h.update(b"HEAD:")
    h.update((head_sha or "UNBORN").encode("utf-8"))
    h.update(b"\x00STATUS:")
    h.update(porcelain_raw)

    diff_unstaged = run_git(repo, ["diff", "--no-ext-diff", "--binary"], text=False)
    h.update(b"\x00DIFF_UNSTAGED:")
    h.update(diff_unstaged.stdout)

    diff_staged = run_git(
        repo, ["diff", "--cached", "--no-ext-diff", "--binary"], text=False
    )
    h.update(b"\x00DIFF_STAGED:")
    h.update(diff_staged.stdout)

    h.update(b"\x00UNTRACKED:")
    for rel in sorted(untracked_paths):
        full = repo / rel
        h.update(rel.encode("utf-8", errors="replace"))
        h.update(b":")
        try:
            st = full.lstat()
            if full.is_symlink():
                h.update(b"SYMLINK:")
                h.update(os.readlink(full).encode("utf-8", errors="replace"))
            elif full.is_file() and st.st_size <= MAX_UNTRACKED_HASH_BYTES:
                h.update(b"FILE:")
                h.update(full.read_bytes())
            else:
                h.update(f"META:{st.st_mode}:{st.st_size}:{st.st_mtime_ns}".encode())
        except OSError as exc:
            h.update(f"ERR:{exc.__class__.__name__}".encode())
        h.update(b"\x00")

    return h.hexdigest()[:16]


def resolve_base(
    repo: Path,
    requested_base: str | None,
    current_branch: str | None,
    head_sha: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "requested": requested_base,
        "status": "uncertain",
        "resolved_ref": None,
        "resolved_sha": None,
        "resolved_short": None,
        "is_remote_tracking_snapshot": False,
        "merge_base_sha": None,
        "merge_base_short": None,
        "commits_ahead": None,
        "commits_behind": None,
        "candidates": [],
        "note": None,
    }

    def finalize_resolved(ref_name: str, is_remote: bool, note: str | None = None) -> dict[str, Any]:
        sha = git_rev_parse(repo, ref_name)
        if not sha:
            result["status"] = "missing"
            result["note"] = f"Ref '{ref_name}' did not resolve to a commit."
            return result
        result["status"] = "resolved"
        result["resolved_ref"] = ref_name
        result["resolved_sha"] = sha
        result["resolved_short"] = sha[:7]
        result["is_remote_tracking_snapshot"] = is_remote
        result["note"] = note
        if head_sha:
            mb = run_git(repo, ["merge-base", ref_name, head_sha])
            if mb.returncode == 0 and mb.stdout.strip():
                mb_sha = mb.stdout.strip()
                result["merge_base_sha"] = mb_sha
                result["merge_base_short"] = mb_sha[:7]
                counts = run_git(
                    repo, ["rev-list", "--left-right", "--count", f"{ref_name}...{head_sha}"]
                )
                if counts.returncode == 0:
                    parts = counts.stdout.strip().split()
                    if len(parts) == 2:
                        result["commits_behind"] = int(parts[0])
                        result["commits_ahead"] = int(parts[1])
            else:
                result["status"] = "no_common_ancestor"
                result["note"] = f"No merge-base found between '{ref_name}' and HEAD."
        return result

    if requested_base:
        is_remote = git_ref_exists(repo, f"refs/remotes/{requested_base}")
        if not git_rev_parse(repo, requested_base):
            result["status"] = "missing"
            result["note"] = f"Requested base ref '{requested_base}' does not exist locally."
            result["candidates"] = collect_candidate_bases(repo, current_branch)
            return result
        return finalize_resolved(
            requested_base,
            is_remote,
            "Local remote-tracking snapshot (may be behind remote)." if is_remote else None,
        )

    # 1. Remote symbolic HEAD (prefer origin, else unique remote symbolic HEAD)
    remotes_proc = run_git(repo, ["remote"])
    remotes = [r.strip() for r in remotes_proc.stdout.splitlines() if r.strip()]
    ordered_remotes = (
        ["origin"] + [r for r in remotes if r != "origin"]
        if "origin" in remotes
        else remotes
    )
    symbolic_hits: list[str] = []
    for remote in ordered_remotes:
        sym = run_git(repo, ["symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD"])
        if sym.returncode == 0:
            full = sym.stdout.strip()
            prefix = "refs/remotes/"
            short_ref = full[len(prefix) :] if full.startswith(prefix) else full
            if git_rev_parse(repo, short_ref):
                symbolic_hits.append(short_ref)
                if remote == "origin":
                    return finalize_resolved(
                        short_ref,
                        True,
                        "Resolved from local refs/remotes/origin/HEAD (local snapshot).",
                    )
    if len(symbolic_hits) == 1:
        return finalize_resolved(
            symbolic_hits[0],
            True,
            "Resolved from local remote symbolic HEAD (local snapshot).",
        )
    if len(symbolic_hits) > 1:
        result["status"] = "ambiguous"
        result["candidates"] = symbolic_hits
        result["note"] = "Multiple remotes define symbolic HEAD; pass --base to disambiguate."
        return result

    # 2. Remote-tracking default branches on origin (or single remote)
    if ordered_remotes:
        primary_remote = ordered_remotes[0]
        remote_defaults = [
            f"{primary_remote}/{name}"
            for name in DEFAULT_BRANCH_CANDIDATES
            if git_ref_exists(repo, f"refs/remotes/{primary_remote}/{name}")
        ]
        if len(remote_defaults) == 1:
            return finalize_resolved(
                remote_defaults[0],
                True,
                f"Resolved from local remote-tracking branch '{remote_defaults[0]}' (local snapshot).",
            )
        if len(remote_defaults) > 1:
            result["status"] = "ambiguous"
            result["candidates"] = remote_defaults
            result["note"] = "Multiple default-like remote branches exist locally; pass --base."
            return result

    # 3. Local default branches (excluding current branch unless there is no alternative)
    local_defaults = [
        name
        for name in DEFAULT_BRANCH_CANDIDATES
        if git_ref_exists(repo, f"refs/heads/{name}")
    ]
    non_current = [b for b in local_defaults if b != current_branch]
    if len(non_current) == 1:
        return finalize_resolved(
            non_current[0],
            False,
            f"Resolved from local branch '{non_current[0]}'.",
        )
    if len(non_current) > 1:
        result["status"] = "ambiguous"
        result["candidates"] = non_current
        result["note"] = "Multiple local default-like branches exist; pass --base."
        return result

    if current_branch and current_branch in local_defaults:
        result["status"] = "uncertain"
        result["candidates"] = local_defaults
        result["note"] = (
            f"Currently on '{current_branch}' with no remote-tracking base; pass --base to specify a target branch."
        )
        return result

    result["status"] = "uncertain"
    result["candidates"] = collect_candidate_bases(repo, current_branch)
    result["note"] = "Could not unambiguously determine a local base branch; pass --base."
    return result


def collect_candidate_bases(repo: Path, current_branch: str | None) -> list[str]:
    candidates: list[str] = []
    proc = run_git(repo, ["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes"])
    if proc.returncode != 0:
        return candidates
    for line in proc.stdout.splitlines():
        ref = line.strip()
        if not ref or ref.endswith("/HEAD") or ref == current_branch:
            continue
        leaf = ref.split("/")[-1]
        if leaf in DEFAULT_BRANCH_CANDIDATES:
            candidates.append(ref)
    return sorted(set(candidates))


def git_diff_check_lines(repo: Path, extra_args: list[str]) -> list[str]:
    proc = run_git(repo, ["diff", "--check", *extra_args])
    if proc.returncode == 0 and not proc.stdout.strip():
        return []
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return lines[:MAX_CHECK_LINES]


def detect_repo_context(repo: Path) -> dict[str, list[str]]:
    ci_files: list[str] = []
    pr_templates: list[str] = []
    instruction_files: list[str] = []
    task_files: list[str] = []

    workflows_dir = repo / ".github" / "workflows"
    if workflows_dir.is_dir():
        for p in sorted(workflows_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in (".yml", ".yaml"):
                ci_files.append(p.relative_to(repo).as_posix())

    for rel in (
        ".gitlab-ci.yml",
        ".circleci/config.yml",
        "azure-pipelines.yml",
        "Jenkinsfile",
        ".buildkite/pipeline.yml",
        ".buildkite/pipeline.yaml",
    ):
        if (repo / rel).is_file():
            ci_files.append(rel)

    for rel in (
        ".github/pull_request_template.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "pull_request_template.md",
        "PULL_REQUEST_TEMPLATE.md",
        "docs/pull_request_template.md",
    ):
        if (repo / rel).is_file():
            pr_templates.append(rel)
    pr_tpl_dir = repo / ".github" / "PULL_REQUEST_TEMPLATE"
    if pr_tpl_dir.is_dir():
        for p in sorted(pr_tpl_dir.glob("*.md")):
            if p.is_file():
                pr_templates.append(p.relative_to(repo).as_posix())

    for rel in (
        "AGENTS.md",
        "CLAUDE.md",
        "CONTRIBUTING.md",
        ".github/CONTRIBUTING.md",
    ):
        if (repo / rel).is_file():
            instruction_files.append(rel)

    for rel in (
        "package.json",
        "pnpm-workspace.yaml",
        "turbo.json",
        "nx.json",
        "pyproject.toml",
        "setup.cfg",
        "pytest.ini",
        "tox.ini",
        "Makefile",
        "GNUmakefile",
        "justfile",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    ):
        if (repo / rel).is_file():
            task_files.append(rel)

    return {
        "ci_files": sorted(set(ci_files)),
        "pr_templates": sorted(set(pr_templates)),
        "instruction_files": sorted(set(instruction_files)),
        "task_files": sorted(set(task_files)),
    }


def collect_snapshot(repo_path: Path, requested_base: str | None) -> dict[str, Any]:
    top = run_git(repo_path, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        return {
            "ok": False,
            "error": top.stderr.strip() or "Not a Git repository.",
        }
    repo = Path(top.stdout.strip())

    head_sha = git_rev_parse(repo, "HEAD")
    head_tree_proc = run_git(repo, ["rev-parse", "--verify", "--quiet", "HEAD^{tree}"])
    head_tree = head_tree_proc.stdout.strip() if head_tree_proc.returncode == 0 else None

    branch_proc = run_git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if branch_proc.returncode == 0:
        branch: str | None = branch_proc.stdout.strip()
        detached_head = False
    else:
        branch = None
        detached_head = head_sha is not None

    porcelain_proc = run_git(repo, ["status", "--porcelain=v1", "-z"], text=False)
    wt = parse_porcelain_z(porcelain_proc.stdout)
    is_clean = not (wt["staged"] or wt["unstaged"] or wt["untracked"])
    state_hash = compute_state_hash(repo, head_sha, porcelain_proc.stdout, wt["untracked"])

    base = resolve_base(repo, requested_base, branch, head_sha)

    committed_files: list[dict[str, str]] = []
    shortstat = ""
    committed_ws_issues: list[str] = []
    if base["status"] == "resolved" and base["merge_base_sha"] and head_sha:
        diff_range = f"{base['merge_base_sha']}..{head_sha}"
        ns = run_git(repo, ["diff", "--name-status", "-z", diff_range], text=False)
        committed_files = parse_name_status_z(ns.stdout)
        stat_proc = run_git(repo, ["diff", "--shortstat", diff_range])
        shortstat = stat_proc.stdout.strip()
        committed_ws_issues = git_diff_check_lines(repo, [diff_range])

    uncommitted_ws_issues = (
        git_diff_check_lines(repo, []) + git_diff_check_lines(repo, ["--cached"])
    )[:MAX_CHECK_LINES]

    committed_path_set: set[str] = set()
    for f in committed_files:
        committed_path_set.add(f["path"])
        if "orig_path" in f:
            committed_path_set.add(f["orig_path"])

    dirty_path_set: set[str] = set(wt["untracked"])
    for group in (wt["staged"], wt["unstaged"]):
        for item in group:
            dirty_path_set.add(item["path"])
            if "orig_path" in item:
                dirty_path_set.add(item["orig_path"])

    overlap_files = sorted(committed_path_set & dirty_path_set)
    dirty_test_files = sorted(p for p in dirty_path_set if TEST_PATH_RE.search(p))

    return {
        "ok": True,
        "repo_root": str(repo),
        "head": {
            "branch": branch,
            "detached_head": detached_head,
            "sha": head_sha,
            "short_sha": head_sha[:7] if head_sha else None,
            "tree_sha": head_tree,
            "unborn": head_sha is None,
        },
        "base": base,
        "working_tree": {
            "clean": is_clean,
            "state_hash": state_hash,
            "staged": wt["staged"],
            "unstaged": wt["unstaged"],
            "untracked": wt["untracked"],
        },
        "committed_diff": {
            "files": committed_files,
            "file_count": len(committed_files),
            "shortstat": shortstat,
            "whitespace_issues": committed_ws_issues,
        },
        "risk_signals": {
            "dirty_tree": not is_clean,
            "overlap_files": overlap_files,
            "dirty_test_files": dirty_test_files,
            "commits_behind_base": base["commits_behind"],
            "zero_committed_changes": (
                base["status"] == "resolved" and len(committed_files) == 0
            ),
            "has_committed_whitespace_issues": bool(committed_ws_issues),
            "uncommitted_whitespace_issues": uncommitted_ws_issues,
        },
        "repo_context": detect_repo_context(repo),
    }


def format_text(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        return f"ERROR: {data.get('error', 'Unknown error')}"

    head = data["head"]
    base = data["base"]
    wt = data["working_tree"]
    diff = data["committed_diff"]
    risks = data["risk_signals"]
    ctx = data["repo_context"]

    lines = [
        f"Repo: {data['repo_root']}",
        f"HEAD: {head['branch'] or 'DETACHED'} @ {head['short_sha'] or 'UNBORN'} (state_hash={wt['state_hash']})",
        (
            f"Base: {base['resolved_ref']} @ {base['resolved_short']} "
            f"(status={base['status']}, merge_base={base['merge_base_short']}, "
            f"ahead={base['commits_ahead']}, behind={base['commits_behind']}, "
            f"remote_snapshot={base['is_remote_tracking_snapshot']})"
            if base["status"] == "resolved"
            else f"Base: {base['status']} ({base['note']})"
        ),
        f"Working tree: {'CLEAN' if wt['clean'] else 'DIRTY'} "
        f"(staged={len(wt['staged'])}, unstaged={len(wt['unstaged'])}, untracked={len(wt['untracked'])})",
        f"Committed diff: {diff['file_count']} file(s) {('— ' + diff['shortstat']) if diff['shortstat'] else ''}".rstrip(),
    ]
    if risks["overlap_files"]:
        lines.append(
            f"RISK — Overlap between committed diff and dirty tree: {', '.join(risks['overlap_files'])}"
        )
    if risks["dirty_test_files"]:
        lines.append(f"RISK — Dirty test files: {', '.join(risks['dirty_test_files'])}")
    if risks["has_committed_whitespace_issues"]:
        lines.append("RISK — Committed diff has whitespace/conflict-marker issues")
    if ctx["ci_files"]:
        lines.append(f"CI files: {', '.join(ctx['ci_files'])}")
    if ctx["pr_templates"]:
        lines.append(f"PR templates: {', '.join(ctx['pr_templates'])}")
    if ctx["task_files"]:
        lines.append(f"Task files: {', '.join(ctx['task_files'])}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Git preflight snapshot for PR Test Receipt."
    )
    parser.add_argument("--repo", default=".", help="Path inside the Git repository.")
    parser.add_argument("--base", default=None, help="Explicit base ref to compare against.")
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format (default: json).",
    )
    args = parser.parse_args()

    data = collect_snapshot(Path(args.repo).resolve(), args.base)
    if args.format == "text":
        print(format_text(data))
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0 if data.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
