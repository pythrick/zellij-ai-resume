"""Shared test scaffolding: load the core, and build sandboxed installs with
fake claude/codex CLIs, private state and a pseudo-terminal per pane."""

import fcntl
import importlib.util
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


def load_core():
    if "zjr" in sys.modules:
        return sys.modules["zjr"]
    spec = importlib.util.spec_from_file_location("zjr", os.path.join(ROOT, "src", "zjr.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["zjr"] = module
    spec.loader.exec_module(module)
    return module


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
    controlling terminal, with a real size), like a Zellij pane."""

    def __init__(self, argv, env, cwd, rows=40, cols=120):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd,
                                     start_new_session=True, close_fds=True, preexec_fn=_controlling_terminal)
        os.close(slave)
        self.master = master
        self.output = bytearray()
        self.reader = threading.Thread(target=self._drain, daemon=True)
        self.reader.start()

    def _drain(self):
        while True:
            try:
                chunk = os.read(self.master, 65536)
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

    def command_line(self):
        """What Zellij would save for this pane: its process's argv as ps shows it."""
        return subprocess.run(["ps", "-o", "args=", "-p", str(self.pid)], capture_output=True, text=True).stdout.strip()

    def signal_group(self, signum):
        try:
            os.killpg(self.pid, signum)
        except ProcessLookupError:
            pass

    def crash(self):
        self.signal_group(signal.SIGKILL)
        self.wait()

    def wait(self, timeout=15):
        code = self.proc.wait(timeout=timeout)
        self.reader.join(timeout=2)
        return code

    def close(self):
        if self.proc.poll() is None:
            self.signal_group(signal.SIGKILL)
            self.proc.wait(timeout=5)
        try:
            os.close(self.master)
        except OSError:
            pass


class Sandbox:
    """A throwaway install of zellij-ai-resume with fake CLIs on PATH."""

    def __init__(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="zjr-test-"))
        self.prefix = os.path.join(self.dir, "prefix")
        self.fakes = os.path.join(self.dir, "fakes")
        self.work = os.path.join(self.dir, "work")
        self.log = os.path.join(self.dir, "events.jsonl")
        for path in (self.fakes, self.work, os.path.join(self.dir, "home")):
            os.makedirs(path)
        for name in ("claude", "codex"):
            with open(os.path.join(ROOT, "tests", "fakes", name)) as handle:
                body = handle.read().split("\n", 1)[1]
            target = os.path.join(self.fakes, name)
            with open(target, "w") as handle:
                handle.write(f"#!{sys.executable}\n{body}")
            os.chmod(target, 0o755)
        self.env = {
            "PATH": os.pathsep.join([self.fakes, os.path.join(self.prefix, "bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
            "HOME": os.path.join(self.dir, "home"),
            "XDG_STATE_HOME": os.path.join(self.dir, "state"),
            "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "claude"),
            "CODEX_HOME": os.path.join(self.dir, "codex"),
            "FAKE_LOG": self.log,
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
        }
        result = subprocess.run(["/bin/sh", os.path.join(ROOT, "install.sh"), "--prefix", self.prefix,
                                 "--python", sys.executable], env=self.env, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"install.sh failed:\n{result.stdout}\n{result.stderr}")
        self.install_output = result.stdout
        self.panes = []

    def launcher(self, name):
        return os.path.join(self.prefix, "bin", name)

    def pane(self, argv, zellij=True, cwd=None, **extra):
        env = dict(self.env)
        if zellij:
            env.update({"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "zjr-test", "ZELLIJ_PANE_ID": "1"})
        env.update({key: str(value) for key, value in extra.items()})
        pane = Pane(argv, env, cwd or self.work)
        self.panes.append(pane)
        return pane

    def events(self, **match):
        try:
            with open(self.log) as handle:
                records = [json.loads(line) for line in handle if line.strip()]
        except FileNotFoundError:
            return []
        return [r for r in records if all(r.get(k) == v for k, v in match.items())]

    def wait_event(self, timeout=10.0, skip=0, **match):
        return wait_for(lambda: (self.events(**match)[skip:] or [None])[0], timeout, what=f"event {match}")

    def states(self):
        panes = os.path.join(self.env["XDG_STATE_HOME"], "zellij-ai-resume", "panes")
        result = {}
        for name in sorted(os.listdir(panes)) if os.path.isdir(panes) else []:
            if name.endswith(".json") and not name.startswith("."):  # skip in-progress atomic writes
                with open(os.path.join(panes, name)) as handle:
                    result[name[:-5]] = json.load(handle)
        return result

    def close(self):
        for pane in self.panes:
            pane.close()
        shutil.rmtree(self.dir, ignore_errors=True)
