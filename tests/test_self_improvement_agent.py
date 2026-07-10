from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import self_improvement_agent


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class TestSelfImprovementWorktree(unittest.TestCase):
    """_create_worktree / _remove_worktree against a disposable temp repo --
    never the real browser-agent repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        origin = base / "origin.git"
        work = base / "work"
        _git(["init", "--bare", "-b", "main", str(origin)], cwd=base)
        _git(["clone", str(origin), str(work)], cwd=base)
        _git(["config", "user.email", "test@example.com"], cwd=work)
        _git(["config", "user.name", "Test"], cwd=work)
        (work / "README.md").write_text("hello\n")
        _git(["add", "-A"], cwd=work)
        _git(["commit", "-m", "initial"], cwd=work)
        _git(["push", "origin", "main"], cwd=work)
        (work / ".venv").mkdir()
        (work / ".venv" / "marker").write_text("fake venv\n")
        self.work = work
        self.worktree_base = base / "worktrees"

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_remove_worktree_round_trip(self):
        from src.self_improvement import worktree

        with patch.object(worktree, "PROJECT_ROOT", self.work), \
             patch.object(worktree, "WORKTREE_BASE", self.worktree_base):
            path, branch = self_improvement_agent._create_worktree()
            try:
                self.assertTrue(path.exists())
                self.assertTrue((path / "README.md").exists())
                self.assertTrue(branch.startswith("self-improvement/"))
                self.assertTrue((path / ".venv").is_symlink())
                self.assertEqual((path / ".venv").resolve(), (self.work / ".venv").resolve())
                self.assertTrue((path / ".venv" / "marker").exists())
                listed = subprocess.run(["git", "worktree", "list"], cwd=self.work,
                                        capture_output=True, text=True, check=True)
                self.assertIn(str(path), listed.stdout)
            finally:
                logger = self_improvement_agent._Logger(self.worktree_base / "cleanup.log")
                self_improvement_agent._remove_worktree(path, branch, logger)
                logger.close()

            self.assertFalse(path.exists())
            listed = subprocess.run(["git", "worktree", "list"], cwd=self.work,
                                    capture_output=True, text=True, check=True)
            self.assertNotIn(str(path), listed.stdout)
            branches = subprocess.run(["git", "branch", "--list", branch], cwd=self.work,
                                      capture_output=True, text=True, check=True)
            self.assertEqual(branches.stdout.strip(), "")


class TestSelfImprovementAgent(unittest.TestCase):
    def test_should_recover_default_failure_outcomes(self):
        for status in ("blocked", "error", "incomplete", "timeout", "not_available"):
            self.assertTrue(self_improvement_agent.should_recover(status))

    def test_should_not_recover_success_or_bookkeeping(self):
        for status in ("submitted", "already_applied", "skipped_duplicate", "no_listing_link"):
            self.assertFalse(self_improvement_agent.should_recover(status))

    def test_estimate_deepseek_cost_matches_verified_run(self):
        # Real numbers from a verified run (logs/self_improvement, 2026-07-01):
        # deepseek-v4-pro rates gave $0.030 for this usage; the SDK's own
        # cost field reported $0.586 for the same run (~19.5x too high).
        usage = {
            "input_tokens": 52241,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 272256,
            "output_tokens": 7198,
        }
        cost = self_improvement_agent._estimate_deepseek_cost_usd(usage)
        self.assertAlmostEqual(cost, 0.0300, places=3)

    def test_estimate_deepseek_cost_handles_empty_usage(self):
        self.assertEqual(self_improvement_agent._estimate_deepseek_cost_usd({}), 0.0)

    def test_browser_url_validation(self):
        self.assertTrue(self_improvement_agent._safe_browser_url("https://example.test/listing"))
        self.assertTrue(self_improvement_agent._safe_browser_url("http://example.test/listing"))
        self.assertFalse(self_improvement_agent._safe_browser_url("file:///etc/passwd"))
        self.assertFalse(self_improvement_agent._safe_browser_url("javascript:alert(1)"))

    def test_browser_click_guard_blocks_submit_like_labels(self):
        for label in (
            "Reageer op deze woning",
            "Bezichtiging aanvragen",
            "Submit application",
            "Reactie intrekken",
            "Wachtwoord vergeten",
        ):
            self.assertTrue(self_improvement_agent._blocked_click_label(label))

    def test_browser_click_guard_allows_benign_labels(self):
        for label in ("Alles accepteren", "Meer informatie", "Details bekijken"):
            self.assertFalse(self_improvement_agent._blocked_click_label(label))

    def test_can_use_tool_denies_raw_git_write_via_bash(self):
        for command in (
            "git commit -m 'oops'",
            "git push origin main",
            "git reset --hard HEAD~1",
        ):
            result = self._run_can_use_tool("Bash", {"command": command})
            self.assertEqual(result.behavior, "deny")
            self.assertIn("commit_push_deploy", result.message)

    def test_can_use_tool_allows_read_only_bash(self):
        for command in ("git status", "git log -5", "just check", "rg -n TODO src"):
            result = self._run_can_use_tool("Bash", {"command": command})
            self.assertEqual(result.behavior, "allow")

    def test_can_use_tool_allows_non_bash_tools(self):
        result = self._run_can_use_tool("Read", {"file_path": "src/apply.py"})
        self.assertEqual(result.behavior, "allow")

    @staticmethod
    def _run_can_use_tool(name: str, tool_input: dict):
        import asyncio

        return asyncio.run(self_improvement_agent._can_use_tool(name, tool_input, ctx=None))

    def test_parse_result_reads_marker_from_text(self):
        msg = type("FakeResult", (), {
            "is_error": False,
            "result": (
                "I diagnosed the issue and it's an external state.\n"
                'SELF_IMPROVEMENT_JSON: {"action":"noop","root_cause":"site unavailable",'
                '"summary":"external state","email_sent":false,'
                '"code_changed":false,"deployed":false}'
            ),
        })()
        result = self_improvement_agent._parse_result(msg)
        self.assertEqual(result.action, "noop")
        self.assertEqual(result.root_cause, "site unavailable")

    def test_parse_result_falls_back_when_no_marker(self):
        msg = type("FakeResult", (), {
            "is_error": True,
            "result": "something went wrong",
        })()
        result = self_improvement_agent._parse_result(msg)
        self.assertEqual(result.action, "error")
        self.assertIn("something went wrong", result.summary)

    def test_send_fix_email_includes_listing_context(self):
        context = {
            "listing": {
                "source_url": "https://example.test/listing",
                "address": "Teststraat 1",
                "source_name": "Example",
            },
            "result": {"outcome": "blocked"},
        }
        with patch.object(self_improvement_agent, "send_alert") as send_alert:
            self_improvement_agent._send_fix_email(context, "Changed code.", "commit ok")

        send_alert.assert_called_once()
        subject, body = send_alert.call_args.args
        self.assertIn("self-improvement", subject)
        self.assertIn("Changed code.", body)
        self.assertIn("https://example.test/listing", body)
        self.assertIn("blocked", body)

    def test_legacy_recovery_env_alias(self):
        # RECOVERY_* aliases are honored by the settings loader now.
        from src.settings import load_settings
        s = load_settings({"RECOVERY_VERIFY_CMD": "echo legacy"})
        self.assertEqual(s.self_improvement_verify_cmd, "echo legacy")
        # The canonical name wins over the alias when both are set.
        s = load_settings({
            "SELF_IMPROVEMENT_VERIFY_CMD": "echo canonical",
            "RECOVERY_VERIFY_CMD": "echo legacy",
        })
        self.assertEqual(s.self_improvement_verify_cmd, "echo canonical")

    def test_parse_marker_reads_diagnosis_json(self):
        msg = type("FakeResult", (), {
            "result": (
                "Diagnosed.\n"
                'DIAGNOSIS_JSON: {"verdict":"fix","root_cause":"off-by-one in '
                'browser_lock timeout","fix_plan":"clamp the wait","summary":"s",'
                '"email_sent":false}'
            ),
        })()
        data = self_improvement_agent._parse_marker(
            msg, self_improvement_agent._DIAGNOSIS_MARKER_RE)
        self.assertEqual(data["verdict"], "fix")
        self.assertIn("browser_lock", data["root_cause"])

    def test_parse_marker_returns_none_without_marker(self):
        msg = type("FakeResult", (), {"result": "no marker here"})()
        self.assertIsNone(self_improvement_agent._parse_marker(
            msg, self_improvement_agent._DIAGNOSIS_MARKER_RE))

    def test_candidate_strategies_expand_for_recurrent_incident(self):
        ctx = {"incident": {"occurrences": 3, "prior_attempts": [
            {"strategy": "control_policy"},
        ]}, "result": {"outcome": "error", "summary": "browser_lock timeout"}}
        strategies = self_improvement_agent._candidate_strategies(
            ctx, {"surface": "control_policy"})
        self.assertGreaterEqual(len(strategies), 2)
        self.assertNotEqual(strategies[0], "control_policy")

    def test_changed_paths_from_porcelain_handles_renames(self):
        out = self_improvement_agent._changed_paths_from_porcelain(
            " M src/apply.py\nR  old.py -> src/browser_agent.py\n")
        self.assertIn("src/apply.py", out)
        self.assertIn("src/browser_agent.py", out)


class TestPushFallback(unittest.TestCase):
    """A verified fix must survive a failed push: format-patch to
    state/pending_patches (production 03-07-2026: five correct browser_lock
    fixes were lost to a read-only deploy key, one rescued by hand)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _git(["init", "-b", "main"], cwd=self.repo)
        _git(["config", "user.email", "test@example.com"], cwd=self.repo)
        _git(["config", "user.name", "Test"], cwd=self.repo)
        (self.repo / "file.py").write_text("x = 1\n")
        _git(["add", "-A"], cwd=self.repo)
        _git(["commit", "-m", "fix(test): the fix"], cwd=self.repo)
        self.patch_dir = Path(self.tmp.name) / "pending_patches"

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_pending_patch_writes_git_am_able_file(self):
        with patch.object(self_improvement_agent, "PENDING_PATCH_DIR", self.patch_dir):
            path = self_improvement_agent._save_pending_patch(
                self.repo, "self-improvement/20260707_test")
        self.assertTrue(path)
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("fix(test): the fix", text)
        self.assertIn("x = 1", text)

    def test_save_pending_patch_fails_open_outside_git(self):
        with patch.object(self_improvement_agent, "PENDING_PATCH_DIR", self.patch_dir):
            path = self_improvement_agent._save_pending_patch(
                Path(self.tmp.name), "branch")
        self.assertEqual(path, "")

    def test_handle_push_failure_saves_patch_and_emails(self):
        with patch.object(self_improvement_agent, "PENDING_PATCH_DIR", self.patch_dir), \
             patch.object(self_improvement_agent, "_send_fix_email") as email:
            out = self_improvement_agent._handle_push_failure(
                None, self.repo, "self-improvement/x",
                "rc=0\ncommitted", "rc=1\npermission denied", target="main")
        self.assertIn("push to main failed", out)
        self.assertIn("git am", out)
        email.assert_called_once()
        summary = email.call_args.args[1]
        self.assertIn("could not push", summary)
        self.assertIn("pending_patches", summary)
        self.assertEqual(len(list(self.patch_dir.glob("*.patch"))), 1)


if __name__ == "__main__":
    unittest.main()
