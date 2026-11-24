#!/usr/bin/env bash
# run_arm.sh - run one arm through the eval runner inside the task containers, signed in
# with the subscription OAuth token, then check the trial folders, search for the token and
# print a sanitised summary.
#
# Usage: eval/scripts/run_arm.sh <plain|fleet> <job-name> <tasks-dir> [extra runner arguments]
#   plain  conclave_agent:GuardedClaudeCode    fleet  conclave_agent:ConclaveClaudeCode
#   The tasks folder selects the agent-phase network (its task.toml files declare it).
# Env: MODEL (required), BUDGET (required, per-trial CLI cap in dollars), SKILL_PREFIX
#   (fleet only, optional; may be empty), PROBE=1 (adds every tool call to the summary),
#   TOKEN_FILE (default ~/.config/conclave-eval/oauth-token),
#   HARBOR_BIN (default eval/.venv/bin/harbor).
# Exit: 0 run completed, nothing found; 2 refused before running; 3 token found (or the
#   search could not read everything, or the records were refused for token-like text);
#   4 anomaly in a trial folder, failed scan, or any other records failure; otherwise the
#   runner's exit code. After 3 or 4, launch nothing more and report to the user.
#
# The token only ever goes into the runner process's environment: never on a command line,
# never with --ae/--ak/--env-file, never exported here, never written. Never enable set -x.
# First: drop inherited tracing (bash -x, SHELLOPTS=xtrace, BASH_ENV) and keep it from children.
set +x; export -n SHELLOPTS; unset -v BASH_ENV
set -uo pipefail

refuse() { echo "REFUSED: $*" >&2; exit 2; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)" || refuse "cannot find the worktree root"
cd "$ROOT" || refuse "cannot enter $ROOT"

[ $# -ge 3 ] || refuse "usage: eval/scripts/run_arm.sh <plain|fleet> <job-name> <tasks-dir> [extra runner arguments]"
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
  PLUGIN_PATHS=(.claude-plugin agents skills guidance hooks eval/hooks/approver.py eval/hooks/redirect_plans.py)
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
CLAUDE_CODE_OAUTH_TOKEN="$(<"$TOKEN_FILE")" CLAUDE_FORCE_OAUTH=1 PYTHONPATH="$ROOT/eval/src" \
  "$HARBOR_BIN" run -p "$TASKS" -a "$AGENT" -m "$MODEL" -k 1 -o "$JOBS_DIR" --job-name "$JOB" \
  --ak version=2.1.327 --ak "max_budget_usd=\"$BUDGET\"" \
  "${AGENT_ARGS[@]}" "${EXTRA[@]}"
RC=$?
echo "== runner exit code: $RC"

report() { python3 -I -B "$ROOT/eval/src/report.py" "$@"; }

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

# 7. One numeric record per trial (records.jsonl), only after a clean search and scan.
[ "$ANOMALY" -eq 0 ] || exit 4
report records "$JOB_DIR" "$TOKEN_FILE" "$JOB" "$ARM" "$BUDGET"
RECORDS=$?
[ "$RECORDS" -eq 3 ] && exit 3
[ "$RECORDS" -eq 0 ] || exit 4
exit "$RC"
