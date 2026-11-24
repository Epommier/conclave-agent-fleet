import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "redirect_plans.py")


class RedirectPlansTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = os.path.realpath(self.tmp.name)
        self.home = os.path.join(root, "home")
        self.r = os.path.join(root, "redirect")
        self.cwd = os.path.join(root, "work")
        os.mkdir(self.home)
        os.mkdir(self.r)
        os.mkdir(self.cwd)
        self.log = os.path.join(root, "redirect.jsonl")
        self.plans = os.path.join(self.home, ".claude", "plans")

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, stdin, eval_vars=True, **overrides):
        """Run the hook with a minimal env; overrides set (str) or remove (None) variables."""
        env = {"HOME": self.home}
        if eval_vars:
            env.update(CONCLAVE_EVAL="1", CONCLAVE_PLANS_DIR=self.r,
                       CONCLAVE_REDIRECT_LOG=self.log)
        for k, v in overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        if not isinstance(stdin, str):
            stdin = json.dumps(stdin)
        return subprocess.run([sys.executable, "-I", SCRIPT], input=stdin, env=env,
                              capture_output=True, text=True)

    def event(self, file_path, tool="Write", cwd=True, **extra):
        tool_input = {"file_path": file_path}
        tool_input.update({"Write": {"content": "PLAN BODY"},
                           "Edit": {"old_string": "old", "new_string": "new"},
                           "Read": {"limit": 5}}.get(tool, {}))
        tool_input.update(extra)
        e = {"session_id": "s1", "hook_event_name": "PreToolUse",
             "tool_name": tool, "tool_input": tool_input}
        if cwd:
            e["cwd"] = self.cwd
        return e

    def assertAllowed(self, r, event):
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "allow")
        expected = dict(event["tool_input"], file_path=os.path.join(os.path.realpath(self.r), "a.md"))
        self.assertEqual(out["updatedInput"], expected)

    def assertNoAllow(self, r, code=0):
        self.assertEqual(r.returncode, code, r.stderr)
        self.assertEqual(r.stdout, "")

    def assertBlocked(self, r):
        self.assertNoAllow(r, code=2)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)

    def plans_event(self):
        return self.event(os.path.join(self.plans, "a.md"))

    # Rewritten (allow, exit 0)

    def test_rewritten_all_tools_and_forms(self):
        forms = {"absolute": os.path.join(self.plans, "a.md"),
                 "tilde": "~/.claude/plans/a.md",
                 "double_slash": self.home + "/.claude//plans/a.md",
                 "dot_segment": self.home + "/.claude/./plans/a.md"}
        for tool in ("Write", "Read", "Edit"):
            for name, path in forms.items():
                with self.subTest(tool=tool, form=name):
                    e = self.event(path, tool)
                    self.assertAllowed(self.run_hook(e), e)

    def test_rewritten_existing_single_link_file(self):
        with open(os.path.join(self.r, "a.md"), "w") as f:
            f.write("x")
        for tool in ("Read", "Edit"):
            with self.subTest(tool=tool):
                e = self.event(os.path.join(self.plans, "a.md"), tool)
                self.assertAllowed(self.run_hook(e), e)

    # Never an allow (non-plans paths, exit 0)

    def test_never_allow_non_plans_paths(self):
        cases = {"dotdot_escape": "~/.claude/plans/../../x",
                 "dotdot_settings": "~/.claude/plans/../settings.json",
                 "plansX": "~/.claude/plansX/a.md",
                 "plans_evil": "~/.claude/plans-evil/a.md",
                 "plans_folder": "~/.claude/plans",
                 "plans_folder_slash": "~/.claude/plans/",
                 "hidden_md": "~/.claude/plans/.hidden.md",
                 "subfolder": "~/.claude/plans/sub/a.md",
                 "not_md": "~/.claude/plans/a.txt",
                 "relative": ".claude/plans/a.md",
                 "tilde_user": "~other/.claude/plans/a.md"}
        for name, path in cases.items():
            for tool in ("Write", "Read", "Edit"):
                with self.subTest(case=name, tool=tool):
                    self.assertNoAllow(self.run_hook(self.event(path, tool)))

    # Exit 2, no allow, for a plans path

    def test_blocked_target_symlink_to_outside_file(self):
        outside = os.path.join(self.tmp.name, "outside.md")
        open(outside, "w").close()
        os.symlink(outside, os.path.join(self.r, "a.md"))
        self.assertBlocked(self.run_hook(self.plans_event()))

    def test_blocked_target_symlink_inside_redirect_folder(self):
        # Resolves directly in R, so only the lstat "not a symlink" rule catches it.
        open(os.path.join(self.r, "b.md"), "w").close()
        os.symlink("b.md", os.path.join(self.r, "a.md"))
        self.assertBlocked(self.run_hook(self.plans_event()))

    def test_blocked_redirect_folder_symlink_to_git_folder(self):
        real = os.path.join(self.tmp.name, "x", ".git")
        os.makedirs(real)
        link = os.path.join(self.tmp.name, "plans_link")
        os.symlink(real, link)
        self.assertBlocked(self.run_hook(self.plans_event(), CONCLAVE_PLANS_DIR=link))

    def test_blocked_redirect_folder_symlink_into_cwd(self):
        real = os.path.join(self.cwd, "sub")
        os.mkdir(real)
        link = os.path.join(self.tmp.name, "plans_link")
        os.symlink(real, link)
        self.assertBlocked(self.run_hook(self.plans_event(), CONCLAVE_PLANS_DIR=link))

    def test_blocked_target_two_hard_links(self):
        target = os.path.join(self.r, "a.md")
        open(target, "w").close()
        os.link(target, os.path.join(self.tmp.name, "other.md"))
        self.assertBlocked(self.run_hook(self.plans_event()))

    def test_blocked_target_is_directory(self):
        os.mkdir(os.path.join(self.r, "a.md"))
        self.assertBlocked(self.run_hook(self.plans_event()))

    def test_blocked_plans_dir_unset(self):
        self.assertBlocked(self.run_hook(self.plans_event(), CONCLAVE_PLANS_DIR=None))

    def test_blocked_plans_dir_relative(self):
        self.assertBlocked(self.run_hook(self.plans_event(), CONCLAVE_PLANS_DIR="redirect"))

    def test_blocked_plans_dir_missing(self):
        missing = os.path.join(self.tmp.name, "missing")
        self.assertBlocked(self.run_hook(self.plans_event(), CONCLAVE_PLANS_DIR=missing))

    def test_blocked_plans_dir_git_segment(self):
        d = os.path.join(self.tmp.name, ".git", "plans")
        os.makedirs(d)
        self.assertBlocked(self.run_hook(self.plans_event(), CONCLAVE_PLANS_DIR=d))

    def test_blocked_plans_dir_claude_segment(self):
        d = os.path.join(self.tmp.name, ".claude", "plans")
        os.makedirs(d)
        self.assertBlocked(self.run_hook(self.plans_event(), CONCLAVE_PLANS_DIR=d))

    def test_blocked_plans_dir_equals_cwd(self):
        self.assertBlocked(self.run_hook(self.plans_event(), CONCLAVE_PLANS_DIR=self.cwd))

    def test_blocked_plans_dir_inside_cwd(self):
        d = os.path.join(self.cwd, "plans")
        os.mkdir(d)
        self.assertBlocked(self.run_hook(self.plans_event(), CONCLAVE_PLANS_DIR=d))

    def test_blocked_log_unset(self):
        self.assertBlocked(self.run_hook(self.plans_event(), CONCLAVE_REDIRECT_LOG=None))

    def test_blocked_log_unwritable(self):
        bad = os.path.join(self.tmp.name, "missing", "redirect.jsonl")
        self.assertBlocked(self.run_hook(self.plans_event(), CONCLAVE_REDIRECT_LOG=bad))

    def test_blocked_log_directly_in_redirect_folder(self):
        log = os.path.join(self.r, "redirect.jsonl")
        self.assertBlocked(self.run_hook(self.plans_event(), CONCLAVE_REDIRECT_LOG=log))

    def test_blocked_log_in_subfolder_of_redirect_folder(self):
        os.mkdir(os.path.join(self.r, "sub"))
        log = os.path.join(self.r, "sub", "redirect.jsonl")
        self.assertBlocked(self.run_hook(self.plans_event(), CONCLAVE_REDIRECT_LOG=log))

    def test_blocked_event_without_cwd(self):
        e = self.event(os.path.join(self.plans, "a.md"), cwd=False)
        self.assertBlocked(self.run_hook(e))

    # Silent (exit 0, no stdout, no stderr) with every CONCLAVE_* variable unset

    def test_silent_without_eval_variables(self):
        a_md = os.path.join(self.cwd, "a.md")
        cases = {
            "read_non_plans": self.event(a_md, "Read"),
            "write_non_plans": self.event(a_md, "Write"),
            "edit_non_plans": self.event(a_md, "Edit"),
            "file_path_missing": {"tool_name": "Write", "tool_input": {"content": "x"},
                                  "cwd": self.cwd},
            "file_path_number": self.event(42),
            "file_path_list": self.event([os.path.join(self.plans, "a.md")]),
            "file_path_nul": self.event(os.path.join(self.plans, "a\0.md")),
            "tool_bash": {"tool_name": "Bash", "tool_input": {"command": "ls"},
                          "cwd": self.cwd},
            "tool_glob": {"tool_name": "Glob", "tool_input": {"pattern": "*"},
                          "cwd": self.cwd},
            "malformed_stdin": "{not json",
            "empty_stdin": "",
            "json_array": json.dumps([self.plans_event()]),
        }
        for name, stdin in cases.items():
            with self.subTest(case=name):
                r = self.run_hook(stdin, eval_vars=False)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    # Guard unset

    def test_guard_unset_plans_path(self):
        for value in (None, "0", ""):
            with self.subTest(CONCLAVE_EVAL=value):
                self.assertNoAllow(self.run_hook(self.plans_event(), CONCLAVE_EVAL=value))
        self.assertFalse(os.path.exists(self.log))

    # Log

    def test_log_line(self):
        e = self.event("~/.claude/plans/a.md", "Write")
        e["agent_type"] = "planner"  # agent_id left absent: logged as None
        self.assertAllowed(self.run_hook(e), e)
        with open(self.log) as f:
            text = f.read()
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row), {"ts", "tool", "raw", "new", "session_id",
                                    "agent_id", "agent_type"})
        self.assertEqual(row["tool"], "Write")
        self.assertEqual(row["raw"], "~/.claude/plans/a.md")
        self.assertEqual(row["new"], os.path.join(os.path.realpath(self.r), "a.md"))
        self.assertEqual((row["session_id"], row["agent_id"], row["agent_type"]),
                         ("s1", None, "planner"))
        self.assertNotIn("PLAN BODY", text)

    def test_log_raw_truncated(self):
        raw = self.home + "/.claude/plans/" + "/" * 5000 + "a.md"
        self.assertAllowed(self.run_hook(self.event(raw, "Read")), self.event(raw, "Read"))
        with open(self.log) as f:
            row = json.loads(f.read())
        self.assertEqual(row["raw"], raw[:4096])


if __name__ == "__main__":
    unittest.main()
