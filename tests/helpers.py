"""Shared test scaffolding: a throwaway HOME with fake claude/codex CLIs on
PATH, and processes on pseudo-terminals (Zellij clients)."""

import fcntl
import json
import os
import pty
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def wait_for(predicate, timeout=10.0, interval=0.05, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


def _controlling_terminal():
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class Pane:
    """A process started in a new session on its own pseudo-terminal (its
    controlling terminal, with a real size), like a terminal running Zellij."""

    def __init__(self, argv, env, cwd, rows=40, cols=120):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd,
                                     start_new_session=True, close_fds=True, preexec_fn=_controlling_terminal)
        os.close(slave)
        self.master = master
        self.output = bytearray()
        self.reader = threading.Thread(target=self._drain, args=(master,), daemon=True)
        self.reader.start()

    def _drain(self, master):
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.output += chunk

    @property
    def pid(self):
        return self.proc.pid

    def text(self):
        return self.output.decode("utf-8", "replace")

    def signal_group(self, signum):
        try:
            os.killpg(self.pid, signum)
        except ProcessLookupError:
            pass

    def close(self):
        if self.proc.poll() is None:
            self.signal_group(signal.SIGKILL)
            self.proc.wait(timeout=5)
        if self.master is not None:
            # Once only: the number may belong to another terminal by now.
            master, self.master = self.master, None
            try:
                os.close(master)
            except OSError:
                pass


class Sandbox:
    """A throwaway HOME, working directory and CLI configuration, with the
    fake CLIs on PATH. They append what they do to one log of events."""

    def __init__(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="zjr-test-"))
        self.home = os.path.join(self.dir, "home")
        self.fakes = os.path.join(self.dir, "fakes")
        self.work = os.path.join(self.dir, "work")
        self.log = os.path.join(self.dir, "events.jsonl")
        for path in (self.fakes, self.work, self.home):
            os.makedirs(path)
        for name in ("claude", "codex"):
            with open(os.path.join(ROOT, "tests", "fakes", name)) as handle:
                body = handle.read().split("\n", 1)[1]
            target = os.path.join(self.fakes, name)
            with open(target, "w") as handle:
                handle.write(f"#!{sys.executable}\n{body}")
            os.chmod(target, 0o755)
        self.env = {
            "PATH": os.pathsep.join([self.fakes, "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
            "HOME": self.home,
            "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "claude"),
            "CODEX_HOME": os.path.join(self.dir, "codex"),
            "FAKE_LOG": self.log,
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
        }

    def events(self, **match):
        try:
            with open(self.log) as handle:
                records = [json.loads(line) for line in handle if line.strip()]
        except FileNotFoundError:
            return []
        return [r for r in records if all(r.get(k) == v for k, v in match.items())]

    def wait_event(self, timeout=10.0, skip=0, **match):
        return wait_for(lambda: (self.events(**match)[skip:] or [None])[0], timeout, what=f"event {match}")

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)
