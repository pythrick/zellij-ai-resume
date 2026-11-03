"""Against a real Zellij: start zclaude in a shell pane, kill -9 the server,
resurrect it the way a person does (attach, press Enter), check the
conversation comes back; then crash and resurrect a second time.

Opt-in, because it starts real Zellij servers:

    ZJR_E2E=1 python3 -m unittest test_zellij

The servers run with a throwaway HOME, configuration directory and socket
directory, so they never see or touch your sessions, configuration or cache.
(Zellij resurrects nothing unless its configuration directory exists, hence
ZELLIJ_CONFIG_DIR.) Panes run bash without rc files and the fake CLIs, never
the real claude or codex."""

import glob
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from helpers import Pane, Sandbox, wait_for

ZELLIJ = shutil.which("zellij")


@unittest.skipUnless(os.environ.get("ZJR_E2E") == "1" and ZELLIJ, "set ZJR_E2E=1 (needs zellij on PATH)")
class Resurrection(unittest.TestCase):
    def setUp(self):
        self.session = f"zjr-e2e-{os.getpid()}"
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.sockets = tempfile.mkdtemp(prefix="zjr-e2e-", dir="/tmp")  # socket paths must be short
        self.addCleanup(shutil.rmtree, self.sockets, True)
        shell = os.path.join(self.box.dir, "shell")
        with open(shell, "w") as handle:
            handle.write("#!/bin/sh\nexec /bin/bash --norc --noprofile -i\n")
        os.chmod(shell, 0o755)
        self.config = os.path.join(self.box.dir, "config.kdl")
        with open(self.config, "w") as handle:
            handle.write(f'default_shell "{shell}"\nserialization_interval 1\n'
                         "show_startup_tips false\nshow_release_notes false\n")
        config_dir = os.path.join(self.box.dir, "zellij-config")
        os.mkdir(config_dir)
        self.env = dict(self.box.env, ZELLIJ_CONFIG_DIR=config_dir, ZELLIJ_SOCKET_DIR=self.sockets,
                        SHELL=shell, FAKE_CLEAR_AFTER="2")
        self.clients = []
        self.addCleanup(self.cleanup)

    def zellij(self, *args, check=True):
        result = subprocess.run([ZELLIJ, *args], env=self.env, capture_output=True, text=True, timeout=30)
        if check and result.returncode != 0:
            raise AssertionError(f"zellij {' '.join(args)} failed: {result.stderr}")
        return result.stdout

    def action(self, *args):
        return self.zellij("--session", self.session, "action", *args)

    def server_pid(self):
        for line in subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True).stdout.splitlines():
            words = line.split()
            if (len(words) >= 4 and words[1].endswith("zellij") and words[2] == "--server"
                    and words[3].startswith(self.sockets) and words[3].endswith("/" + self.session)):
                return int(words[0])
        return None

    def kill_server(self):
        pid = wait_for(self.server_pid, what="zellij server")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # gone already
        wait_for(lambda: self.server_pid() is None, what="server to die")

    def cache_dirs(self):
        home = self.env["HOME"]
        return (glob.glob(f"{home}/Library/Caches/org.Zellij-Contributors.Zellij/*/session_info/{self.session}")
                + glob.glob(f"{home}/.cache/zellij/*/session_info/{self.session}"))

    def cleanup(self):
        for client in self.clients:
            client.close()
        if self.server_pid():
            self.kill_server()
        self.zellij("delete-session", self.session, "--force", check=False)
        for path in self.cache_dirs():
            shutil.rmtree(path, ignore_errors=True)

    def saved_layout(self):
        paths = [os.path.join(path, "session-layout.kdl") for path in self.cache_dirs()]
        paths = [path for path in paths if os.path.isfile(path)]
        if not paths:
            return ""
        with open(max(paths, key=os.path.getmtime)) as handle:
            return handle.read()

    @staticmethod
    def pane_command(layout):
        match = re.search(r'pane command="/bin/sh"[^{]*\{\s*args "([^"]+)" "--zj-pane" "([0-9a-f-]{36})"', layout)
        return match and (match.group(1), match.group(2))

    def tracked(self, token, session_id):
        return self.box.states().get(token, {}).get("session_id") == session_id

    def test_conversation_survives_repeated_crashes(self):
        launcher = self.box.launcher("zclaude")
        self.zellij("--config", self.config, "attach", "--create-background", self.session)
        time.sleep(1.5)
        self.action("write-chars", "--pane-id", "terminal_0", f"{launcher} --dangerously-skip-permissions 'first prompt'")
        self.action("write", "--pane-id", "terminal_0", "13")

        start = self.box.wait_event(event="start", timeout=15)
        self.assertEqual(start["argv"][2:], ["--dangerously-skip-permissions", "first prompt"])
        cleared = self.box.wait_event(event="clear", timeout=15)["session"]
        saved = wait_for(lambda: self.pane_command(self.action("dump-layout")), what="pane command in the layout")
        self.assertEqual(saved[0], launcher)
        token = saved[1]
        wait_for(lambda: self.tracked(token, cleared), what="state to follow /clear")
        time.sleep(2.5)  # let a serialization tick record the current layout
        self.kill_server()
        layout = self.saved_layout()
        self.assertEqual(self.pane_command(layout), saved)
        self.assertIn("start_suspended true", layout)

        # Resurrect the way a person does: attach from a terminal; nothing runs until Enter.
        client = Pane([ZELLIJ, "--config", self.config, "attach", self.session], self.env, self.box.work)
        self.clients.append(client)
        time.sleep(4)
        self.assertEqual(len(self.box.events(event="start")), 1, "a resurrected pane ran before Enter")
        os.write(client.master, b"\r")
        resumed = self.box.wait_event(event="start", skip=1, timeout=15)
        self.assertEqual(resumed["argv"], ["--resume", cleared, "--dangerously-skip-permissions"])
        self.assertEqual(resumed["cwd"], start["cwd"])

        # The resurrected pane saves the same command, so a second crash is survived too.
        cleared = self.box.wait_event(event="clear", skip=1, timeout=15)["session"]
        wait_for(lambda: self.tracked(token, cleared), what="state to follow the second /clear")
        self.assertEqual(self.pane_command(self.action("dump-layout")), saved)
        time.sleep(2.5)
        self.kill_server()
        client.close()
        self.zellij("--config", self.config, "attach", "--create-background", "--force-run-commands", self.session)
        again = self.box.wait_event(event="start", skip=2, timeout=15)
        self.assertEqual(again["argv"], ["--resume", cleared, "--dangerously-skip-permissions"])


    def test_keybinding_reloads_the_focused_pane(self):
        snippet = subprocess.run([self.box.launcher("zjr"), "keybinding"], env=self.env, capture_output=True,
                                 text=True, check=True).stdout
        with open(self.config, "a") as handle:
            handle.write(snippet + "\n")  # what `zjr keybinding` tells you to add, as is
        self.env["FAKE_CLEAR_AFTER"] = ""
        launcher = self.box.launcher("zclaude")
        client = Pane([ZELLIJ, "--config", self.config, "attach", "--create", self.session], self.env, self.box.work)
        self.clients.append(client)

        def keys(data, pause=1.0):
            os.write(client.master, data)
            time.sleep(pause)

        time.sleep(3)
        keys(f"{launcher} --verbose\r".encode())
        left = self.box.wait_event(event="start", timeout=15)
        keys(b"\x1bn", 1.5)  # Alt n: a new pane, which takes the focus
        keys(f"{launcher} --model x\r".encode())
        right = self.box.wait_event(event="start", skip=1, timeout=15)
        wait_for(lambda: len([s for s in self.box.states().values() if s.get("core_pid")]) == 2, what="two running panes")

        def no_floating_pane():
            panes = json.loads(self.action("list-panes", "--json", "--all"))
            return not [p for p in panes if p.get("is_floating") and not p.get("is_plugin")]

        keys(b"\x1br", 0)  # Alt r: reload the focused (right) pane
        again = self.box.wait_event(event="start", skip=2, timeout=20)
        self.assertEqual(again["argv"], ["--resume", right["session"], "--model", "x"])
        self.assertEqual([e["pid"] for e in self.box.events(event="signal")], [right["pid"]])
        wait_for(no_floating_pane, timeout=3, what="the reload pane to close")

        keys(b"\x1bh", 1.5)  # Alt h: focus the left pane
        keys(b"\x1br", 0)
        again = self.box.wait_event(event="start", skip=3, timeout=20)
        self.assertEqual(again["argv"], ["--resume", left["session"], "--verbose"])
        self.assertEqual(sorted(e["pid"] for e in self.box.events(event="signal")), sorted([right["pid"], left["pid"]]))

        wait_for(no_floating_pane, timeout=3, what="the reload pane to close")
        layout = self.action("dump-layout")
        self.assertEqual(len(re.findall(r'args "%s" "--zj-pane"' % re.escape(launcher), layout)), 2)

if __name__ == "__main__":
    unittest.main()
