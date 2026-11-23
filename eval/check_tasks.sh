#!/usr/bin/env bash
# check_tasks.sh - the free checks on a folder of task copies, with no token and no model:
# reference-fix and do-nothing grades, the structural leak check and agent files per image.
#
# Usage: eval/check_tasks.sh <tasks-dir> <job-prefix>
#   <tasks-dir>   a folder under eval/.runs/tasks/ holding only task folders
#   <job-prefix>  letters, digits, '_' and '-' (1 to 40); jobs <prefix>-oracle and <prefix>-nop
# Env: HARBOR_BIN (default eval/.venv/bin/harbor), DOCKER_BIN (default docker; the leak
#   script calls the docker found on PATH).
# Stdout: fixed-text progress lines and, per task folder sorted by name,
#   RESULT <task> oracle=<0|1|none> nop=<0|1|none> leak=<CLEAN|LEAK|ERROR> agentfiles=<none|present|error>
#   Nothing from a container or an image reaches stdout or stderr: the output of the runner,
#   eval/leak_check.sh and docker goes to eval/.runs/<prefix>-check.log.
# Exit: 0 ran to the end; 2 refused before running; 4 anomaly in a job folder.
# Never enable set -x. First: drop inherited tracing and keep it from children.
set +x; export -n SHELLOPTS; unset -v BASH_ENV
set -uo pipefail
shopt -s nullglob dotglob
LC_ALL=C

refuse() { echo "REFUSED: $*" >&2; exit 2; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)" || refuse "cannot find the worktree root"
cd "$ROOT" || refuse "cannot enter the worktree root"

[ $# -eq 2 ] || refuse "usage: eval/check_tasks.sh <tasks-dir> <job-prefix>"
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
python3 -I - "$TASKS" "$JOBS_DIR/$PREFIX-oracle" "$JOBS_DIR/$PREFIX-nop" "$LOG" "$DOCKER_BIN" \
  2>>"$LOG" <<'PYEOF'
import json, os, re, shutil, stat, subprocess, sys, tempfile

tasks_dir, oracle_dir, nop_dir, log_path, docker = sys.argv[1:6]
NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,80}")
IMAGE = re.compile(r"swerebench/sweb\.eval\.x86_64\.[A-Za-z0-9_.-]+:latest")
FIND = ("set -o pipefail; find /testbed -name .git -prune -o \\( -name CLAUDE.md"
        " -o -name CLAUDE.local.md -o -name AGENTS.md -o -name .mcp.json"
        " -o -type d -name .claude \\) -print | wc -l")
log = open(log_path, "ab")


def read(path, limit=10 * 1024 * 1024):
    """A regular file under the limit, without following symlinks or blocking; else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError):
        return None
    with os.fdopen(fd, "rb") as handle:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        return handle.read(limit).decode("utf-8", "replace")


def load(path):
    try:
        data = json.loads(read(path) or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else {}


def rewards(job_dir):
    """Task folder name -> '0', '1' or 'none', from each trial's result.json only."""
    found = {}
    try:
        trials = os.listdir(job_dir)
    except OSError:
        return found
    for trial in trials:
        tdir = os.path.join(job_dir, trial)
        if not stat.S_ISDIR(os.lstat(tdir).st_mode):
            continue
        path = ((load(os.path.join(tdir, "config.json")) or {}).get("task") or {})
        path = path.get("path") if isinstance(path, dict) else None
        if not isinstance(path, str):
            continue
        task = path.rstrip("/").rsplit("/", 1)[-1]
        r = (load(os.path.join(tdir, "result.json")) or {}).get("verifier_result")
        r = r.get("rewards") if isinstance(r, dict) else None
        r = r.get("reward") if isinstance(r, dict) else None
        value = str(int(r)) if isinstance(r, (int, float)) and not isinstance(r, bool) \
            and r in (0, 1) else "none"
        found[task] = value if task not in found else "none"  # two trials of a task: none
    return found


def run(args, timeout, out):
    log.write(f"\n== {' '.join(args[:2])}\n".encode("ascii", "replace"))
    log.flush()
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=out, stderr=log,
                              timeout=timeout).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def check(task):
    lines = (read(os.path.join(tasks_dir, task, "environment", "Dockerfile")) or "").splitlines()
    froms = [ln.split() for ln in lines if ln.lstrip().upper().startswith("FROM")]
    if len(froms) != 1 or len(froms[0]) != 2 or not IMAGE.fullmatch(froms[0][1]):
        return "ERROR", "error"
    image = froms[0][1]
    if run([docker, "image", "inspect", image], 120, log) != 0 \
            and run([docker, "pull", image], 3600, log) != 0:
        return "ERROR", "error"  # leak_check.sh cannot pull; only a local image is checked
    rc = run(["eval/leak_check.sh", image, "/testbed"], 1800, log)
    leak = {0: "CLEAN", 1: "LEAK"}.get(rc, "ERROR")
    with tempfile.TemporaryFile() as out:
        rc = run([docker, "run", "--rm", "--network", "none", "--entrypoint", "bash", image,
                  "-c", FIND], 300, out)
        out.seek(0)
        head = out.read(32)
        out.seek(0)
        shutil.copyfileobj(out, log)  # the raw output, for the log only
    m = re.fullmatch(rb"(0|[1-9][0-9]{0,9})\n?", head) if rc == 0 else None
    return leak, "error" if m is None else "none" if m.group(1) == b"0" else "present"


oracle, nop = rewards(oracle_dir), rewards(nop_dir)
for task in sorted(os.listdir(tasks_dir)):
    try:
        leak, agentfiles = check(task)
    except Exception as exc:
        log.write(f"\n== check failed: {type(exc).__name__}\n".encode())
        leak, agentfiles = "ERROR", "error"
    shown = task if NAME.fullmatch(task) else "invalid"
    print(f"RESULT {shown} oracle={oracle.get(task, 'none')} nop={nop.get(task, 'none')}"
          f" leak={leak} agentfiles={agentfiles}", flush=True)
PYEOF
RC=$?
[ "$RC" -eq 0 ] || { echo "== image checks failed: exit $RC"; exit "$RC"; }
echo "== done"
