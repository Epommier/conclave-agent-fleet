"""Check, summarise and record one eval job folder; called by eval/scripts/run_arm.sh.

Usage: python3 -I -B eval/src/report.py <scan|records|summary> <job-dir> <token-file>
           [<job> <arm> <budget>]

The token file must be a regular file (no symlink, FIFO or device), owned by the current user,
mode 600, with one link, holding one line: sk-ant-oat followed by A-Z a-z 0-9 _ - only. The
token is used only to redact container text and to refuse token-like output.

Modes:
  scan     lists trial-folder entries that are not a regular file or folder, setuid or setgid,
           or owned by another user. Exit 0 nothing found (or no job folder), 4 anomalies found
           or the scan failed.
  records  writes <job-dir>/records.jsonl, one numeric record per trial (needs <job> <arm>
           <budget>). Exit 0 written, 3 not written (token-like text in a record), 1 any other
           failure.
  summary  (any other mode word) prints a sanitised summary of each trial. Exit 0.
Every mode exits 1 when the token file is refused.
"""
import json, math, os, re, stat, sys, tomllib, unicodedata
from datetime import datetime

sys.path.append(os.path.dirname(os.path.realpath(__file__)))
import fixed_lists

MAX_READ = 50 * 1024 * 1024
DROP = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
NOTICE = "are ignored because the effective network policy is public"


def clean(text, limit=200):
    """Container text: drop control and escape characters, redact the token, cut."""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    text = "".join(" " if c in "\n\t" else c for c in text
                   if c in "\n\t" or unicodedata.category(c) not in DROP)
    text = text.replace(token, "[TOKEN]")
    return text if len(text) <= limit else text[:limit] + "...[cut]"


def num(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return repr(value)
    return "none" if value is None else "invalid"


def flag(value):
    for known, label in ((True, "true"), (False, "false"), (None, "none")):
        if value is known:
            return label
    return "invalid"


def read(path):
    """A regular file under 50 MB, without following symlinks or blocking; else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_READ:
            return None
        with os.fdopen(os.dup(fd), "rb") as handle:
            return handle.read(MAX_READ).decode("utf-8", "replace")
    finally:
        os.close(fd)


def load_json(path):
    try:
        data = json.loads(read(path) or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def trials():
    try:
        names = sorted(os.listdir(job_dir))
    except OSError:
        return []
    return [n for n in names if stat.S_ISDIR(os.lstat(os.path.join(job_dir, n)).st_mode)]


def scan():
    if not os.path.isdir(job_dir) or os.path.islink(job_dir):
        print("anomaly scan: no job folder")
        return 0
    found = []
    uid = os.getuid()

    def unreadable(err):
        found.append(f"{os.path.relpath(err.filename, job_dir)}: unreadable")

    for top, dirs, files in os.walk(job_dir, onerror=unreadable):
        for name in dirs + files:
            path = os.path.join(top, name)
            rel = os.path.relpath(path, job_dir)
            try:
                st = os.lstat(path)
            except OSError:
                found.append(f"{rel}: unreadable")
                continue
            reasons = []
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                reasons.append("not a regular file or folder")
            if st.st_mode & (stat.S_ISUID | stat.S_ISGID):
                reasons.append("setuid or setgid")
            if st.st_uid != uid:
                reasons.append(f"owned by uid {st.st_uid}")
            if reasons:
                found.append(f"{rel}: {', '.join(reasons)}")
    for line in found[:20]:
        print(f"ANOMALY {clean(line)}")
    if len(found) > 20:
        print(f"anomalies not shown: {len(found) - 20}")
    print(f"anomaly scan: {len(found)} found")
    return 4 if found else 0


def events(text):
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                out.append(event)
    return out


def blocks(event):
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def result_text(content):
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return content if isinstance(content, str) else ""


def error_kinds(event):
    words = [event.get(k) for k in ("error", "subtype")]
    if event.get("type") == "result" and event.get("is_error") is True:
        words.append(event.get("result"))
    text = " ".join(w for w in words if isinstance(w, str)).lower()
    status = event.get("error_status")
    rate = status == 429 or any(s in text for s in ("rate_limit", "rate limit", "usage limit"))
    auth = status in (401, 403) or any(
        s in text for s in ("authentication", "invalid api key", "/login", "oauth"))
    return rate, auth


def network(trial_dir):
    config = load_json(os.path.join(trial_dir, "config.json")) or {}
    task = config.get("task") if isinstance(config.get("task"), dict) else {}
    path = task.get("path")
    if not isinstance(path, str):
        return "unknown (no task path in config.json)"
    try:
        agent = tomllib.loads(read(os.path.join(path, "task.toml")) or "?").get("agent", {})
    except tomllib.TOMLDecodeError:
        return f"unknown (no readable task.toml under {clean(path)})"
    mode = agent.get("network_mode")
    hosts = agent.get("allowed_hosts")
    declared = f"{clean(mode)} {clean(hosts)}" if mode else "public (not declared)"
    return f"{declared} (task {clean(path)})"


def summary_of(trial):
    tdir = os.path.join(job_dir, trial)
    host = os.path.join(tdir, "agent-host")
    print(f"\n== trial {clean(trial)}")
    result = load_json(os.path.join(tdir, "result.json")) or {}
    verifier = result.get("verifier_result") or {}
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    if isinstance(rewards, dict) and rewards:
        print("reward: " + ", ".join(f"{clean(k)}={num(v)}" for k, v in rewards.items()))
    else:
        print("reward: none")
    exc = result.get("exception_info")
    print(f"exception: {clean(exc.get('exception_type')) if isinstance(exc, dict) else 'none'}")
    timing = result.get("agent_execution") or {}
    if isinstance(timing, dict):
        print(f"agent phase: {clean(timing.get('started_at'))} to {clean(timing.get('finished_at'))}")

    if not os.path.isdir(host) or os.path.islink(host):
        print("host-only copy: missing (the agent folder is not read)")
        return
    evs = events(read(os.path.join(host, "claude-code.txt")))
    final = next((e for e in reversed(evs) if e.get("type") == "result"), None)
    init = next((e for e in evs if e.get("type") == "system" and e.get("subtype") == "init"), None)
    if final is None:
        print("cost_usd: none (no final result event)")
        print("duration_ms: none  turns: none  is_error: none")
    else:
        print(f"cost_usd: {num(final.get('total_cost_usd'))} (final result event)")
        print(f"duration_ms: {num(final.get('duration_ms'))}  turns: {num(final.get('num_turns'))}"
              f"  is_error: {flag(final.get('is_error'))}")
    rate = sum(error_kinds(e)[0] for e in evs)
    auth = sum(error_kinds(e)[1] for e in evs)
    print(f"rate-limit errors: {rate}  authentication errors: {auth}")

    usage = final.get("modelUsage") if final else None
    if isinstance(usage, dict) and len(usage) > 1:
        print("tokens (source: final result event per-model usage, includes subagents):")
        for model, u in usage.items():
            u = u if isinstance(u, dict) else {}
            print(f"  {clean(model)}: input={num(u.get('inputTokens'))} output={num(u.get('outputTokens'))}"
                  f" cache_read={num(u.get('cacheReadInputTokens'))}"
                  f" cache_creation={num(u.get('cacheCreationInputTokens'))} cost_usd={num(u.get('costUSD'))}")
    else:
        agent = result.get("agent_result") or {}
        agent = agent if isinstance(agent, dict) else {}
        print(f"tokens (source: runner, main-session only): input={num(agent.get('n_input_tokens'))}"
              f" cache={num(agent.get('n_cache_tokens'))} output={num(agent.get('n_output_tokens'))}")

    print(f"agent-phase network declared: {network(tdir)}")
    logs = (read(os.path.join(tdir, "trial.log")) or "") + (read(os.path.join(job_dir, "job.log")) or "")
    print(f"host-restriction ignored notice: {'yes' if NOTICE in logs else 'no'}")

    if init is None:
        print("init event: none")
    else:
        def listed(key):
            items = init.get(key)
            return [i for i in items if isinstance(i, str)] if isinstance(items, list) else []
        print(f"init: question tool listed: {'yes' if 'AskUserQuestion' in listed('tools') else 'no'}"
              f"  conclave skills: {sum(s.startswith('conclave:') for s in listed('slash_commands'))}"
              f"  conclave agents: {sum(a.startswith('conclave:') for a in listed('agents'))}")

    calls, outcomes = [], {}
    for event in evs:
        for b in blocks(event):
            if b.get("type") == "tool_use":
                calls.append(b)
            elif b.get("type") == "tool_result":
                outcomes[b.get("tool_use_id")] = b
    questions = sum(c.get("name") == "AskUserQuestion" for c in calls)
    dispatched = {}
    for c in calls:
        if c.get("name") in ("Agent", "Task"):
            kind = (c.get("input") or {}).get("subagent_type") if isinstance(c.get("input"), dict) else None
            dispatched[clean(kind)] = dispatched.get(clean(kind), 0) + 1
    print(f"question-tool calls: {questions}  subagent dispatches: {sum(dispatched.values())}"
          + (" (" + ", ".join(f"{k}: {v}" for k, v in sorted(dispatched.items())) + ")" if dispatched else ""))

    for log in ("approver.jsonl", "redirect.jsonl"):
        lines = (read(os.path.join(host, log)) or "").splitlines()
        print(f"{log}: {len(lines)} line(s)")
        for line in lines:
            print(f"  {clean(line)}")
    plans = os.path.join(host, "plans")
    names = sorted(os.listdir(plans)) if os.path.isdir(plans) and not os.path.islink(plans) else []
    print("plans/: " + (", ".join(clean(n) for n in names) if names else "(none)"))
    guard = (read(os.path.join(host, "ANOMALIES.txt")) or "").splitlines()
    print(f"guard anomalies: {len(guard)}")
    for line in guard:
        print(f"  {clean(line)}")
    print(f"final result text: {clean(final.get('result'), 600) if final else 'none'}")

    if probe:
        print("tool calls:")
        for i, c in enumerate(calls, 1):
            outcome = outcomes.get(c.get("id")) or {}
            print(f"  {i}. {clean(c.get('name'))} error={flag(outcome.get('is_error', False))}")
            if c.get("name") == "Bash":
                command = (c.get("input") or {}).get("command") if isinstance(c.get("input"), dict) else None
                print(f"     command: {clean(command)}")
                print(f"     output: {clean(result_text(outcome.get('content')))}")


# Records: numbers, booleans, null, and strings from these fixed lists or strict patterns only.
EXCEPTIONS, SUBAGENTS = fixed_lists.EXCEPTIONS, fixed_lists.SUBAGENTS
MODELS, NAME = fixed_lists.MODELS, fixed_lists.NAME
USAGE_KEYS = {"inputTokens": "input", "outputTokens": "output",
              "cacheReadInputTokens": "cache_read", "cacheCreationInputTokens": "cache_creation"}
RUNNER_KEYS = {"n_input_tokens": "input", "n_cache_tokens": "cache", "n_output_tokens": "output"}


def disguised(text):
    """True when text holds 12 characters in a row of the token, forwards or reversed."""
    w = min(12, len(token))
    return any(t[i:i + w] in text for t in (token, token[::-1]) for i in range(len(t) - w + 1))


def pick(value, allowed):
    return value if isinstance(value, str) and value in allowed else "other"


def name(value):
    ok = isinstance(value, str) and NAME.fullmatch(value) and not disguised(value)
    return value if ok else "invalid"


def count(value, top):
    ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= top
    return value if ok else None


def dig(data, *keys):
    for key in keys:
        data = data.get(key) if isinstance(data, dict) else None
    return data


def seconds(timing):
    try:
        value = (datetime.fromisoformat(timing["finished_at"])
                 - datetime.fromisoformat(timing["started_at"])).total_seconds()
    except Exception:
        return None
    return round(value, 3) if 0 <= value <= 1e6 else None


def tokens_of(final, result, model):
    usage = final.get("modelUsage") if final else None
    if isinstance(usage, dict) and len(usage) > 1:
        source, keys, items = "final_event_per_model", USAGE_KEYS, usage.items()
    else:
        agent = result.get("agent_result")
        if not (isinstance(agent, dict) and any(agent.get(k) is not None for k in RUNNER_KEYS)):
            return None, "none"
        source, keys, items = "runner_main_session", RUNNER_KEYS, [(model, agent)]
    out = {}
    for key, u in items:
        u = u if isinstance(u, dict) else {}
        entry = out.setdefault(pick(key, MODELS), dict.fromkeys(keys.values(), 0))
        for src, dst in keys.items():
            v = count(u.get(src), 10**10)
            entry[dst] = None if v is None or entry[dst] is None else count(entry[dst] + v, 10**10)
    return out, source


def network_mode(path):
    try:
        agent = tomllib.loads(read(os.path.join(path, "task.toml")) or "?").get("agent", {})
    except (TypeError, ValueError):
        return "unknown"
    mode = agent.get("network_mode", "public") if isinstance(agent, dict) else None
    return mode if mode in fixed_lists.NETWORKS else "unknown"


def record_of(trial, job, arm, cap):
    tdir = os.path.join(job_dir, trial)
    host = os.path.join(tdir, "agent-host")
    result = load_json(os.path.join(tdir, "result.json")) or {}
    config = load_json(os.path.join(tdir, "config.json")) or {}
    path = dig(config, "task", "path")
    kwargs = dig(config, "agent", "kwargs")
    model_name = dig(config, "agent", "model_name")
    model = next((m for m in MODELS if model_name in (m, "anthropic/" + m)), "other")
    version = dig(kwargs, "version")
    plugin = dig(kwargs, "plugin_src")
    plugin = plugin.rstrip("/").rsplit("/", 1)[-1] if isinstance(plugin, str) else None
    reward = dig(result, "verifier_result", "rewards", "reward")
    exc = result.get("exception_info")
    exc = pick(exc.get("exception_type"), EXCEPTIONS) if isinstance(exc, dict) else None
    host_ok = os.path.isdir(host) and not os.path.islink(host)
    evs = events(read(os.path.join(host, "claude-code.txt"))) if host_ok else []
    final = next((e for e in reversed(evs) if e.get("type") == "result"), None)
    cost = final.get("total_cost_usd") if final else None
    cost_ok = isinstance(cost, (int, float)) and not isinstance(cost, bool) and 0 <= cost <= cap
    tokens, tokens_source = tokens_of(final, result, model)
    calls = [b for e in evs for b in blocks(e) if b.get("type") == "tool_use"]
    dispatches = {}
    for c in calls:
        if c.get("name") in ("Agent", "Task"):
            key = pick(dig(c, "input", "subagent_type"), SUBAGENTS)
            dispatches[key] = dispatches.get(key, 0) + 1
    plans = os.path.join(host, "plans")
    lines = lambda log: len((read(os.path.join(host, log)) or "").splitlines())
    n = lambda value: count(value, 10**6) if host_ok else None
    return {
        "job": name(job), "arm": arm if arm in fixed_lists.ARMS else None,
        "round": int(m.group(1)) if (m := re.fullmatch(r"[A-Za-z0-9._-]*-r([1-9])", job)) else None,
        "task": name(path.rstrip("/").rsplit("/", 1)[-1] if isinstance(path, str) else None),
        "trial": name(trial),
        "reward": int(reward) if isinstance(reward, (int, float)) and not isinstance(reward, bool)
        and reward in (0, 1) else None,
        "exception": exc, "timed_out": exc == "AgentTimeoutError",
        "cost_usd": float(cost) if cost_ok else None,
        "cost_source": "none" if final is None else "final_event" if cost_ok else "out_of_range",
        "tokens": tokens, "tokens_source": tokens_source,
        "agent_seconds": seconds(result.get("agent_execution")), "trial_seconds": seconds(result),
        "is_error": final.get("is_error") if final and isinstance(final.get("is_error"), bool) else None,
        "budget_capped": final.get("subtype") == "error_max_budget_usd" if final else None,
        "rate_limit_events": n(sum(error_kinds(e)[0] for e in evs)),
        "auth_errors": n(sum(error_kinds(e)[1] for e in evs)),
        "question_calls": n(sum(c.get("name") == "AskUserQuestion" for c in calls)),
        "approver_lines": n(lines("approver.jsonl")), "anomalies": n(lines("ANOMALIES.txt")),
        "dispatches": {k: count(v, 10**6) for k, v in dispatches.items()} if host_ok else None,
        "plan_saved": host_ok and os.path.isdir(plans) and not os.path.islink(plans) and any(
            stat.S_ISREG(os.lstat(os.path.join(plans, p)).st_mode) for p in os.listdir(plans)),
        "model": model,
        "cli_version": version if isinstance(version, str)
        and fixed_lists.VERSION.fullmatch(version) else None,
        "fleet_version": plugin if plugin and fixed_lists.FLEET.fullmatch(plugin)
        and not disguised(plugin) else None,
        "network": network_mode(path) if isinstance(path, str) else "unknown",
    }


def write_records(job, arm, cap):
    if not os.path.isdir(job_dir) or os.path.islink(job_dir):
        print("records: not written (no job folder)")
        return 1
    lines = []
    for trial in trials():
        try:
            lines.append(json.dumps(record_of(trial, job, arm, cap), allow_nan=False, sort_keys=True))
        except Exception as exc:
            print(f"records: no record for a trial ({type(exc).__name__})")
    text = "".join(line + "\n" for line in lines)
    if disguised(text):
        print("records: not written (token-like text in a record)")
        return 3
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    with os.fdopen(os.open(os.path.join(job_dir, "records.jsonl"), flags, 0o644), "w",
                   encoding="ascii") as handle:
        handle.write(text)
    return 0


def main():
    # Module-level names: the functions above read job_dir, probe and token as globals.
    global mode, job_dir, token_file, probe, token
    mode, job_dir, token_file = sys.argv[1:4]
    probe = os.environ.get("PROBE") == "1"
    # Same checks as run_arm.sh, re-made on the open file: no symlink, no blocking, regular,
    # ours, mode 600, one link, one token line.
    token = None
    try:
        fd = os.open(token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)
            if (stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
                    and stat.S_IMODE(st.st_mode) == 0o600 and st.st_nlink == 1
                    and st.st_size <= 4096):
                text = os.read(fd, 4096).decode("ascii")
                if re.fullmatch(r"sk-ant-oat[A-Za-z0-9_-]+\n?", text):
                    token = text.strip()
        finally:
            os.close(fd)
    except (OSError, ValueError):
        pass
    if token is None:
        sys.exit("report: token file refused")
    if not token:
        sys.exit("empty token")
    if mode == "scan":
        try:
            sys.exit(scan())
        except Exception as exc:
            print(f"anomaly scan failed: {type(exc).__name__}")
            sys.exit(4)
    if mode == "records":
        try:
            sys.exit(write_records(sys.argv[4], sys.argv[5], float(sys.argv[6]) + 1.00))
        except Exception as exc:
            print(f"records: not written ({type(exc).__name__})")
            sys.exit(1)
    for trial in trials():
        try:
            summary_of(trial)
        except Exception as exc:
            # The type only: a message could carry container text.
            print(f"summary failed for this trial: {type(exc).__name__}")
    if not trials():
        print("no trial folders")


if __name__ == "__main__":
    main()
