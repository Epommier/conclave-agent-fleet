"""Claude Code agents for the eval runner (Harbor 0.26.0).

GuardedClaudeCode (plain arm): the runner's post-run step reads the agent log
folder and writes trajectory.json into it with plain opens, but that folder is
bind-mounted into the container and written by root there. This class first
copies it, without following symlinks, to a host-only sibling `agent-host/`
and points the agent at the copy, so the post-run step and the trial's later
read of trajectory.json never touch the mounted folder.

ConclaveClaudeCode (fleet arm): adds the host-channel flags, the plugin, the
approver and plan-redirect hooks, and the `/conclave:implement ` prefix.
"""

import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from harbor.agents.installed.claude_code import ClaudeCode, ClaudeCodeOptions
from harbor.agents.options import Cli

FILE_CAP = 200 * 1024 * 1024
TOTAL_CAP = 500 * 1024 * 1024
HOST_COPY = "agent-host"
ANOMALIES = "ANOMALIES.txt"
_CHUNK = 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_PATH never blocks or triggers device side effects, whatever the entry is.
_PATH_FLAGS = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
_KINDS = (
    (stat.S_ISLNK, "symlink"),
    (stat.S_ISFIFO, "fifo"),
    (stat.S_ISCHR, "char device"),
    (stat.S_ISBLK, "block device"),
    (stat.S_ISSOCK, "socket"),
)


def sanitise_name(name: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in name)[:200]


def _kind(mode: int) -> str:
    return next((label for test, label in _KINDS if test(mode)), "other")


class _NoFollowCopy:
    def __init__(self) -> None:
        self.total = 0
        self.anomalies: list[str] = []

    def skip(self, rel: str, reason: str) -> None:
        self.anomalies.append(f"{sanitise_name(rel)}\t{reason}")

    def copy_dir(self, dir_fd: int, dst: Path, rel: str) -> None:
        for name in sorted(os.listdir(dir_fd)):
            path = f"{rel}/{name}" if rel else name
            if path == ANOMALIES:
                self.skip(path, "reserved name")
                continue
            try:
                fd = os.open(name, _PATH_FLAGS, dir_fd=dir_fd)
            except OSError as exc:
                self.skip(path, f"unopenable ({exc.errno})")
                continue
            try:
                mode = os.fstat(fd).st_mode
                if stat.S_ISDIR(mode):
                    sub_fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
                    try:
                        # Swapped between the two opens: refuse the new one.
                        if os.path.samestat(os.fstat(sub_fd), os.fstat(fd)):
                            (dst / name).mkdir(mode=0o700)
                            self.copy_dir(sub_fd, dst / name, path)
                        else:
                            self.skip(path, "changed during copy")
                    finally:
                        os.close(sub_fd)
                elif stat.S_ISREG(mode):
                    self.copy_file(fd, dst / name, path)
                else:
                    self.skip(path, _kind(mode))
            except OSError as exc:
                self.skip(path, f"copy failed ({exc.errno})")
            finally:
                os.close(fd)

    def copy_file(self, path_fd: int, dst: Path, rel: str) -> None:
        size = os.fstat(path_fd).st_size
        if size > FILE_CAP:
            self.skip(rel, "over 200 MB")
            return
        if self.total + size > TOTAL_CAP:
            self.skip(rel, "over 500 MB in total")
            return
        # Reopen the very inode the O_PATH descriptor holds, for reading.
        src = os.open(f"/proc/self/fd/{path_fd}", os.O_RDONLY | os.O_CLOEXEC)
        try:
            out = os.open(
                dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            try:
                limit = min(FILE_CAP, TOTAL_CAP - self.total)
                copied = 0
                while chunk := os.read(src, min(_CHUNK, limit + 1 - copied)):
                    copied += len(chunk)
                    if copied > limit:
                        break
                    os.write(out, chunk)
            finally:
                os.close(out)
        finally:
            os.close(src)
        if copied > limit:
            os.unlink(dst)
            self.skip(rel, "grew past the cap during copy")
            return
        self.total += copied


def copy_no_follow(src: Path, dst: Path) -> list[str]:
    """Copy regular files and directories from src into a new dst; never raises
    because of what src holds. Returns the skipped entries, also written to
    dst/ANOMALIES.txt."""
    if dst.exists() or dst.is_symlink():
        shutil.rmtree(dst)
    dst.mkdir(mode=0o700)
    copier = _NoFollowCopy()
    try:
        root = os.open(src, _DIR_FLAGS)
    except OSError as exc:
        copier.skip(".", f"log folder unopenable ({exc.errno})")
    else:
        try:
            copier.copy_dir(root, dst, "")
        except (OSError, RecursionError) as exc:
            # Too deep a tree or too many open folders.
            copier.skip(".", f"copy aborted ({type(exc).__name__})")
        finally:
            os.close(root)
    (dst / ANOMALIES).write_text("".join(f"{line}\n" for line in copier.anomalies))
    return copier.anomalies


class GuardedClaudeCode(ClaudeCode):
    def __init__(self, logs_dir: Path, *args, **kwargs):
        super().__init__(logs_dir, *args, **kwargs)
        self._mounted_logs_dir = Path(logs_dir)
        # Pointed at the copy from the start, so every host-side use through
        # the agent object (the post-run step, and the trial's later read of
        # logs_dir / "trajectory.json", which also runs when the post-run step
        # is skipped) lands in the host-only folder, never the mounted one.
        self.logs_dir = self._mounted_logs_dir.parent / HOST_COPY

    def populate_context_post_run(self, context) -> None:
        copy_no_follow(self._mounted_logs_dir, self.logs_dir)
        super().populate_context_post_run(context)


def _hook(command: str, timeout: int) -> list[dict]:
    return [{"type": "command", "command": command, "timeout": timeout}]


FLEET_SETTINGS = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "AskUserQuestion",
                "hooks": _hook(
                    "CONCLAVE_EVAL=1 CONCLAVE_APPROVER_LOG=/logs/agent/approver.jsonl "
                    "python3 /opt/conclave/eval/hooks/approver.py",
                    30,
                ),
            },
            {
                "matcher": "Write|Edit|Read",
                "hooks": _hook(
                    "CONCLAVE_EVAL=1 CONCLAVE_PLANS_DIR=/logs/agent/plans "
                    "CONCLAVE_REDIRECT_LOG=/logs/agent/redirect.jsonl "
                    "python3 -I /opt/conclave/eval/hooks/redirect_plans.py",
                    10,
                ),
            },
        ]
    }
}

_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*|www\.\S+")
_PATHLIKE = re.compile(r"[~./]\S*|\S*[/\\]\S*|\S+\.[A-Za-z0-9]{1,8}")


def fleet_instruction(prefix: str, task: str) -> str:
    """The stream-json user message the fleet arm sends on stdin."""
    text = task.strip()
    if not text:
        raise ValueError("refusing an empty task")
    # The skill reads a bare path as a file and fetches a bare URL.
    if _URL.fullmatch(text) or _PATHLIKE.fullmatch(text):
        raise ValueError("refusing a task that is only a path or a URL")
    if prefix and not prefix[-1].isspace():
        # The runner strips --ak values, which drops the trailing space.
        prefix += " "
    message = {"type": "user", "message": {"role": "user", "content": prefix + task}}
    return json.dumps(message) + "\n"


class ConclaveClaudeCodeOptions(ClaudeCodeOptions):
    input_format: Annotated[
        Literal["stream-json"] | None, Cli("--input-format")
    ] = Field(default="stream-json", description="Host channel input.")
    permission_prompt_tool: Annotated[
        Literal["stdio"] | None, Cli("--permission-prompt-tool")
    ] = Field(default="stdio", description="Host channel permission tool.")
    plugin_dir: Annotated[
        Literal["/opt/conclave"] | None, Cli("--plugin-dir")
    ] = Field(default="/opt/conclave", description="Plugin folder in the container.")
    plugin_src: str = Field(description="Host folder holding the exported plugin.")
    skill_prefix: str = Field(
        default="/conclave:implement ", description="Text put before the task."
    )


class ConclaveClaudeCode(GuardedClaudeCode):
    options_model = ConclaveClaudeCodeOptions
    options: ConclaveClaudeCodeOptions

    def __init__(self, logs_dir: Path, *args, **kwargs):
        kwargs["config"] = FLEET_SETTINGS
        super().__init__(logs_dir, *args, **kwargs)
        if not Path(self.options.plugin_src).is_dir():
            raise ValueError(f"plugin_src is not a folder: {self.options.plugin_src}")

    async def setup(self, environment) -> None:
        await super().setup(environment)
        await self.exec_as_root(environment, command="mkdir -p /opt/conclave")
        await environment.upload_dir(self.options.plugin_src, "/opt/conclave")
        await self.exec_as_root(environment, command="mkdir -p -m 700 /logs/agent/plans")

    async def run(self, instruction, environment, context) -> None:
        return await super().run(
            instruction=fleet_instruction(self.options.skill_prefix, instruction),
            environment=environment,
            context=context,
        )
