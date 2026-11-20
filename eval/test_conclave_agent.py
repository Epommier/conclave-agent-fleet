"""Run: PYTHONPATH=eval eval/.venv/bin/python -m unittest eval/test_conclave_agent.py"""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import conclave_agent
from conclave_agent import ConclaveClaudeCode, GuardedClaudeCode, fleet_instruction
from harbor.agents.base import BaseAgent
from harbor.agents.factory import AgentFactory
from harbor.models.agent.context import AgentContext
from harbor.models.trial.config import AgentConfig
from harbor.utils.import_path import import_class
from harbor.utils.trajectory_utils import load_model_usage_from_trajectory

SESSION_ID = "11111111-2222-3333-4444-555555555555"
SESSION = [
    {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": SESSION_ID,
     "timestamp": "2026-11-20T10:00:00.000Z", "cwd": "/testbed", "version": "2.1.327",
     "message": {"role": "user", "content": "hi"}},
    {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": SESSION_ID,
     "timestamp": "2026-11-20T10:00:01.000Z", "cwd": "/testbed", "version": "2.1.327",
     "message": {"id": "msg_1", "model": "claude-haiku-5-5", "role": "assistant",
                 "type": "message", "content": [{"type": "text", "text": "ok"}],
                 "usage": {"input_tokens": 10, "output_tokens": 2}}},
]
SENTINEL = "SENTINEL-must-stay-unread-and-unchanged\n"
OLD = 1_000_000_000  # an atime far in the past, so any read would move it


def within(seconds, fn):
    """Run fn in a daemon thread; fail instead of hanging if it blocks."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise AssertionError(f"blocked for more than {seconds} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def agent(cls, logs_dir, **kwargs):
    """Built the way the runner builds a `module:Class` agent."""
    config = AgentConfig(import_path=f"conclave_agent:{cls.__name__}",
                         model_name="anthropic/claude-haiku-5-5", kwargs=kwargs)
    return AgentFactory.create_agent_from_config(config, logs_dir=logs_dir)


class Rewrite(unittest.TestCase):
    def test_shape_prefix_and_special_text(self):
        task = 'Fix "quoted" \\ text\nsecond line é日本\n'
        line = fleet_instruction("/conclave:implement ", task)
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(line.count("\n"), 1)
        self.assertEqual(json.loads(line), {"type": "user", "message": {
            "role": "user", "content": "/conclave:implement " + task}})

    def test_empty_prefix_and_missing_space(self):
        self.assertEqual(json.loads(fleet_instruction("", "Do it."))["message"]["content"], "Do it.")
        self.assertEqual(json.loads(fleet_instruction("/conclave:implement", "Do it."))
                         ["message"]["content"], "/conclave:implement Do it.")

    def test_refusals(self):
        for task in ("", "  \n\t", "/root/.claude/plans/x.md", "~/plan.md", "plans/x.md",
                     "./x", "README.md", "https://github.com/a/b/issues/1", "www.example.com",
                     "  https://example.com/x  \n"):
            with self.subTest(task=task), self.assertRaises(ValueError):
                fleet_instruction("/conclave:implement ", task)


class Construction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "plugin").mkdir()
        (self.root / "trial" / "agent").mkdir(parents=True)
        self.logs = self.root / "trial" / "agent"

    def tearDown(self):
        self.tmp.cleanup()

    def test_importable_like_the_runner(self):
        for name in ("GuardedClaudeCode", "ConclaveClaudeCode"):
            self.assertTrue(issubclass(import_class(f"conclave_agent:{name}", base=BaseAgent),
                                       BaseAgent))
        # The runner's preflight validates --ak values against the options class.
        ConclaveClaudeCode.parse_options({"plugin_src": "/x", "skill_prefix": ""})
        with self.assertRaises(ValueError):
            ConclaveClaudeCode.parse_options({})

    def test_flags(self):
        fleet = agent(ConclaveClaudeCode, self.logs, plugin_src=str(self.root / "plugin"),
                      max_budget_usd="0.50", version="2.1.327")
        flags = fleet.build_cli_flags()
        for part in ("--input-format stream-json", "--permission-prompt-tool stdio",
                     "--plugin-dir /opt/conclave", "--permission-mode=bypassPermissions",
                     "--max-budget-usd 0.50"):
            self.assertIn(part, flags)
        self.assertEqual(fleet.options.skill_prefix, "/conclave:implement ")
        plain = agent(GuardedClaudeCode, self.logs, max_budget_usd="2.00").build_cli_flags()
        self.assertIn("--permission-mode=bypassPermissions", plain)
        self.assertNotIn("--input-format", plain)

    def test_settings(self):
        fleet = agent(ConclaveClaudeCode, self.logs, plugin_src=str(self.root / "plugin"))
        self.assertEqual(fleet.config_source, {"hooks": {"PreToolUse": [
            {"matcher": "AskUserQuestion", "hooks": [{"type": "command", "timeout": 30, "command":
             "CONCLAVE_EVAL=1 CONCLAVE_APPROVER_LOG=/logs/agent/approver.jsonl "
             "python3 /opt/conclave/eval/approver.py"}]},
            {"matcher": "Write|Edit|Read", "hooks": [{"type": "command", "timeout": 10, "command":
             "CONCLAVE_EVAL=1 CONCLAVE_PLANS_DIR=/logs/agent/plans "
             "CONCLAVE_REDIRECT_LOG=/logs/agent/redirect.jsonl "
             "python3 -I /opt/conclave/eval/redirect_plans.py"}]},
        ]}})

    def test_plugin_src_required_and_a_folder(self):
        with self.assertRaises(ValueError):
            agent(ConclaveClaudeCode, self.logs)
        with self.assertRaises(ValueError):
            agent(ConclaveClaudeCode, self.logs, plugin_src=str(self.root / "missing"))


class GuardedCopy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.outside = root / "outside"
        (self.outside / "dir").mkdir(parents=True)
        self.sentinel = self.outside / "sentinel.txt"
        self.sentinel.write_text(SENTINEL)
        (self.outside / "dir" / "secret.txt").write_text(SENTINEL)
        self.mounted = root / "trial" / "agent"
        sessions = self.mounted / "sessions" / "projects" / "-app"
        sessions.mkdir(parents=True)
        (sessions / f"{SESSION_ID}.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in SESSION))
        (self.mounted / "claude-code.txt").write_text(
            json.dumps({"type": "result", "total_cost_usd": 0.01, "num_turns": 1}) + "\n")
        (self.mounted / "sub").mkdir()
        (self.mounted / "sub" / "ok.txt").write_text("ok")
        (self.mounted / "link").symlink_to(self.sentinel)
        (self.mounted / "sub" / "linkdir").symlink_to(self.outside / "dir")
        os.mkfifo(self.mounted / "fifo")
        self.fifos = [self.mounted / "fifo"]
        (self.mounted / "ANOMALIES.txt").write_text("planted")
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(self.mounted / "sock"))
        self.host = root / "trial" / "agent-host"

    def tearDown(self):
        # Release any reader a regression left blocked on a FIFO.
        for fifo in self.fifos:
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
        self.sock.close()
        self.tmp.cleanup()

    def plant_trajectory(self, kind):
        path = self.mounted / "trajectory.json"
        if kind == "fifo":
            os.mkfifo(path)
            self.fifos.append(path)
        else:
            path.symlink_to(self.sentinel)
        for target in (self.sentinel, self.outside / "dir" / "secret.txt"):
            os.utime(target, (OLD, OLD))

    def assert_outside_untouched(self):
        for target in (self.sentinel, self.outside / "dir" / "secret.txt"):
            st = target.stat()
            self.assertEqual(target.read_text(), SENTINEL)
            self.assertEqual((st.st_atime, st.st_mtime), (OLD, OLD), f"{target} was read or written")

    def post_run(self):
        guarded = GuardedClaudeCode(logs_dir=self.mounted, model_name="anthropic/claude-haiku-5-5")
        context = AgentContext()
        within(10, lambda: guarded.populate_context_post_run(context))
        return guarded, context

    def test_copy_skips_planted_entries_and_writes_only_the_copy(self):
        self.plant_trajectory("symlink")
        guarded, context = self.post_run()
        self.assertEqual(guarded.logs_dir, self.host)
        self.assert_outside_untouched()
        self.assertTrue((self.mounted / "trajectory.json").is_symlink())
        self.assertEqual(context.cost_usd, 0.01)
        copied = set()
        for top, dirs, files in os.walk(self.host):
            for name in dirs + files:
                path = Path(top, name)
                self.assertFalse(path.is_symlink(), path)
                self.assertTrue(path.is_file() or path.is_dir(), path)
                copied.add(path.relative_to(self.host).as_posix())
        self.assertEqual(copied, {
            "ANOMALIES.txt", "claude-code.txt", "trajectory.json", "sub", "sub/ok.txt",
            "sessions", "sessions/projects", "sessions/projects/-app",
            f"sessions/projects/-app/{SESSION_ID}.jsonl"})
        self.assertEqual(sorted((self.host / "ANOMALIES.txt").read_text().splitlines()), [
            "ANOMALIES.txt\treserved name", "fifo\tfifo", "link\tsymlink", "sock\tsocket",
            "sub/linkdir\tsymlink", "trajectory.json\tsymlink"])
        trajectory = json.loads((self.host / "trajectory.json").read_text())
        self.assertEqual(trajectory["session_id"], SESSION_ID)

    def test_trials_later_read_hits_the_copy(self):
        for kind in ("fifo", "symlink"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    (self.mounted / "trajectory.json").unlink()
                self.plant_trajectory(kind)
                guarded, _ = self.post_run()
                # The call trial.py `_sync_agent_output` makes.
                usage = within(5, lambda: load_model_usage_from_trajectory(
                    guarded.logs_dir / "trajectory.json"))
                self.assertEqual(set(usage), {"claude-haiku-5-5"})
                self.assert_outside_untouched()

    def test_later_read_when_post_run_was_skipped(self):
        self.plant_trajectory("fifo")
        guarded = GuardedClaudeCode(logs_dir=self.mounted, model_name="anthropic/claude-haiku-5-5")
        self.assertIsNone(within(5, lambda: load_model_usage_from_trajectory(
            guarded.logs_dir / "trajectory.json")))

    def test_size_caps(self):
        (self.mounted / "big.bin").write_bytes(b"x" * 300)
        (self.mounted / "c2.bin").write_bytes(b"x" * 150)
        (self.mounted / "c3.bin").write_bytes(b"x" * 150)
        with mock.patch.object(conclave_agent, "FILE_CAP", 200), \
             mock.patch.object(conclave_agent, "TOTAL_CAP", 250):
            self.post_run()
        anomalies = (self.host / "ANOMALIES.txt").read_text()
        self.assertIn("big.bin\tover 200 MB", anomalies)
        self.assertIn("c3.bin\tover 500 MB in total", anomalies)
        self.assertFalse((self.host / "big.bin").exists())
        self.assertEqual((self.host / "c2.bin").read_bytes(), b"x" * 150)

    def test_rerun_replaces_the_copy(self):
        self.post_run()
        (self.host / "stale").write_text("x")
        self.post_run()
        self.assertFalse((self.host / "stale").exists())


if __name__ == "__main__":
    unittest.main()
