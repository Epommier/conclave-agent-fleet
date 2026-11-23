import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.dont_write_bytecode = True  # -I ignores PYTHONDONTWRITEBYTECODE
_spec = importlib.util.spec_from_file_location(
    "summary", os.path.join(os.path.dirname(os.path.abspath(__file__)), "summary.py"))
summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summary)

TASKS = ["task-a", "task-b"]


def rec(job, task, reward=1, **changes):
    """A valid record in the writer's shape (see eval/run_arm.sh, record_of)."""
    arm, rnd = summary.JOB.fullmatch(job).groups()
    r = {
        "agent_seconds": 100.0, "anomalies": 0, "approver_lines": 1 if arm == "fleet" else 0,
        "arm": arm, "auth_errors": 0, "budget_capped": False, "cli_version": "2.1.327",
        "cost_source": "final_event", "cost_usd": 0.5 if arm == "fleet" else 0.1,
        "dispatches": {"conclave:executor": 2} if arm == "fleet" else {}, "exception": None,
        "fleet_version": "6b4c4cf" if arm == "fleet" else None, "is_error": False, "job": job,
        "model": "claude-sonnet-5-5", "network": "allowlist", "plan_saved": arm == "fleet",
        "question_calls": 1 if arm == "fleet" else 0, "rate_limit_events": 0, "reward": reward,
        "round": int(rnd), "task": task, "timed_out": False,
        "tokens": {"claude-sonnet-5-5": {"cache_creation": 200, "cache_read": 1000,
                                         "input": 100, "output": 50}},
        "tokens_source": "final_event_per_model", "trial": task + "__AAAAAAA",
        "trial_seconds": 200.0,
    }
    r.update(changes)
    return r


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.list = self.make_list(TASKS)
        self.out = os.path.join(self.root, "out")

    def tearDown(self):
        self.tmp.cleanup()

    def make_list(self, tasks, name="list.json"):
        path = os.path.join(self.root, name)
        with open(path, "w") as handle:
            json.dump({"version": "test-v1", "tasks": tasks}, handle)
        return path

    def job(self, name, lines=None):
        """A job folder; lines are dicts (dumped) or raw strings; None means no records file."""
        path = os.path.join(self.root, "jobs", name)
        os.makedirs(path)
        if lines is not None:
            with open(os.path.join(path, "records.jsonl"), "w") as handle:
                for line in lines:
                    handle.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
        return path

    def run_main(self, *jobs, out=None):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return summary.main([self.list, out or self.out, *jobs])

    def read(self, name):
        with open(os.path.join(self.out, name)) as handle:
            return handle.read()

    def meta(self):
        return json.loads(self.read("meta.json"))

    def round_jobs(self, rnd, plain_rewards=(1, 0), fleet_rewards=(1, 1), **fleet_changes):
        p, f = f"cmp1-plain-r{rnd}", f"cmp1-fleet-r{rnd}"
        return [self.job(p, [rec(p, t, w) for t, w in zip(TASKS, plain_rewards)]),
                self.job(f, [rec(f, t, w, **fleet_changes) for t, w in zip(TASKS, fleet_rewards)])]

    def test_rates_and_fractions(self):
        jobs = self.round_jobs(1, (1, 0), (1, 1)) + self.round_jobs(2, (0, 0), (1, 0))
        with open(os.path.join(jobs[0], "records.jsonl")) as handle:
            recs = [json.loads(line) for line in handle]
        self.assertTrue(all(summary.valid(r, "cmp1-plain-r1", "plain", 1, TASKS) for r in recs))
        self.assertEqual(self.run_main(*jobs), 0)
        f = summary.figures([json.loads(x) for x in self.read("records.jsonl").splitlines()],
                            TASKS)
        self.assertEqual((f["arms"]["plain"]["resolved"], f["arms"]["plain"]["trials"]), (1, 4))
        self.assertEqual((f["arms"]["fleet"]["resolved"], f["arms"]["fleet"]["trials"]), (3, 4))
        rows = {row["task"]: row for row in f["tasks"]}
        self.assertEqual(rows["task-a"]["plain"], (1, 2))
        self.assertEqual(rows["task-a"]["fleet"], (2, 2))
        self.assertEqual(rows["task-a"]["diff"], 0.5)
        self.assertEqual(rows["task-b"]["diff"], 0.5)
        self.assertEqual(self.meta()["rounds_completed"], [1, 2])
        self.assertIn("| plain | 4 | 1 | 0.250 | 0 |", self.read("summary.md"))
        self.assertAlmostEqual(f["ratios"]["cost_usd"]["total"], 5.0)

    def test_bootstrap(self):
        diffs = [0.0, 1.0, -0.5, 0.5, 1.0, 0.0]
        self.assertEqual(summary.bootstrap(diffs), summary.bootstrap(diffs))
        self.assertEqual(summary.bootstrap([0.25] * 7), (0.25, 0.25, 0.25))
        mean, lo, hi = summary.bootstrap(diffs)
        self.assertTrue(lo <= mean <= hi)

    def test_incomplete_round(self):
        p2, f2 = "cmp1-plain-r2", "cmp1-fleet-r2"
        jobs = self.round_jobs(1) + [self.job(p2, [rec(p2, "task-a")]),
                                     self.job(f2, [rec(f2, t) for t in TASKS])]
        self.assertEqual(self.run_main(*jobs), 0)
        meta = self.meta()
        self.assertEqual(meta["rounds_completed"], [1])
        self.assertEqual(len(self.read("records.jsonl").splitlines()), 7)
        # round 1: 0.1*2 + 0.5*2; round 2: 0.1 + 2.00 (missing task-b) + 0.5*2
        self.assertAlmostEqual(meta["spend_complete_rounds"], 1.2)
        self.assertAlmostEqual(meta["spend_all_jobs"], 1.2 + 3.1)
        self.assertIn("| `task-a` | 1 / 1 | 1 / 1 |", self.read("summary.md"))

    def test_approver_errors(self):
        self.assertEqual(self.run_main(*self.round_jobs(1, (1, 1), (0, 1))), 0)
        f = summary.figures([json.loads(x) for x in self.read("records.jsonl").splitlines()],
                            TASKS)
        self.assertEqual(f["arms"]["fleet"]["trials"], 2)
        out = os.path.join(self.root, "out2")
        jobs = self.round_jobs(2, (1, 1), (0, 1), question_calls=2)
        self.assertEqual(self.run_main(*jobs, out=out), 0)
        with open(os.path.join(out, "records.jsonl")) as handle:
            f = summary.figures([json.loads(x) for x in handle], TASKS)
        self.assertEqual(f["arms"]["fleet"]["trials"], 0)
        self.assertEqual(f["arms"]["fleet"]["approver_errors"], 2)
        self.assertEqual(f["no_fleet_trial"], 2)
        self.assertEqual(f["bootstrap"], (None, None, None))
        with open(os.path.join(out, "summary.md")) as handle:
            self.assertIn("| fleet | 0 | 0 | n/a | 2 |", handle.read())

    def test_spend(self):
        p, f = "cmp1-plain-r1", "cmp1-fleet-r1"
        jobs = [self.job(p, [rec(p, "task-a", cost_usd=None, cost_source="out_of_range"),
                             rec(p, "task-b")]),
                self.job(f, None), self.job("cmp1-plain-r2", [rec("cmp1-plain-r2", "task-a")])]
        self.assertEqual(self.run_main(*jobs), 0)
        meta = self.meta()
        # plain r1: 2.00 + 0.1; fleet r1: 2 x 3.50; plain r2: 0.1 + 2.00 (task-b missing)
        self.assertAlmostEqual(meta["spend_all_jobs"], 2.1 + 7.0 + 2.1)
        self.assertEqual(meta["spend_complete_rounds"], 0)
        self.assertEqual(meta["rounds_completed"], [])
        self.assertIn("no complete round", self.read("summary.md"))

    def test_hostile(self):
        p, f = "cmp1-plain-r1", "cmp1-fleet-r1"
        bad = [
            '{"cost_usd": NaN}', rec(p, "task-a", agent_seconds=float("inf")),
            rec(p, "task-a", cost_usd=-1.0), rec(p, "task-a", reward=True),
            rec(p, "task-a", anomalies=False), rec(p, "task-a", trial="/etc/passwd"),
            rec(p, "task-a", trial="https://evil.example/x"),
            rec(p, "task-a", exception="Bad\x1b[31mError\n"), rec(p, "evil-task"),
            rec(p, "task-a", dispatches={f"evil{i}": 1 for i in range(1000)}),
            [1, 2, 3], "not json SECRETMARKER", rec(p, "task-a", extra="SECRETMARKER"),
            dict(rec(p, "task-a"), job="cmp1-plain-r2"), rec(p, "task-a", model="evil-model"),
            rec(p, "task-a", cost_usd=1e9), rec(p, "task-a", round=1.0),
        ]
        lines = [json.dumps(b, allow_nan=True) if not isinstance(b, str) else b for b in bad]
        jobs = [self.job(p, lines + [rec(p, "task-a"), rec(p, "task-b")]),
                self.job(f, [rec(f, t) for t in TASKS] + [rec(f, "task-b")])]
        self.assertEqual(self.run_main(*jobs), 0)
        meta = self.meta()
        self.assertEqual(meta["dropped_records"], len(bad) + 2)  # + the fleet task-b pair
        self.assertEqual(meta["records_per_job"], {f: 1, p: 2})
        self.assertEqual(meta["rounds_completed"], [])
        text = "".join(self.read(n) for n in ("records.jsonl", "meta.json", "summary.md"))
        for needle in ("NaN", "Infinity", "/etc", "https", "evil", "\x1b", "SECRETMARKER",
                       "extra", "-1.0", "1000000000"):
            self.assertNotIn(needle, text)
        self.assertTrue(text.isascii())

    def test_refusals(self):
        jobs = self.round_jobs(1)
        os.symlink(self.root, os.path.join(self.root, "link"))
        self.assertEqual(self.run_main(*jobs, out=os.path.join(self.root, "link", "o")), 2)
        self.assertEqual(self.run_main(*jobs, out=os.path.join(self.root, "link")), 2)
        os.makedirs(os.path.join(self.root, "full"))
        open(os.path.join(self.root, "full", "x"), "w").close()
        self.assertEqual(self.run_main(*jobs, out=os.path.join(self.root, "full")), 2)
        self.assertEqual(self.run_main(*jobs, self.job("cmp1-other-r1", [])), 2)
        self.assertEqual(self.run_main(jobs[0], jobs[0]), 2)
        linked = self.job("cmp1-plain-r3")
        os.symlink(os.path.join(jobs[0], "records.jsonl"), os.path.join(linked, "records.jsonl"))
        self.assertEqual(self.run_main(linked), 2)
        self.list = self.make_list(["../escape"], "bad.json")
        self.assertEqual(self.run_main(*jobs), 2)
        self.assertFalse(os.path.lexists(self.out))
        self.assertFalse(os.path.lexists(os.path.join(self.root, "link", "o")))

    def test_time_outlier(self):
        p1, f1 = "cmp1-plain-r1", "cmp1-fleet-r1"
        jobs = [self.job(p1, [rec(p1, t) for t in TASKS]),
                self.job(f1, [rec(f1, "task-a", agent_seconds=50000), rec(f1, "task-b")])]
        jobs += self.round_jobs(2)
        self.assertEqual(self.run_main(*jobs), 0)
        meta = self.meta()
        self.assertEqual(meta["time_outliers"], {"plain": 0, "fleet": 1})
        self.assertAlmostEqual(meta["spend_all_jobs"], 4 * 0.1 + 4 * 0.5)
        text = self.read("summary.md")
        self.assertIn("Agent time: 1 trial(s) (plain 0, fleet 1) recorded more than 3000 "
                      "seconds", text)
        # fleet total n/a, ratio n/a, median over both tasks (task-a keeps its round-2 trial)
        self.assertIn("| Agent time (seconds) | 400.0 | n/a | n/a | 1.000 | 2 |", text)
        self.assertIn(json.dumps(rec(f1, "task-a", agent_seconds=50000), sort_keys=True),
                      self.read("records.jsonl").splitlines())
        self.assertIn("Small numbers: 2 tasks and 2 attempts per task", text)
        self.assertIn("Small numbers: 2 tasks and 2 attempts per task confirm only large "
                      "differences.", meta["limits"])

    def test_no_verdict_words(self):
        jobs = self.round_jobs(1, (1, 0), (0, 1)) + self.round_jobs(2, (1, 1), (1, 0))
        self.assertEqual(self.run_main(*jobs), 0)
        text = self.read("summary.md").lower()
        for word in ("better", "worse", "wins", "win", "passes", "fails", "verdict"):
            self.assertIsNone(re.search(rf"\b{word}\b", text), word)
        self.assertIn("## Limits", self.read("summary.md"))


if __name__ == "__main__":
    unittest.main()
