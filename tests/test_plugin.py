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
        # The default shell itself, as Zellij usually runs it: a shell pane
        # is then saved as a plain pane, without a command (the HOME is empty,
        # so there are no startup files).
        shell = "/bin/bash"
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

    def configure(self, serialization_interval=None, **plugin):
        """Change config.kdl: Zellij's save interval, the plugin's settings."""
        config = os.path.join(self.box.home, ".config", "zellij", "config.kdl")
        with open(config) as handle:
            text = handle.read()
        if serialization_interval is not None:
            text = text.replace("serialization_interval 1\n", f"serialization_interval {serialization_interval}\n")
        if plugin:
            settings = "".join(f'        {key} "{value}"\n' for key, value in plugin.items())
            text = text.replace(f'"{URL}"\n', f'"{URL}" {{\n{settings}    }}\n')
        with open(config, "w") as handle:
            handle.write(text)

    def saved_layout(self):
        """What Zellij saved to resurrect the session from."""
        for cache in ("Library/Caches/org.Zellij-Contributors.Zellij", ".cache/zellij"):
            path = os.path.join(self.box.home, cache, "contract_version_1", "session_info", SESSION, "session-layout.kdl")
            try:
                with open(path) as handle:
                    return handle.read()
            except FileNotFoundError:
                continue
        return ""


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

    def test_a_shutdown_that_stops_the_clis_first(self):
        # A reboot or logout: every process gets SIGTERM, the CLIs exit, the
        # shells and Zellij outlive them a moment, and Zellij saves its layout
        # once more, with plain shells where the CLIs ran.
        self.shut_down_and_come_back(start_shell="")

    def test_a_shutdown_with_the_clis_in_a_nested_shell(self):
        # Zellij saves a shell other than the default one as the pane's
        # command, and brings the pane back holding it, waiting for Enter.
        self.shut_down_and_come_back(start_shell="bash --norc && ")

    def shut_down_and_come_back(self, start_shell):
        self.start(FAKE_ROLLOUT_DELAY=1)
        first = self.terminals()[0]["id"]
        if start_shell:
            self.type(first, start_shell.removesuffix(" && "))
            time.sleep(1)
        self.type(first, "mkdir -p a && cd a && claude --model one")
        self.new_pane()  # a plain shell beside it
        self.action("new-tab", "--cwd", self.box.work)
        wait_for(lambda: len(self.terminals()) == 3, interval=0.5, what="the second tab")
        tab2 = max(pane["id"] for pane in self.terminals())
        self.type(tab2, "mkdir -p c && cd c && codex --search")
        claude = self.box.wait_event(event="start", timeout=15)
        codex_session = self.box.wait_event(tool="codex", event="rollout", timeout=15)["session"]
        self.recorded(claude["session"])
        self.recorded(codex_session)
        time.sleep(2.5)
        codex = self.starts("codex")[0]
        for pid in (claude["pid"], codex["pid"]):
            os.kill(pid, signal.SIGTERM)
        wait_for(lambda: "fakes/claude" not in self.saved_layout() and "fakes/codex" not in self.saved_layout(),
                 timeout=15, what="Zellij to save shells where the CLIs ran")
        if start_shell:
            self.assertIn("--norc", self.saved_layout())  # the nested shell, as the pane's command
        time.sleep(3)  # the plugin's probes see both gone
        self.assertEqual(sorted(entry["session_id"] for entry in self.saved().values()), sorted([claude["session"], codex_session]))
        os.kill(self.server(), signal.SIGKILL)
        wait_for(lambda: self.server() is None, what="the server to die")
        for client in self.clients:
            client.close()
        self.attach()
        wait_for(lambda: len(self.starts()) == 2 and len(self.starts("codex")) == 2, timeout=25, what="both back")
        self.assertEqual(self.starts()[1]["argv"], ["--resume", claude["session"], "--model", "one"])
        self.assertEqual(self.starts()[1]["cwd"], claude["cwd"])
        self.assertEqual(self.starts("codex")[1]["argv"], ["resume", "--search", codex_session])
        self.assertEqual(self.starts("codex")[1]["cwd"], codex["cwd"])
        self.assertEqual(len(self.terminals()), 3)  # the shell beside Claude is still a shell

    def test_a_cli_quit_well_before_the_end_stays_quit(self):
        self.configure(exit_grace=2)
        self.start(FAKE_EXIT_AFTER=6)  # long enough to be seen running first
        self.type(self.terminals()[0]["id"], "claude --verbose")
        self.recorded(self.box.wait_event(event="start", timeout=15)["session"])
        self.box.wait_event(event="exit", timeout=10)
        wait_for(lambda: self.saved() == {}, timeout=15, what="the plugin to forget it")
        self.crash_and_attach()
        time.sleep(8)
        self.assertEqual(len(self.starts()), 1)

    def test_a_new_session_named_like_a_deleted_one_is_left_alone(self):
        self.start()
        self.type(self.terminals()[0]["id"], "claude --verbose")
        self.recorded(self.box.wait_event(event="start", timeout=15)["session"])
        time.sleep(2.5)
        os.kill(self.server(), signal.SIGKILL)
        wait_for(lambda: self.server() is None, what="the server to die")
        for client in self.clients:
            client.close()
        self.zellij("delete-session", SESSION)
        self.assertEqual(self.saved_layout(), "")
        # Zellij's default: a new session saves its first layout a minute in.
        self.configure(serialization_interval=60)
        self.attach(create=True)
        wait_for(lambda: self.terminals(), timeout=30, interval=0.5, what="the new session")
        time.sleep(8)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(len(self.terminals()), 1)

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


class Upgrade(PluginTest):
    def test_restarting_the_plugin_in_a_running_session_changes_no_pane(self):
        # `zellij action start-or-reload-plugin`, as after reinstalling: the
        # plugin starts again in the session that wrote its map. A CLI quit
        # moments before is still in the map, but its shell is left alone.
        self.start(FAKE_EXIT_AFTER=3)
        pane = self.terminals()[0]["id"]
        self.type(pane, "claude --verbose")
        session = self.box.wait_event(event="start", timeout=15)["session"]
        self.recorded(session)
        self.box.wait_event(event="exit", timeout=10)
        time.sleep(3)
        self.assertIn(session, [entry["session_id"] for entry in self.saved().values()])  # kept for exit_grace
        self.zellij("--session", SESSION, "action", "start-or-reload-plugin", URL)
        time.sleep(10)
        self.assertEqual(len(self.starts()), 1)
        self.type(pane, "FAKE_EXIT_AFTER= claude --model two")  # and it follows what starts next
        self.recorded(self.box.wait_event(event="start", skip=1, timeout=15)["session"])


class TabTitles(PluginTest):
    # Zellij shows the title of a tab's only pane as the tab's name while the
    # tab has its default name; a resurrected tab's saved "Tab #2" counts as a
    # name of its own. A title is set the way Claude Code keeps one: by a
    # program in the foreground (a shell's prompt may set its own, as Arch's
    # does). It is set in parts, so that only the tab bar shows it whole.

    def title(self, pane, text):
        head, tail = text[:-1], text[-1]
        self.type(pane, f"printf '\\033]0;%s%s\\007' {head} {tail}; sleep 600")

    def tab_bar_shows(self, client, since, *titles):
        text = client.output[since:].decode("utf-8", "replace")
        return all(title in text for title in titles)

    def tabs(self):
        return [line.split()[2:] for line in self.action("list-tabs", "--state").strip().splitlines()[1:]]

    def three_tabs(self):
        for _ in range(2):
            self.action("new-tab", "--cwd", self.box.work)
            time.sleep(0.5)  # a burst of actions can stall a Zellij server
        wait_for(lambda: len(self.terminals()) == 3, interval=0.5, what="three tabs")
        self.action("go-to-tab", "2")
        time.sleep(0.5)

    def test_after_a_resurrection_tabs_show_their_pane_titles_again(self):
        self.start()
        self.three_tabs()
        time.sleep(2)
        client = self.crash_and_attach()
        panes = wait_for(lambda: len(self.terminals()) == 3 and sorted(p["id"] for p in self.terminals()), interval=0.5,
                         what="the tabs back")
        self.assertEqual([tab[0] for tab in self.tabs()], ["Tab", "Tab", "Tab"])
        since = len(client.output)
        for index, pane in enumerate(panes):
            self.title(pane, f"tab-title-{index}")
        wait_for(lambda: self.tab_bar_shows(client, since, "tab-title-0", "tab-title-1", "tab-title-2"), timeout=50,
                 interval=1, what="the tab bar to show the panes' titles")
        self.assertEqual([(p["id"], p["tab_position"]) for p in sorted(self.terminals(), key=lambda p: p["id"])],
                         [(pane, index) for index, pane in enumerate(panes)])  # same order
        self.assertEqual([tab[2] for tab in self.tabs()], ["false", "true", "false"])  # the second still focused

    def test_on_request_and_never_for_a_tab_named_by_the_user(self):
        client = self.start()
        self.three_tabs()
        for tab, name in ((1, "work"), (2, "Tab #12"), (3, "Tab #13")):  # the last two as a resurrection names them
            self.action("go-to-tab", str(tab))
            time.sleep(0.3)
            self.action("rename-tab", name)
            time.sleep(0.3)
        panes = sorted(p["id"] for p in self.terminals())
        since = len(client.output)
        for index, pane in enumerate(panes):
            self.title(pane, f"tab-title-{index}")
        time.sleep(2)
        self.assertFalse(self.tab_bar_shows(client, since, "tab-title-1"))
        self.zellij("--session", SESSION, "pipe", "--name", "zjr-tabs")  # done once the restore window is over
        wait_for(lambda: self.tab_bar_shows(client, since, "tab-title-1", "tab-title-2"), timeout=50, interval=1,
                 what="the tab bar to show the titles")
        names = [tab[0] for tab in self.tabs()]
        self.assertEqual(names[0], "work")
        self.assertNotIn("#12", " ".join(names))
        self.assertEqual([p["tab_position"] for p in sorted(self.terminals(), key=lambda p: p["id"])], [0, 1, 2])


    def test_a_session_whose_tabs_were_moved_comes_back_right(self):
        # Resurrected, the tabs move to new ones (new ids, by the plugin);
        # the next resurrection must still find each CLI at its position.
        self.start()
        self.three_tabs()
        for index, pane in enumerate(sorted(p["id"] for p in self.terminals())):
            self.type(pane, f"claude --model m{index}")
        starts = wait_for(lambda: len(self.starts()) == 3 and self.starts(), timeout=15, what="three CLIs")
        for start in starts:
            self.recorded(start["session"])
        self.crash_and_attach()
        wait_for(lambda: len(self.starts()) == 6, timeout=25, what="all three back")
        tab_ids = lambda: [line.split()[0] for line in self.action("list-tabs").strip().splitlines()[1:]]
        ids_before = tab_ids()
        wait_for(lambda: tab_ids() != ids_before and len(tab_ids()) == 3, timeout=50, interval=1,
                 what="the panes to move to new tabs")
        model_at = lambda: {key: entry["args"][entry["args"].index("--model") + 1] for key, entry in self.saved().items()}
        wait_for(lambda: model_at() == {"0:0": "m0", "1:0": "m1", "2:0": "m2"}, timeout=10, what="the map at the same positions")
        self.crash_and_attach()
        wait_for(lambda: len(self.starts()) == 9, timeout=25, what="all three back again")
        session_of = {start["argv"][1]: start["session"] for start in starts}
        for start in self.starts()[6:]:
            model = start["argv"][3]
            self.assertEqual(start["argv"][:2], ["--resume", session_of[model]])
        self.assertEqual(len(self.terminals()), 3)
        wait_for(lambda: model_at() == {"0:0": "m0", "1:0": "m1", "2:0": "m2"}, timeout=10, what="each at its position")

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

    def test_reload_before_the_first_message_starts_afresh(self):
        # Claude Code names the conversation at start but writes it only with
        # the first message: there is nothing to resume yet.
        client = self.start(FAKE_NO_TRANSCRIPT=1)
        self.type(self.terminals()[0]["id"], "claude --verbose 'a prompt'")
        self.recorded(self.box.wait_event(event="start", timeout=15)["session"])
        os.write(client.master, b"\x1br")  # Alt r
        again = self.box.wait_event(event="start", skip=1, timeout=15)
        self.assertEqual(again["argv"], ["--verbose"])
        self.assertNotIn("failed", again)

    def test_a_configured_reload_key(self):
        self.configure(reload_key="Alt y")
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
