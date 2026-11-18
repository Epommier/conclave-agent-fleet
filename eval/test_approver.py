import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "approver.py")


def q(text, labels, header="H", multi=None):
    d = {"question": text, "header": header,
         "options": [{"label": l, "description": ""} for l in labels]}
    if multi is not None:
        d["multiSelect"] = multi
    return d


class ApproverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "approver.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, stdin, eval_on=True, log=None):
        env = {k: v for k, v in os.environ.items() if k != "CONCLAVE_EVAL"}
        env["CONCLAVE_APPROVER_LOG"] = log or self.log
        if eval_on:
            env["CONCLAVE_EVAL"] = "1"
        if not isinstance(stdin, str):
            stdin = json.dumps(stdin)
        return subprocess.run([sys.executable, SCRIPT], input=stdin, env=env,
                              capture_output=True, text=True)

    def ask(self, *questions):
        return {"session_id": "s1", "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion",
                "tool_input": {"questions": list(questions)}}

    def answers(self, *questions):
        r = self.run_hook(self.ask(*questions))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["hookSpecificOutput"]

    def assertBlocked(self, r):
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertTrue(r.stderr.strip())

    def test_approval_gate(self):
        gate = q("Proceed?", ["Go (Recommended)", "Stop here"], header="Gate")
        out = self.answers(gate)
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertEqual(out["updatedInput"]["questions"], [gate])
        self.assertEqual(out["updatedInput"]["answers"], {"Proceed?": "Go (Recommended)"})

    def test_recommended_not_first(self):
        out = self.answers(q("Pick", ["A", "B (recommended)", "C"]))
        self.assertEqual(out["updatedInput"]["answers"], {"Pick": "B (recommended)"})

    def test_no_recommended_takes_first(self):
        out = self.answers(q("Pick", ["A", "B"]))
        self.assertEqual(out["updatedInput"]["answers"], {"Pick": "A"})

    def test_several_questions(self):
        out = self.answers(q("One", ["x", "y (Recommended)"]), q("Two", ["z", "w"]))
        self.assertEqual(out["updatedInput"]["answers"], {"One": "y (Recommended)", "Two": "z"})

    def test_multiselect_recommended(self):
        out = self.answers(q("M", ["A (Recommended)", "B", "C (Recommended)"], multi=True))
        self.assertEqual(out["updatedInput"]["answers"], {"M": "A (Recommended), C (Recommended)"})

    def test_multiselect_none_recommended(self):
        out = self.answers(q("M", ["A", "B"], multi=True))
        self.assertEqual(out["updatedInput"]["answers"], {"M": "A"})

    def test_invalid_json(self):
        self.assertBlocked(self.run_hook("{not json"))

    def test_empty_options(self):
        self.assertBlocked(self.run_hook(self.ask(q("Pick", []))))

    def test_other_tool(self):
        r = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_guard_unset(self):
        self.assertBlocked(self.run_hook(self.ask(q("Pick", ["A"])), eval_on=False))

    def test_log(self):
        self.answers(q("Proceed?", ["Go (Recommended)", "Stop"], header="Gate"),
                     q("Two", ["z"], header="Other"))
        with open(self.log) as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual([(r["header"], r["answer"]) for r in rows],
                         [("Gate", "Go (Recommended)"), ("Other", "z")])
        self.assertEqual(rows[0]["session_id"], "s1")
        self.assertEqual(rows[0]["options"], ["Go (Recommended)", "Stop"])

    def test_unwritable_log(self):
        bad = os.path.join(self.tmp.name, "missing", "approver.jsonl")
        self.assertBlocked(self.run_hook(self.ask(q("Pick", ["A"])), log=bad))


if __name__ == "__main__":
    unittest.main()
