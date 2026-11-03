"""End-to-end tests of the installed launchers with fake CLIs: real
processes, a pseudo-terminal per pane, crashes simulated with SIGKILL, and
the saved command re-run exactly as Zellij would (argv split on spaces)."""

import os
import shutil
import signal
import subprocess
import time
import unittest
import uuid

from helpers import Sandbox, wait_for


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def saved_command(self, pane):
        """The pane's command line once it is tracked: /bin/sh <launcher> --zj-pane <token>."""
        line = wait_for(lambda: "--zj-pane" in pane.command_line() and pane.command_line(), what="tracked pane")
        words = line.split()
        self.assertEqual(words[:2], ["/bin/sh", self.box.launcher(self.tool)])
        self.assertEqual(words[2], "--zj-pane")
        self.assertEqual(len(words), 4)
        return words

    def state(self, token):
        return self.box.states()[token]


class ClaudeLauncher(LauncherTest):
    tool = "zclaude"

    def test_full_cycle(self):
        first = self.box.pane([self.box.launcher("zclaude"), "--dangerously-skip-permissions", "--model", "opus",
                               "do the thing"], FAKE_CLEAR_AFTER=1)
        command = self.saved_command(first)
        token = command[3]
        start = self.box.wait_event(event="start")
        self.assertEqual(start["argv"], ["--session-id", start["session"], "--dangerously-skip-permissions",
                                         "--model", "opus", "do the thing"])
        cleared = self.box.wait_event(event="clear")["session"]
        wait_for(lambda: self.state(token)["session_id"] == cleared, what="state to follow /clear")
        first.crash()

        second = self.box.pane(command)
        resumed = self.box.wait_event(event="start", skip=1)
        self.assertEqual(resumed["argv"], ["--resume", cleared, "--dangerously-skip-permissions", "--model", "opus"])
        self.assertEqual(resumed["cwd"], self.box.work)
        wait_for(lambda: "resuming conversation" in second.text(), what="resume notice")
        self.assertIn("not repeated: 'do the thing'", second.text())
        self.assertEqual(second.command_line().split(), command)  # the same command again after resurrection

        duplicate = self.box.pane(command)
        forked = self.box.wait_event(event="start", skip=2)
        self.assertEqual(forked["argv"][:3], ["--resume", cleared, "--fork-session"])
        wait_for(lambda: "already running in another pane" in duplicate.text(), what="duplicate notice")

    def test_passthrough_outside_zellij_and_for_non_interactive_use(self):
        for args, zellij in ((["--model", "x"], False), (["--version"], True), (["-p", "hi"], True),
                             (["doctor"], True)):
            pane = self.box.pane([self.box.launcher("zclaude"), *args], zellij=zellij, FAKE_EXIT_AFTER=0.2)
            pane.wait()
        argvs = [event["argv"] for event in self.box.events() if "argv" in event]
        self.assertEqual(argvs, [["--model", "x"], ["--version"], ["-p", "hi"], ["doctor"]])
        self.assertEqual(self.box.states(), {})

    def test_exit_status_is_passed_on(self):
        pane = self.box.pane([self.box.launcher("zclaude")], FAKE_EXIT_AFTER=0.5, FAKE_EXIT_CODE=3)
        self.assertEqual(pane.wait(), 3)

    def leftovers(self, token):
        lines = subprocess.run(["ps", "-A", "-o", "args="], capture_output=True, text=True).stdout.splitlines()
        return [line for line in lines if token in line]

    def test_closing_the_pane_ends_everything(self):
        pane = self.box.pane([self.box.launcher("zclaude")])
        token = self.saved_command(pane)[3]
        self.box.wait_event(event="start")
        pane.signal_group(signal.SIGHUP)  # what the terminal going away does
        self.assertEqual(pane.wait(), -signal.SIGHUP)
        self.assertEqual(self.box.wait_event(event="signal")["signum"], signal.SIGHUP)
        wait_for(lambda: not self.leftovers(token), what="all pane processes to exit")
        self.assertEqual(self.state(token)["last_exit"], 128 + signal.SIGHUP)

    def test_cli_follows_if_the_launcher_is_killed_alone(self):
        pane = self.box.pane([self.box.launcher("zclaude")])
        self.saved_command(pane)
        self.box.wait_event(event="start")
        os.kill(pane.pid, signal.SIGKILL)
        self.assertEqual(self.box.wait_event(event="signal", timeout=5)["signum"], signal.SIGHUP)

    def test_failed_resume_falls_back_to_a_new_conversation(self):
        pane = self.box.pane([self.box.launcher("zclaude"), "--verbose"])
        command = self.saved_command(pane)
        self.box.wait_event(event="start")
        pane.crash()
        failing = self.box.pane(command, FAKE_RESUME_FAIL=1)
        self.assertEqual(failing.wait(), 1)
        self.assertIn("press Enter to start a new conversation", failing.text())
        retry = self.box.pane(command)
        fresh = self.box.wait_event(event="start", skip=2)
        self.assertEqual(fresh["argv"], ["--session-id", fresh["session"], "--verbose"])
        wait_for(lambda: "failed last time" in retry.text(), what="fallback notice")

    def test_closing_a_pane_right_after_resuming_is_not_a_failed_resume(self):
        pane = self.box.pane([self.box.launcher("zclaude")])
        command = self.saved_command(pane)
        token = command[3]
        self.box.wait_event(event="start")
        pane.crash()
        resumed = self.box.pane(command)
        self.box.wait_event(event="start", skip=1)
        resumed.signal_group(signal.SIGHUP)
        resumed.wait()
        self.assertIsNone(self.state(token)["failed_resume"])

    def test_a_claude_on_path_that_is_zclaude_does_not_loop(self):
        shim = os.path.join(self.box.dir, "shim")
        os.mkdir(shim)
        os.symlink(self.box.launcher("zclaude"), os.path.join(shim, "claude"))
        wrapper = os.path.join(shim, "wrapped")
        os.mkdir(wrapper)
        with open(os.path.join(wrapper, "claude"), "w") as handle:  # a wrapper script calling zclaude
            handle.write(f'#!/bin/sh\nexec {self.box.launcher("zclaude")} "$@"\n')
        os.chmod(os.path.join(wrapper, "claude"), 0o755)
        linked = self.box.pane([os.path.join(shim, "claude"), "--verbose"], PATH=shim + os.pathsep + self.box.env["PATH"])
        self.assertEqual(self.box.wait_event(event="start")["argv"][2:], ["--verbose"])  # the real (fake) CLI
        self.assertEqual(len(self.box.states()), 1)
        looping = self.box.pane([self.box.launcher("zclaude")], PATH=wrapper + os.pathsep + "/usr/bin:/bin")
        self.assertNotEqual(looping.wait(timeout=20), 0)
        self.assertIn("leads back to zclaude", looping.text())
        linked.close()

    def test_missing_core_still_starts_the_cli(self):
        os.unlink(os.path.join(self.box.prefix, "share", "zellij-ai-resume", "zjr.py"))
        pane = self.box.pane([self.box.launcher("zclaude"), "--verbose"], FAKE_EXIT_AFTER=0.3)
        pane.wait()
        self.assertEqual(self.box.events(event="start")[0]["argv"], ["--verbose"])
        self.assertIn("starting claude without recovery", pane.text())

    def test_expired_conversation_starts_fresh(self):
        pane = self.box.pane([self.box.launcher("zclaude")])
        command = self.saved_command(pane)
        session = self.box.wait_event(event="start")["session"]
        pane.crash()
        for root, _, files in os.walk(os.path.join(self.box.env["CLAUDE_CONFIG_DIR"], "projects")):
            for name in files:
                os.unlink(os.path.join(root, name))
        again = self.box.pane(command)
        fresh = self.box.wait_event(event="start", skip=1)
        self.assertEqual(fresh["argv"][0], "--session-id")
        self.assertNotEqual(fresh["session"], session)
        wait_for(lambda: "no longer on disk" in again.text(), what="expiry notice")

    def test_unknown_or_invalid_tokens(self):
        unknown = self.box.pane(["/bin/sh", self.box.launcher("zclaude"), "--zj-pane", str(uuid.uuid4())])
        self.assertEqual(self.box.wait_event(event="start")["argv"][0], "--session-id")
        wait_for(lambda: "no saved state for this pane" in unknown.text(), what="notice")
        bad = self.box.pane(["/bin/sh", self.box.launcher("zclaude"), "--zj-pane", "../../etc/passwd"])
        self.assertEqual(self.box.wait_event(event="start", skip=1)["argv"], [])
        wait_for(lambda: "invalid pane token" in bad.text(), what="notice")
        self.assertFalse(os.path.exists(os.path.join(self.box.dir, "etc")))

    def test_state_directory_owned_elsewhere_fails_open(self):
        root = os.path.join(self.box.env["XDG_STATE_HOME"], "zellij-ai-resume")
        shutil.rmtree(root, ignore_errors=True)  # created by the installer's `zjr doctor`
        os.makedirs(os.path.dirname(root), exist_ok=True)
        os.symlink("/tmp", root)
        pane = self.box.pane([self.box.launcher("zclaude"), "--verbose"], FAKE_EXIT_AFTER=0.3)
        pane.wait()
        self.assertEqual(self.box.events(event="start")[0]["argv"], ["--verbose"])
        self.assertIn("starting without recovery", pane.text())


class Reload(LauncherTest):
    tool = "zclaude"

    def zjr(self, *args, **extra):
        env = dict(self.box.env, **{key: str(value) for key, value in extra.items()})
        return subprocess.run([self.box.launcher("zjr"), *args], env=env, capture_output=True, text=True, timeout=60)

    def test_reload_restarts_the_cli_on_its_current_conversation(self):
        pane = self.box.pane([self.box.launcher("zclaude"), "--verbose", "a prompt"], FAKE_CLEAR_AFTER=1)
        command = self.saved_command(pane)
        token = command[3]
        first = self.box.wait_event(event="start")
        cleared = self.box.wait_event(event="clear")["session"]
        wait_for(lambda: self.state(token)["session_id"] == cleared, what="state to follow /clear")
        done = self.zjr("reload", token[:8])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"reloaded claude pane {token[:8]} on conversation {cleared}", done.stdout)
        self.assertEqual(self.box.wait_event(event="signal")["signum"], signal.SIGTERM)
        again = self.box.wait_event(event="start", skip=1)
        self.assertEqual(again["argv"], ["--resume", cleared, "--verbose"])
        self.assertNotEqual(again["pid"], first["pid"])
        self.assertEqual(pane.command_line().split(), command)  # same pane process, same saved command
        self.assertEqual(self.state(token)["runs"], 2)
        self.assertIn("\x1b[H\x1b[2J", pane.text())  # the screen was reset between the two
        self.assertIsNone(self.state(token)["failed_resume"])

    def test_reload_from_inside_the_cli(self):
        output = os.path.join(self.box.dir, "ran.txt")
        pane = self.box.pane([self.box.launcher("zclaude"), "--verbose"], FAKE_RUN_AFTER=1, FAKE_RUN="zjr reload",
                             FAKE_RUN_OUTPUT=output)
        token = self.saved_command(pane)[3]
        first = self.box.wait_event(event="start")
        again = self.box.wait_event(event="start", skip=1)
        self.assertEqual(again["argv"], ["--resume", first["session"], "--verbose"])
        wait_for(lambda: open(output).read().strip(), what="zjr reload output")
        self.assertEqual(open(output).read().strip(), f"reloading claude pane {token[:8]}")

    def test_a_cli_that_ignores_sigterm_is_killed_after_the_grace_period(self):
        pane = self.box.pane([self.box.launcher("zclaude")], FAKE_IGNORE_TERM=1)
        token = self.saved_command(pane)[3]
        first = self.box.wait_event(event="start")
        began = time.monotonic()
        self.assertEqual(self.zjr("reload", token).returncode, 0)
        self.box.wait_event(event="ignored")
        again = self.box.wait_event(event="start", skip=1, timeout=15)
        self.assertGreaterEqual(time.monotonic() - began, 4.5)
        self.assertEqual(again["argv"][:2], ["--resume", first["session"]])

    def test_closing_the_pane_during_a_reload_does_not_relaunch(self):
        pane = self.box.pane([self.box.launcher("zclaude")], FAKE_IGNORE_TERM=1)
        token = self.saved_command(pane)[3]
        self.box.wait_event(event="start")
        reload = subprocess.Popen([self.box.launcher("zjr"), "reload", token], env=self.box.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.box.wait_event(event="ignored")
        pane.signal_group(signal.SIGHUP)
        pane.wait()
        reload.wait(timeout=30)
        time.sleep(1)
        self.assertEqual(len(self.box.events(event="start")), 1)
        self.assertIsNone(self.state(token)["core_pid"])

    def test_reload_requests_in_quick_succession_are_safe(self):
        pane = self.box.pane([self.box.launcher("zclaude")])
        token = self.saved_command(pane)[3]
        self.box.wait_event(event="start")
        core = self.state(token)["core_pid"]
        for _ in range(5):
            os.kill(core, signal.SIGUSR1)
            time.sleep(0.05)
        wait_for(lambda: len(self.box.events(event="start")) >= 2, what="a restart")
        time.sleep(2)
        self.assertIsNone(pane.proc.poll())  # the pane is still running
        self.assertEqual(self.state(token)["core_pid"], core)
        self.assertEqual(self.zjr("reload", token).returncode, 0)

    def test_nothing_to_reload(self):
        done = self.zjr("reload")
        self.assertEqual(done.returncode, 1)
        self.assertIn("not inside Zellij", done.stderr)

    def test_codex_reload(self):
        self.tool = "zcodex"
        pane = self.box.pane([self.box.launcher("zcodex"), "-m", "o3"])
        token = self.saved_command(pane)[3]
        session = self.box.wait_event(event="rollout")["session"]
        wait_for(lambda: self.state(token)["session_id"] == session, timeout=8, what="codex session")
        self.assertEqual(self.zjr("reload", token).returncode, 0)
        again = self.box.wait_event(event="start", skip=1)
        self.assertEqual(again["argv"], ["resume", "-m", "o3", session])


class CodexLauncher(LauncherTest):
    tool = "zcodex"

    def test_full_cycle(self):
        sub = os.path.join(self.box.work, "sub")
        os.mkdir(sub)
        first = self.box.pane([self.box.launcher("zcodex"), "-m", "o3", "-C", "sub", "write the tests"],
                              FAKE_ROLLOUT_DELAY=1)
        command = self.saved_command(first)
        token = command[3]
        session = self.box.wait_event(event="rollout")["session"]
        wait_for(lambda: self.state(token)["session_id"] == session, timeout=8, what="codex session discovery")
        self.assertEqual(self.state(token)["session_cwd"], os.path.realpath(sub))
        first.crash()

        second = self.box.pane(command)
        resumed = self.box.wait_event(event="start", skip=1)
        self.assertEqual(resumed["argv"], ["resume", "-m", "o3", "-C", "sub", session])
        self.assertEqual(resumed["cwd"], self.box.work)
        wait_for(lambda: "resuming conversation" in second.text(), what="resume notice")

    def test_new_conversation_inside_the_pane_is_followed(self):
        first = self.box.pane([self.box.launcher("zcodex")], FAKE_NEW_AFTER=1)
        token = self.saved_command(first)[3]
        newer = self.box.wait_event(event="new")["session"]
        wait_for(lambda: self.state(token)["session_id"] == newer, timeout=8, what="switch to /new")

    def test_shared_daemon_guess_and_no_takeover(self):
        first = self.box.pane([self.box.launcher("zcodex")], FAKE_DAEMON=1)
        token = self.saved_command(first)[3]
        mine = self.box.wait_event(event="rollout")["session"]
        wait_for(lambda: self.state(token)["session_id"] == mine, timeout=8, what="guessed session")
        other = self.box.pane(["codex"], FAKE_DAEMON=1)  # a plain codex in the same directory
        theirs = self.box.wait_event(event="rollout", skip=1)["session"]
        self.assertNotEqual(theirs, mine)
        time.sleep(5)
        self.assertEqual(self.state(token)["session_id"], mine)
        other.close()

    def test_subcommands_pass_through(self):
        pane = self.box.pane([self.box.launcher("zcodex"), "exec", "ls"], FAKE_EXIT_AFTER=0.2)
        pane.wait()
        self.assertEqual(self.box.events(event="start")[0]["argv"], ["exec", "ls"])
        self.assertEqual(self.box.states(), {})


@unittest.skipUnless(os.access("/bin/zsh", os.X_OK), "needs /bin/zsh")
class JobControl(unittest.TestCase):
    """Ctrl+C and Ctrl+Z under a real interactive shell with job control."""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def shell(self, **extra):
        shell = self.box.pane(["/bin/zsh", "-f", "-i"], **extra)
        wait_for(lambda: shell.text().strip(), what="zsh prompt")
        os.write(shell.master, f"{self.box.launcher('zclaude')} --verbose\r".encode())
        self.box.wait_event(event="start")
        return shell

    def launcher_state(self, shell):
        for line in subprocess.run(["ps", "-A", "-o", "ppid=,stat=,args="], capture_output=True, text=True).stdout.splitlines():
            words = line.split()
            if words[0] == str(shell.pid) and "--zj-pane" in line:
                return words[1]
        return None

    def suspend_and_continue(self, shell):
        wait_for(lambda: "suspended" in shell.text(), what="zsh to report the job suspended")
        self.assertTrue(self.launcher_state(shell).startswith("T"))
        os.write(shell.master, b"fg\r")
        self.box.wait_event(event="continued")
        time.sleep(1.5)  # a stale stop report must not suspend the job again
        self.assertFalse(self.launcher_state(shell).startswith("T"))
        self.assertEqual(len(self.box.events(event="start")), 1)

    def test_cli_suspending_itself_suspends_the_job(self):
        shell = self.shell(FAKE_SUSPEND_AFTER=1, FAKE_EXIT_AFTER=8)
        self.box.wait_event(event="suspend")
        self.suspend_and_continue(shell)
        self.box.wait_event(event="exit", timeout=15)

    def test_ctrl_z_from_the_terminal(self):
        shell = self.shell(FAKE_EXIT_AFTER=8)
        os.write(shell.master, b"\x1a")
        self.suspend_and_continue(shell)
        self.box.wait_event(event="exit", timeout=15)

    def test_ctrl_c_reaches_the_cli_and_nothing_else_breaks(self):
        shell = self.shell()
        token = next(iter(self.box.states()))
        os.write(shell.master, b"\x03")
        wait_for(lambda: self.box.states()[token]["last_exit"] == 128 + signal.SIGINT, what="CLI to exit on Ctrl+C")
        wait_for(lambda: self.launcher_state(shell) is None, what="launcher to exit")
        self.assertNotIn("zjr.py", shell.text())  # the fake CLI may print its own KeyboardInterrupt


class Installer(unittest.TestCase):
    def test_refuses_to_overwrite_foreign_files_and_uninstalls_cleanly(self):
        box = Sandbox()
        self.addCleanup(box.close)
        install = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
                   for name in ("install.sh", "uninstall.sh")]
        self.assertIn("No shell, Zellij, Claude Code or Codex configuration was changed", box.install_output)
        os.unlink(box.launcher("zjr"))
        with open(box.launcher("zjr"), "w") as handle:
            handle.write("#!/bin/sh\necho mine\n")
        result = subprocess.run(["/bin/sh", install[0], "--prefix", box.prefix], env=box.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("was not installed by zellij-ai-resume", result.stderr)
        bad = subprocess.run(["/bin/sh", install[0], "--prefix", box.prefix + "/with space"], env=box.env,
                             capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        result = subprocess.run(["/bin/sh", install[1], "--prefix", box.prefix], env=box.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(box.launcher("zclaude")))
        self.assertTrue(os.path.exists(box.launcher("zjr")))  # not ours, left alone
        self.assertFalse(os.path.exists(os.path.join(box.prefix, "share", "zellij-ai-resume")))


if __name__ == "__main__":
    unittest.main()
