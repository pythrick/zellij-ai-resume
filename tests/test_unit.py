"""Unit tests for the core: argument handling, plans, state, locks, watchers."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest import mock

from helpers import load_core

zjr = load_core()
CLAUDE = zjr.SPECS["claude"]
CODEX = zjr.SPECS["codex"]
U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="zjr-unit-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": os.path.join(self.dir, "state"),
            "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "claude"),
            "CODEX_HOME": os.path.join(self.dir, "codex"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def claude_transcript(self, session_id, cwd="/work"):
        project = os.path.join(self.dir, "claude", "projects", cwd.replace("/", "-"))
        os.makedirs(project, exist_ok=True)
        with open(os.path.join(project, session_id + ".jsonl"), "w") as handle:
            handle.write("{}\n")

    def state(self, tool, args, **fields):
        state = zjr.new_state(tool, args, self.dir)
        state.update(fields)
        return state


class ClaudeArguments(unittest.TestCase):
    def split(self, args):
        return CLAUDE.split(CLAUDE.parse(args), {})

    def test_settings_are_kept_and_prompt_dropped(self):
        kept, dropped = self.split(["--dangerously-skip-permissions", "--model", "opus", "fix the bug"])
        self.assertEqual(kept, ["--dangerously-skip-permissions", "--model", "opus"])
        self.assertEqual(dropped, ["fix the bug"])

    def test_equals_form(self):
        self.assertEqual(self.split(["--model=opus", "hello"]), (["--model=opus"], ["hello"]))

    def test_variadic_option_takes_words_until_next_option(self):
        kept, dropped = self.split(["--add-dir", "a", "b", "--effort", "high", "prompt"])
        self.assertEqual(kept, ["--add-dir", "a", "b", "--effort", "high"])
        self.assertEqual(dropped, ["prompt"])

    def test_value_option_takes_dash_value(self):
        self.assertEqual(self.split(["--append-system-prompt", "-be terse-"])[0], ["--append-system-prompt", "-be terse-"])

    def test_separator_and_everything_after_it_is_dropped(self):
        self.assertEqual(self.split(["--verbose", "--", "--not-an-option"]), (["--verbose"], ["--", "--not-an-option"]))

    def test_unknown_option_and_its_words_are_dropped(self):
        kept, dropped = self.split(["--brand-new", "value", "--model", "x"])
        self.assertEqual(kept, ["--model", "x"])
        self.assertEqual(dropped, ["--brand-new", "value"])

    def test_one_shot_options_are_not_repeated(self):
        args = ["--resume", U1, "--fork-session", "-n", "name", "-w", "tree", "--file", "f:a.txt", "-c", "--verbose"]
        self.assertEqual(self.split(args)[0], ["--verbose"])

    def test_passthrough(self):
        for args in (["-p", "hi"], ["--print"], ["--version"], ["-h"], ["--bg"], ["doctor"],
                     ["--model", "opus", "mcp", "list"], ["update"], ["--cloud"]):
            self.assertIsNotNone(CLAUDE.passthrough_reason(CLAUDE.parse(args)), args)
        for args in ([], ["fix the doctor"], ["--model", "opus"], ["--", "doctor"], ["-r"]):
            self.assertIsNone(CLAUDE.passthrough_reason(CLAUDE.parse(args)), args)

    def test_first_launch_adds_session_id(self):
        args, expected = CLAUDE.first_args(CLAUDE.parse(["--verbose", "go"]), ["--verbose", "go"], U1)
        self.assertEqual(args, ["--session-id", U1, "--verbose", "go"])
        self.assertEqual(expected, U1)

    def test_first_launch_respects_selectors(self):
        cases = {
            ("--session-id", U2): U2,
            ("--resume", U2): U2,
            ("--resume=" + U2,): U2,
            ("-r", U2, "--fork-session"): None,
            ("--resume",): None,
            ("--resume", "named"): None,
            ("-c",): None,
            ("--from-pr", "12"): None,
        }
        for args, expected in cases.items():
            result, chosen = CLAUDE.first_args(CLAUDE.parse(list(args)), list(args), U1)
            self.assertEqual(result, list(args), args)
            self.assertEqual(chosen, expected, args)


class CodexArguments(unittest.TestCase):
    def test_split(self):
        args = ["-m", "o3", "-C", "sub", "-i", "a.png", "b.png", "--search", "write tests"]
        parsed = CODEX.parse(args)
        self.assertEqual(CODEX.split(parsed, {}), (["-m", "o3", "-C", "sub", "--search"],
                                                    ["-i", "a.png", "b.png", "write tests"]))

    def test_attached_short_values(self):
        parsed = CODEX.parse(["-mo3", "-m=o4", "prompt"])
        self.assertEqual(CODEX.split(parsed, {})[0], ["-mo3", "-m=o4"])
        self.assertEqual(parsed.option("-m").value(), "o3")

    def test_passthrough(self):
        for args in (["exec", "x"], ["e"], ["review"], ["login"], ["--version"], ["mcp", "list"], ["-m", "o3", "apply"]):
            self.assertIsNotNone(CODEX.passthrough_reason(CODEX.parse(args)), args)
        for args in ([], ["resume"], ["resume", "--last"], ["fork", U1], ["explain exec"]):
            self.assertIsNone(CODEX.passthrough_reason(CODEX.parse(args)), args)

    def test_first_launch(self):
        self.assertEqual(CODEX.first_args(CODEX.parse(["resume", U1]), ["resume", U1], U2)[1], U1)
        self.assertIsNone(CODEX.first_args(CODEX.parse(["resume", "--last"]), ["resume", "--last"], U2)[1])
        self.assertIsNone(CODEX.first_args(CODEX.parse(["fork", U1]), ["fork", U1], U2)[1])
        self.assertIsNone(CODEX.first_args(CODEX.parse(["hello"]), ["hello"], U2)[1])

    def test_resume_drops_subcommand_words(self):
        kept, dropped = CODEX.split(CODEX.parse(["resume", "--last", "-m", "o3", "go on"]), {})
        self.assertEqual(kept, ["-m", "o3"])
        self.assertEqual(CODEX.resume_args(U1, kept, fork=False), ["resume", "-m", "o3", U1])
        self.assertEqual(CODEX.resume_args(U1, kept, fork=True), ["fork", "-m", "o3", U1])


class Plans(TempDirTest):
    def plan(self, spec, state, busy=False):
        return zjr.make_plan(spec, state, U2, lambda session_id: busy)

    def test_first_launch_runs_the_original_arguments(self):
        plan = self.plan(CLAUDE, self.state("claude", ["--verbose", "prompt"]))
        self.assertEqual((plan.mode, plan.args, plan.session_id), ("first", ["--session-id", U2, "--verbose", "prompt"], U2))

    def test_resume_repeats_settings_only(self):
        self.claude_transcript(U1)
        plan = self.plan(CLAUDE, self.state("claude", ["--model", "opus", "prompt"], launched=True, session_id=U1))
        self.assertEqual((plan.mode, plan.args, plan.session_id), ("resume", ["--resume", U1, "--model", "opus"], U1))
        self.assertIn("not repeated: prompt", plan.message)

    def test_busy_conversation_is_forked(self):
        self.claude_transcript(U1)
        plan = self.plan(CLAUDE, self.state("claude", ["--verbose"], launched=True, session_id=U1), busy=True)
        self.assertEqual((plan.mode, plan.args, plan.session_id), ("fork", ["--resume", U1, "--fork-session", "--verbose"], None))

    def test_missing_transcript_starts_fresh(self):
        plan = self.plan(CLAUDE, self.state("claude", ["--verbose", "prompt"], launched=True, session_id=U1))
        self.assertEqual((plan.mode, plan.args, plan.session_id), ("fresh", ["--session-id", U2, "--verbose"], U2))
        self.assertIn("no longer on disk", plan.message)

    def test_failed_resume_is_not_retried(self):
        self.claude_transcript(U1)
        state = self.state("claude", [], launched=True, session_id=U1, failed_resume=U1)
        self.assertEqual(self.plan(CLAUDE, state).mode, "fresh")

    def test_resume_cwd_follows_the_conversation(self):
        self.claude_transcript(U1)
        elsewhere = os.path.join(self.dir, "worktree")
        os.mkdir(elsewhere)
        state = self.state("claude", ["-w"], launched=True, session_id=U1, session_cwd=elsewhere)
        plan = self.plan(CLAUDE, state)
        self.assertEqual((plan.args, plan.cwd), (["--resume", U1], elsewhere))

    def test_codex_resume_and_fresh(self):
        day = os.path.join(self.dir, "codex", "sessions", "2026", "11", "03")
        os.makedirs(day)
        open(os.path.join(day, f"rollout-2026-11-03T10-00-00-{U1}.jsonl"), "w").close()
        state = self.state("codex", ["-m", "o3", "prompt"], launched=True, session_id=U1)
        self.assertEqual(self.plan(CODEX, state).args, ["resume", "-m", "o3", U1])
        state["session_id"] = None
        plan = self.plan(CODEX, state)
        self.assertEqual((plan.mode, plan.args), ("fresh", ["-m", "o3"]))


class StateFiles(TempDirTest):
    def test_round_trip_and_privacy(self):
        store = zjr.Store()
        token = str(uuid.uuid4())
        store.save(token, self.state("claude", ["a"]))
        self.assertEqual(store.load(token, "claude")["args"], ["a"])
        self.assertIsNone(store.load(token, "codex"))
        for path in (store.root, store.panes, store.locks):
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(store.pane_file(token)).st_mode & 0o777, 0o600)

    def test_tokens_are_validated(self):
        store = zjr.Store()
        store.pane_file("abcdef01-2345-4678-9abc-def012345678")
        for bad in ("../../etc/passwd", "x", "ABCDEF01-2345-4678-9ABC-DEF012345678", U1 + "\n",
                    "11111111-1111-1111-1111-111111111111", "/" + U1):
            with self.assertRaises(ValueError):
                store.pane_file(bad)

    def test_malformed_state_is_rejected(self):
        store = zjr.Store()
        token = str(uuid.uuid4())
        good = self.state("claude", [])
        for field, value in (("session_id", "--dangerously-skip-permissions"), ("cwd", "relative"),
                             ("args", ["ok", 3]), ("args", "not a list"), ("launched", "yes"),
                             ("format", 99), ("session_cwd", "x"), ("args", ["a\0b"])):
            bad = dict(good, **{field: value})
            zjr.write_json(store.pane_file(token), bad)
            self.assertIsNone(store.load(token, "claude"), field)
        with open(store.pane_file(token), "w") as handle:
            handle.write("{not json")
        self.assertIsNone(store.load(token, "claude"))
        with open(store.pane_file(token), "w") as handle:
            handle.write(json.dumps(dict(good, args=["x" * 1024] * 300)))
        self.assertIsNone(store.load(token, "claude"))  # over the size limit

    def test_symlinks_are_not_followed(self):
        store = zjr.Store()
        token = str(uuid.uuid4())
        target = os.path.join(self.dir, "elsewhere.json")
        zjr.write_json(target, self.state("claude", []))
        os.symlink(target, store.pane_file(token))
        self.assertIsNone(store.load(token, "claude"))
        os.symlink(target, store.pane_lock(token).path)
        with self.assertRaises(OSError):
            store.pane_lock(token).acquire()

    def test_state_directory_must_be_a_real_private_directory(self):
        root = zjr.state_root()
        os.makedirs(os.path.dirname(root))
        os.symlink(self.dir, root)
        with self.assertRaises(zjr.StoreError):
            zjr.Store()
        os.unlink(root)
        os.mkdir(root, 0o777)
        os.chmod(root, 0o777)
        zjr.Store()
        self.assertEqual(os.stat(root).st_mode & 0o777, 0o700)

    def test_locks(self):
        store = zjr.Store()
        first, second = store.session_lock("claude", U1), store.session_lock("claude", U1)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        self.assertTrue(second.held_elsewhere())
        first.release()
        self.assertFalse(second.held_elsewhere())
        self.assertTrue(second.acquire())
        second.release()

    def test_gc(self):
        store = zjr.Store()
        old, running, recent = (str(uuid.uuid4()) for _ in range(3))
        for token in (old, running, recent):
            store.save(token, self.state("claude", []))
        past = time.time() - 40 * 86400
        for token in (old, running):
            os.utime(store.pane_file(token), (past, past))
        lock = store.pane_lock(running)
        self.assertTrue(lock.acquire())
        try:
            self.assertEqual(store.gc(), 1)
        finally:
            lock.release()
        self.assertEqual(store.tokens(), sorted([running, recent]))


class ClaudeRegistry(TempDirTest):
    def write_entry(self, pid, session_id, **extra):
        registry = os.path.join(self.dir, "claude", "sessions")
        os.makedirs(registry, exist_ok=True)
        entry = {"pid": pid, "sessionId": session_id, "cwd": self.dir, "startedAt": int(time.time() * 1000)}
        with open(os.path.join(registry, f"{pid}.json"), "w") as handle:
            json.dump(dict(entry, **extra), handle)

    def lstart(self, pid):
        """procStart the way Claude Code writes it: UTC, C locale."""
        env = dict(os.environ, LC_ALL="C", TZ="UTC0")
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, env=env).stdout
        return " ".join(out.split())

    def test_open_elsewhere(self):
        me = os.getpid()
        self.write_entry(me, U1, procStart=self.lstart(me))
        self.assertTrue(CLAUDE.open_elsewhere(U1))
        self.assertFalse(CLAUDE.open_elsewhere(U2))

    def test_open_elsewhere_whatever_the_local_time_zone_and_locale(self):
        me = os.getpid()
        self.write_entry(me, U1, procStart=self.lstart(me))
        with mock.patch.dict(os.environ, {"TZ": "America/Chicago", "LC_ALL": "en_CA.UTF-8", "LC_TIME": "fr_FR.UTF-8"}):
            self.assertTrue(CLAUDE.open_elsewhere(U1))

    def test_dead_or_reused_pid_is_not_open(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.write_entry(dead.pid, U1)
        self.write_entry(os.getpid(), U2, procStart="Mon Jan  1 00:00:00 2001")
        self.assertFalse(CLAUDE.open_elsewhere(U1))
        self.assertFalse(CLAUDE.open_elsewhere(U2))

    def pane(self, session_id=U1):
        store = zjr.Store()
        token = str(uuid.uuid4())
        return store, token, zjr.Pane(store, token, self.state("claude", [], launched=True, session_id=session_id), CLAUDE, True)

    def test_watcher_records_switches(self):
        store, token, pane = self.pane()
        watcher = zjr.ClaudeWatcher(pane, os.getpid())
        self.write_entry(os.getpid(), U2)
        watcher.poll()
        self.assertEqual(store.load(token, "claude")["session_id"], U2)
        self.assertEqual(store.load(token, "claude")["session_cwd"], self.dir)
        self.assertTrue(store.session_lock("claude", U2).held_elsewhere())  # the pane holds it now
        pane.release()
        self.assertFalse(store.session_lock("claude", U2).held_elsewhere())

    def test_watcher_ignores_an_entry_left_by_an_earlier_process(self):
        store, token, pane = self.pane()
        self.write_entry(os.getpid(), U2, startedAt=int((time.time() - 3600) * 1000))
        watcher = zjr.ClaudeWatcher(pane, os.getpid())
        watcher.poll()
        self.assertEqual(pane.current(), U1)
        self.write_entry(os.getpid(), U2)  # rewritten by the new process
        watcher.next_search = 0
        watcher.poll()
        self.assertEqual(pane.current(), U2)
        pane.release()


class CodexDiscovery(TempDirTest):
    def rollout(self, session_id, cwd, created=None, source="cli", hold=False):
        now = time.localtime()
        day = os.path.join(self.dir, "codex", "sessions", time.strftime("%Y", now), time.strftime("%m", now), time.strftime("%d", now))
        os.makedirs(day, exist_ok=True)
        created = created if created is not None else time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(created))
        meta = {"type": "session_meta", "payload": {"id": session_id, "timestamp": stamp, "cwd": cwd, "source": source}}
        path = os.path.join(day, f"rollout-x-{session_id}.jsonl")
        handle = open(path, "w")
        handle.write(json.dumps(meta) + "\n")
        handle.flush()
        if hold:  # the way Codex running in-process keeps it open
            self.addCleanup(handle.close)
        else:
            handle.close()
        return path

    def pane(self, session_id=None):
        store = zjr.Store()
        token = str(uuid.uuid4())
        pane = zjr.Pane(store, token, self.state("codex", [], launched=True, session_id=session_id), CODEX, True)
        self.addCleanup(pane.release)
        return pane

    def watcher(self, pane, guess=True, look_back=False):
        # This test process plays the CLI: files it holds open are "the pane's".
        return zjr.CodexWatcher(pane, os.getpid(), os.path.realpath(self.dir), guess=guess, look_back=look_back)

    def test_guesses_a_new_session_in_its_directory(self):
        pane = self.pane()
        watcher = self.watcher(pane)
        self.rollout(U1, "/somewhere/else")
        self.rollout(U2, self.dir, source="exec")
        watcher.poll()
        self.assertIsNone(pane.current())
        mine = str(uuid.uuid4())
        self.rollout(mine, self.dir)
        watcher.poll()
        self.assertEqual(pane.current(), mine)

    def test_skips_sessions_claimed_by_other_panes(self):
        other = self.pane()
        self.assertTrue(other.lock_session(U1))
        pane = self.pane()
        self.rollout(U1, self.dir)
        self.watcher(pane).poll()
        self.assertIsNone(pane.current())

    def test_the_file_the_cli_holds_open_beats_a_guess(self):
        pane = self.pane()
        self.rollout(U1, self.dir, created=time.time() - 1)
        self.rollout(U2, self.dir, hold=True)
        self.watcher(pane).poll()
        self.assertEqual(pane.current(), U2)

    def test_a_guess_never_replaces_a_conversation(self):
        pane = self.pane(U1)
        watcher = self.watcher(pane)
        old = self.rollout(U2, self.dir, created=time.time() - 3600)
        os.utime(old)  # appended to now, but created before this pane started
        watcher.poll()
        self.rollout(str(uuid.uuid4()), self.dir)  # another Codex starting in the same directory
        watcher.poll()
        self.assertEqual(pane.current(), U1)

    def test_new_conversation_in_the_pane_is_followed(self):
        pane = self.pane(U1)
        newer = str(uuid.uuid4())
        self.rollout(newer, self.dir, hold=True)
        self.watcher(pane).poll()
        self.assertEqual(pane.current(), newer)

    def test_resume_picker_is_only_followed_by_open_files(self):
        pane = self.pane()
        old = self.rollout(U1, self.dir, created=time.time() - 3600)
        os.utime(old)
        watcher = self.watcher(pane, guess=False, look_back=True)
        watcher.poll()
        self.assertIsNone(pane.current())
        self.rollout(U2, self.dir, created=time.time() - 7200, hold=True)  # the one picked
        watcher.next_look = 0
        watcher.poll()
        self.assertEqual(pane.current(), U2)


class Misc(TempDirTest):
    def test_own_launchers_and_relative_path_entries_are_skipped(self):
        shim, real = os.path.join(self.dir, "shim"), os.path.join(self.dir, "real")
        for directory, body in ((shim, "#!/bin/sh\n# Generated by zellij-ai-resume install.sh 1.0\n"),
                                (real, "#!/bin/sh\nexit 0\n")):
            os.mkdir(directory)
            with open(os.path.join(directory, "claude"), "w") as handle:
                handle.write(body)
            os.chmod(os.path.join(directory, "claude"), 0o755)
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(["shim", shim, real])}):
            self.assertEqual(zjr.find_binary(CLAUDE), os.path.join(real, "claude"))

    def test_gc_rejects_odd_ages(self):
        for days in ("nan", "inf", "-1", "x"):
            with mock.patch("sys.stderr"):
                self.assertEqual(zjr.admin(self.dir, ["gc", "--days", days]), 2, days)

    def test_doctor_fails_without_a_zellij_config_directory(self):
        import contextlib, io
        empty = os.path.join(self.dir, "home")
        os.mkdir(empty)
        out = io.StringIO()
        env = {"HOME": empty, "XDG_CONFIG_HOME": os.path.join(empty, ".config"), "ZELLIJ_CONFIG_DIR": ""}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            code = zjr.admin(self.dir, ["doctor"])
        if os.path.isdir("/etc/zellij"):
            self.skipTest("/etc/zellij exists here")
        self.assertEqual(code, 1)
        self.assertIn("Zellij then resurrects nothing", out.getvalue())

    def test_codex_picker_is_offered_again_when_nothing_was_seen(self):
        plan = zjr.make_plan(CODEX, self.state("codex", ["resume", "-m", "o3"], launched=True), U2, lambda s: False)
        self.assertEqual((plan.mode, plan.args), ("fresh", ["resume", "-m", "o3"]))


class Reload(TempDirTest):
    def running(self, *entries):
        """entries: (token, pane_id, zellij_session[, owned]) of panes whose CLI runs."""
        return [{"pid": 1000 + index, "token": entry[0], "tool": "claude", "owned": entry[3] if len(entry) > 3 else True,
                 "pane_id": entry[1], "zellij_session": entry[2], "runs": 1}
                for index, entry in enumerate(entries)]

    def target(self, running, panes, env, name=None, ancestors=(), keybinding=False):
        store = zjr.Store()
        with mock.patch.object(zjr, "running_panes", return_value=running), \
                mock.patch.object(zjr, "zellij_panes", return_value=panes), \
                mock.patch.object(zjr, "ancestors", return_value=list(ancestors)), \
                mock.patch.dict(os.environ, env, clear=False):
            for key in ("ZJR_PANE", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
                if key not in env:
                    os.environ.pop(key, None)
            result = zjr.reload_target(store, name, keybinding)
        return result if isinstance(result, str) else (result["token"], result["pid"])

    def layout(self, *panes):
        """panes: (id, tab, focused, floating)."""
        return {str(i): {"id": i, "tab_id": tab, "is_focused": focused, "is_floating": floating, "is_plugin": False}
                for i, tab, focused, floating in panes}

    def test_inside_the_cli_reloads_the_pane_it_runs_in(self):
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        running = self.running((a, "1", "s"), (b, "2", "s"))
        self.assertEqual(self.target(running, None, {"ZJR_PANE": b}, ancestors=[77, 1001]), (b, 1001))
        self.assertIn("not running", self.target(running, None, {"ZJR_PANE": b}, ancestors=[77]))

    def test_a_duplicate_pane_reloads_itself_not_the_original(self):
        a = str(uuid.uuid4())
        running = self.running((a, "1", "s", True), (a, "2", "s", False))  # pane 2 runs the same saved command
        self.assertEqual(self.target(running, None, {"ZJR_PANE": a}, ancestors=[1001]), (a, 1001))
        panes = self.layout((1, 0, False, False), (2, 0, True, False), (9, 0, True, True))
        self.assertEqual(self.target(running, panes, {"ZELLIJ_SESSION_NAME": "s", "ZELLIJ_PANE_ID": "9"},
                                     keybinding=True), (a, 1001))
        self.assertEqual(self.target(running, None, {}, name=a[:8]), (a, 1000))  # by name: the original

    def test_keybinding_ignores_the_cli_environment(self):
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        running = self.running((a, "1", "s"), (b, "2", "s"))
        panes = self.layout((1, 0, True, False), (2, 0, False, False), (9, 0, True, True))
        env = {"ZJR_PANE": b, "ZELLIJ_SESSION_NAME": "s", "ZELLIJ_PANE_ID": "9"}
        self.assertEqual(self.target(running, panes, env, ancestors=[1001], keybinding=True)[0], a)

    def test_keybinding_targets_the_pane_under_the_floating_pane(self):
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        running = self.running((a, "1", "s"), (b, "2", "s"))
        panes = self.layout((1, 0, False, False), (2, 0, True, False), (9, 0, True, True))  # 9: the keybinding's pane
        self.assertEqual(self.target(running, panes, {"ZELLIJ_SESSION_NAME": "s", "ZELLIJ_PANE_ID": "9"})[0], b)

    def test_single_pane_in_the_tab_and_ambiguity(self):
        a, b, c = (str(uuid.uuid4()) for _ in range(3))
        env = {"ZELLIJ_SESSION_NAME": "s", "ZELLIJ_PANE_ID": "9"}
        one = self.running((a, "1", "s"), (b, "2", "s"))
        panes = self.layout((1, 0, False, False), (2, 1, True, False), (5, 0, True, False), (9, 0, True, True))
        self.assertEqual(self.target(one, panes, env)[0], a)  # the only one in this tab, though not focused
        two = self.running((a, "1", "s"), (c, "3", "s"))
        panes = self.layout((1, 0, False, False), (3, 0, False, False), (5, 0, True, False), (9, 0, True, True))
        self.assertIn("several", self.target(two, panes, env))
        other_tab = self.running((b, "2", "s"))
        self.assertIn("in this tab", self.target(other_tab, self.layout((2, 1, True, False), (9, 0, True, True)), env))

    def test_other_zellij_sessions_are_never_targeted(self):
        a = str(uuid.uuid4())
        running = self.running((a, "1", "other"))
        env = {"ZELLIJ_SESSION_NAME": "s", "ZELLIJ_PANE_ID": "9"}
        self.assertIn("no zclaude/zcodex pane", self.target(running, self.layout((1, 0, True, False)), env))

    def test_by_name(self):
        a, b = "abcdef01-2345-4678-9abc-def012345678", str(uuid.uuid4())
        running = self.running((a, "1", "s"), (b, "2", "s"))
        self.assertEqual(self.target(running, None, {}, name="abcdef")[0], a)
        self.assertIn("0 running panes", self.target(running, None, {}, name="ffff"))

    def test_entries_of_dead_cores_are_removed(self):
        store = zjr.Store()
        dead = subprocess.Popen(["true"])
        dead.wait()
        stale = os.path.join(store.running, f"{dead.pid}.json")
        zjr.write_json(stale, {"pid": dead.pid, "token": str(uuid.uuid4()), "tool": "claude"})
        mine = os.path.join(store.running, f"{os.getpid()}.json")  # alive, but not a zjr core
        zjr.write_json(mine, {"pid": os.getpid(), "token": str(uuid.uuid4()), "tool": "claude"})
        self.assertEqual(zjr.running_panes(store), [])
        self.assertFalse(os.path.exists(stale) or os.path.exists(mine))

    def test_core_pid_is_checked_against_the_process(self):
        token = str(uuid.uuid4())
        self.assertFalse(zjr._is_core(os.getpid(), token))  # a live pid that is not this pane's core

    def test_new_state_fields_are_validated(self):
        good = self.state("claude", [])
        for field, value in (("core_pid", True), ("core_pid", -1), ("runs", "2"), ("pane_id", "terminal_1"),
                             ("pane_id", "1; rm")):
            self.assertIsNone(zjr.validate_state(dict(good, **{field: value}), "claude"), (field, value))
        self.assertIsNotNone(zjr.validate_state(dict(good, core_pid=123, runs=4, pane_id="12"), "claude"))

    def test_own_conversation_lock_is_not_someone_elses(self):
        store = zjr.Store()
        pane = zjr.Pane(store, str(uuid.uuid4()), self.state("claude", []), CLAUDE, True)
        self.assertTrue(pane.lock_session(U1))
        self.assertFalse(pane.claimed_elsewhere(U1))
        other = zjr.Pane(store, str(uuid.uuid4()), self.state("claude", []), CLAUDE, True)
        self.assertTrue(other.claimed_elsewhere(U1))
        pane.release()


class KeybindingConfig(TempDirTest):
    def config(self, text):
        path = os.path.join(self.dir, "config.kdl")
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def run_admin(self, *argv, config=None):
        import contextlib, io
        out = io.StringIO()
        env = {"ZELLIJ_CONFIG_FILE": config or "", "ZELLIJ_CONFIG_DIR": self.dir, "HOME": self.dir}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            code = zjr.admin("/opt/x/bin", list(argv))
        return code, out.getvalue()

    BIND = 'bind "Alt r" { Run "/opt/x/bin/zjr" "reload" "--from-keybinding" { floating true; close_on_exit true; }; }'

    def test_snippet_matches_the_config(self):
        code, text = self.run_admin("keybinding", config=self.config('keybinds clear-defaults=true {\n}\n'))
        self.assertEqual(code, 0)
        self.assertIn("Zellij ignores a", text)
        self.assertNotIn("keybinds {", text)
        code, text = self.run_admin("keybinding", "Ctrl", "Alt", "r", config=self.config("// keybinds {\n"))
        self.assertIn('keybinds {\n    shared_except "locked" {\n        bind "Ctrl Alt r"', text)
        self.assertIn('Run "/opt/x/bin/zjr" "reload" "--from-keybinding"', text)
        self.assertEqual(self.run_admin("keybinding", 'r" { Quit; } bind "x')[0], 2)

    def test_comments_are_comments(self):
        text = (
            '/* keybinds { } */\n'
            'keybinds {  // the real one\n'
            '    shared_except "locked" {\n'
            '        /-bind "Alt r" { Run "/opt/x/bin/zjr" "reload"; }\n'
            '        bind "Alt x" { Run "/a//b" "https://x/*y*/"; }\n'
            '        /* nested /* comment */ still */\n'
            '    }\n'
            '}\n')
        stripped = zjr.strip_kdl_comments(text)
        self.assertEqual(len(zjr._keybinds_blocks(stripped)), 1)
        self.assertIsNone(zjr._reload_binding(stripped))
        self.assertIn('Run "/a//b" "https://x/*y*/"', stripped)
        self.assertNotIn("still", stripped)
        ignored = self.config("/* keybinds {} */\nkeybinds {\n    shared_except \"locked\" {\n        " + self.BIND
                              + "\n    }\n}\n")
        self.assertIn("Alt r reloads the focused pane", self.run_admin("doctor", config=ignored)[1])

    def test_doctor_spots_a_binding_zellij_ignores(self):
        ignored = self.config("keybinds {\n    normal {\n    }\n}\nkeybinds {\n    shared_except \"locked\" {\n        "
                              + self.BIND + "\n    }\n}\n")
        self.assertIn("second `keybinds` block", self.run_admin("doctor", config=ignored)[1])
        honoured = self.config("keybinds {\n    shared_except \"locked\" {\n        " + self.BIND + "\n    }\n}\n")
        self.assertIn("Alt r reloads the focused pane", self.run_admin("doctor", config=honoured)[1])


if __name__ == "__main__":
    unittest.main()
