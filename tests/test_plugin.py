"""The plugin against a real Zellij: plain `claude` and `codex` in panes; the
server is killed with SIGKILL and the session attached again; every pane must
come back on its conversation, in its directory, without a key being pressed.
Plus reload, with the key the plugin binds and with `zellij pipe`.

Opt-in (it builds the plugin and starts real Zellij servers):

    ZJR_E2E=1 python3 -m unittest test_plugin

Each test runs its own server with a throwaway HOME, Zellij configuration and
socket directory, and the fake CLIs from tests/fakes. The plugin is put in
place by install.sh --edit-config, as a user would."""

import fnmatch
import glob
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from helpers import ROOT, Pane, Sandbox, wait_for

ZELLIJ = shutil.which("zellij")
CARGO = shutil.which("cargo") or os.path.expanduser("~/.cargo/bin/cargo")
PLUGIN = os.path.join(ROOT, "plugin")
WASM = os.environ.get("ZJR_PLUGIN_WASM") or os.path.join(PLUGIN, "target", "wasm32-wasip1", "release",
                                                          "zellij-ai-resume.wasm")
SESSION = "zjr-plugin"
URL = "file:~/.config/zellij/plugins/zellij-ai-resume.wasm"  # what install.sh writes and prints
PERMISSIONS = ("ReadApplicationState", "RunCommands", "WriteToStdin", "Reconfigure", "ReadCliPipes", "ChangeApplicationState")


@unittest.skipUnless(os.environ.get("ZJR_E2E") == "1" and ZELLIJ, "set ZJR_E2E=1 (needs zellij)")
class PluginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("ZJR_PLUGIN_WASM") and os.path.exists(CARGO):
            subprocess.run([CARGO, "build", "--release", "--target", "wasm32-wasip1"], cwd=PLUGIN, check=True,
                           capture_output=True)
        if not os.path.exists(WASM):
            raise unittest.SkipTest(f"no plugin build at {WASM}")

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.sockets = tempfile.mkdtemp(prefix="zjr-plugin-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.sockets, True)
        home = self.box.home
        shell = os.path.join(self.box.dir, "shell")
        with open(shell, "w") as handle:
            handle.write("#!/bin/sh\nexec /bin/bash --norc --noprofile -i\n")
        os.chmod(shell, 0o755)
        path = os.pathsep.join([self.box.fakes, os.path.dirname(ZELLIJ), self.box.env["PATH"]])
        self.env = dict(self.box.env, PATH=path, ZELLIJ_SOCKET_DIR=self.sockets, SHELL=shell)
        config_dir = os.path.join(home, ".config", "zellij")
        os.makedirs(config_dir)
        with open(os.path.join(config_dir, "config.kdl"), "w") as handle:
            handle.write(f'default_shell "{shell}"\nserialization_interval 1\nshow_startup_tips false\n'
                         'show_release_notes false\n')
        installed = subprocess.run(["/bin/sh", os.path.join(ROOT, "install.sh"), "--wasm", WASM, "--edit-config"],
                                   env=self.env, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn(f"zellij plugin -- '{URL}'", installed.stdout)
        # The one-time grant (`zellij plugin -- ...`, then y), as Zellij caches it.
        grant = f'"{config_dir}/plugins/zellij-ai-resume.wasm" {{\n' + "".join(f"    {name}\n" for name in PERMISSIONS) + "}\n"
        for cache in ("Library/Caches/org.Zellij-Contributors.Zellij", ".cache/zellij"):
            os.makedirs(os.path.join(home, cache), exist_ok=True)
            with open(os.path.join(home, cache, "permissions.kdl"), "w") as handle:
                handle.write(grant)
        self.clients = []
        self.addCleanup(self.cleanup)

    # --- plumbing -----------------------------------------------------------------------

    def cleanup(self):
        for client in self.clients:
            client.close()
        pid = self.server()
        if pid:
            os.kill(pid, signal.SIGKILL)

    def server(self):
        for line in subprocess.run(["ps", "-A", "-o", "pid=,args="], capture_output=True, text=True).stdout.splitlines():
            words = line.split()
            if len(words) >= 4 and words[1].endswith("zellij") and words[2] == "--server" and words[3].startswith(self.sockets):
                return int(words[0])
        return None

    def zellij(self, *args, timeout=15):
        # stdin not a terminal, as for `! zellij pipe ...` typed inside Claude Code
        return subprocess.run([ZELLIJ, *args], env=self.env, capture_output=True, text=True, timeout=timeout,
                              cwd=self.box.work, stdin=subprocess.DEVNULL).stdout

    def action(self, *args):
        return self.zellij("--session", SESSION, "action", *args)

    def attach(self, create=False):
        client = Pane([ZELLIJ, "attach", *(["--create"] if create else []), SESSION], self.env, self.box.work)
        self.clients.append(client)
        return client

    def start(self, **fake):
        self.env.update({key: str(value) for key, value in fake.items()})
        client = self.attach(create=True)
        time.sleep(2)  # a burst of actions against a server that is still starting can stall it
        wait_for(lambda: self.terminals(), timeout=60, interval=0.5, what="the first pane")
        time.sleep(1)
        return client

    def terminals(self):
        try:
            panes = json.loads(self.action("list-panes", "--json", "--all") or "[]")
        except (ValueError, subprocess.TimeoutExpired):  # a new server compiles its plugins first
            return []
        return [pane for pane in panes if not pane["is_plugin"]]

    def type(self, pane_id, line):
        self.action("write-chars", "--pane-id", f"terminal_{pane_id}", line)
        self.action("write", "--pane-id", f"terminal_{pane_id}", "13")

    def new_pane(self):
        before = {pane["id"] for pane in self.terminals()}
        self.action("new-pane")
        return wait_for(lambda: next((p["id"] for p in self.terminals() if p["id"] not in before), None),
                        interval=0.5, what="a new pane")

    def saved(self):
        # The plugin's /cache, in Zellij's cache folder under a path made from
        # the plugin's URL (file:~/.config/...: a hidden folder, which glob skips).
        files = []
        for cache in ("Library/Caches/org.Zellij-Contributors.Zellij", ".cache/zellij"):
            for folder, _, names in os.walk(os.path.join(self.box.home, cache)):
                if os.path.basename(folder) == "plugin_cache":
                    files += [os.path.join(folder, name) for name in names if fnmatch.fnmatch(name, "session-*.json")]
        if not files:
            return {}
        try:
            with open(files[0]) as handle:
                return json.load(handle).get("panes", {})
        except ValueError:
            return {}

    def recorded(self, session_id):
        return wait_for(lambda: any(entry["session_id"] == session_id for entry in self.saved().values()),
                        timeout=20, what=f"the plugin to record {session_id}")

    def crash_and_attach(self):
        time.sleep(2.5)  # a serialization tick
        os.kill(self.server(), signal.SIGKILL)
        wait_for(lambda: self.server() is None, what="the server to die")
        for client in self.clients:
            client.close()
        return self.attach()

    def starts(self, tool="claude"):
        return self.box.events(tool=tool, event="start")


class Resurrection(PluginTest):
    def test_plain_claude_resumes_in_its_directory(self):
        self.start(FAKE_CLEAR_AFTER=3)
        self.type(self.terminals()[0]["id"], "mkdir -p sub && cd sub && claude --verbose 'a prompt'")
        first = self.box.wait_event(event="start", timeout=15)
        self.assertEqual(first["argv"], ["--verbose", "a prompt"])
        self.assertTrue(first["cwd"].endswith("/work/sub"))
        for crash in (1, 2):
            cleared = self.box.wait_event(event="clear", skip=crash - 1, timeout=15)["session"]
            self.recorded(cleared)
            self.crash_and_attach()
            resumed = self.box.wait_event(event="start", skip=crash, timeout=20)  # no key pressed
            self.assertEqual(resumed["argv"], ["--resume", cleared, "--verbose"])
            self.assertEqual(resumed["cwd"], first["cwd"])

    def test_codex_resumes_the_rollout_it_had_open(self):
        self.start(FAKE_ROLLOUT_DELAY=1)
        self.type(self.terminals()[0]["id"], "mkdir -p proj && cd proj && codex -m o3 'write the tests'")
        session = self.box.wait_event(tool="codex", event="rollout", timeout=15)["session"]
        first = self.starts("codex")[0]
        self.recorded(session)
        self.crash_and_attach()
        resumed = self.box.wait_event(tool="codex", event="start", skip=1, timeout=20)
        self.assertEqual(resumed["argv"], ["resume", "-m", "o3", session])
        self.assertEqual(resumed["cwd"], first["cwd"])

    def test_codex_on_its_shared_daemon_is_found_once_and_kept(self):
        # Nothing in the pane holds the rollout open: the first one started in
        # its directory is taken, and a later Codex there does not take over.
        self.start(FAKE_DAEMON=1, FAKE_ROLLOUT_DELAY=1)
        self.type(self.terminals()[0]["id"], "mkdir -p proj && cd proj && codex --search")
        session = self.box.wait_event(tool="codex", event="rollout", timeout=15)["session"]
        self.recorded(session)
        other = self.new_pane()
        self.type(other, "cd proj && codex")
        later = self.box.wait_event(tool="codex", event="rollout", skip=1, timeout=15)["session"]
        self.recorded(later)
        entries = {entry["session_id"]: key for key, entry in self.saved().items()}
        self.assertEqual(sorted(entries), sorted([session, later]))
        self.crash_and_attach()
        wait_for(lambda: len(self.starts("codex")) == 4, timeout=25, what="both back")
        resumed = {tuple(start["argv"]) for start in self.starts("codex")[2:]}
        self.assertEqual(resumed, {("resume", "--search", session), ("resume", later)})

    def test_several_panes_and_tabs(self):
        self.start(FAKE_ROLLOUT_DELAY=1)
        first = self.terminals()[0]["id"]
        self.type(first, "mkdir -p a && cd a && claude --model one")
        second = self.new_pane()
        self.type(second, "mkdir -p b && cd b && claude --model two")
        self.new_pane()  # a plain shell between them
        self.action("new-tab", "--cwd", self.box.work)
        wait_for(lambda: len(self.terminals()) == 4, interval=0.5, what="the second tab")
        tab2 = max(pane["id"] for pane in self.terminals())
        self.type(tab2, "mkdir -p c && cd c && codex --search")
        claude_starts = wait_for(lambda: len(self.starts()) == 2 and self.starts(), timeout=15, what="two claudes")
        codex_session = self.box.wait_event(tool="codex", event="rollout", timeout=15)["session"]
        by_model = {start["argv"][1]: start for start in claude_starts}
        for start in claude_starts:
            self.recorded(start["session"])
        self.recorded(codex_session)

        self.crash_and_attach()
        wait_for(lambda: len(self.starts()) == 4 and len(self.starts("codex")) == 2, timeout=25, what="all three back")
        for start in self.starts()[2:]:
            model = start["argv"][3]
            self.assertEqual(start["argv"], ["--resume", by_model[model]["session"], "--model", model])
            self.assertEqual(start["cwd"], by_model[model]["cwd"])
        codex = self.starts("codex")[1]
        self.assertEqual(codex["argv"], ["resume", "--search", codex_session])
        self.assertEqual(codex["cwd"], self.starts("codex")[0]["cwd"])
        self.assertTrue(codex["cwd"].endswith("/work/c"))
        self.assertEqual(len(self.terminals()), 4)  # the shell pane came back as a shell

    def test_a_conversation_that_is_gone_starts_fresh_without_its_prompt(self):
        self.start()
        self.type(self.terminals()[0]["id"], "claude --verbose 'do the thing'")
        session = self.box.wait_event(event="start", timeout=15)["session"]
        self.recorded(session)
        time.sleep(2.5)
        os.kill(self.server(), signal.SIGKILL)
        wait_for(lambda: self.server() is None, what="the server to die")
        for path in glob.glob(os.path.join(self.env["CLAUDE_CONFIG_DIR"], "projects", "*", session + ".jsonl")):
            os.unlink(path)
        for client in self.clients:
            client.close()
        self.attach()
        fresh = self.box.wait_event(event="start", skip=1, timeout=20)
        self.assertEqual(fresh["argv"], ["--verbose"])


class FirstRun(PluginTest):
    def test_granting_once_in_a_pane_starts_the_background_plugin(self):
        for cache in ("Library/Caches/org.Zellij-Contributors.Zellij", ".cache/zellij"):  # never granted
            path = os.path.join(self.env["HOME"], cache, "permissions.kdl")
            if os.path.exists(path):
                os.unlink(path)
        client = self.start()
        self.type(self.terminals()[0]["id"], "claude --verbose")
        session = self.box.wait_event(event="start", timeout=15)["session"]
        time.sleep(3)
        self.assertEqual(self.saved(), {})  # the background plugin waits for permission
        self.zellij("--session", SESSION, "plugin", "--", URL)  # what install.sh says to do
        time.sleep(2)
        os.write(client.master, b"y")
        self.recorded(session)


class Reload(PluginTest):
    def test_the_reload_key_restarts_the_focused_cli(self):
        client = self.start(FAKE_CLEAR_AFTER=2)
        pane = self.terminals()[0]["id"]
        self.type(pane, "sleep 307 &")  # a job of the same shell, which reload leaves alone
        self.type(pane, "mkdir -p sub && cd sub && claude --verbose 'a prompt'")
        first = self.box.wait_event(event="start", timeout=15)
        cleared = self.box.wait_event(event="clear", timeout=15)["session"]
        self.recorded(cleared)
        job = next(int(line.split()[0]) for line in self.processes(pid=True) if line.split()[1:] == ["sleep", "307"])
        self.addCleanup(subprocess.run, ["kill", "-9", str(job)], capture_output=True)
        os.write(client.master, b"\x1br")  # Alt r
        self.assertEqual(self.box.wait_event(event="signal", timeout=10)["signum"], signal.SIGTERM)
        again = self.box.wait_event(event="start", skip=1, timeout=15)
        self.assertEqual(again["argv"], ["--resume", cleared, "--verbose"])
        self.assertEqual(again["cwd"], first["cwd"])
        self.assertEqual(len(self.terminals()), 1)
        os.kill(job, 0)  # still running
        os.write(client.master, b"\x1bn")  # Alt n: the plugin's binding left the others alone
        wait_for(lambda: len(self.terminals()) == 2, interval=0.5, what="Alt n to open a pane")

    def test_a_configured_reload_key(self):
        config = os.path.join(self.box.home, ".config", "zellij", "config.kdl")
        with open(config) as handle:
            text = handle.read()
        with open(config, "w") as handle:
            handle.write(text.replace(f'"{URL}"', f'"{URL}" {{\n        reload_key "Alt y"\n    }}'))
        client = self.start()
        self.type(self.terminals()[0]["id"], "claude --verbose")
        self.recorded(self.box.wait_event(event="start", timeout=15)["session"])
        os.write(client.master, b"\x1by")  # Alt y
        self.assertEqual(self.box.wait_event(event="signal", timeout=10)["signum"], signal.SIGTERM)
        self.box.wait_event(event="start", skip=1, timeout=15)

    def test_pipe_reload_of_a_restored_pane_by_its_id(self):
        self.start()
        self.type(self.terminals()[0]["id"], "claude --verbose")
        session = self.box.wait_event(event="start", timeout=15)["session"]
        self.recorded(session)
        self.crash_and_attach()
        restored = self.box.wait_event(event="start", skip=1, timeout=20)
        pane = self.terminals()[0]["id"]  # the restored pane is a new one
        self.new_pane()  # focused now, and runs no CLI
        time.sleep(3)  # the plugin sees the restored pane
        self.zellij("--session", SESSION, "pipe", "--name", "zjr-reload", "--", "no-such-pane")  # ignored
        self.zellij("--session", SESSION, "pipe", "--name", "zjr-reload", "--", f"terminal_{pane}")
        again = self.box.wait_event(event="start", skip=2, timeout=15)
        self.assertEqual(again["argv"], ["--resume", session, "--verbose"])
        self.assertNotEqual(again["pid"], restored["pid"])

    def test_reload_from_inside_claude(self):
        # `! zellij pipe --name zjr-reload -- reload` typed into Claude Code.
        output = os.path.join(self.box.dir, "pipe.out")
        self.start(FAKE_RUN_AFTER=4, FAKE_RUN=f"{ZELLIJ} pipe --name zjr-reload -- reload", FAKE_RUN_OUTPUT=output)
        self.type(self.terminals()[0]["id"], "claude --verbose")
        first = self.box.wait_event(event="start", timeout=15)
        self.box.wait_event(event="ran", timeout=15)
        again = self.box.wait_event(event="start", skip=1, timeout=15)
        self.assertEqual(again["argv"], ["--resume", first["session"], "--verbose"])
        wait_for(lambda: not any(p.split()[1:3] == ["pipe", "--name"] for p in self.processes()), what="zellij pipe to exit")

    def processes(self, pid=False):
        columns = "pid=,args=" if pid else "args="
        return subprocess.run(["ps", "-A", "-o", columns], capture_output=True, text=True).stdout.splitlines()

if __name__ == "__main__":
    unittest.main()
