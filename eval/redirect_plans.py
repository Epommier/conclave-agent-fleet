#!/usr/bin/env python3
"""PreToolUse hook that moves plan files out of ~/.claude/plans during an eval run.

Eval-only. Claude Code refuses writes into any folder named .claude in
unattended runs, so a Read, Write or Edit of ~/.claude/plans/<name>.md is
rewritten to $CONCLAVE_PLANS_DIR/<name>.md and approved. Nothing else is ever
approved. Any other call, and any call made without CONCLAVE_EVAL=1, gets no
output and exit 0, so this hook never blocks or alters unrelated file tools.
For a plans path under CONCLAVE_EVAL=1, every failure exits 2 (blocks the call).

Limits:
- Not a confidentiality control: other path forms can still reach the real
  ~/.claude/plans folder through an unscoped Read rule.
- A race remains between these checks and the tool's write (a link swapped in
  between); in containers the container is the boundary.
- An Edit addressed to a plans path whose file does not exist at the real
  location fails before hooks run, so this hook never sees it.
"""
import datetime
import json
import os
import stat
import sys

TOOLS = ("Read", "Write", "Edit")
PROTECTED = {".git", ".vscode", ".idea", ".claude", ".husky", ".cargo",
             ".devcontainer", ".yarn", ".mvn"}


def classify(raw):
    """Return (event, tool_input, normalised path) for a plans path, else None. Never raises."""
    try:
        event = json.loads(raw)
        if not isinstance(event, dict) or event.get("tool_name") not in TOOLS:
            return None
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict):
            return None
        path = tool_input.get("file_path")
        if not isinstance(path, str) or not path or "\0" in path:
            return None
        if path.startswith("~/"):
            path = os.path.expanduser(path)
        if not os.path.isabs(path):
            return None
        n = os.path.normpath(path)
        plans = os.path.normpath(os.path.expanduser("~/.claude/plans"))
        name = os.path.basename(n)
        if os.path.dirname(n) != plans or not name.endswith(".md") or name.startswith("."):
            return None
        return event, tool_input, n
    except Exception:
        return None


def inside(path, folder):
    """True when real path `path` equals or sits inside real path `folder`."""
    return os.path.commonpath([path, folder]) == folder


def check(ok, message):
    if not ok:
        raise ValueError(message)


def redirect(event, tool_input, n):
    """Validate the redirect folder and log, append the log line, return the hook output."""
    plans_dir = os.environ.get("CONCLAVE_PLANS_DIR", "")
    check(os.path.isabs(plans_dir) and os.path.isdir(plans_dir),
          "CONCLAVE_PLANS_DIR must be an existing absolute folder")
    r = os.path.realpath(plans_dir)
    check(not PROTECTED.intersection(r.split(os.sep)), f"protected segment in {r!r}")
    cwd = event.get("cwd")
    check(isinstance(cwd, str) and os.path.isabs(cwd), "event has no absolute cwd")
    check(not inside(r, os.path.realpath(cwd)), "redirect folder is inside the event cwd")
    check(not inside(r, os.path.realpath(os.path.expanduser("~/.claude"))),
          "redirect folder is inside ~/.claude")
    candidate = os.path.join(r, os.path.basename(n))
    t = os.path.realpath(candidate)
    check(os.path.dirname(t) == r, f"target {t!r} is not directly in the redirect folder")
    try:
        st = os.lstat(candidate)
    except FileNotFoundError:
        pass
    else:
        check(stat.S_ISREG(st.st_mode) and st.st_nlink == 1,
              "existing target is not a single-link regular file")
    log = os.environ.get("CONCLAVE_REDIRECT_LOG", "")
    check(os.path.isabs(log), "CONCLAVE_REDIRECT_LOG must be an absolute path")
    check(not inside(os.path.realpath(os.path.dirname(log)), r),
          "redirect log is inside the redirect folder")
    with open(log, "a") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "tool": event["tool_name"], "raw": tool_input["file_path"][:4096], "new": t,
            "session_id": event.get("session_id"), "agent_id": event.get("agent_id"),
            "agent_type": event.get("agent_type"),
        }) + "\n")
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {**tool_input, "file_path": t},
    }}


def main():
    try:
        hit = classify(sys.stdin.buffer.read())
    except Exception:
        return
    if hit is None or os.environ.get("CONCLAVE_EVAL") != "1":
        return
    try:
        out = json.dumps(redirect(*hit)) + "\n"
        sys.stdout.write(out)
        sys.stdout.flush()
    except BaseException as e:
        msg = f"conclave plans redirect refused: {type(e).__name__}: {e}"
        print(msg.replace("\n", "\\n").replace("\r", "\\r"), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
