#!/usr/bin/env python3
"""Compare the plain and fleet arms from their records.jsonl files.

Usage: eval/summary.py <list.json> <out-dir> <job-folder>...

Records come from a less trusted source: each is validated again with the writer's rules
(eval/run_arm.sh, record_of) and dropped when any field is outside them. Only validated
fields and task names from the list reach the output files. Exit 0 written, 2 refused.
"""
import json
import math
import os
import random
import re
import stat
import statistics
import sys
from datetime import date

# Fixed lists copied from eval/run_arm.sh (records writer).
EXCEPTIONS = frozenset((
    "EnvironmentStartTimeoutError", "SandboxBuildFailedError", "AgentSetupTimeoutError",
    "HealthcheckError", "SandboxLikelyOutOfMemoryError", "MemoryLimitExceededError",
    "SSHAccessError", "GKEExecStreamClosedError", "GKEPodNotFoundError", "RuntimeRequestError",
    "AddTestsDirError", "DownloadVerifierDirError", "ApiError", "ApiRateLimitError",
    "ApiInternalServerError", "ApiOverloadedError", "ApiConnectionClosedError",
    "ApiResponseStalledError", "UnknownApiError", "NetworkConnectionError", "ApiConnectionError",
    "ApiUsageLimitError", "RateLimitError", "InternalServerError", "ServiceUnavailableError",
    "APIConnectionError", "APIError", "Timeout", "UnsupportedModelParametersError",
    "BadRequestError", "UnprocessableEntityError", "UnsupportedParamsError",
    "APIResponseValidationError", "BudgetExceededError", "VerifierTimeoutError",
    "RewardFileNotFoundError", "RewardFileEmptyError", "VerifierOutputParseError",
    "AgentAuthenticationError", "ModelNotFoundError", "ApiProviderResourceNotFoundError",
    "AuthenticationError", "PermissionDeniedError", "NotFoundError",
    "ServiceOperationsUnsupportedError", "VercelScopeError", "DspyImportError",
    "MissingExtraError", "RegradeError", "CancelledError", "NonZeroAgentExitCodeError",
    "AgentTimeoutError", "ContextWindowExceededError", "OutputTokenExceededError",
    "ContextLengthExceededError", "OutputLengthExceededError", "AgentSafetyRefusalError",
    "ContentPolicyViolationError", "other"))
SUBAGENTS = frozenset(["conclave:" + a for a in (
    "dependency-analyzer", "diagnostician", "executor", "mech-executor", "plan-verifier",
    "researcher", "scout", "security-executor", "security-reviewer", "verifier")]
    + ["general-purpose", "Explore", "Plan", "statusline-setup", "claude-code-guide", "other"])
MODELS = frozenset(("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "other"))
NETWORKS = frozenset(("allowlist", "public", "no-network", "unknown"))
TOKEN_FIELDS = {"final_event_per_model": ("input", "output", "cache_read", "cache_creation"),
                "runner_main_session": ("input", "cache", "output")}
COUNTS = ("rate_limit_events", "auth_errors", "question_calls", "approver_lines", "anomalies")
KEYS = frozenset((
    "agent_seconds", "anomalies", "approver_lines", "arm", "auth_errors", "budget_capped",
    "cli_version", "cost_source", "cost_usd", "dispatches", "exception", "fleet_version",
    "is_error", "job", "model", "network", "plan_saved", "question_calls", "rate_limit_events",
    "reward", "round", "task", "timed_out", "tokens", "tokens_source", "trial", "trial_seconds"))
NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}")
JOB = re.compile(r"cmp1-(plain|fleet)-r([1-9])")
VERSION = re.compile(r"[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}")
FLEET = re.compile(r"[0-9a-f]{7,40}")
CAPS = {"plain": 1.00, "fleet": 2.50}  # the arms' CLI caps; a missing cost counts at cap + 1.00
ARMS = ("plain", "fleet")
MAX_BYTES = 20 * 1024 * 1024
SEED, RESAMPLES = 20261120, 10000
AGENT_LIMIT_SECONDS = 3000  # the agent time limit of every task in this task set
LIMITS = [
    "Not fresh: all tasks were created by 2026-05-12 and the models' training cutoff is "
    "June 2026, so the models may have seen the fixes.",
    "Small numbers: {tasks} tasks and {rounds} attempts per task confirm only large "
    "differences.",
    "Cost figures come from the run log, a file the container can write; a container that "
    "falsified it could understate spend.",
    "Token figures come from different sources: the final event's per-model usage "
    "(input, output, cache read, cache creation) or the runner's main-session figures "
    "(input including cache, plus output); the source of each arm is named.",
]


class Refused(Exception):
    pass


def charge(arm):
    return CAPS[arm] + 1.00


def one_of(value, allowed):
    return isinstance(value, str) and value in allowed


def is_int(value, top):
    return type(value) is int and 0 <= value <= top


def is_num(value, top):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= top


def opt(check, value, *args):
    return value is None or check(value, *args)


def matches(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def tokens_ok(tokens, source):
    if source == "none":
        return tokens is None
    fields = TOKEN_FIELDS.get(source) if isinstance(source, str) else None
    if fields is None or not isinstance(tokens, dict) or not tokens:
        return False
    if source == "runner_main_session" and len(tokens) != 1:
        return False
    return all(one_of(m, MODELS) and isinstance(u, dict) and set(u) == set(fields)
               and all(opt(is_int, u[f], 10**10) for f in fields) for m, u in tokens.items())


def valid(r, job, arm, rnd, tasks):
    """True when the record matches the writer's field table and its job folder."""
    try:
        if not isinstance(r, dict) or set(r) != KEYS:
            return False
        final = r["cost_source"] != "none"
        host = r["dispatches"] is not None
        d = r["dispatches"]
        return all((
            r["job"] == job, r["arm"] == arm, type(r["round"]) is int and r["round"] == rnd,
            one_of(r["task"], tasks), matches(r["trial"], NAME),
            r["reward"] is None or (type(r["reward"]) is int and r["reward"] in (0, 1)),
            r["exception"] is None or one_of(r["exception"], EXCEPTIONS),
            r["timed_out"] is (r["exception"] == "AgentTimeoutError"),
            (r["cost_source"] == "final_event" and is_num(r["cost_usd"], charge(arm)))
            or (r["cost_usd"] is None and one_of(r["cost_source"], ("none", "out_of_range"))),
            tokens_ok(r["tokens"], r["tokens_source"]),
            opt(is_num, r["agent_seconds"], 1e6), opt(is_num, r["trial_seconds"], 1e6),
            r["is_error"] is None or (final and type(r["is_error"]) is bool),
            type(r["budget_capped"]) is bool if final else r["budget_capped"] is None,
            all(opt(is_int, r[k], 10**6) for k in COUNTS),
            d is None or (isinstance(d, dict) and all(
                one_of(k, SUBAGENTS) and opt(is_int, v, 10**6) for k, v in d.items())),
            type(r["plan_saved"]) is bool,
            host or (r["plan_saved"] is False and all(r[k] is None for k in COUNTS)),
            one_of(r["model"], MODELS), opt(matches, r["cli_version"], VERSION),
            opt(matches, r["fleet_version"], FLEET), one_of(r["network"], NETWORKS),
        ))
    except Exception:
        return False


def load_list(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        version, tasks = data["version"], data["tasks"]
    except Exception:
        raise Refused("task list is not readable JSON with version and tasks")
    if not matches(version, NAME) or not isinstance(tasks, list) or not tasks \
            or not all(matches(t, NAME) for t in tasks) or len(set(tasks)) != len(tasks):
        raise Refused("task list is empty or has a version or task name outside the pattern, or a repeat")
    return version, tasks


def check_out_dir(path):
    path = os.path.abspath(path)
    p = path
    while True:
        if os.path.islink(p):
            raise Refused("out-dir is, or is under, a symbolic link")
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    if os.path.lexists(path) and (not os.path.isdir(path) or os.listdir(path)):
        raise Refused("out-dir exists and is not an empty folder")
    return path


def read_lines(folder):
    """The lines of folder/records.jsonl, or None when there is no such file."""
    try:
        fd = os.open(os.path.join(folder, "records.jsonl"),
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        raise Refused("a records.jsonl is a link or not readable")
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise Refused("a records.jsonl is not a regular file")
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise Refused("a records.jsonl is larger than 20 MB")
    return [line for line in data.split(b"\n") if line.strip()]


def no_constant(name):
    raise ValueError("NaN or infinity")


def load_jobs(folders, tasks):
    """Valid records, dropped count and per-job info: {job: (arm, round, valid records)}."""
    jobs, seen = {}, set()
    for folder in folders:
        job = os.path.basename(os.path.normpath(folder))
        m = JOB.fullmatch(job)
        if not m:
            raise Refused("a job folder name is not cmp1-<plain|fleet>-r<1-9>")
        if job in seen:
            raise Refused("a job folder is given twice")
        seen.add(job)
        try:
            mode = os.lstat(folder).st_mode
        except OSError:
            raise Refused("a job folder does not exist")
        if not stat.S_ISDIR(mode):
            raise Refused("a job folder is a link or not a folder")
        jobs[job] = (m.group(1), int(m.group(2)), read_lines(folder))
    dropped, out = 0, {}
    for job, (arm, rnd, lines) in jobs.items():
        by_task = {}
        for line in lines or []:
            try:
                r = json.loads(line, parse_constant=no_constant)
            except Exception:
                r = None
            if valid(r, job, arm, rnd, tasks):
                by_task.setdefault(r["task"], []).append(r)
            else:
                dropped += 1
        kept = []
        for group in by_task.values():
            if len(group) == 1:
                kept += group
            else:
                dropped += len(group)
        out[job] = (arm, rnd, kept)
    return out, dropped


def job_spend(arm, records, tasks):
    have = {r["task"] for r in records}
    paid = sum(charge(arm) if r["cost_usd"] is None else r["cost_usd"] for r in records)
    return paid + charge(arm) * sum(t not in have for t in tasks)


def complete_rounds(jobs, tasks):
    full = {(arm, rnd) for arm, rnd, recs in jobs.values()
            if sorted(r["task"] for r in recs) == sorted(tasks)}
    return sorted({rnd for _, rnd in full if ("plain", rnd) in full and ("fleet", rnd) in full})


def approver_error(r):
    return r["arm"] == "fleet" and r["question_calls"] != r["approver_lines"]


def token_total(r):
    if r["tokens"] is None:
        return None
    keys = ("input", "output") if r["tokens_source"] == "runner_main_session" else None
    values = [v for u in r["tokens"].values() for k, v in u.items() if keys is None or k in keys]
    return None if any(v is None for v in values) else sum(values)


def agent_time(r):
    """agent_seconds, or None when missing or above the agent limit (a wall-clock artefact)."""
    s = r["agent_seconds"]
    return None if s is None or s > AGENT_LIMIT_SECONDS else s


def is_outlier(r):
    return r["agent_seconds"] is not None and r["agent_seconds"] > AGENT_LIMIT_SECONDS


def limits(tasks, rounds):
    small = (LIMITS[1].format(tasks=tasks, rounds=rounds) if rounds else
             f"Small numbers: {tasks} tasks and no complete round confirm only large differences.")
    return [LIMITS[0], small] + LIMITS[2:]


def ratio(a, b):
    return None if a is None or b is None or b == 0 else a / b


def bootstrap(diffs):
    """Mean of diffs and the 2.5th and 97.5th percentiles of resampled means (nearest rank)."""
    rng, k = random.Random(SEED), len(diffs)
    means = sorted(math.fsum(rng.choices(diffs, k=k)) / k for _ in range(RESAMPLES))
    return math.fsum(diffs) / k, means[249], means[9749]


def figures(records, tasks):
    """Every figure of the table, from the records of complete rounds."""
    arm_recs = {a: [r for r in records if r["arm"] == a] for a in ARMS}
    scored = {a: [r for r in arm_recs[a] if not approver_error(r)] for a in ARMS}
    f = {"arms": {}, "tasks": [], "no_fleet_trial": 0, "ratios": {}}
    for a in ARMS:
        no_reward = {}
        for r in arm_recs[a]:
            if r["reward"] is None:
                key = r["exception"] or "none"
                no_reward[key] = no_reward.get(key, 0) + 1
        f["arms"][a] = {
            "trials": len(scored[a]), "resolved": sum(r["reward"] == 1 for r in scored[a]),
            "approver_errors": sum(map(approver_error, arm_recs[a])),
            "timeouts": sum(r["timed_out"] for r in arm_recs[a]),
            "budget_capped": sum(r["budget_capped"] is True for r in arm_recs[a]),
            "no_reward": no_reward,
            "token_sources": sorted({r["tokens_source"] for r in arm_recs[a]}),
        }
    diffs = []
    for t in tasks:
        row = {"task": t}
        for a in ARMS:
            got = [r for r in scored[a] if r["task"] == t]
            row[a] = (sum(r["reward"] == 1 for r in got), len(got))
        p, q = (ratio(*row[a]) for a in ARMS)
        row["diff"] = None if p is None or q is None else q - p
        if row["fleet"][1] == 0:
            f["no_fleet_trial"] += 1
        elif row["diff"] is not None:
            diffs.append(row["diff"])
        f["tasks"].append(row)
    f["used_tasks"] = len(diffs)
    f["bootstrap"] = bootstrap(diffs) if diffs else (None, None, None)
    for name, get in (("cost_usd", lambda r: r["cost_usd"]),
                      ("agent_seconds", agent_time),
                      ("tokens", token_total)):
        totals, per_task = {}, []
        for a in ARMS:
            values = [get(r) for r in arm_recs[a]]
            totals[a] = None if not values or None in values else sum(values)
        for t in tasks:
            sums = []
            for a in ARMS:
                values = [get(r) for r in arm_recs[a] if r["task"] == t]
                if name == "agent_seconds":  # mean over the trials with a usable time
                    values = [v for v in values if v is not None]
                sums.append(None if not values or None in values else sum(values) / len(values))
            per_task.append(ratio(sums[1], sums[0]))
        defined = [x for x in per_task if x is not None]
        f["ratios"][name] = {"plain": totals["plain"], "fleet": totals["fleet"],
                             "total": ratio(totals["fleet"], totals["plain"]),
                             "median": statistics.median(defined) if defined else None,
                             "tasks": len(defined)}
    fleet = arm_recs["fleet"]
    f["plan_saved"] = (sum(r["plan_saved"] for r in fleet), len(fleet))
    hosted = [r["dispatches"] for r in fleet if r["dispatches"] is not None]
    kinds = sorted({k for d in hosted for k in d})
    f["dispatch_trials"] = len(hosted)
    f["dispatches"] = {k: (None if any(d.get(k, 0) is None for d in hosted)
                           else sum(d.get(k, 0) for d in hosted) / len(hosted)) for k in kinds}
    return f


def agreed(records, field):
    values = {r[field] for r in records}
    return None if not values else values.pop() if len(values) == 1 else "mixed"


def num(value, digits=3):
    return "n/a" if value is None else f"{value:.{digits}f}"


def render(meta, f):
    out = [f"# Comparison summary, task list `{meta['list_version']}`", ""]
    for label, key in (("Model", "model"), ("CLI version", "cli_version"),
                       ("Fleet version", "fleet_version")):
        value = meta[key]
        note = " (the valid records disagree on this field)" if value == "mixed" else ""
        out.append(f"- {label}: `{value if value is not None else 'n/a'}`{note}")
    out += [f"- Date: {meta['date']}",
            f"- Listed tasks: {meta['listed_tasks']}",
            f"- Complete rounds: {len(meta['rounds_completed'])} "
            f"({', '.join(map(str, meta['rounds_completed'])) or 'none'})",
            f"- Records dropped by validation: {meta['dropped_records']}",
            f"- Spend, all given jobs: {meta['spend_all_jobs']:.2f} dollars",
            f"- Spend, complete rounds: {meta['spend_complete_rounds']:.2f} dollars",
            "- Spend counts each trial at its cost, and a trial without a cost or without a "
            "record at its arm's CLI cap plus 1.00 (plain 2.00, fleet 3.50).", ""]
    if f is None:
        out += ["There is no complete round: no comparison table is produced.", ""]
    else:
        a = f["arms"]
        out += ["## Resolve rate per arm", "",
                "Approver errors (fleet trials whose question calls differ from their "
                "approver lines) are left out of the rates.", "",
                "| Arm | Trials counted | Resolved | Resolve rate | Approver errors left out |",
                "|---|---|---|---|---|"]
        for arm in ARMS:
            out.append(f"| {arm} | {a[arm]['trials']} | {a[arm]['resolved']} | "
                       f"{num(ratio(a[arm]['resolved'], a[arm]['trials']))} | "
                       f"{a[arm]['approver_errors']} |")
        out += ["", "## Per task", "",
                "| Task | Plain resolved / trials | Fleet resolved / trials | Plain fraction | "
                "Fleet fraction | Difference (fleet minus plain) |", "|---|---|---|---|---|---|"]
        for row in f["tasks"]:
            (pp, pn), (fp, fn) = row["plain"], row["fleet"]
            out.append(f"| `{row['task']}` | {pp} / {pn} | {fp} / {fn} | {num(ratio(pp, pn))} | "
                       f"{num(ratio(fp, fn))} | {num(row['diff'])} |")
        mean, lo, hi = f["bootstrap"]
        out += ["", "## Mean per-task difference", "",
                f"Fleet pass fraction minus plain pass fraction, averaged over tasks; interval "
                f"from {RESAMPLES} bootstrap resamples of tasks (seed {SEED}), 2.5th and 97.5th "
                f"percentiles.", "",
                "| Tasks used | Tasks left out (no fleet trial) | Mean | 2.5th percentile | "
                "97.5th percentile |", "|---|---|---|---|---|",
                f"| {f['used_tasks']} | {f['no_fleet_trial']} | {num(mean)} | {num(lo)} | "
                f"{num(hi)} |",
                "", "## Cost, time and tokens", "",
                "Ratios are fleet over plain. A total is n/a when any trial of the arm lacks the "
                "figure; the median is over tasks where both arms have the figure for every "
                "trial (per-task mean per arm); for agent time, the per-task mean is over the "
                "trials with a usable time and a task needs one such trial per arm.", "",
                "| Figure | Plain total | Fleet total | Ratio of totals | Median per-task ratio | "
                "Tasks in median |", "|---|---|---|---|---|---|"]
        for name, label, digits in (("cost_usd", "Cost (dollars, run log)", 4),
                                    ("agent_seconds", "Agent time (seconds)", 1),
                                    ("tokens", "Tokens", 0)):
            x = f["ratios"][name]
            out.append(f"| {label} | {num(x['plain'], digits)} | {num(x['fleet'], digits)} | "
                       f"{num(x['total'])} | {num(x['median'])} | {x['tasks']} |")
        o = meta["time_outliers"]
        if o["plain"] or o["fleet"]:
            out += ["", f"Agent time: {o['plain'] + o['fleet']} trial(s) (plain {o['plain']}, "
                    f"fleet {o['fleet']}) recorded more than {AGENT_LIMIT_SECONDS} seconds, the "
                    "tasks' agent limit; such figures are wall-clock artefacts (for example a "
                    "suspended machine) and are left out of the time figures."]
        sources = {arm: ", ".join(f"`{s}`" for s in a[arm]["token_sources"]) or "n/a"
                   for arm in ARMS}
        out += ["", f"Token sources: plain {sources['plain']}; fleet {sources['fleet']}. "
                "Per-model figures sum input, output, cache read and cache creation; runner "
                "figures sum input (which includes cache) and output."]
        if a["plain"]["token_sources"] != a["fleet"]["token_sources"]:
            out.append("The two arms use different token sources, so the token ratio compares "
                       "figures counted differently.")
        out += ["", "## Counts", "",
                "| Arm | Timeouts | Budget-capped | Without a reward | Approver errors |",
                "|---|---|---|---|---|"]
        for arm in ARMS:
            out.append(f"| {arm} | {a[arm]['timeouts']} | {a[arm]['budget_capped']} | "
                       f"{sum(a[arm]['no_reward'].values())} | {a[arm]['approver_errors']} |")
        out += ["", "Trials without a reward, by exception type:", "",
                "| Arm | Exception type | Trials |", "|---|---|---|"]
        rows = [f"| {arm} | `{k}` | {v} |" for arm in ARMS
                for k, v in sorted(a[arm]["no_reward"].items())]
        out += rows or ["| n/a | n/a | 0 |"]
        saved, total = f["plan_saved"]
        out += ["", "## Fleet behaviour", "",
                f"- Fleet trials with a saved plan: {saved} of {total} "
                f"({num(ratio(saved, total))})",
                f"- Fleet trials with a host-only copy (dispatch counts): {f['dispatch_trials']}",
                "", "| Subagent type | Mean dispatches per trial |", "|---|---|"]
        out += [f"| `{k}` | {num(v, 2)} |" for k, v in f["dispatches"].items()] \
            or ["| n/a | n/a |"]
        out.append("")
    out += ["## Limits", ""] + [f"- {text}" for text in meta["limits"]] + [""]
    return "\n".join(out)


def write(path, text):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    with os.fdopen(os.open(path, flags, 0o644), "w", encoding="ascii") as handle:
        handle.write(text)


def main(argv):
    if len(argv) < 3:
        print("usage: summary.py <list.json> <out-dir> <job-folder>...", file=sys.stderr)
        return 2
    try:
        version, tasks = load_list(argv[0])
        out_dir = check_out_dir(argv[1])
        jobs, dropped = load_jobs(argv[2:], tasks)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    records = sorted((r for _, _, recs in jobs.values() for r in recs),
                     key=lambda r: (r["round"], r["arm"], r["task"]))
    rounds = complete_rounds(jobs, tasks)
    used = [r for r in records if r["round"] in rounds]
    fleet = [r for r in records if r["arm"] == "fleet"]
    meta = {
        "list_version": version, "model": agreed(records, "model"),
        "cli_version": agreed(records, "cli_version"),
        "fleet_version": agreed(fleet, "fleet_version"),
        "date": date.today().isoformat(), "rounds_completed": rounds,
        "spend_all_jobs": round(sum(job_spend(arm, recs, tasks)
                                    for arm, _, recs in jobs.values()), 4),
        "spend_complete_rounds": round(sum(job_spend(arm, recs, tasks)
                                           for arm, rnd, recs in jobs.values()
                                           if rnd in rounds), 4),
        "dropped_records": dropped, "listed_tasks": len(tasks),
        "records_per_job": {job: len(recs) for job, (_, _, recs) in sorted(jobs.items())},
        "limits": limits(len(tasks), len(rounds)),
        "time_outliers": {a: sum(is_outlier(r) for r in used if r["arm"] == a) for a in ARMS},
    }
    os.makedirs(out_dir, exist_ok=True)
    write(os.path.join(out_dir, "records.jsonl"), "".join(
        json.dumps(r, allow_nan=False, sort_keys=True) + "\n" for r in records))
    write(os.path.join(out_dir, "meta.json"),
          json.dumps(meta, allow_nan=False, sort_keys=True, indent=2) + "\n")
    write(os.path.join(out_dir, "summary.md"), render(meta, figures(used, tasks) if rounds else None))
    print(f"written: {len(records)} records, {dropped} dropped, {len(rounds)} complete rounds")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
