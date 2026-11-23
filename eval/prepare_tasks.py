#!/usr/bin/env python3
"""Build restricted task-folder copies from a task list.

Usage: prepare_tasks.py <tasklist.json> <candidates|tasks> <source-dir> <out-dir>

Copies each listed task folder from <source-dir> into a new <out-dir> and adds the
network policy: agent phase allowlist api.anthropic.com, verifier phase no-network.
Refuses (non-zero exit, nothing left behind) on any check failure.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib

NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")
# The tables and keys every task.toml of the 860-task download uses (checked on all 860).
ALLOWED = {
    "task": {"name", "authors"},
    "metadata": {"author_name", "author_email", "difficulty", "category", "tags", "source"},
    "verifier": {"timeout_sec"},
    "agent": {"timeout_sec"},
    "environment": {"build_timeout_sec", "cpus", "memory", "storage"},
}
NETWORK_KEYS = {"network_mode", "allowed_hosts"}
ADDED = {
    "agent": ['network_mode = "allowlist"', 'allowed_hosts = ["api.anthropic.com"]'],
    "verifier": ['network_mode = "no-network"'],
}
ADDED_PARSED = {
    "agent": {"network_mode": "allowlist", "allowed_hosts": ["api.anthropic.com"]},
    "verifier": {"network_mode": "no-network"},
}


class Refusal(Exception):
    pass


def check_toml(name, parsed):
    for table, value in parsed.items():
        if not isinstance(value, dict):
            raise Refusal(f"{name}: task.toml top-level key {table!r} outside the fixed set")
        if NETWORK_KEYS & value.keys():
            raise Refusal(f"{name}: task.toml [{table}] already holds a network key")
        extra = value.keys() - ALLOWED.get(table, set())
        if table not in ALLOWED or extra:
            raise Refusal(f"{name}: task.toml [{table}] {sorted(extra)} outside the fixed set")


def check_tree(name, folder):
    for root, dirs, files in os.walk(folder):
        for entry in dirs + files:
            mode = os.lstat(os.path.join(root, entry)).st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise Refusal(f"{name}: {os.path.relpath(os.path.join(root, entry), folder)} is not a regular file or folder")


def add_network(name, text):
    lines = text.split("\n")
    for table in ("agent", "verifier"):
        idx = [i for i, line in enumerate(lines) if line == f"[{table}]"]
        if len(idx) != 1:
            raise Refusal(f"{name}: task.toml needs exactly one [{table}] header line")
        lines[idx[0] + 1:idx[0] + 1] = ADDED[table]
    return "\n".join(lines)


def prepare(name, expected_sha, source, dest):
    if not NAME_RE.match(name):
        raise Refusal(f"bad task name {name!r}")
    src = os.path.join(source, name)
    if not stat.S_ISDIR(os.lstat(src).st_mode):
        raise Refusal(f"{name}: source is not a folder")
    check_tree(name, src)
    shutil.copytree(src, dest, symlinks=True)
    # Check the copy itself, so a source changed during the copy cannot slip through.
    check_tree(name, dest)
    with open(os.path.join(dest, "instruction.md"), "rb") as f:
        if hashlib.sha256(f.read()).hexdigest() != expected_sha:
            raise Refusal(f"{name}: instruction.md sha256 differs from the list")
    with open(os.path.join(dest, "task.toml"), "rb") as f:
        raw = f.read()
    parsed = tomllib.loads(raw.decode("utf-8"))
    check_toml(name, parsed)
    new_text = add_network(name, raw.decode("utf-8"))
    with open(os.path.join(dest, "task.toml"), "w", encoding="utf-8", newline="") as f:
        f.write(new_text)
    with open(os.path.join(dest, "task.toml"), "rb") as f:
        result = tomllib.load(f)
    for table, keys in ADDED_PARSED.items():
        parsed[table].update(keys)
    if result != parsed:
        raise Refusal(f"{name}: written task.toml is not the source plus the three network keys")


def main(argv):
    if len(argv) != 5 or argv[2] not in ("candidates", "tasks"):
        raise Refusal("usage: prepare_tasks.py <tasklist.json> <candidates|tasks> <source-dir> <out-dir>")
    listfile, field, source, out = argv[1:]
    out = os.path.abspath(out)
    if os.path.lexists(out):
        raise Refusal(f"out-dir exists: {out}")
    with open(listfile, encoding="utf-8") as f:
        tasklist = json.load(f)
    shas = {c["name"]: c["instruction_sha256"] for c in tasklist["candidates"]}
    names = [c["name"] for c in tasklist["candidates"]] if field == "candidates" else tasklist["tasks"]
    if not names or len(set(names)) != len(names):
        raise Refusal(f"{field}: empty or duplicate names")
    for name in names:
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise Refusal(f"bad task name {name!r}")
        if name not in shas:
            raise Refusal(f"{name}: not among the candidates, no sha256")
    tmp = tempfile.mkdtemp(prefix=".prepare-", dir=os.path.dirname(out))
    try:
        for name in names:
            prepare(name, shas[name], source, os.path.join(tmp, name))
        os.rename(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    print(f"wrote {len(names)} task folders to {out}")


if __name__ == "__main__":
    try:
        main(sys.argv)
    except (Refusal, OSError, ValueError, KeyError, TypeError) as e:
        print(f"prepare_tasks: refused: {e}".replace("\n", " "), file=sys.stderr)
        sys.exit(1)
