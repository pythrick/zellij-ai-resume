"""Proof of concept of the plugin approach, against a real Zellij: plain
`claude` (no launcher) in a shell pane; kill -9 the server; attach again; the
plugin must replace the restored pane with `claude --resume <conversation>`
in the directory it ran in, without a key being pressed. Twice.

Opt-in (it builds the plugin and starts real Zellij servers):

    ZJR_E2E=1 python3 -m unittest test_plugin

Throwaway HOME, configuration and socket directory; the fake claude only."""

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
WASM = os.path.join(PLUGIN, "target", "wasm32-wasip1", "release", "zellij-ai-resume.wasm")
SESSION = "zjr-plugin"


@unittest.skipUnless(os.environ.get("ZJR_E2E") == "1" and ZELLIJ and os.path.exists(CARGO),
                     "set ZJR_E2E=1 (needs zellij and cargo)")
class PluginResurrection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run([CARGO, "build", "--release", "--target", "wasm32-wasip1"], cwd=PLUGIN, check=True,
                       capture_output=True)

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.sockets = tempfile.mkdtemp(prefix="zjr-plugin-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.sockets, True)
        home = self.box.env["HOME"]
        config_dir = os.path.join(self.box.dir, "zellij-config")
        os.mkdir(config_dir)
        shell = os.path.join(self.box.dir, "shell")
        with open(shell, "w") as handle:
            handle.write("#!/bin/sh\nexec /bin/bash --norc --noprofile -i\n")
        os.chmod(shell, 0o755)
        self.config = os.path.join(config_dir, "config.kdl")
        with open(self.config, "w") as handle:
            handle.write(f'default_shell "{shell}"\nserialization_interval 1\nshow_startup_tips false\n'
                         f'show_release_notes false\nload_plugins {{\n    "file:{WASM}"\n}}\n')
        # The one-time grant (zellij plugin -- file:... then y), as Zellij caches it.
        for cache in ("Library/Caches/org.Zellij-Contributors.Zellij", ".cache/zellij"):
            os.makedirs(os.path.join(home, cache), exist_ok=True)
            with open(os.path.join(home, cache, "permissions.kdl"), "w") as handle:
                handle.write(f'"{WASM}" {{\n    ReadApplicationState\n    RunCommands\n}}\n')
        self.env = dict(self.box.env, ZELLIJ_CONFIG_DIR=config_dir, ZELLIJ_SOCKET_DIR=self.sockets, SHELL=shell,
                        FAKE_CLEAR_AFTER="3")
        self.clients = []
        self.addCleanup(self.cleanup)

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

    def attach(self, create=False):
        client = Pane([ZELLIJ, "--config", self.config, "attach", *(["--create"] if create else []), SESSION],
                      self.env, self.box.work)
        self.clients.append(client)
        return client

    def saved(self):
        home = self.env["HOME"]
        files = glob.glob(f"{home}/Library/Caches/org.Zellij-Contributors.Zellij/**/plugin_cache/session-*.json", recursive=True)
        files += glob.glob(f"{home}/.cache/zellij/**/plugin_cache/session-*.json", recursive=True)
        if not files:
            return {}
        with open(files[0]) as handle:
            return json.load(handle).get("panes", {})

    def crash_and_attach(self):
        time.sleep(2.5)  # a serialization tick
        os.kill(self.server(), signal.SIGKILL)
        wait_for(lambda: self.server() is None, what="the server to die")
        for client in self.clients:
            client.close()
        self.attach()

    def test_plain_claude_resumes_in_its_directory(self):
        client = self.attach(create=True)
        time.sleep(3)
        os.write(client.master, b"mkdir -p sub && cd sub && claude --verbose 'a prompt'\r")
        first = self.box.wait_event(event="start", timeout=15)
        self.assertEqual(first["argv"], ["--verbose", "a prompt"])
        self.assertTrue(first["cwd"].endswith("/work/sub"))
        for crash in (1, 2):
            cleared = self.box.wait_event(event="clear", skip=crash - 1, timeout=15)["session"]
            wait_for(lambda: any(e["session_id"] == cleared for e in self.saved().values()), timeout=20,
                     what="the plugin to record the conversation")
            self.crash_and_attach()
            resumed = self.box.wait_event(event="start", skip=crash, timeout=20)  # no key pressed
            self.assertEqual(resumed["argv"], ["--resume", cleared, "--verbose"])
            self.assertEqual(resumed["cwd"], first["cwd"])


if __name__ == "__main__":
    unittest.main()
