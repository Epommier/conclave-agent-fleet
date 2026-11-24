"""Check each task's Docker image: oracle/nop rewards, leak verdict and agent files.

Usage (from the worktree root, as eval/scripts/check_tasks.sh does):
    python3 -I -B eval/src/check_images.py TASKS_DIR ORACLE_JOBS_DIR NOP_JOBS_DIR LOG_PATH DOCKER_BIN

Prints one "RESULT <task> oracle=.. nop=.. leak=.. agentfiles=.." line per task on stdout;
stderr goes to the caller's log. Exit code 0 when the checks ran, non-zero otherwise
(the caller passes it through).
"""
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
    rc = run(["eval/scripts/leak_check.sh", image, "/testbed"], 1800, log)
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
