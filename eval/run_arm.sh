#!/usr/bin/env bash
# run_arm.sh - run one arm through the eval runner inside the task containers, signed in
# with the subscription OAuth token, then check the trial folders, search for the token and
# print a sanitised summary.
#
# Usage: eval/run_arm.sh <plain|fleet> <job-name> <tasks-dir> [extra runner arguments]
#   plain  conclave_agent:GuardedClaudeCode    fleet  conclave_agent:ConclaveClaudeCode
#   The tasks folder selects the agent-phase network (its task.toml files declare it).
# Env: MODEL (required), BUDGET (required, per-trial CLI cap in dollars), SKILL_PREFIX
#   (fleet only, optional; may be empty), PROBE=1 (adds every tool call to the summary),
#   TOKEN_FILE (default ~/.config/conclave-eval/oauth-token),
#   HARBOR_BIN (default eval/.venv/bin/harbor).
# Exit: 0 run completed, nothing found; 2 refused before running; 3 token found (or the
#   search could not read everything); 4 anomaly in a trial folder; otherwise the runner's
#   exit code. After 3 or 4, launch nothing more and report to the user.
#
# The token only ever goes into the runner process's environment: never on a command line,
# never with --ae/--ak/--env-file, never exported here, never written. Never enable set -x.
# First: drop inherited tracing (bash -x, SHELLOPTS=xtrace, BASH_ENV) and keep it from children.
set +x; export -n SHELLOPTS; unset -v BASH_ENV
set -uo pipefail

refuse() { echo "REFUSED: $*" >&2; exit 2; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)" || refuse "cannot find the worktree root"
cd "$ROOT" || refuse "cannot enter $ROOT"

[ $# -ge 3 ] || refuse "usage: eval/run_arm.sh <plain|fleet> <job-name> <tasks-dir> [extra runner arguments]"
ARM=$1 JOB=$2 TASKS=$3
shift 3
case $ARM in
  plain) AGENT=conclave_agent:GuardedClaudeCode ;;
  fleet) AGENT=conclave_agent:ConclaveClaudeCode ;;
  *) refuse "arm must be plain or fleet" ;;
esac
[[ $JOB =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || refuse "job name: letters, digits, '.', '_' and '-' only"
[ -d "$TASKS" ] || refuse "tasks folder not found: $TASKS"
[ -n "${MODEL:-}" ] || refuse "MODEL is required"
[[ ${BUDGET:-} =~ ^[0-9]+(\.[0-9]+)?$ ]] || refuse "BUDGET is required, in dollars (for example 2.00)"
EXTRA=()
while [ $# -gt 0 ]; do
  case $1 in
    -n) [[ ${2:-} =~ ^[1-9][0-9]*$ ]] || refuse "-n needs a positive integer"
        EXTRA+=("$1" "$2"); shift 2 ;;
    -i) [[ ${2:-} =~ ^[A-Za-z0-9_.*?-]+$ ]] || refuse "-i needs a task name or glob: letters, digits, _ - . * ? only"
        EXTRA+=("$1" "$2"); shift 2 ;;
    --disable-verification) EXTRA+=("$1"); shift ;;
    *) refuse "extra runner argument not allowed (only -n <count>, -i <task>, --disable-verification)" ;;
  esac
done
TOKEN_FILE=${TOKEN_FILE:-$HOME/.config/conclave-eval/oauth-token}
HARBOR_BIN=${HARBOR_BIN:-eval/.venv/bin/harbor}
JOBS_DIR=eval/.runs/jobs
JOB_DIR=$JOBS_DIR/$JOB

# 1. Token file, .env.local, job name.
{ [ -f "$TOKEN_FILE" ] && [ ! -L "$TOKEN_FILE" ]; } || refuse "token file missing or not a regular file: $TOKEN_FILE"
read -r T_UID T_MODE T_LINKS < <(stat -c '%u %a %h' -- "$TOKEN_FILE") || refuse "cannot stat the token file"
{ [ "$T_UID" = "$(id -u)" ] && [ "$T_MODE" = 600 ] && [ "$T_LINKS" = 1 ]; } \
  || refuse "token file must be owned by you, mode 600, with a single link"
{ [ "$(LC_ALL=C grep -c '' -- "$TOKEN_FILE")" = 1 ] \
  && [ "$(LC_ALL=C grep -cxE 'sk-ant-oat[A-Za-z0-9_-]+' -- "$TOKEN_FILE")" = 1 ]; } \
  || refuse "token file must hold exactly one line: sk-ant-oat followed by A-Z a-z 0-9 _ - only"
# The runner loads .env.local from its working folder, under our environment.
{ [ -e .env.local ] || [ -L .env.local ]; } && refuse ".env.local exists in $ROOT"
{ [ -e "$JOB_DIR" ] || [ -L "$JOB_DIR" ]; } && refuse "job already exists: $JOB_DIR"

AGENT_ARGS=()
# 2. Fleet arm: the plugin as committed at HEAD.
if [ "$ARM" = fleet ]; then
  PLUGIN_PATHS=(.claude-plugin agents skills guidance hooks eval/approver.py eval/redirect_plans.py)
  [ -z "$(git status --porcelain -- "${PLUGIN_PATHS[@]}")" ] || refuse "uncommitted changes to the plugin paths"
  SHA=$(git rev-parse --short HEAD) || refuse "cannot read HEAD"
  EXPORT=$ROOT/eval/.runs/export/$SHA
  rm -rf -- "$EXPORT" && mkdir -p -- "$EXPORT" || refuse "cannot create $EXPORT"
  git archive HEAD -- "${PLUGIN_PATHS[@]}" | tar -x -C "$EXPORT" || refuse "plugin export failed"
  AGENT_ARGS+=(--ak "plugin_src=$EXPORT")
  if [ -n "${SKILL_PREFIX+set}" ]; then
    # JSON-quoted: the runner strips --ak values, which would drop a trailing space.
    AGENT_ARGS+=(--ak "skill_prefix=$(python3 -I -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$SKILL_PREFIX")")
  fi
fi

# 3. The run. The token is a prefix assignment: in the runner's environment only.
# No CLAUDE_*, ANTHROPIC_* or MAX_THINKING_TOKENS from the caller reaches the runner.
for v in $(compgen -e); do
  case $v in CLAUDE_*|ANTHROPIC_*|MAX_THINKING_TOKENS) unset -v "$v" || refuse "cannot remove $v" ;; esac
done
env -0 | LC_ALL=C grep -zqE '^(CLAUDE_|ANTHROPIC_|MAX_THINKING_TOKENS=)' \
  && refuse "the environment holds a CLAUDE_ or ANTHROPIC_ entry that cannot be removed"
echo "== run: arm=$ARM job=$JOB tasks=$TASKS"
CLAUDE_CODE_OAUTH_TOKEN="$(<"$TOKEN_FILE")" CLAUDE_FORCE_OAUTH=1 PYTHONPATH=eval \
  "$HARBOR_BIN" run -p "$TASKS" -a "$AGENT" -m "$MODEL" -k 1 -o "$JOBS_DIR" --job-name "$JOB" \
  --ak version=2.1.327 --ak "max_budget_usd=\"$BUDGET\"" \
  "${AGENT_ARGS[@]}" "${EXTRA[@]}"
RC=$?
echo "== runner exit code: $RC"

report() {
  python3 -I - "$@" <<'PYEOF'
import json, math, os, re, stat, sys, tomllib, unicodedata

mode, job_dir, token_file = sys.argv[1:4]
probe = os.environ.get("PROBE") == "1"
token = open(token_file, encoding="ascii").read().strip()
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


if not token:
    sys.exit("empty token")
if mode == "scan":
    try:
        sys.exit(scan())
    except Exception as exc:
        print(f"anomaly scan failed: {type(exc).__name__}")
        sys.exit(4)
for trial in trials():
    try:
        summary_of(trial)
    except Exception as exc:
        # The type only: a message could carry container text.
        print(f"summary failed for this trial: {type(exc).__name__}")
if not trials():
    print("no trial folders")
PYEOF
}

# 4. Token search first: names only, symlinks not followed, devices and FIFOs skipped. On a
#    hit, nothing from the trial folders (names included) is printed.
HITS=$(LC_ALL=C grep -rlF -D skip -f "$TOKEN_FILE" -- "$ROOT" 2>/dev/null)
GREP_RC=$?
if [ -n "$HITS" ]; then
  echo "!!! TOKEN FOUND in $(printf '%s\n' "$HITS" | wc -l) file(s) under $ROOT. Launch nothing more."
  echo "!!! List them yourself: grep -rlF -D skip -f \"\$TOKEN_FILE\" $ROOT"
  exit 3
elif [ "$GREP_RC" -ne 1 ]; then
  echo "!!! Token search incomplete (grep exit $GREP_RC: unreadable entries); treated as found."
  exit 3
fi
echo "token search: clean (literal value only; an encoded copy would not be found)"

# 5. Trial-folder anomalies, before any summary reads them.
report scan "$JOB_DIR" "$TOKEN_FILE"
ANOMALY=$?

# 6. Summary from the host-only copies and the result files.
report summary "$JOB_DIR" "$TOKEN_FILE"

[ "$ANOMALY" -eq 4 ] && exit 4
exit "$RC"
