#!/usr/bin/env bash
# check_tasks.sh - the free checks on a folder of task copies, with no token and no model:
# reference-fix and do-nothing grades, the structural leak check and agent files per image.
#
# Usage: eval/scripts/check_tasks.sh <tasks-dir> <job-prefix>
#   <tasks-dir>   a folder under eval/.runs/tasks/ holding only task folders
#   <job-prefix>  letters, digits, '_' and '-' (1 to 40); jobs <prefix>-oracle and <prefix>-nop
# Env: HARBOR_BIN (default eval/.venv/bin/harbor), DOCKER_BIN (default docker; the leak
#   script calls the docker found on PATH).
# Stdout: fixed-text progress lines and, per task folder sorted by name,
#   RESULT <task> oracle=<0|1|none> nop=<0|1|none> leak=<CLEAN|LEAK|ERROR> agentfiles=<none|present|error>
#   Nothing from a container or an image reaches stdout or stderr: the output of the runner,
#   eval/scripts/leak_check.sh and docker goes to eval/.runs/<prefix>-check.log.
# Exit: 0 ran to the end; 2 refused before running; 4 anomaly in a job folder.
# Never enable set -x. First: drop inherited tracing and keep it from children.
set +x; export -n SHELLOPTS; unset -v BASH_ENV
set -uo pipefail
shopt -s nullglob dotglob
LC_ALL=C

refuse() { echo "REFUSED: $*" >&2; exit 2; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)" || refuse "cannot find the worktree root"
cd "$ROOT" || refuse "cannot enter the worktree root"

[ $# -eq 2 ] || refuse "usage: eval/scripts/check_tasks.sh <tasks-dir> <job-prefix>"
[[ $2 =~ ^[A-Za-z0-9_-]{1,40}$ ]] || refuse "job prefix: letters, digits, '_' and '-' only, 1 to 40"
PREFIX=$2
[ -n "${CLAUDE_CODE_OAUTH_TOKEN+set}" ] && refuse "CLAUDE_CODE_OAUTH_TOKEN is set; these checks run with no token"
{ [ -e .env.local ] || [ -L .env.local ]; } && refuse ".env.local exists in the worktree root"
TASKS_ROOT=$ROOT/eval/.runs/tasks
REAL=$(cd -- "$1" 2>/dev/null && pwd -P) || refuse "tasks folder not found"
[[ $REAL == "$TASKS_ROOT"/?* ]] || refuse "the tasks folder must be under eval/.runs/tasks/"
TASKS=${REAL#"$ROOT"/}
for d in "$TASKS"/*; do
  { [ -d "$d" ] && [ ! -L "$d" ]; } || refuse "the tasks folder holds an entry that is not a folder"
done
JOBS_DIR=eval/.runs/jobs
for j in "$JOBS_DIR/$PREFIX-oracle" "$JOBS_DIR/$PREFIX-nop" "eval/.runs/$PREFIX-check.log"; do
  { [ -e "$j" ] || [ -L "$j" ]; } && refuse "already exists: $j"
done
LOG=eval/.runs/$PREFIX-check.log
HARBOR_BIN=${HARBOR_BIN:-eval/.venv/bin/harbor}
DOCKER_BIN=${DOCKER_BIN:-docker}

# No CLAUDE_*, ANTHROPIC_* or MAX_THINKING_TOKENS reaches the runner or docker.
for v in $(compgen -e); do
  case $v in CLAUDE_*|ANTHROPIC_*|MAX_THINKING_TOKENS) unset -v "$v" || refuse "cannot remove $v" ;; esac
done
env -0 | grep -zqE '^(CLAUDE_|ANTHROPIC_|MAX_THINKING_TOKENS=)' \
  && refuse "the environment holds a CLAUDE_ or ANTHROPIC_ entry that cannot be removed"
: > "$LOG" || refuse "cannot create the log file"

# 1. Reference-fix and do-nothing runs, three at a time; their output goes to the log only.
for AG in oracle nop; do
  echo "== runner: agent $AG, job $PREFIX-$AG"
  "$HARBOR_BIN" run -p "$TASKS" -a "$AG" -k 1 -n 3 -o "$JOBS_DIR" --job-name "$PREFIX-$AG" \
    </dev/null >>"$LOG" 2>&1
  echo "== runner exit code: $?"
done

# 2. Job-folder anomalies, before anything reads them: a count only.
ROOTS=()
for j in "$JOBS_DIR/$PREFIX-oracle" "$JOBS_DIR/$PREFIX-nop"; do
  { [ -e "$j" ] || [ -L "$j" ]; } && ROOTS+=("$j")
done
FOUND=0
if [ ${#ROOTS[@]} -gt 0 ]; then
  FOUND=$(find "${ROOTS[@]}" \( \! -type f \! -type d -o -perm /6000 -o \! -uid "$(id -u)" \) \
    -printf x 2>>"$LOG" | wc -c) || FOUND=unreadable
fi
echo "anomaly scan: $FOUND found"
[ "$FOUND" = 0 ] || exit 4

# 3-5. Rewards from result.json, leak verdict and agent files per image, one line per task.
echo "== checking images"
python3 -I -B "$ROOT/eval/src/check_images.py" "$TASKS" "$JOBS_DIR/$PREFIX-oracle" "$JOBS_DIR/$PREFIX-nop" "$LOG" "$DOCKER_BIN" \
  2>>"$LOG"
RC=$?
[ "$RC" -eq 0 ] || { echo "== image checks failed: exit $RC"; exit "$RC"; }
echo "== done"
