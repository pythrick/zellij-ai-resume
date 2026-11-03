#!/usr/bin/env python3
"""zellij-ai-resume: per-pane session recovery for Claude Code and Codex in Zellij.

When a Zellij session dies (crash, reboot, kill -9) Zellij can resurrect it
from the layout it saved: every pane gets its last command back, suspended
until you press Enter. The zclaude and zcodex launchers make that command

    /bin/sh <launcher> --zj-pane <token>

and this module keeps one small state file per token: the arguments the pane
was started with and the conversation it is on now. Re-running the saved
command resumes that conversation instead of starting an empty one.

Entry points, used by the generated launchers:

    zjr.py start <tool> <launcher> [args...]       first launch from a shell
    zjr.py run   <tool> <launcher> --zj-pane <token>
    zjr.py admin <bindir> [list|doctor|gc|forget|version]

Standard library only; Python 3.8 or newer.
"""

from __future__ import annotations

import datetime
import fcntl
import glob
import json
import math
import os
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import termios
import threading
import time
import uuid

VERSION = "1.0.0"
STATE_FORMAT = 1
PANE_FLAG = "--zj-pane"
STALE_DAYS = 30
FAST_FAIL_SECONDS = 5.0
MAX_ARGS = 512
MAX_STATE_BYTES = 256 * 1024
MAX_REGISTRY_BYTES = 64 * 1024
MAX_META_BYTES = 4 * 1024 * 1024
MAX_LOG_BYTES = 512 * 1024

# Pane tokens are uuid4 strings this module generates; nothing else is
# accepted, so a token can never name a file outside the state directory.
TOKEN_RE = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
# Conversation ids come from the CLIs (Claude Code: uuid4, Codex: uuid7).
SESSION_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
PANE_ID_RE = re.compile(r"\A[0-9]{1,9}\Z")  # ZELLIJ_PANE_ID
LOCK_NAME_RE = re.compile(r"\A(pane|claude|codex)-[0-9a-fA-F-]{36}\.lock\Z")
# The format of `ps -o lstart`, which Claude Code also uses for procStart.
LSTART_RE = re.compile(r"\A[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d\d:\d\d:\d\d \d{4}\Z")


# --- small helpers ------------------------------------------------------------


def note(tool: str, message: str) -> None:
    try:
        sys.stderr.write(f"[z{tool}] {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def is_session_id(value: object) -> bool:
    return isinstance(value, str) and SESSION_RE.match(value) is not None


def is_abs_path(value: object) -> bool:
    return isinstance(value, str) and value.startswith("/") and "\0" not in value


def fallback_cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return os.path.expanduser("~")


def first_dir(*candidates: object) -> str:
    for candidate in candidates:
        if is_abs_path(candidate) and os.path.isdir(candidate):
            return candidate
    return os.path.expanduser("~")


def short(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _flags(base: int) -> int:
    return base | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _open_regular(path: str, owner: int | None) -> int | None:
    """An fd for reading a regular file; never follows a symlink, never
    blocks on a FIFO, and optionally insists on the file's owner."""
    try:
        fd = os.open(path, _flags(os.O_RDONLY | os.O_NONBLOCK))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(st.st_mode) or (owner is not None and st.st_uid != owner):
        os.close(fd)
        return None
    return fd


def read_file(path: str, limit: int, owner: int | None = None) -> bytes | None:
    """The whole file if it is a regular file of at most limit bytes."""
    fd = _open_regular(path, owner)
    if fd is None:
        return None
    try:
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, 65536)
            if not chunk:
                return data
            data += chunk
        return None
    except OSError:
        return None
    finally:
        os.close(fd)


def read_first_line(path: str, limit: int) -> bytes | None:
    fd = _open_regular(path, None)
    if fd is None:
        return None
    try:
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, 65536)
            if not chunk:
                return data or None
            newline = chunk.find(b"\n")
            if newline >= 0:
                return data + chunk[:newline]
            data += chunk
        return None
    except OSError:
        return None
    finally:
        os.close(fd)


def read_json(path: str, limit: int, owner: int | None = None) -> dict | None:
    data = read_file(path, limit, owner)
    if data is None:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def write_json(path: str, value: dict) -> None:
    """Replace path atomically with a private (0600) JSON file."""
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def parse_utc(text: object) -> float | None:
    """Seconds since the epoch for an ISO 8601 UTC time such as Codex writes."""
    if not isinstance(text, str):
        return None
    value = re.sub(r"\.\d+", "", text.strip())
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        moment = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()


# --- processes ----------------------------------------------------------------


def pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tool_output(name: str, fallback: str, args: list[str]) -> str | None:
    """Output of a system tool, in the C locale and UTC so that dates read
    the same everywhere (Claude Code writes its procStart that way)."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("LC_")}
    env.update(LC_ALL="C", LANG="C", TZ="UTC0")
    try:
        result = subprocess.run(
            [shutil.which(name) or fallback, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.decode("utf-8", "replace")


def ps_field(pid: int, field: str) -> str | None:
    output = _tool_output("ps", "/bin/ps", ["-o", f"{field}=", "-p", str(pid)])
    text = " ".join(output.split()) if output else ""
    return text or None


def open_files(pids: list[int]) -> set[str] | None:
    """Paths the processes have open, or None if that cannot be seen."""
    if not pids:
        return set()
    if os.path.isdir("/proc/self/fd"):
        paths = set()
        for pid in pids:
            directory = f"/proc/{pid}/fd"
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                try:
                    paths.add(os.readlink(os.path.join(directory, name)))
                except OSError:
                    pass
        return paths
    output = _tool_output("lsof", "/usr/sbin/lsof", ["-n", "-P", "-w", "-F", "n", "-p", ",".join(map(str, pids))])
    if output is None:
        return None
    return {line[1:] for line in output.splitlines() if line.startswith("n/")}


def process_table() -> dict[int, tuple[int, str]]:
    """Every process: pid -> (parent pid, start time). A pid and its start
    time together name one process, even after the pid is reused."""
    table: dict[int, tuple[int, str]] = {}
    for line in (_tool_output("ps", "/bin/ps", ["-A", "-o", "pid=,ppid=,lstart="]) or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), " ".join(parts[2].split()))
    return table


def ancestors(pid: int, table: dict[int, tuple[int, str]] | None = None) -> list[int]:
    table = process_table() if table is None else table
    found: list[int] = []
    while pid in table and len(found) < 64:
        pid = table[pid][0]
        if pid <= 1 or pid in found:
            break
        found.append(pid)
    return found


def descendants(pid: int, table: dict[int, tuple[int, str]] | None = None) -> list[int]:
    table = process_table() if table is None else table
    children: dict[int, list[int]] = {}
    for child, (parent, _) in table.items():
        children.setdefault(parent, []).append(child)
    found, queue = [], [pid]
    while queue and len(found) < 256:
        for child in children.get(queue.pop(0), []):
            if child not in found:
                found.append(child)
                queue.append(child)
    return found


def exit_code(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


# --- state ----------------------------------------------------------------------


class StoreError(Exception):
    pass


def state_root() -> str:
    base = os.environ.get("XDG_STATE_HOME") or ""
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "zellij-ai-resume")


def private_dir(path: str) -> None:
    """Create path as a 0700 directory, or check that an existing one is a
    real directory of ours (not a symlink) and make it private."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise StoreError(f"{path} is not a directory")
    if info.st_uid != os.getuid():
        raise StoreError(f"{path} belongs to another user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)


class Lock:
    """An exclusive flock on a file in the state directory. The kernel drops
    it when the holder exits, so a crash never leaves a stale lock."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> bool:
        if self.fd is not None:
            return True
        fd = os.open(self.path, _flags(os.O_RDWR | os.O_CREAT | os.O_NONBLOCK), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise StoreError(f"{self.path} is not a lock file of ours")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        try:
            os.utime(fd)  # keeps locks in use away from garbage collection
        except (OSError, NotImplementedError):
            pass
        return True

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def held_elsewhere(self) -> bool:
        if self.fd is not None:
            return False
        if self.acquire():
            self.release()
            return False
        return True


def new_state(tool: str, args: list[str], cwd: str) -> dict:
    now = time.time()
    return {
        "format": STATE_FORMAT,
        "tool": tool,
        "args": list(args),
        "cwd": cwd,
        "binary": None,
        "launched": False,
        "session_id": None,
        "session_cwd": None,
        "failed_resume": None,
        "last_exit": None,
        "zellij_session": os.environ.get("ZELLIJ_SESSION_NAME"),
        "created_at": now,
        "updated_at": now,
    }


def validate_state(value: object, tool: str) -> dict | None:
    """The state if every field the launcher acts on is well formed."""
    if not isinstance(value, dict) or value.get("format") != STATE_FORMAT or value.get("tool") != tool:
        return None
    args = value.get("args")
    if not isinstance(args, list) or len(args) > MAX_ARGS:
        return None
    if not all(isinstance(arg, str) and "\0" not in arg for arg in args):
        return None
    if not is_abs_path(value.get("cwd")) or not isinstance(value.get("launched"), bool):
        return None
    for key in ("session_cwd", "binary"):
        if value.get(key) is not None and not is_abs_path(value.get(key)):
            return None
    for key in ("session_id", "failed_resume"):
        if value.get(key) is not None and not is_session_id(value.get(key)):
            return None
    for key in ("core_pid", "runs"):
        number = value.get(key)
        if number is not None and (not isinstance(number, int) or isinstance(number, bool) or number < 0):
            return None
    pane_id = value.get("pane_id")
    if pane_id is not None and not (isinstance(pane_id, str) and PANE_ID_RE.match(pane_id)):
        return None
    return value


class Store:
    """Pane state under $XDG_STATE_HOME/zellij-ai-resume (default
    ~/.local/state/zellij-ai-resume), private to the user."""

    def __init__(self) -> None:
        self.root = state_root()
        self.panes = os.path.join(self.root, "panes")
        self.locks = os.path.join(self.root, "locks")
        self.running = os.path.join(self.root, "running")
        self.log_path = os.path.join(self.root, "events.log")
        self.uid = os.getuid()
        try:
            os.makedirs(os.path.dirname(self.root), 0o700, exist_ok=True)
            for path in (self.root, self.panes, self.locks, self.running):
                private_dir(path)
        except OSError as error:
            raise StoreError(f"cannot use {self.root}: {error.strerror or error}") from None

    def pane_file(self, token: str) -> str:
        if not TOKEN_RE.match(token):
            raise ValueError("invalid pane token")
        return os.path.join(self.panes, token + ".json")

    def load(self, token: str, tool: str) -> dict | None:
        return validate_state(read_json(self.pane_file(token), MAX_STATE_BYTES, self.uid), tool)

    def load_any(self, token: str) -> dict | None:
        value = read_json(self.pane_file(token), MAX_STATE_BYTES, self.uid)
        tool = value.get("tool") if value else None
        return validate_state(value, tool) if tool in SPECS else None

    def save(self, token: str, state: dict) -> None:
        state["updated_at"] = time.time()
        write_json(self.pane_file(token), state)

    def delete(self, token: str) -> None:
        for path in (self.pane_file(token), self.pane_lock(token).path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def tokens(self) -> list[str]:
        try:
            names = os.listdir(self.panes)
        except OSError:
            return []
        return sorted(name[:-5] for name in names if name.endswith(".json") and TOKEN_RE.match(name[:-5]))

    def pane_lock(self, token: str) -> Lock:
        if not TOKEN_RE.match(token):
            raise ValueError("invalid pane token")
        return Lock(os.path.join(self.locks, f"pane-{token}.lock"))

    def session_lock(self, tool: str, session_id: str) -> Lock:
        if tool not in SPECS or not is_session_id(session_id):
            raise ValueError("invalid session")
        return Lock(os.path.join(self.locks, f"{tool}-{session_id.lower()}.lock"))

    def log(self, event: str, **fields: object) -> None:
        """Append one line to events.log; ids and paths only, never prompts."""
        record = {"t": datetime.datetime.now().isoformat(timespec="seconds"), "pid": os.getpid(), "event": event}
        record.update(fields)
        line = (json.dumps(record, sort_keys=True, default=str) + "\n").encode("utf-8")
        try:
            try:
                if os.lstat(self.log_path).st_size > MAX_LOG_BYTES:
                    os.replace(self.log_path, self.log_path + ".1")
            except FileNotFoundError:
                pass
            fd = os.open(self.log_path, _flags(os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK), 0o600)
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    os.write(fd, line)
            finally:
                os.close(fd)
        except OSError:
            pass

    def gc(self, days: float = STALE_DAYS) -> int:
        """Forget panes untouched for `days` (Claude Code deletes its own
        transcripts after 30 days by default). Running panes hold their lock
        and are never removed."""
        cutoff = time.time() - days * 86400
        removed = 0
        for token in self.tokens():
            path = self.pane_file(token)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if info.st_mtime > cutoff:
                continue
            lock = self.pane_lock(token)
            try:
                if not lock.acquire():
                    continue
                os.unlink(path)
                os.unlink(lock.path)
                removed += 1
            except (OSError, StoreError):
                continue
            finally:
                lock.release()
        for directory in (self.panes, self.locks):
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                path = os.path.join(directory, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                if name.startswith(".tmp-") and info.st_mtime < time.time() - 86400:
                    try:
                        os.unlink(path)  # left behind by an interrupted write
                    except OSError:
                        pass
                elif LOCK_NAME_RE.match(name) and info.st_mtime < cutoff:
                    lock = Lock(path)
                    try:
                        if lock.acquire():
                            os.unlink(path)
                    except (OSError, StoreError):
                        pass
                    finally:
                        lock.release()
        return removed


# --- command lines ----------------------------------------------------------------
#
# The launchers need to know, without running the CLI, whether an invocation
# is an interactive session (tracked) or anything else (handed straight to
# the CLI), and which words to repeat when resuming. Repeating a positional
# prompt would send it to the resumed conversation a second time, so only
# options known to be settings are repeated; anything unrecognised is left
# out, and the launcher says so.

FLAG, VALUE, OPTIONAL, MANY = "flag", "value", "optional", "many"


class Word:
    __slots__ = ("kind", "name", "words")

    def __init__(self, kind: str, name: str | None, words: list[str]) -> None:
        self.kind = kind  # "option", "unknown", "positional" or "separator"
        self.name = name
        self.words = words

    def value(self) -> str | None:
        if len(self.words) > 1:
            return self.words[1]
        head = self.words[0]
        if head.startswith("--") and "=" in head:
            return head.partition("=")[2]
        if self.name and len(head) > 2 and not head.startswith("--"):
            return head[3:] if head[2] == "=" else head[2:]  # -m=o3, -mo3
        return None


def _is_option(word: str) -> bool:
    return len(word) > 1 and word.startswith("-")


def tokenize(argv: list[str], options: dict[str, str]) -> list[Word]:
    """Group argv the way the CLI's own parser does (commander for Claude
    Code, clap for Codex): FLAG takes no value, VALUE always takes the next
    word, OPTIONAL takes it unless it looks like an option, MANY takes words
    up to the next option."""
    items: list[Word] = []
    index, count = 0, len(argv)
    while index < count:
        word = argv[index]
        if word == "--":
            items.append(Word("separator", None, [word]))
            items.extend(Word("positional", None, [rest]) for rest in argv[index + 1 :])
            break
        if not _is_option(word):
            items.append(Word("positional", None, [word]))
            index += 1
            continue
        name, equals, _ = word.partition("=")
        arity = options.get(name)
        if arity is None and not word.startswith("--") and options.get(word[:2], FLAG) != FLAG:
            items.append(Word("option", word[:2], [word]))  # short option with its value attached
            index += 1
            continue
        end = index + 1
        if arity is None:
            # Not an option we know. Whether it takes a value cannot be told
            # apart from a prompt, so it owns the words up to the next option.
            if not equals:
                while end < count and not _is_option(argv[end]):
                    end += 1
            items.append(Word("unknown", name, argv[index:end]))
            index = end
            continue
        if not equals:
            if arity == VALUE:
                end = min(end + 1, count)
            elif arity == OPTIONAL:
                if end < count and not _is_option(argv[end]):
                    end += 1
            elif arity == MANY:
                while end < count and not _is_option(argv[end]):
                    end += 1
        items.append(Word("option", name, argv[index:end]))
        index = end
    return items


class Parsed:
    def __init__(self, items: list[Word]) -> None:
        self.items = items
        self.names = {item.name for item in items if item.kind == "option"}
        self.positionals: list[str] = []
        for item in items:
            if item.kind == "separator":
                break
            if item.kind == "positional":
                self.positionals.append(item.words[0])
        self.command = self.positionals[0] if self.positionals else None

    def option(self, *names: str) -> Word | None:
        for item in self.items:
            if item.kind == "option" and item.name in names:
                return item
        return None

    def split(self, drop: set[str] | frozenset[str]) -> tuple[list[str], list[str]]:
        """(options to repeat on resume, everything else)."""
        kept: list[str] = []
        dropped: list[str] = []
        for item in self.items:
            if item.kind == "option" and item.name not in drop:
                kept.extend(item.words)
            else:
                dropped.extend(item.words)
        return kept, dropped


class Plan:
    def __init__(self, mode: str, args: list[str], cwd: str, session_id: str | None, message: str | None = None) -> None:
        self.mode = mode  # "first", "resume", "fork" or "fresh"
        self.args = args
        self.cwd = cwd
        self.session_id = session_id  # the conversation it will be on, when known up front
        self.message = message


# --- Claude Code ------------------------------------------------------------------


def claude_dir() -> str:
    configured = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    return configured if os.path.isabs(configured) else os.path.join(os.path.expanduser("~"), ".claude")


class Claude:
    name = "claude"
    options = {
        "--add-dir": MANY, "--agent": VALUE, "--agents": VALUE,
        "--allow-dangerously-skip-permissions": FLAG,
        "--allowedTools": MANY, "--allowed-tools": MANY,
        "--append-system-prompt": VALUE, "--append-system-prompt-file": VALUE,
        "--autocompact": VALUE, "--ax-screen-reader": FLAG,
        "--bg": FLAG, "--background": FLAG, "--bare": FLAG, "--betas": MANY, "--brief": FLAG,
        "--chrome": FLAG, "--no-chrome": FLAG, "--cloud": OPTIONAL,
        "-c": FLAG, "--continue": FLAG, "--dangerously-skip-permissions": FLAG,
        "-d": OPTIONAL, "--debug": OPTIONAL, "--debug-file": VALUE,
        "--desktop": FLAG, "--disable-slash-commands": FLAG,
        "--disallowedTools": MANY, "--disallowed-tools": MANY,
        "--effort": VALUE, "--environment": VALUE,
        "--exclude-dynamic-system-prompt-sections": FLAG,
        "--fallback-model": VALUE, "--file": MANY, "--fork-session": FLAG,
        "--forward-subagent-text": FLAG, "--from-pr": OPTIONAL,
        "-h": FLAG, "--help": FLAG, "--ide": FLAG,
        "--include-hook-events": FLAG, "--include-partial-messages": FLAG,
        "--input-format": VALUE, "--json-schema": VALUE, "--max-budget-usd": VALUE, "--max-turns": VALUE,
        "--mcp-config": MANY, "--mcp-debug": FLAG, "--model": VALUE, "-n": VALUE, "--name": VALUE,
        "--no-session-persistence": FLAG, "--output-format": VALUE,
        "--permission-mode": VALUE, "--permission-prompt-tool": VALUE, "--permission-prompts": VALUE,
        "--plugin-dir": VALUE, "--plugin-url": VALUE,
        "-p": FLAG, "--print": FLAG, "--prompt-suggestions": OPTIONAL,
        "--remote-control": OPTIONAL, "--remote-control-session-name-prefix": VALUE,
        "--replay-user-messages": FLAG, "--restricted": FLAG,
        "-r": OPTIONAL, "--resume": OPTIONAL, "--safe-mode": FLAG,
        "--session-id": VALUE, "--setting-sources": VALUE, "--settings": VALUE,
        "--strict-mcp-config": FLAG, "--system-prompt": VALUE, "--system-prompt-file": VALUE,
        "--system-prompt-snapshot": VALUE, "--teleport": OPTIONAL, "--tmux": FLAG,
        "--tools": MANY, "--verbose": FLAG, "-v": FLAG, "--version": FLAG,
        "-w": OPTIONAL, "--worktree": OPTIONAL,
    }
    # Not an interactive local session: run the CLI untouched.
    passthrough = frozenset({
        "-p", "--print", "-h", "--help", "-v", "--version", "--bg", "--background",
        "--cloud", "--teleport", "--desktop", "--environment",
    })
    commands = frozenset({
        "agents", "attach", "auth", "auto-mode", "config", "doctor", "gateway", "help", "import",
        "install", "kill", "logs", "mcp", "migrate-installer", "plugin", "plugins", "purge",
        "respawn", "rm", "setup-token", "stop", "ultrareview", "update", "upgrade",
    })
    # Options that pick or create the conversation themselves.
    selectors = frozenset({"--session-id", "-r", "--resume", "-c", "--continue", "--from-pr", "--fork-session"})
    # One-shot options that must not be repeated when resuming.
    resume_drop = selectors | {"-n", "--name", "-w", "--worktree", "--tmux", "--file"}

    def parse(self, args: list[str]) -> Parsed:
        return Parsed(tokenize(args, self.options))

    def passthrough_reason(self, parsed: Parsed) -> str | None:
        if parsed.names & self.passthrough:
            return "not an interactive session"
        if parsed.command in self.commands:
            return "a subcommand"
        return None

    def first_args(self, parsed: Parsed, args: list[str], new_id: str) -> tuple[list[str], str | None]:
        if not parsed.names & self.selectors:
            return ["--session-id", new_id, *args], new_id
        chosen = parsed.option("--session-id")
        if chosen and is_session_id(chosen.value()):
            return list(args), chosen.value()
        resumed = parsed.option("-r", "--resume")
        if resumed and is_session_id(resumed.value()) and "--fork-session" not in parsed.names:
            return list(args), resumed.value()
        return list(args), None  # --continue, a picker, a name or a fork: learned from the registry

    def resume_args(self, session_id: str, kept: list[str], fork: bool) -> list[str]:
        return ["--resume", session_id, *(["--fork-session"] if fork else []), *kept]

    def fresh_args(self, parsed: Parsed, kept: list[str], new_id: str) -> tuple[list[str], str | None]:
        return ["--session-id", new_id, *kept], new_id

    def split(self, parsed: Parsed, state: dict) -> tuple[list[str], list[str]]:
        return parsed.split(self.resume_drop)

    def resume_cwd(self, parsed: Parsed, state: dict) -> str:
        # Transcripts are stored per working directory, and --worktree moves
        # Claude Code into a new one: resume where the conversation lives.
        return first_dir(state.get("session_cwd"), state.get("cwd"))

    def transcript_exists(self, session_id: str) -> bool:
        pattern = os.path.join(glob.escape(claude_dir()), "projects", "*", session_id + ".jsonl")
        return any(os.path.isfile(path) and not os.path.islink(path) for path in glob.glob(pattern))

    def open_elsewhere(self, session_id: str) -> bool:
        """Whether a live Claude Code process (ours or not) has the conversation open."""
        registry = os.path.join(claude_dir(), "sessions")
        try:
            names = os.listdir(registry)
        except OSError:
            return False
        needle = session_id.encode("ascii")
        for name in names:
            if not name.endswith(".json") or not name[:-5].isdigit():
                continue
            data = read_file(os.path.join(registry, name), MAX_REGISTRY_BYTES)
            if not data or needle not in data:
                continue
            try:
                entry = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("sessionId") != session_id or not pid_alive(entry.get("pid")):
                continue
            recorded = entry.get("procStart")
            if isinstance(recorded, str) and LSTART_RE.match(" ".join(recorded.split())):
                actual = ps_field(entry["pid"], "lstart")
                if actual and actual != " ".join(recorded.split()):
                    continue  # the pid was reused by another process
            return True
        return False

    def watcher(self, pane: "Pane", child_pid: int, plan: Plan) -> "Watcher":
        return ClaudeWatcher(pane, child_pid)


# --- Codex ------------------------------------------------------------------------


def codex_home() -> str:
    configured = os.environ.get("CODEX_HOME") or ""
    return configured if os.path.isabs(configured) else os.path.join(os.path.expanduser("~"), ".codex")


class Codex:
    name = "codex"
    options = {
        "-c": VALUE, "--config": VALUE, "--enable": VALUE, "--disable": VALUE,
        "--remote": VALUE, "--remote-auth-token-env": VALUE, "--strict-config": FLAG,
        "-i": MANY, "--image": MANY, "-m": VALUE, "--model": VALUE,
        "--oss": FLAG, "--local-provider": VALUE, "-p": VALUE, "--profile": VALUE,
        "-s": VALUE, "--sandbox": VALUE, "--approve-for-me": FLAG,
        "--dangerously-bypass-approvals-and-sandbox": FLAG, "--yolo": FLAG, "--full-auto": FLAG,
        "--dangerously-bypass-hook-trust": FLAG,
        "-C": VALUE, "--cd": VALUE, "--worktree": FLAG, "--add-dir": VALUE,
        "-a": VALUE, "--ask-for-approval": VALUE, "--search": FLAG,
        "--no-alt-screen": FLAG, "--no-daemon": FLAG,
        "-h": FLAG, "--help": FLAG, "-V": FLAG, "--version": FLAG,
        "--last": FLAG, "--all": FLAG, "--include-non-interactive": FLAG,
    }
    passthrough = frozenset({"-h", "--help", "-V", "--version", "--remote"})
    commands = frozenset({
        "a", "agents", "app", "app-server", "apply", "archive", "cloud", "completion", "debug",
        "delete", "doctor", "e", "exec", "exec-server", "features", "generate-ts", "help", "login",
        "logout", "mcp", "mcp-server", "migrate-nfs-db", "migrate-rollouts", "plugin", "queue",
        "remote-control", "responses-api-proxy", "review", "sandbox", "unarchive", "update",
    })
    resume_drop = frozenset({"-i", "--image", "--worktree", "--last", "--all", "--include-non-interactive"})

    def parse(self, args: list[str]) -> Parsed:
        return Parsed(tokenize(args, self.options))

    def passthrough_reason(self, parsed: Parsed) -> str | None:
        if parsed.names & self.passthrough:
            return "not an interactive local session"
        if parsed.command in self.commands:
            return "a subcommand"
        return None

    def first_args(self, parsed: Parsed, args: list[str], new_id: str) -> tuple[list[str], str | None]:
        if parsed.command == "resume" and len(parsed.positionals) > 1 and is_session_id(parsed.positionals[1]):
            return list(args), parsed.positionals[1]
        return list(args), None  # Codex picks the id; the watcher finds it

    def resume_args(self, session_id: str, kept: list[str], fork: bool) -> list[str]:
        return ["fork" if fork else "resume", *kept, session_id]

    def fresh_args(self, parsed: Parsed, kept: list[str], new_id: str) -> tuple[list[str], str | None]:
        if parsed.command == "resume":
            return ["resume", *kept], None  # the conversation was picked but never seen: offer the picker again
        return list(kept), None

    def _worktree_cwd(self, parsed: Parsed, state: dict) -> str | None:
        session_cwd = state.get("session_cwd")
        if "--worktree" in parsed.names and is_abs_path(session_cwd) and os.path.isdir(session_cwd):
            return session_cwd
        return None

    def split(self, parsed: Parsed, state: dict) -> tuple[list[str], list[str]]:
        drop = set(self.resume_drop)
        if self._worktree_cwd(parsed, state):
            drop |= {"-C", "--cd"}  # resuming inside the worktree already
        return parsed.split(drop)

    def resume_cwd(self, parsed: Parsed, state: dict) -> str:
        # -C/--cd is repeated and relative to where the pane was started.
        return self._worktree_cwd(parsed, state) or first_dir(state.get("cwd"))

    def transcript_exists(self, session_id: str) -> bool:
        pattern = os.path.join(glob.escape(codex_home()), "sessions", "*", "*", "*", f"rollout-*-{session_id}.jsonl")
        return any(os.path.isfile(path) and not os.path.islink(path) for path in glob.glob(pattern))

    def open_elsewhere(self, session_id: str) -> bool:
        return False  # Codex keeps no process registry; the session locks cover zcodex panes

    def watcher(self, pane: "Pane", child_pid: int, plan: Plan) -> "Watcher":
        running = self.parse(plan.args)
        if "--worktree" in running.names:
            expected = None  # the new worktree's path is not known in advance
        else:
            cd = running.option("-C", "--cd")
            expected = os.path.realpath(os.path.join(plan.cwd, cd.value()) if cd and cd.value() else plan.cwd)
        # `codex resume` without an id (picker, --last, a name) reopens an
        # older file: which one cannot be guessed, only seen.
        look_back = running.command == "resume" and plan.session_id is None
        return CodexWatcher(pane, child_pid, expected, guess=expected is not None and not look_back, look_back=look_back)


SPECS = {"claude": Claude(), "codex": Codex()}


LAUNCHER_MARK = b"# Generated by zellij-ai-resume install.sh"


def _usable_binary(path: str) -> bool:
    """An executable file that is not one of our own launchers (a `claude`
    on PATH that is really zclaude would otherwise start itself forever)."""
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return False
    try:
        with open(path, "rb") as handle:
            head = handle.read(512)
    except OSError:
        return False
    return not (head.startswith(b"#!") and LAUNCHER_MARK in head)


def find_binary(spec, recorded: object = None) -> str | None:
    """The CLI on PATH (absolute entries only), else where it was found at
    first launch: a resurrected Zellij server may have a PATH without (say)
    mise's directories."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory.startswith("/"):
            candidate = os.path.join(directory, spec.name)
            if _usable_binary(candidate):
                return candidate
    if is_abs_path(recorded) and _usable_binary(recorded):
        return recorded
    return None


def child_env(token: str | None) -> dict[str, str]:
    """The CLI's environment: ours, marked so that a zclaude/zcodex started
    from inside it (or a `claude` wrapper that calls zclaude) runs the plain
    CLI instead of opening a second recovery layer."""
    env = dict(os.environ)
    env.pop("ZJR_HOPS", None)
    if token:
        env["ZJR_PANE"] = token
    return env


# --- a running pane -----------------------------------------------------------------


class Pane:
    """A pane's state file plus the lock on the conversation it is on."""

    def __init__(self, store: Store, token: str, state: dict, spec, owned: bool) -> None:
        self.store = store
        self.token = token
        self.state = state
        self.spec = spec
        self.owned = owned  # False when another pane runs the same saved command
        self.guard = threading.Lock()
        self.session_lock: Lock | None = None

    def current(self) -> str | None:
        with self.guard:
            return self.state.get("session_id")

    def claimed_elsewhere(self, session_id: str) -> bool:
        try:
            lock = self.store.session_lock(self.spec.name, session_id)
            with self.guard:
                if self.session_lock is not None and self.session_lock.path == lock.path:
                    return False  # held by this pane
            return lock.held_elsewhere()
        except (OSError, StoreError, ValueError):
            return True

    def lock_session(self, session_id: str | None) -> bool:
        """Hold the lock for session_id (dropping the previous one). False if
        another pane holds it."""
        with self.guard:
            return self._lock_session(session_id)

    def _lock_session(self, session_id: str | None) -> bool:
        if session_id is not None and self.session_lock is not None:
            if self.session_lock.path == self.store.session_lock(self.spec.name, session_id).path:
                return True  # already ours: never let go of it in between
        previous, self.session_lock = self.session_lock, None
        if previous is not None:
            previous.release()
        if session_id is None:
            return True
        lock = self.store.session_lock(self.spec.name, session_id)
        try:
            if lock.acquire():
                self.session_lock = lock
                return True
        except (OSError, StoreError):
            pass
        return False

    def set_session(self, session_id: str, session_cwd: str | None = None, claim: bool = False) -> bool:
        """Record the conversation the CLI is on. With claim, only if this
        pane can take its lock (another pane may be about to take it)."""
        with self.guard:
            changed = session_id != self.state.get("session_id")
            if not changed and (session_cwd is None or session_cwd == self.state.get("session_cwd")):
                return True
            if changed:
                if not self._lock_session(session_id) and claim:
                    return False
                self.state["failed_resume"] = None
            self.state["session_id"] = session_id
            if session_cwd is not None:
                self.state["session_cwd"] = session_cwd
            self._save()
        if changed:
            self.store.log("session", token=self.token, tool=self.spec.name, session=session_id, cwd=session_cwd)
        return True

    def save(self) -> bool:
        with self.guard:
            return self._save()

    def _save(self) -> bool:
        if not self.owned:
            return True
        try:
            self.store.save(self.token, self.state)
            return True
        except OSError as error:
            note(self.spec.name, f"could not save pane state: {error.strerror or error}")
            return False

    def release(self) -> None:
        with self.guard:
            self._lock_session(None)


class Running:
    """This core's entry in running/<pid>.json while it runs: how `zjr
    reload` finds and checks it. A duplicate pane, which has no state file of
    its own, has one too."""

    def __init__(self, store: Store, token: str, tool: str, owned: bool) -> None:
        self.path = os.path.join(store.running, f"{os.getpid()}.json")
        pane_id = os.environ.get("ZELLIJ_PANE_ID") or ""
        session = os.environ.get("ZELLIJ_SESSION_NAME") or None
        self.info = {"pid": os.getpid(), "token": token, "tool": tool, "owned": owned, "runs": 0,
                     "session_id": None, "pane_id": pane_id if PANE_ID_RE.match(pane_id) else None,
                     "zellij_session": session if session and len(session) <= 256 else None}

    def update(self, **fields: object) -> None:
        self.info.update(fields)
        try:
            write_json(self.path, self.info)
        except OSError:
            pass  # reload cannot find this pane; everything else carries on

    def remove(self) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass


class Watcher(threading.Thread):
    """Follows which conversation the CLI is on and records it."""

    interval = 1.0

    def __init__(self, pane: Pane | None, child_pid: int) -> None:
        super().__init__(name="zjr-watcher", daemon=True)
        self.pane = pane
        self.child_pid = child_pid
        self.spawned = time.time() - 2.0  # created right after the CLI was started
        self.parent = os.getppid()
        self.halt = threading.Event()
        self.hung_up = False
        self.reported: set[str] = set()

    def run(self) -> None:
        while not self.halt.wait(self.interval):
            self.tick()

    def tick(self) -> None:
        if not self.hung_up and os.getppid() != self.parent:
            # The launcher shell is gone (killed on its own): take the CLI
            # down the way closing the pane would.
            self.hung_up = True
            try:
                os.kill(self.child_pid, signal.SIGHUP)
            except OSError:
                pass
        if self.pane is None:
            return
        try:
            self.poll()
        except Exception as error:  # tracking must never take the CLI down
            kind = type(error).__name__
            if kind not in self.reported:
                self.reported.add(kind)
                self.pane.store.log("watcher-error", token=self.pane.token, error=f"{kind}: {error}")

    def poll(self) -> None:
        pass

    def finish(self) -> None:
        self.halt.set()
        if self.is_alive():
            self.join(timeout=5)
        if self.pane is not None:
            try:
                self.poll()
            except Exception:
                pass


class ClaudeWatcher(Watcher):
    """Claude Code keeps ~/.claude/sessions/<pid>.json up to date with the
    conversation each running process is on (/clear, /resume, ...)."""

    def __init__(self, pane: Pane, child_pid: int) -> None:
        super().__init__(pane, child_pid)
        self.registry = os.path.join(claude_dir(), "sessions")
        self.entry_pid: int | None = None
        self.seen: tuple[int, int] | None = None
        self.next_search = time.monotonic() + 2.0

    def _entry(self, pid: int) -> dict | None:
        """The registry entry of pid, if it was written by this process: a
        Claude Code that was killed leaves its file behind, and the pid can
        come round again."""
        entry = read_json(os.path.join(self.registry, f"{pid}.json"), MAX_REGISTRY_BYTES)
        if entry is None or entry.get("pid") != pid:
            return None
        started = entry.get("startedAt")
        if isinstance(started, (int, float)) and not isinstance(started, bool):
            return entry if started / 1000.0 >= self.spawned else None
        try:
            fresh = os.stat(os.path.join(self.registry, f"{pid}.json")).st_mtime >= self.spawned
        except OSError:
            return None
        return entry if fresh else None

    def poll(self) -> None:
        if self.entry_pid is None or not pid_alive(self.entry_pid):
            self.entry_pid = None
            if self._entry(self.child_pid):
                self.entry_pid = self.child_pid
            elif time.monotonic() >= self.next_search:
                # Started through a wrapper that forks: look among descendants.
                self.next_search = time.monotonic() + 5.0
                self.entry_pid = next((pid for pid in descendants(self.child_pid) if self._entry(pid)), None)
            if self.entry_pid is None:
                return
        path = os.path.join(self.registry, f"{self.entry_pid}.json")
        try:
            info = os.stat(path)
        except OSError:
            return
        signature = (info.st_mtime_ns, info.st_size)
        if signature == self.seen:
            return
        entry = self._entry(self.entry_pid)
        if entry is None:
            return
        self.seen = signature
        session_id = entry.get("sessionId")
        if is_session_id(session_id):
            cwd = entry.get("cwd")
            self.pane.set_session(session_id, cwd if is_abs_path(cwd) else None)


class CodexWatcher(Watcher):
    """Codex writes each conversation to sessions/YYYY/MM/DD/rollout-*-<id>.jsonl
    and keeps no process registry.

    Exact: the rollout file the pane's own processes hold open (Codex running
    in-process keeps it open; read from /proc on Linux, lsof on macOS). That
    also follows /new and /resume inside the pane.

    Otherwise (Codex's shared daemon writes the files for every client) a
    guess: the first interactive rollout created after the CLI started, in
    its working directory, that no other zcodex pane has claimed. A guess is
    taken only once, never replaces a conversation already found, and is not
    made at all where it would be ambiguous (--worktree, the resume picker).
    Two Codex sessions started in one directory within seconds of each other
    can still be swapped this way."""

    interval = 2.0

    def __init__(self, pane: Pane, child_pid: int, expected_cwd: str | None, guess: bool, look_back: bool) -> None:
        super().__init__(pane, child_pid)
        self.root = os.path.realpath(os.path.join(codex_home(), "sessions"))
        self.expected_cwd = expected_cwd
        self.guess = guess
        self.look_back = look_back  # `codex resume` picker: an older rollout is reopened
        self.examined: set[str] = set()  # new rollouts already checked against open files
        self.next_look = 0.0
        self.look_delay = 4.0
        self.meta: dict[str, tuple[str, float | None, str | None] | None] = {}

    def _paths(self) -> list[str]:
        days = set()
        for moment in (datetime.datetime.now(), datetime.datetime.now(datetime.timezone.utc)):
            for back in (0, 1):
                day = moment - datetime.timedelta(days=back)
                days.add(os.path.join(self.root, f"{day:%Y}", f"{day:%m}", f"{day:%d}"))
        paths = []
        for day in days:
            try:
                names = os.listdir(day)
            except OSError:
                continue
            paths.extend(os.path.join(day, name) for name in names if name.startswith("rollout-") and name.endswith(".jsonl"))
        return paths

    def _read_meta(self, path: str) -> tuple[str, float | None, str | None] | None:
        if path in self.meta:
            return self.meta[path]
        line = read_first_line(path, MAX_META_BYTES)
        if line is None:
            return None  # being written; try again next time
        result = None
        try:
            record = json.loads(line.decode("utf-8"))
            payload = record.get("payload") if record.get("type") == "session_meta" else None
        except (ValueError, AttributeError):
            payload = None
        if isinstance(payload, dict):
            session_id = payload.get("id")
            interactive = payload.get("source") == "cli" or payload.get("originator") in ("codex-tui", "codex_cli_rs")
            if is_session_id(session_id) and path.endswith(f"-{session_id}.jsonl") and interactive:
                cwd = payload.get("cwd")
                result = (session_id, parse_utc(payload.get("timestamp")), os.path.realpath(cwd) if is_abs_path(cwd) else None)
        self.meta[path] = result
        return result

    def _held(self) -> list[str]:
        """Rollout files open in the CLI's process tree (empty if unknown)."""
        paths = open_files([self.child_pid, *descendants(self.child_pid)]) or set()
        prefix = self.root + os.sep
        return [path for path in paths if path.startswith(prefix) and os.path.basename(path).startswith("rollout-")]

    def poll(self) -> None:
        current = self.pane.current()
        fresh = []  # interactive rollouts created since the CLI started
        for path in self._paths():
            meta = self._read_meta(path)
            if meta and meta[0] != current and meta[1] is not None and meta[1] >= self.spawned:
                fresh.append((meta[1], path, meta))
        unexamined = {path for _, path, _ in fresh} - self.examined
        if not unexamined:
            # Nothing new. Only the resume picker, which reopens an older
            # file, keeps looking, and less and less often.
            if not (self.look_back and current is None and time.monotonic() >= self.next_look):
                return
            self.next_look = time.monotonic() + self.look_delay
            self.look_delay = min(self.look_delay * 2, 60.0)
        self.examined |= unexamined
        newest = None
        for path in self._held():
            meta = self._read_meta(path)
            try:
                modified = os.path.getmtime(path)
            except OSError:
                continue
            if meta and (newest is None or modified > newest[0]):
                newest = (modified, meta)
        if newest is not None:
            if newest[1][0] != current:
                self.pane.set_session(newest[1][0], newest[1][2])
            return
        if current is not None or not self.guess:
            return  # never replace a conversation with a guess
        for _, path, (session_id, _, cwd) in sorted(fresh):
            if self.expected_cwd and cwd != self.expected_cwd:
                continue
            if not self.pane.claimed_elsewhere(session_id) and self.pane.set_session(session_id, cwd, claim=True):
                return


# --- launching ------------------------------------------------------------------------


def _ignore(signum: int, frame: object) -> None:
    pass  # handled (not ignored), so the CLI starts with the default action


def exec_plain(spec, args: list[str]) -> int:
    """Become the CLI itself: no tracking."""
    binary = find_binary(spec)
    if binary is None:
        note(spec.name, f"{spec.name} not found on PATH")
        return 127
    # A `claude` wrapper that calls zclaude would bounce between the two
    # forever; such hops come within milliseconds, nested use does not.
    env = dict(os.environ)
    count, _, stamp = env.get("ZJR_HOPS", "").partition("@")
    try:
        recent = count.isdigit() and 0 <= time.time() - float(stamp) < 2.0
    except ValueError:
        recent = False
    hops = int(count) + 1 if recent else 1
    if hops > 3:
        note(spec.name, f"{binary} leads back to z{spec.name}; fix PATH so that {spec.name} is the real CLI")
        return 126
    env["ZJR_HOPS"] = f"{hops}@{time.time():.3f}"
    try:
        os.execve(binary, [spec.name, *args], env)
    except OSError as error:
        note(spec.name, f"cannot run {binary}: {error.strerror or error}")
        return 126


def make_plan(spec, state: dict, new_id: str, busy) -> Plan:
    """Decide how to start the CLI for this pane. busy(session_id) tells
    whether another pane or process has the conversation open."""
    parsed = spec.parse(state["args"])
    if not state["launched"]:
        args, expected = spec.first_args(parsed, state["args"], new_id)
        return Plan("first", args, first_dir(state["cwd"]), expected)
    kept, dropped = spec.split(parsed, state)
    session_id = state.get("session_id")
    message = None
    if session_id and state.get("failed_resume") == session_id:
        message = (f"resuming conversation {session_id} failed last time; starting a new one "
                   f"(the old one is still listed by /resume)")
        session_id = None
    elif session_id and not spec.transcript_exists(session_id):
        message = f"conversation {session_id} is no longer on disk; starting a new one"
        session_id = None
    if session_id:
        if busy(session_id):
            return Plan("fork", spec.resume_args(session_id, kept, fork=True), spec.resume_cwd(parsed, state), None,
                        f"conversation {session_id} is open elsewhere; continuing in a copy of it")
        message = f"resuming conversation {session_id}"
        if dropped:
            message += f" (not repeated: {short(shlex.join(dropped))})"
        return Plan("resume", spec.resume_args(session_id, kept, fork=False), spec.resume_cwd(parsed, state),
                    session_id, message)
    args, expected = spec.fresh_args(parsed, kept, new_id)
    return Plan("fresh", args, first_dir(state["cwd"]), expected, message)


def _still_stopped(pid: int) -> bool:
    state = ps_field(pid, "stat")
    return bool(state) and state.startswith("T")


RELOAD_GRACE_SECONDS = 5.0

# Leave modes a CLI may have set and not undone (Codex, for one, leaves
# them all on when it is killed): alternate screen, mouse and focus
# reporting, bracketed paste, the kitty keyboard protocol, application cursor
# keys, attributes, a hidden cursor, autowrap, the scroll region (whose reset
# homes the cursor, hence the save and restore around it).
TERMINAL_RESET = (
    b"\x1b[?1049l"
    b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[?1015l"
    b"\x1b[?1004l\x1b[?2004l"
    b"\x1b[<u"
    b"\x1b[?1l\x1b>"
    b"\x1b[0m\x1b[?25h\x1b[?7h"
    b"\x1b7\x1b[r\x1b8"
)
CLEAR_SCREEN = b"\x1b[H\x1b[2J"


def reset_terminal(saved: list | None, clear: bool) -> None:
    """Put the terminal back as it was before the CLI started, if this job
    owns it (from the background, tcsetattr would stop us with SIGTTOU or
    change the modes under whatever runs in the foreground)."""
    try:
        if not (os.isatty(1) and os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp()):
            return
    except OSError:
        return
    if saved is not None:
        try:
            termios.tcsetattr(0, termios.TCSADRAIN, saved)
        except (termios.error, OSError):
            pass
    try:
        os.write(1, TERMINAL_RESET + (CLEAR_SCREEN if clear else b""))
    except OSError:
        pass


def supervise(spec, binary: str, plan: Plan, env: dict[str, str], started,
              reload: list[bool]) -> tuple[int, float, bool, bool]:
    """Run the CLI in the foreground of this terminal and wait for it.

    Returns its exit status, how long it ran, whether a hangup or
    termination signal arrived on the way, and whether it was stopped for a
    reload (reload[0] set by SIGUSR1: SIGTERM, then SIGKILL after a grace
    period).

    Signal handlers only take note; waiting for the CLI and signalling it
    both happen on this thread, so a pid is never signalled once reaped."""
    wake_read, wake_write = os.pipe()
    for fd in (wake_read, wake_write):
        os.set_blocking(fd, False)
    pending: list = []  # [signal, when to pass it on]
    stopping: list = []  # [when to stop asking] while a reload ends the CLI
    tree: dict[int, str] = {}  # the CLI's descendants when the reload began: pid -> start time
    signalled = [False]

    def end_tree(signum: int) -> None:
        # A wrapper may fork the real CLI rather than exec it: signal all of
        # the CLI's processes, each only while it is the same process (pids
        # of exited ones can be reused).
        table = process_table()
        for pid, started in tree.items():
            if pid in table and table[pid][1] == started:
                _signal(pid, signum)

    def tree_alive() -> bool:
        table = process_table()
        return any(pid in table and table[pid][1] == started for pid, started in tree.items())

    def forward(signum: int, frame: object) -> None:
        # A terminal hangup reaches the whole foreground job, the CLI too:
        # pass the signal on only if the CLI is still running a moment later,
        # that is, when this process alone was signalled.
        signalled[0] = True
        pending[:] = [signum, time.monotonic() + 0.5]

    handlers = {signal.SIGINT: _ignore, signal.SIGQUIT: _ignore, signal.SIGCHLD: _ignore,
                signal.SIGTERM: forward, signal.SIGHUP: forward}
    previous = {signum: signal.signal(signum, handler) for signum, handler in handlers.items()}
    previous_wakeup = signal.set_wakeup_fd(wake_write, warn_on_full_buffer=False)
    began = time.monotonic()
    try:
        try:
            process = subprocess.Popen([spec.name, *plan.args], executable=binary, cwd=plan.cwd, env=env, close_fds=True)
        except OSError as error:
            note(spec.name, f"cannot run {binary}: {error.strerror or error}")
            return 127, 0.0, False, False
        try:
            started(process.pid)
        except Exception as error:  # still wait for the CLI, just untracked
            note(spec.name, f"cannot track this session: {error}")
        while True:
            try:
                pid, status = os.waitpid(process.pid, os.WNOHANG | os.WUNTRACED)
            except ChildProcessError:
                code = process.returncode if process.returncode is not None else 1
                return code, time.monotonic() - began, signalled[0], bool(stopping) and not signalled[0]
            now = time.monotonic()
            if pid == 0:
                # Not reaped yet, so the pid is still our child's.
                if reload[0] and not stopping and not signalled[0]:
                    stopping[:] = [now + RELOAD_GRACE_SECONDS]
                    table = process_table()
                    tree.update((child, table[child][1]) for child in descendants(process.pid, table))
                    _signal(process.pid, signal.SIGTERM)
                    end_tree(signal.SIGTERM)
                elif stopping and now >= stopping[0]:
                    stopping[:] = [now + 3600.0]
                    _signal(process.pid, signal.SIGKILL)
                    end_tree(signal.SIGKILL)
                if pending and now >= pending[1]:
                    _signal(process.pid, pending[0])
                    del pending[:]
                deadlines = [entry[-1] for entry in (pending, stopping) if entry]
                timeout = max(0.0, min(deadlines) - now) if deadlines else None
                select.select([wake_read], [], [], timeout)  # any signal, SIGCHLD included, wakes this
                try:
                    while os.read(wake_read, 512):
                        pass
                except BlockingIOError:
                    pass
                continue
            if os.WIFSTOPPED(status):
                # The CLI suspended itself (Ctrl+Z while the terminal is in raw
                # mode). Suspend the whole job so the shell gets the terminal
                # back; `fg` then continues all of it.
                if _still_stopped(process.pid):
                    try:
                        os.killpg(os.getpgrp(), signal.SIGTSTP)
                    except OSError:
                        pass
                continue
            process.returncode = exit_code(status)
            if stopping and not signalled[0]:
                # Reloading: the new CLI must not share the terminal with what
                # is left of the old one.
                deadline = max(stopping[0], time.monotonic() + 0.5) if stopping[0] < now + 3000 else now + 1.0
                while tree_alive() and not signalled[0] and time.monotonic() < deadline:
                    time.sleep(0.1)
                if tree_alive() and not signalled[0]:
                    end_tree(signal.SIGKILL)
                    time.sleep(0.2)
            return process.returncode, time.monotonic() - began, signalled[0], bool(stopping) and not signalled[0]
    finally:
        signal.set_wakeup_fd(previous_wakeup)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        os.close(wake_read)
        os.close(wake_write)


def _signal(pid: int, signum: int) -> None:
    try:
        os.kill(pid, signum)
    except OSError:
        pass


def launcher_problem(launcher: str) -> str | None:
    if not launcher.startswith("/") or any(ch.isspace() for ch in launcher):
        return f"launcher path {launcher!r} cannot be saved by Zellij"
    if not os.path.isfile(launcher) or not os.access("/bin/sh", os.X_OK):
        return f"launcher {launcher} is missing"
    return None


def start(tool: str, launcher: str, args: list[str]) -> int:
    """First launch from a shell: save the arguments under a new pane token
    and become `/bin/sh <launcher> --zj-pane <token>`, the command Zellij
    will save for this pane."""
    spec = SPECS[tool]
    reason = spec.passthrough_reason(spec.parse(args))
    if reason is None:
        if "ZELLIJ" not in os.environ:
            reason = "not inside Zellij"
        elif os.environ.get("ZJR_PANE"):
            reason = "already inside a zclaude/zcodex pane"
        elif not (os.isatty(0) and os.isatty(1)):
            reason = "not attached to a terminal"
        elif len(args) > MAX_ARGS or any("\0" in arg for arg in args):
            reason = "too many arguments"
        else:
            reason = launcher_problem(launcher)
            if reason:
                note(tool, f"{reason}; starting without recovery")
    if reason:
        return exec_plain(spec, args)
    try:
        store = Store()
        token = str(uuid.uuid4())
        state = new_state(tool, args, fallback_cwd())
        state["binary"] = find_binary(spec)
        store.save(token, state)
    except (StoreError, OSError) as error:
        note(tool, f"{error}; starting without recovery")
        return exec_plain(spec, args)
    store.log("start", token=token, tool=tool, args=len(args))
    try:
        store.gc()
    except Exception:
        pass
    try:
        os.execv("/bin/sh", ["/bin/sh", launcher, PANE_FLAG, token])
    except OSError as error:
        note(tool, f"cannot run /bin/sh: {error.strerror or error}; starting without recovery")
        store.delete(token)
        return exec_plain(spec, args)


def run(tool: str, launcher: str, argv: list[str]) -> int:
    """Run (or resume) the pane's CLI: what `/bin/sh <launcher> --zj-pane <token>`
    does. `zjr reload` (SIGUSR1) restarts the CLI on the same conversation."""
    spec = SPECS[tool]
    # Handlers for as long as this runs, between CLI runs too: SIGUSR1 means
    # "reload" (its default action would end this process, and the pane's CLI
    # with it); keys like Ctrl+C belong to the CLI; a hangup ends the loop.
    reload = [False]
    hangup = [0]
    signal.signal(signal.SIGUSR1, lambda signum, frame: reload.__setitem__(0, True))
    signal.signal(signal.SIGINT, _ignore)
    signal.signal(signal.SIGQUIT, _ignore)
    for signum in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(signum, lambda signum, frame: hangup.__setitem__(0, signum))
    if len(argv) != 2 or argv[0] != PANE_FLAG or not TOKEN_RE.match(argv[1]):
        note(tool, "invalid pane token; starting without recovery")
        return exec_plain(spec, [])
    token = argv[1]
    try:
        store = Store()
        pane_lock = store.pane_lock(token)
        owned = pane_lock.acquire()
        state = store.load(token, tool)
    except (StoreError, OSError) as error:
        note(tool, f"{error}; starting without recovery")
        return exec_plain(spec, [])
    if state is None:
        note(tool, "no saved state for this pane (expired or removed); starting a new conversation")
        state = new_state(tool, [], fallback_cwd())
        state["launched"] = True
    if not owned:
        note(tool, "this pane's saved command is already running in another pane; this one is not tracked")
    binary = find_binary(spec, state.get("binary"))
    if binary is None:
        note(tool, f"{tool} not found on PATH (press Enter to retry once it is)")
        return 127
    state.update(binary=binary, zellij_session=os.environ.get("ZELLIJ_SESSION_NAME"))
    running = Running(store, token, tool, owned)
    try:
        saved_tty = termios.tcgetattr(0) if os.isatty(0) else None
    except termios.error:
        saved_tty = None

    pane = Pane(store, token, state, spec, owned)
    copied_from = state.get("session_id")  # what the owning pane has open, when this one is a duplicate

    def busy(session_id: str) -> bool:
        return ((not owned and session_id == copied_from) or pane.claimed_elsewhere(session_id)
                or spec.open_elsewhere(session_id))

    code, reloads = 0, 0
    try:
        while True:
            if hangup[0]:  # between two runs: the pane is going away
                return 128 + hangup[0]
            plan = make_plan(spec, state, str(uuid.uuid4()), busy)
            if plan.session_id and not pane.lock_session(plan.session_id) and plan.mode == "resume":
                # Another pane resumed the same conversation a moment ago.
                kept = spec.split(spec.parse(state["args"]), state)[0]
                plan = Plan("fork", spec.resume_args(plan.session_id, kept, fork=True), plan.cwd, None,
                            f"conversation {plan.session_id} is open elsewhere; continuing in a copy of it")
            if plan.message:
                note(tool, ("reloaded; " if reloads else "") + plan.message)
            with pane.guard:
                state["launched"] = True
                state["session_id"] = plan.session_id
                if plan.mode != "resume":
                    state["session_cwd"] = None
                saved = pane._save()
            # Recorded before the CLI starts, so a crash never replays a first
            # launch (and its prompt). If that cannot be recorded, do not track.
            if not saved and plan.mode == "first":
                try:
                    store.delete(token)
                except OSError:
                    pass
                note(tool, "starting without recovery")
                pane.release()
                pane_lock.release()
                return exec_plain(spec, plan.args)
            store.log("run", token=token, tool=tool, mode=plan.mode, session=plan.session_id, cwd=plan.cwd,
                      owned=owned, reload=reloads)
            running.update(runs=reloads + 1, session_id=plan.session_id)

            watcher: list[Watcher] = []

            def started(pid: int) -> None:
                thread = spec.watcher(pane, pid, plan)  # a duplicate pane tracks too, in memory only
                watcher.append(thread)
                thread.start()

            reload[0] = False
            if hangup[0]:
                return 128 + hangup[0]
            try:
                code, seconds, signalled, reloaded = supervise(spec, binary, plan, child_env(token), started, reload)
            finally:
                if watcher:
                    watcher[0].finish()
            # A resume that failed at once (Claude Code: "No conversation
            # found") is not retried. Being closed, killed, interrupted or
            # reloaded is not a failure.
            failed = (plan.mode == "resume" and 0 < code < 128 and seconds < FAST_FAIL_SECONDS and not signalled
                      and not reloaded and state.get("session_id") == plan.session_id)
            with pane.guard:
                state["last_exit"] = code
                if failed:
                    state["failed_resume"] = plan.session_id
                elif state.get("failed_resume") and (seconds >= FAST_FAIL_SECONDS or reloaded):
                    state["failed_resume"] = None
                pane._save()
            store.log("exit", token=token, tool=tool, code=code, seconds=round(seconds, 1), reload=reloaded)
            if failed:
                note(tool, f"{tool} exited at once while resuming {plan.session_id}; "
                           f"press Enter to start a new conversation instead")
            if not reloaded:
                if code >= 128:  # killed: it may not have undone its terminal modes
                    reset_terminal(saved_tty, clear=False)
                return code
            reloads += 1
            reset_terminal(saved_tty, clear=True)
    finally:
        running.remove()
        pane.release()
        pane_lock.release()


# --- zjr: inspection and upkeep ---------------------------------------------------------


def _home_short(path: object) -> str:
    if not isinstance(path, str):
        return "-"
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def _when(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "-"
    return datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def admin_list(store: Store) -> int:
    rows = []
    for token in store.tokens():
        state = store.load_any(token)
        if state is None:
            rows.append((token[:8], "?", "(unreadable)", "", "", ""))
            continue
        status = "running" if store.pane_lock(token).held_elsewhere() else "saved"
        rows.append((token[:8], state["tool"], state.get("session_id") or "-", status,
                     _when(state.get("updated_at")), _home_short(state.get("session_cwd") or state["cwd"])))
    if not rows:
        print("No panes saved. Start one inside Zellij with zclaude or zcodex.")
        return 0
    header = ("PANE", "TOOL", "CONVERSATION", "STATUS", "UPDATED", "DIRECTORY")
    widths = [max(len(row[i]) for row in rows + [header]) for i in range(5)]
    for row in [header] + rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:5])) + "  " + row[5])
    return 0


def _zellij_config_dirs() -> list[str]:
    """Existing directories Zellij reads its configuration from. Zellij
    0.45 resurrects sessions only when one of them exists."""
    home = os.path.expanduser("~")
    candidates = [
        os.environ.get("ZELLIJ_CONFIG_DIR") or "",
        os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "zellij"),
        os.path.join(home, "Library", "Application Support", "org.Zellij-Contributors.Zellij"),
        "/etc/zellij",
    ]
    return [path for path in candidates if path and os.path.isdir(path)]


def _zellij_config() -> str | None:
    candidates = [os.environ.get("ZELLIJ_CONFIG_FILE") or ""]
    if os.environ.get("ZELLIJ_CONFIG_DIR"):
        candidates.append(os.path.join(os.environ["ZELLIJ_CONFIG_DIR"], "config.kdl"))
    home = os.path.expanduser("~")
    candidates += [
        os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "zellij", "config.kdl"),
        os.path.join(home, "Library", "Application Support", "org.Zellij-Contributors.Zellij", "config.kdl"),
    ]
    return next((path for path in candidates if path and os.path.isfile(path)), None)


def admin_doctor(store: Store | None, bindir: str) -> int:
    problems = 0

    def report(ok: bool | None, label: str, detail: str) -> None:
        nonlocal problems
        mark = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
        problems += ok is False
        print(f"{mark}  {label:<10} {detail}")

    version = ".".join(map(str, sys.version_info[:3]))
    report(sys.version_info >= (3, 8), "python", f"{version} ({sys.executable})")
    report(store is not None, "state", _home_short(state_root()) + (" (private)" if store else " (unusable)"))
    for name in ("zclaude", "zcodex", "zjr"):
        path = os.path.join(bindir, name)
        report(os.path.isfile(path), "launcher", _home_short(path))
    on_path = any(os.path.realpath(entry) == os.path.realpath(bindir) for entry in os.environ.get("PATH", "").split(os.pathsep) if entry)
    report(True if on_path else None, "PATH", f"{_home_short(bindir)} {'is' if on_path else 'is NOT'} on PATH")

    zellij = shutil.which("zellij")
    if zellij:
        try:
            output = subprocess.run([zellij, "--version"], stdin=subprocess.DEVNULL, capture_output=True, timeout=10).stdout
            report(True, "zellij", output.decode("utf-8", "replace").strip())
        except (OSError, subprocess.SubprocessError):
            report(None, "zellij", f"{zellij} (version unknown)")
    else:
        report(False, "zellij", "not found on PATH")
    if not _zellij_config_dirs():
        report(False, "config", "no Zellij configuration directory: Zellij then resurrects nothing "
                                "(mkdir -p ~/.config/zellij fixes it)")
    config = _zellij_config()
    if config:
        text = read_file(config, 4 * 1024 * 1024) or b""
        active = [line.strip() for line in text.decode("utf-8", "replace").splitlines() if not line.strip().startswith("//")]
        if any(re.match(r"session_serialization\s+false\b", line) for line in active):
            report(False, "config", f"{_home_short(config)}: session_serialization is false; nothing can be restored")
        elif any(line.startswith("post_command_discovery_hook") for line in active):
            report(None, "config", f"{_home_short(config)}: a post_command_discovery_hook is set; it must leave "
                                   f"commands containing {PANE_FLAG} unchanged")
        else:
            report(True, "config", f"{_home_short(config)}: session serialization on")
        active_text = _active_kdl(config)
        bound, blocks = _reload_binding(active_text), _keybinds_blocks(active_text)
        if bound and len(blocks) > 1 and bound.start() > blocks[1]:
            report(False, "reload", f"{bound.group(1)} is bound in a second `keybinds` block, which Zellij "
                                    "ignores: move the bind into the first one")
        elif bound:
            report(True, "reload", f"{bound.group(1)} reloads the focused pane")
        else:
            report(True, "reload", "no shortcut (optional: zjr keybinding prints one); "
                                   "`! zjr reload` works inside Claude Code")
    else:
        report(True, "config", "Zellij defaults (session serialization on)")

    for spec in SPECS.values():
        binary = find_binary(spec)
        if not binary:
            report(None, spec.name, "not found on PATH")
            continue
        detail = binary
        if spec.name == "claude":
            registry = os.path.isdir(os.path.join(claude_dir(), "sessions"))
            detail += "; conversation switches tracked" if registry else "; no session registry yet (run claude once)"
        report(True, spec.name, _home_short(detail))
    inherited = [name for name in ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION") if os.environ.get(name)]
    if inherited and os.environ.get("ZELLIJ") is not None:
        report(None, "env", f"{', '.join(inherited)} set: this Zellij server was started from inside Claude Code, "
                            f"so its panes run Claude Code as a child session (switches may go unnoticed)")
    return 1 if problems else 0


ADMIN_USAGE = """usage: zjr [list|reload [PANE]|keybinding [KEY]|doctor|gc [--days N]|forget PANE|version]

  list              panes zclaude/zcodex can restore (default)
  reload [PANE]     restart a pane's CLI on the conversation it is on: the pane
                    this runs in (`! zjr reload` inside Claude Code), else the
                    focused zclaude/zcodex pane of this Zellij tab, or PANE
  keybinding [KEY]  print a Zellij keybinding for reload (default: Alt r)
  doctor            check the installation and environment
  gc [--days N]     forget panes untouched for N days (default 30)
  forget PANE       forget one pane (a prefix of its id from `zjr list`)
  version           print the version
"""

KEYBINDING_BIND = """bind "{key}" {{
    Run "{zjr}" "reload" "--from-keybinding" {{
        floating true
        close_on_exit true
    }}
}}"""

KEYBINDING_INTO = """// zellij-ai-resume: {key} restarts the focused zclaude/zcodex pane on its current
// conversation. {config} already has a `keybinds` block, and Zellij ignores a
// second one: put this `bind` inside that block, in its `shared_except "locked"`
// section (add the section if there is none).
"""

KEYBINDING_NEW = """// zellij-ai-resume: {key} restarts the focused zclaude/zcodex pane on its current
// conversation. Add this to {config} (create the file if it does not exist).
keybinds {{
    shared_except "locked" {{
{bind}
    }}
}}"""


def _skip_kdl_node(text: str, index: int) -> int:
    """Where the KDL node starting at index ends (after its { } children)."""
    depth, quoted = 0, False
    while index < len(text):
        char = text[index]
        if quoted:
            if char == "\\":
                index += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                return index  # the end of the enclosing block
            depth -= 1
            if depth == 0:
                return index + 1
        elif char in "\n;" and depth == 0:
            return index
        index += 1
    return index


def strip_kdl_comments(text: str) -> str:
    """KDL without its comments: // to the end of the line, /* */ (nested),
    and /- which comments out the next node. Strings are left alone."""
    out: list[str] = []
    index, quoted = 0, False
    while index < len(text):
        char = text[index]
        if quoted:
            out.append(char)
            if char == "\\" and index + 1 < len(text):
                out.append(text[index + 1])
                index += 1
            elif char == '"':
                quoted = False
            index += 1
        elif char == '"':
            quoted = True
            out.append(char)
            index += 1
        elif text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif text.startswith("/*", index):
            depth, index = 1, index + 2
            while index < len(text) and depth:
                if text.startswith("/*", index):
                    depth, index = depth + 1, index + 2
                elif text.startswith("*/", index):
                    depth, index = depth - 1, index + 2
                else:
                    if text[index] == "\n":
                        out.append("\n")
                    index += 1
        elif text.startswith("/-", index):
            index = _skip_kdl_node(text, index + 2)
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _active_kdl(path: str) -> str:
    """A Zellij config without its comments."""
    return strip_kdl_comments((read_file(path, 4 * 1024 * 1024) or b"").decode("utf-8", "replace"))


def _keybinds_blocks(text: str) -> list[int]:
    return [match.start() for match in re.finditer(r"(?m)^[ \t]*keybinds\b", text)]


def _reload_binding(text: str) -> re.Match | None:
    return re.search(r'bind\s+"([^"]+)"\s*\{[^}]*?Run\s+"[^"]*/zjr"\s+"reload"', text)


def _is_core(pid: int, token: str) -> bool:
    """Whether pid is the core running this pane, not a reused pid."""
    args = ps_field(pid, "args") or ""
    return "zjr.py" in args and f"{PANE_FLAG} {token}" in args


def running_panes(store: Store) -> list[dict]:
    """The running/ entries of cores that run now, each checked to be that
    pane's core; entries left by a core that died are removed."""
    found = []
    try:
        names = os.listdir(store.running)
    except OSError:
        return []
    for name in names:
        if not (name.endswith(".json") and name[:-5].isdigit()):
            continue
        path, pid = os.path.join(store.running, name), int(name[:-5])
        entry = read_json(path, MAX_STATE_BYTES, store.uid)
        token = entry.get("token") if entry else None
        if (entry and entry.get("pid") == pid and isinstance(token, str) and TOKEN_RE.match(token)
                and entry.get("tool") in SPECS and pid_alive(pid) and _is_core(pid, token)):
            found.append(entry)
        elif not pid_alive(pid) or entry is None or not _is_core(pid, str(token)):
            try:
                os.unlink(path)  # its core is gone (or the pid belongs to something else now)
            except OSError:
                pass
    return found


def _executable(pid: int) -> str | None:
    """The file a process is running (not its argv[0])."""
    if os.path.isdir("/proc/self"):
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return None
    output = _tool_output("lsof", "/usr/sbin/lsof", ["-a", "-p", str(pid), "-d", "txt", "-F", "n", "-n", "-P", "-w"])
    return next((line[1:] for line in (output or "").splitlines() if line.startswith("n/")), None)


def zellij_binary() -> str | None:
    """zellij on PATH, else the binary of the Zellij server this process
    runs under (a server's PATH need not include it)."""
    found = shutil.which("zellij")
    if found:
        return found
    pid = os.getpid()
    for _ in range(16):
        parent = ps_field(pid, "ppid")
        if not parent or not parent.isdigit() or int(parent) <= 1:
            return None
        pid = int(parent)
        path = _executable(pid)
        if path and os.path.basename(path) == "zellij" and os.access(path, os.X_OK):
            return path
    return None


def zellij_panes(session: str) -> dict[str, dict] | None:
    """The terminal panes of a Zellij session by pane id, or None."""
    zellij = zellij_binary()
    if not zellij:
        return None
    try:
        result = subprocess.run([zellij, "--session", session, "action", "list-panes", "--json", "--all"],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
        panes = json.loads(result.stdout.decode("utf-8")) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(panes, list):
        return None
    return {str(pane["id"]): pane for pane in panes
            if isinstance(pane, dict) and not pane.get("is_plugin") and isinstance(pane.get("id"), int)}


def reload_target(store: Store, name: str | None, from_keybinding: bool = False) -> dict | str:
    """The running/ entry of the pane to reload, or why there is none."""
    running = running_panes(store)
    if name:
        matches = [entry for entry in running if entry["token"].startswith(name)]
        owners = [entry for entry in matches if entry.get("owned")]
        if len(matches) == 1 or len(owners) == 1:
            return matches[0] if len(matches) == 1 else owners[0]
        return f"{len(matches)} running panes match {name!r} (zjr list shows them)"
    if not from_keybinding and TOKEN_RE.match(os.environ.get("ZJR_PANE", "")):
        # Run from inside a CLI (`! zjr reload` in Claude Code): its core is an
        # ancestor of this process, which tells a duplicate pane from the original.
        mine = set(ancestors(os.getpid()))
        return next((entry for entry in running if entry["pid"] in mine), "this pane's CLI is not running under zjr")
    session = os.environ.get("ZELLIJ_SESSION_NAME")
    if not session:
        return running[0] if len(running) == 1 else "not inside Zellij: name the pane to reload (zjr list shows them)"
    here = [entry for entry in running if entry.get("zellij_session") == session and entry.get("pane_id")]
    if not here:
        return "no zclaude/zcodex pane is running in this Zellij session"
    panes = zellij_panes(session)
    me = os.environ.get("ZELLIJ_PANE_ID", "")
    if panes is None or me not in panes:
        return here[0] if len(here) == 1 else "several zclaude/zcodex panes are running: name one (zjr list shows them)"
    # The pane a keybinding's floating pane was opened over stays focused in
    # the tiled layer, so "focused, in this tab, not me" finds it.
    tab = panes[me].get("tab_id")
    in_tab = [entry for entry in here if entry["pane_id"] in panes and panes[entry["pane_id"]].get("tab_id") == tab]
    focused = [entry for entry in in_tab if entry["pane_id"] != me and panes[entry["pane_id"]].get("is_focused")]
    if len(focused) == 1:
        return focused[0]
    if len(in_tab) == 1:
        return in_tab[0]
    if in_tab:
        return "the focused pane is not a zclaude/zcodex pane, and this tab has several"
    return "no zclaude/zcodex pane is running in this tab"


def _reload_failed(message: str, from_keybinding: bool) -> int:
    print(f"zjr reload: {message}", file=sys.stderr)
    if from_keybinding and os.isatty(0):
        # The keybinding's floating pane closes when this exits: keep the message up.
        print("(Enter closes this)", file=sys.stderr)
        try:
            select.select([0], [], [], 15)
        except (OSError, ValueError):
            pass
    return 1


def admin_reload(store: Store, argv: list[str]) -> int:
    from_keybinding = "--from-keybinding" in argv
    names = [arg for arg in argv if arg != "--from-keybinding"]
    if len(names) > 1 or (names and not re.match(r"\A[0-9a-f-]{4,36}\Z", names[0])):
        print(ADMIN_USAGE, end="", file=sys.stderr)
        return 2
    target = reload_target(store, names[0] if names else None, from_keybinding)
    if isinstance(target, str):
        return _reload_failed(target, from_keybinding)
    label = f"{target['tool']} pane {target['token'][:8]}"
    before = int(target.get("runs") or 0)
    try:
        os.kill(target["pid"], signal.SIGUSR1)
    except OSError as error:
        return _reload_failed(f"cannot signal {label}: {error.strerror or error}", from_keybinding)
    if from_keybinding or target["pid"] in ancestors(os.getpid()):
        # Inside the CLI, this command ends along with it; from a keybinding,
        # its floating pane should be gone before the next key. The pane
        # itself says "reloaded; resuming ..." when the CLI is back.
        print(f"reloading {label}")
        return 0
    entry = os.path.join(store.running, f"{target['pid']}.json")
    deadline = time.monotonic() + RELOAD_GRACE_SECONDS + 10.0
    while time.monotonic() < deadline:
        current = read_json(entry, MAX_STATE_BYTES, store.uid)
        if current and int(current.get("runs") or 0) > before:
            conversation = current.get("session_id")
            print(f"reloaded {label}" + (f" on conversation {conversation}" if conversation else ""))
            return 0
        time.sleep(0.1)
    return _reload_failed(f"{label} did not restart", from_keybinding)


def admin_keybinding(bindir: str, argv: list[str]) -> int:
    key = " ".join(argv) if argv else "Alt r"
    if not re.match(r"\A[A-Za-z0-9 +-]{1,32}\Z", key):
        print("zjr keybinding: KEY looks like Alt r, Ctrl Alt r, F5 ...", file=sys.stderr)
        return 2
    existing = _zellij_config()
    config = existing or os.path.join(os.path.expanduser("~"), ".config", "zellij", "config.kdl")
    bind = KEYBINDING_BIND.format(key=key, zjr=os.path.join(bindir, "zjr"))
    if existing and _keybinds_blocks(_active_kdl(existing)):
        print(KEYBINDING_INTO.format(key=key, config=_home_short(config)) + bind)
    else:
        indented = "\n".join("        " + line for line in bind.splitlines())
        print(KEYBINDING_NEW.format(key=key, config=_home_short(config), bind=indented))
    return 0


def admin(bindir: str, argv: list[str]) -> int:
    command = argv[0] if argv else "list"
    if command in ("-h", "--help", "help"):
        print(ADMIN_USAGE, end="")
        return 0
    if command == "version":
        print(f"zellij-ai-resume {VERSION}")
        return 0
    if command == "keybinding":
        return admin_keybinding(bindir, argv[1:])
    try:
        store = Store()
    except StoreError as error:
        if command == "doctor":
            print(f"state directory unusable: {error}")
            return admin_doctor(None, bindir)
        print(f"zjr: {error}", file=sys.stderr)
        return 1
    if command == "list" and len(argv) <= 1:
        return admin_list(store)
    if command == "reload":
        return admin_reload(store, argv[1:])
    if command == "doctor" and len(argv) <= 1:
        return admin_doctor(store, bindir)
    if command == "gc":
        days = float(STALE_DAYS)
        if len(argv) == 3 and argv[1] == "--days":
            try:
                days = float(argv[2])
            except ValueError:
                days = -1.0
        elif len(argv) != 1:
            days = -1.0
        if not math.isfinite(days) or days < 0:
            print(ADMIN_USAGE, end="", file=sys.stderr)
            return 2
        print(f"forgot {store.gc(days)} pane(s)")
        return 0
    if command == "forget" and len(argv) == 2 and re.match(r"\A[0-9a-f-]{4,36}\Z", argv[1]):
        matches = [token for token in store.tokens() if token.startswith(argv[1])]
        if len(matches) != 1:
            print(f"zjr: {len(matches)} panes match {argv[1]!r}", file=sys.stderr)
            return 1
        if store.pane_lock(matches[0]).held_elsewhere():
            print(f"zjr: pane {matches[0][:8]} is running; quit it first", file=sys.stderr)
            return 1
        store.delete(matches[0])
        print(f"forgot pane {matches[0][:8]}")
        return 0
    print(ADMIN_USAGE, end="", file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] in ("start", "run") and argv[1] in SPECS:
        entry = start if argv[0] == "start" else run
        return entry(argv[1], argv[2], argv[3:])
    if len(argv) >= 2 and argv[0] == "admin":
        return admin(argv[1], argv[2:])
    sys.stderr.write(__doc__ or "")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
