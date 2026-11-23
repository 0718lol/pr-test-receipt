#!/usr/bin/env python3
"""Run a repository check against committed HEAD in an isolated temp worktree.

Never modifies tracked files, the Git index, branches, or remotes in the main
checkout. Creates a detached worktree under the OS temp directory with hooks
disabled, executes the specified check command with a timeout, redacts obvious
secrets from captured output, and removes the temporary worktree on exit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,})\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})\b"), "[REDACTED_SLACK_TOKEN]"),
    (
        re.compile(
            r"(?i)\b((?:api|access|auth|client|private|secret|db|database)?[_-]?(?:token|secret|password|passwd|api[_-]?key))\s*([:=])\s*(['\"]?)([^\s'\"`,;]{6,})\3"
        ),
        r"\1\2\3[REDACTED]\3",
    ),
]

MISSING_DEPS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"Cannot find package ['\"][^./]|Cannot find module ['\"][^./<>]", re.IGNORECASE),
        "Missing Node/JS dependency in isolated worktree (likely gitignored node_modules)",
    ),
    (
        re.compile(r"node_modules/.*ENOENT|sh:\s+.*:\s+command not found|:\s+command not found\s*$", re.IGNORECASE | re.MULTILINE),
        "Missing local CLI binary or node_modules in isolated worktree",
    ),
    (
        re.compile(r"No module named ['\"](?:pytest|unittest2|flask|django|fastapi|pydantic|numpy|pandas|requests|httpx|sqlalchemy)['\"]", re.IGNORECASE),
        "Missing Python third-party dependency in isolated worktree",
    ),
    (
        re.compile(r"could not find `Cargo\.lock`|missing go\.sum entry", re.IGNORECASE),
        "Missing build/module dependency in isolated worktree",
    ),
]

ASSERTION_FAILURE_RE = re.compile(
    r"AssertionError|failing tests:|FAIL\s+\S+|FAILED\s+\S+|test result:\s+FAILED|--- FAIL:|Expected values to be|Assertion failed",
    re.IGNORECASE,
)

ZERO_TESTS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"collected\s+0\s+items", re.IGNORECASE),
    re.compile(r"no\s+tests?\s+(?:ran|found|collected|matched)", re.IGNORECASE),
    re.compile(r"\[no test files\]", re.IGNORECASE),
    re.compile(r"\b0\s+specs?,\s+0\s+failures?\b", re.IGNORECASE),
    re.compile(r"\bran\s+0\s+tests?\b", re.IGNORECASE),
    re.compile(r"(?m)^(?:ℹ\s*)?tests\s+0\s*$"),
    re.compile(r"test result:\s+ok\.\s+0\s+passed;\s+0\s+failed", re.IGNORECASE),
    re.compile(r"Test Files\s+0\b|Tests\s+0\b"),
]

MAX_EXCERPT_LINES = 60
MAX_EXCERPT_CHARS = 4000


def run_git(repo: Path, args: list[str], *, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        check=False,
    )


def redact_secrets(text: str) -> str:
    out = text
    for pattern, repl in SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def normalize_worktree_paths(text: str, worktree_paths: list[str]) -> str:
    out = text
    for p in sorted(set(worktree_paths), key=len, reverse=True):
        if p:
            out = out.replace(p, "<worktree>")
    return out


def trim_excerpt(text: str, worktree_paths: list[str] | None = None) -> str:
    cleaned = redact_secrets(text)
    if worktree_paths:
        cleaned = normalize_worktree_paths(cleaned, worktree_paths)
    redacted = cleaned.strip()
    if not redacted:
        return ""
    lines = redacted.splitlines()
    if len(lines) > MAX_EXCERPT_LINES:
        kept = lines[-MAX_EXCERPT_LINES:]
        redacted = f"... ({len(lines) - MAX_EXCERPT_LINES} earlier lines omitted) ...\n" + "\n".join(kept)
    if len(redacted) > MAX_EXCERPT_CHARS:
        redacted = "... (earlier output truncated) ...\n" + redacted[-MAX_EXCERPT_CHARS:]
    return redacted


def main_repo_fingerprint(repo: Path) -> tuple[str | None, str]:
    head = run_git(repo, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
    head_sha = head.stdout.strip() if head.returncode == 0 else None
    st = run_git(repo, ["status", "--porcelain=v1", "-z"], text=False)
    h = hashlib.sha256()
    h.update((head_sha or "UNBORN").encode("utf-8"))
    h.update(b"\x00")
    h.update(st.stdout)
    return head_sha, h.hexdigest()[:16]


def detect_missing_deps(exit_code: int, combined: str) -> tuple[bool, str | None]:
    if exit_code == 0:
        return False, None
    if ASSERTION_FAILURE_RE.search(combined):
        return False, None
    if exit_code == 127:
        return True, "Command exited 127 (binary not found in isolated worktree PATH)."
    for pat, hint in MISSING_DEPS_PATTERNS:
        if pat.search(combined):
            return True, hint
    return False, None


def detect_zero_tests(exit_code: int, combined: str) -> bool:
    # Exit 0 catches silent false-pass runners (Go, Cargo, Jest --passWithNoTests,
    # Python <3.12 unittest); exit 5 is standard "no tests ran" in pytest and
    # Python 3.12+ unittest.
    if exit_code not in (0, 5):
        return False
    if ASSERTION_FAILURE_RE.search(combined):
        return False
    for pat in ZERO_TESTS_PATTERNS:
        if pat.search(combined):
            return True
    # Node test runner: explicit "pass 0" and "fail 0"
    if re.search(r"(?m)^(?:ℹ\s*)?pass\s+0\s*$", combined) and re.search(
        r"(?m)^(?:ℹ\s*)?fail\s+0\s*$", combined
    ):
        return True
    return False


def extract_observed_summary(combined: str, exit_code: int) -> str:
    clean = redact_secrets(combined)
    # Node built-in test runner summary
    node_tests = re.search(r"(?m)^(?:ℹ\s*)?tests\s+(\d+)\s*$", clean)
    node_pass = re.search(r"(?m)^(?:ℹ\s*)?pass\s+(\d+)\s*$", clean)
    node_fail = re.search(r"(?m)^(?:ℹ\s*)?fail\s+(\d+)\s*$", clean)
    node_skip = re.search(r"(?m)^(?:ℹ\s*)?skipped\s+(\d+)\s*$", clean)
    if node_tests and node_pass and node_fail:
        skip_part = f", {node_skip.group(1)} skipped" if node_skip and node_skip.group(1) != "0" else ""
        return f"tests {node_tests.group(1)} ({node_pass.group(1)} passed, {node_fail.group(1)} failed{skip_part})"

    # Pytest / Unittest / Cargo / Vitest / Jest / Go summary lines
    for pat in (
        re.compile(r"(?m)^=+\s*(.+?(?:passed|failed|skipped|error|no tests ran).*?)\s*=+$", re.IGNORECASE),
        re.compile(r"(?m)^(Ran\s+\d+\s+tests?\s+in\s+.+)$"),
        re.compile(r"(?m)^(test result:\s+.+)$"),
        re.compile(r"(?m)^(Tests:\s+.+)$"),
        re.compile(r"(?m)^((?:ok|FAIL)\s+\S+\s+.+)$"),
    ):
        matches = pat.findall(clean)
        if matches:
            return matches[-1].strip()

    non_empty = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    if non_empty:
        return non_empty[-1][:200]
    return f"exit code {exit_code} (no output)"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a check against committed HEAD (or --rev) inside an isolated temporary "
            "git worktree. Exits 0 when the isolated run completed and produced JSON "
            "(even if the check itself failed); exits 2 on setup or argument errors."
        )
    )
    parser.add_argument("--repo", default=".", help="Path inside the Git repository.")
    parser.add_argument("--rev", default="HEAD", help="Git revision to verify (default: HEAD).")
    parser.add_argument(
        "--subdir",
        default="",
        help="Optional relative subdirectory inside the repo to run the command from.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Command timeout in seconds (default: 120).",
    )
    parser.add_argument(
        "--shell-cmd",
        default=None,
        help="Shell command string to run inside the isolated worktree (alternative to -- cmd args).",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run after '--', e.g. -- node --test",
    )
    args = parser.parse_args()

    cmd_list = list(args.command)
    if cmd_list and cmd_list[0] == "--":
        cmd_list = cmd_list[1:]

    if bool(args.shell_cmd) == bool(cmd_list):
        print(
            json.dumps(
                {
                    "ok": False,
                    "result_status": "setup_error",
                    "error": "Provide either '-- <command> [args...]' OR '--shell-cmd <string>' (not both).",
                },
                indent=2,
            )
        )
        return 2

    top = run_git(Path(args.repo).resolve(), ["rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        print(
            json.dumps(
                {
                    "ok": False,
                    "result_status": "setup_error",
                    "error": top.stderr.strip() or "Not a Git repository.",
                },
                indent=2,
            )
        )
        return 2
    repo = Path(top.stdout.strip())

    rev_proc = run_git(repo, ["rev-parse", "--verify", "--quiet", f"{args.rev}^{{commit}}"])
    if rev_proc.returncode != 0 or not rev_proc.stdout.strip():
        print(
            json.dumps(
                {
                    "ok": False,
                    "result_status": "setup_error",
                    "error": f"Revision '{args.rev}' did not resolve to a commit.",
                },
                indent=2,
            )
        )
        return 2
    rev_sha = rev_proc.stdout.strip()
    rev_short = rev_sha[:7]

    head_before, status_before = main_repo_fingerprint(repo)
    tmpdir = Path(tempfile.mkdtemp(prefix="pr-receipt-head-"))
    worktree_created = False
    cleanup_ok = False
    cleanup_error: str | None = None

    def cleanup() -> None:
        nonlocal cleanup_ok, cleanup_error
        errs: list[str] = []
        if worktree_created:
            rm = run_git(repo, ["worktree", "remove", "--force", str(tmpdir)])
            if rm.returncode != 0 and tmpdir.exists():
                errs.append(rm.stderr.strip())
        if tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)
        if worktree_created and tmpdir.exists():
            errs.append(f"Temp directory still exists: {tmpdir}")
        # Prune stale entry only if remove failed
        if errs and worktree_created:
            run_git(repo, ["worktree", "prune"])
        cleanup_ok = not tmpdir.exists()
        cleanup_error = "; ".join(e for e in errs if e) or None

    def handle_signal(signum: int, _frame: Any) -> None:
        cleanup()
        sys.exit(128 + signum)

    prev_int = signal.signal(signal.SIGINT, handle_signal)
    prev_term = signal.signal(signal.SIGTERM, handle_signal)

    try:
        add = run_git(
            repo,
            [
                "-c",
                "core.hooksPath=/dev/null",
                "worktree",
                "add",
                "--detach",
                str(tmpdir),
                rev_sha,
            ],
        )
        if add.returncode != 0:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "result_status": "setup_error",
                        "error": f"git worktree add failed: {add.stderr.strip()}",
                    },
                    indent=2,
                )
            )
            return 2
        worktree_created = True

        run_cwd = tmpdir
        if args.subdir:
            candidate = (tmpdir / args.subdir).resolve()
            resolved_root = tmpdir.resolve()
            if candidate != resolved_root and resolved_root not in candidate.parents:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "result_status": "setup_error",
                            "error": f"Invalid --subdir '{args.subdir}': escapes repository root.",
                        },
                        indent=2,
                    )
                )
                return 2
            if not candidate.is_dir():
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "result_status": "setup_error",
                            "rev_sha": rev_sha,
                            "rev_short": rev_short,
                            "error": (
                                f"Subdirectory '{args.subdir}' does not exist in committed {rev_short} "
                                "(it may only exist in your uncommitted working tree)."
                            ),
                        },
                        indent=2,
                    )
                )
                return 2
            run_cwd = candidate

        display_cmd = args.shell_cmd if args.shell_cmd else shlex.join(cmd_list)
        start = time.monotonic()
        timed_out = False
        try:
            if args.shell_cmd:
                proc = subprocess.run(
                    args.shell_cmd,
                    cwd=str(run_cwd),
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=args.timeout,
                    check=False,
                )
            else:
                proc = subprocess.run(
                    cmd_list,
                    cwd=str(run_cwd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=args.timeout,
                    check=False,
                )
            exit_code: int | None = proc.returncode
            stdout_raw = proc.stdout or ""
            stderr_raw = proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = None
            stdout_raw = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            stderr_raw = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
        except FileNotFoundError as exc:
            exit_code = 127
            stdout_raw = ""
            stderr_raw = str(exc)

        duration_ms = int((time.monotonic() - start) * 1000)
        worktree_paths = [str(tmpdir), str(tmpdir.resolve())]
        combined_raw = "\n".join(part for part in (stdout_raw, stderr_raw) if part)
        combined = normalize_worktree_paths(combined_raw, worktree_paths)

        if timed_out:
            result_status = "timed_out"
            missing_deps = False
            missing_deps_hint = None
            zero_tests = False
            observed = f"Timed out after {args.timeout}s"
        else:
            assert exit_code is not None
            missing_deps, missing_deps_hint = detect_missing_deps(exit_code, combined)
            zero_tests = detect_zero_tests(exit_code, combined)
            if exit_code == 0:
                result_status = "incomplete_zero_tests" if zero_tests else "pass"
            elif zero_tests:
                result_status = "incomplete_zero_tests"
            elif missing_deps:
                result_status = "blocked_missing_deps"
            else:
                result_status = "fail"
            observed = extract_observed_summary(combined, exit_code)

    finally:
        cleanup()
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)

    head_after, status_after = main_repo_fingerprint(repo)
    output = {
        "ok": True,
        "result_status": result_status,
        "snapshot": {
            "type": "isolated_committed_worktree",
            "rev_requested": args.rev,
            "rev_sha": rev_sha,
            "rev_short": rev_short,
            "subdir": args.subdir or ".",
        },
        "command": {
            "display": display_cmd,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_ms": duration_ms,
        },
        "signals": {
            "zero_tests_suspected": zero_tests,
            "missing_deps_suspected": missing_deps,
            "missing_deps_hint": missing_deps_hint,
        },
        "observed_summary": observed,
        "stdout_excerpt": trim_excerpt(stdout_raw, worktree_paths),
        "stderr_excerpt": trim_excerpt(stderr_raw, worktree_paths),
        "integrity": {
            "main_repo_untouched": (head_before == head_after and status_before == status_after),
            "main_head_changed": head_before != head_after,
            "cleanup_ok": cleanup_ok,
            "cleanup_error": cleanup_error,
        },
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
