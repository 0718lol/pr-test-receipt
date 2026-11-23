"""Self-contained integration tests for PR Test Receipt helper scripts."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PREFLIGHT = REPO_ROOT / "scripts" / "preflight_snapshot.py"
VERIFY = REPO_ROOT / "scripts" / "verify_head_worktree.py"


def run_json(cmd: list[str]) -> tuple[int, dict, str]:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc.returncode, json.loads(proc.stdout), proc.stderr


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc.stdout


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test User")


class PrTestReceiptScriptsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_root = Path(tempfile.mkdtemp(prefix="pr-receipt-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def test_dirty_fix_hides_committed_head_failure(self) -> None:
        repo = self.tmp_root / "dirty_demo"
        init_repo(repo)
        (repo / "shipping.py").write_text("def shipping_cost(subtotal):\n    return 0 if subtotal >= 50 else 5\n")
        (repo / "test_shipping.py").write_text(
            "import unittest\n"
            "from shipping import shipping_cost\n"
            "class T(unittest.TestCase):\n"
            "    def test_below(self): self.assertEqual(shipping_cost(49), 5)\n"
            "    def test_threshold(self): self.assertEqual(shipping_cost(50), 0)\n"
        )
        git(repo, "add", ".")
        git(repo, "commit", "-m", "Add shipping rates")

        git(repo, "checkout", "-b", "fix/shipping-threshold")
        # Commit a broken boundary condition
        (repo / "shipping.py").write_text("def shipping_cost(subtotal):\n    return 0 if subtotal > 50 else 5\n")
        git(repo, "add", "shipping.py")
        git(repo, "commit", "-m", "Update shipping threshold handling")
        # Apply fix ONLY in unstaged working tree
        (repo / "shipping.py").write_text("def shipping_cost(subtotal):\n    return 0 if subtotal >= 50 else 5\n")

        rc, snap, _ = run_json([sys.executable, str(PREFLIGHT), "--repo", str(repo)])
        self.assertEqual(rc, 0)
        self.assertFalse(snap["working_tree"]["clean"])
        self.assertEqual(snap["risk_signals"]["overlap_files"], ["shipping.py"])

        # Dirty working tree passes
        dirty_run = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-v"],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(dirty_run.returncode, 0)

        # Isolated committed HEAD fails cleanly without touching main checkout
        rc, vres, _ = run_json([
            sys.executable,
            str(VERIFY),
            "--repo",
            str(repo),
            "--rev",
            "HEAD",
            "--",
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-v",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(vres["result_status"], "fail")
        self.assertTrue(vres["integrity"]["main_repo_untouched"])
        self.assertTrue(vres["integrity"]["cleanup_ok"])

    def test_remote_base_context_and_state_hash_mutation(self) -> None:
        origin = self.tmp_root / "origin.git"
        origin.mkdir()
        git(origin, "init", "--bare", "-b", "main")

        repo = self.tmp_root / "remote_demo"
        init_repo(repo)
        (repo / ".github" / "workflows").mkdir(parents=True)
        (repo / ".github" / "workflows" / "ci.yml").write_text("name: CI\n")
        (repo / ".github" / "pull_request_template.md").write_text("## Testing\n- [ ] Unit tests\n")
        (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        (repo / "mod.py").write_text("x = 1\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "Init")
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-u", "origin", "main")
        git(repo, "remote", "set-head", "origin", "main")

        git(repo, "checkout", "-b", "feat/demo")
        (repo / "mod.py").write_text("x = 2   \n")
        git(repo, "add", "mod.py")
        git(repo, "commit", "-m", "Trailing whitespace")

        git(repo, "checkout", "main")
        (repo / "README.md").write_text("# Demo\n")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "Advance main")
        git(repo, "push", "origin", "main")
        git(repo, "checkout", "feat/demo")

        rc, snap, _ = run_json([sys.executable, str(PREFLIGHT), "--repo", str(repo)])
        self.assertEqual(rc, 0)
        self.assertEqual(snap["base"]["resolved_ref"], "origin/main")
        self.assertTrue(snap["base"]["is_remote_tracking_snapshot"])
        self.assertEqual(snap["base"]["commits_ahead"], 1)
        self.assertEqual(snap["base"]["commits_behind"], 1)
        self.assertTrue(snap["working_tree"]["clean"])
        self.assertTrue(snap["risk_signals"]["has_committed_whitespace_issues"])
        self.assertIn(".github/workflows/ci.yml", snap["repo_context"]["ci_files"])
        self.assertIn(".github/pull_request_template.md", snap["repo_context"]["pr_templates"])

        before_hash = snap["working_tree"]["state_hash"]
        (repo / "artifact.tmp").write_text("created during run\n")
        rc, snap_after, _ = run_json([sys.executable, str(PREFLIGHT), "--repo", str(repo)])
        self.assertEqual(rc, 0)
        self.assertNotEqual(snap_after["working_tree"]["state_hash"], before_hash)

    def test_zero_tests_trap_detected(self) -> None:
        repo = self.tmp_root / "zero_tests"
        init_repo(repo)
        (repo / "a.txt").write_text("hi\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "Init")

        rc, vres, _ = run_json([
            sys.executable,
            str(VERIFY),
            "--repo",
            str(repo),
            "--",
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-p",
            "nope_*.py",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(vres["result_status"], "incomplete_zero_tests")
        self.assertTrue(vres["signals"]["zero_tests_suspected"])

    def test_untracked_local_file_is_fail_not_missing_deps(self) -> None:
        repo = self.tmp_root / "untracked_mod"
        init_repo(repo)
        (repo / "app.py").write_text("from helper_local import val\nassert val == 42\n")
        git(repo, "add", "app.py")
        git(repo, "commit", "-m", "Forgot git add helper_local.py")
        (repo / "helper_local.py").write_text("val = 42\n")

        rc, vres, _ = run_json([
            sys.executable,
            str(VERIFY),
            "--repo",
            str(repo),
            "--",
            sys.executable,
            "app.py",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(vres["result_status"], "fail")
        self.assertFalse(vres["signals"]["missing_deps_suspected"])

    def test_missing_third_party_dep_classified_honestly(self) -> None:
        repo = self.tmp_root / "missing_dep"
        init_repo(repo)
        (repo / "check.py").write_text(
            "import sys; sys.stderr.write(\"Cannot find package 'vitest' imported from <worktree>/test.ts\\n\"); sys.exit(1)\n"
        )
        git(repo, "add", "check.py")
        git(repo, "commit", "-m", "Simulate missing npm package")

        rc, vres, _ = run_json([
            sys.executable,
            str(VERIFY),
            "--repo",
            str(repo),
            "--",
            sys.executable,
            "check.py",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(vres["result_status"], "blocked_missing_deps")
        self.assertTrue(vres["signals"]["missing_deps_suspected"])

    def test_secret_redaction_subdir_guard_and_timeout(self) -> None:
        repo = self.tmp_root / "security"
        init_repo(repo)
        (repo / "ok.txt").write_text("ok\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "Init")

        rc, vres, _ = run_json([
            sys.executable,
            str(VERIFY),
            "--repo",
            str(repo),
            "--",
            sys.executable,
            "-c",
            "import sys; print('Bearer supersecrettoken123456 ghp_1234567890abcdefghij1234567890 API_KEY=my_secret_999'); sys.exit(1)",
        ])
        self.assertEqual(rc, 0)
        self.assertNotIn("supersecrettoken123456", vres["stdout_excerpt"])
        self.assertNotIn("ghp_1234567890abcdefghij1234567890", vres["stdout_excerpt"])
        self.assertNotIn("my_secret_999", vres["stdout_excerpt"])

        rc_esc, v_esc, _ = run_json([
            sys.executable,
            str(VERIFY),
            "--repo",
            str(repo),
            "--subdir",
            "../outside",
            "--",
            sys.executable,
            "-c",
            "print(1)",
        ])
        self.assertEqual(rc_esc, 2)
        self.assertEqual(v_esc["result_status"], "setup_error")

        rc_to, v_to, _ = run_json([
            sys.executable,
            str(VERIFY),
            "--repo",
            str(repo),
            "--timeout",
            "1",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(3)",
        ])
        self.assertEqual(rc_to, 0)
        self.assertEqual(v_to["result_status"], "timed_out")
        self.assertTrue(v_to["integrity"]["cleanup_ok"])

    def test_unborn_ambiguous_missing_base_and_renames(self) -> None:
        repo = self.tmp_root / "edge_cases"
        init_repo(repo)

        rc, unborn, _ = run_json([sys.executable, str(PREFLIGHT), "--repo", str(repo)])
        self.assertEqual(rc, 0)
        self.assertTrue(unborn["head"]["unborn"])

        (repo / "old name.txt").write_text("v1\n")
        git(repo, "add", "old name.txt")
        git(repo, "commit", "-m", "Init")
        git(repo, "branch", "master")
        git(repo, "checkout", "-b", "feat/rename")
        git(repo, "mv", "old name.txt", "new name.txt")
        git(repo, "commit", "-m", "Rename with spaces")

        rc, ambig, _ = run_json([sys.executable, str(PREFLIGHT), "--repo", str(repo)])
        self.assertEqual(ambig["base"]["status"], "ambiguous")
        self.assertEqual(set(ambig["base"]["candidates"]), {"main", "master"})

        rc, resolved, _ = run_json([sys.executable, str(PREFLIGHT), "--repo", str(repo), "--base", "main"])
        self.assertEqual(resolved["base"]["status"], "resolved")
        self.assertEqual(resolved["committed_diff"]["files"][0]["path"], "new name.txt")
        self.assertEqual(resolved["committed_diff"]["files"][0]["orig_path"], "old name.txt")

        rc, missing, _ = run_json([sys.executable, str(PREFLIGHT), "--repo", str(repo), "--base", "no-such-branch"])
        self.assertEqual(missing["base"]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
