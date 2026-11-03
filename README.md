# zellij-ai-resume

Per-pane conversation recovery for **Claude Code** and **Codex** in **Zellij**,
on macOS and Linux, without touching your dotfiles.

When a Zellij session dies (crash, reboot, `kill -9`) Zellij can resurrect it:
every pane gets its last command back, waiting for Enter. Start the CLIs with
`zclaude` / `zcodex` instead of `claude` / `codex`, and pressing Enter in a
resurrected pane resumes the conversation that pane was on, including after a
`/clear` or `/resume`, with the options you started it with.

```sh
./install.sh            # ~/.local/bin/{zclaude,zcodex,zjr}, nothing else
zclaude --dangerously-skip-permissions     # any claude arguments
zcodex -m o3                               # any codex arguments
# ...Zellij dies...
zellij attach my-session                   # then Enter in each pane
```

Outside Zellij, and for anything non-interactive (`-p`, `--version`, `mcp`,
`codex exec`, ...), `zclaude` and `zcodex` are exactly `claude` and `codex`.

## What it changes on your system

| Path | What |
|---|---|
| `~/.local/bin/zclaude`, `zcodex`, `zjr` | small `/bin/sh` launchers (generated) |
| `~/.local/share/zellij-ai-resume/zjr.py` | the core: one Python file, standard library only |
| `~/.local/state/zellij-ai-resume/` | one 0600 JSON file per pane, lock files, `events.log` (created on first use, 0700) |

Nothing else: no shell rc files, no Zellij, Claude Code or Codex configuration,
no hooks, no background services. `./uninstall.sh` removes it
(`--purge` also deletes the saved state). `--prefix DIR` installs elsewhere.

Requirements: Zellij (tested with 0.45.1), Python 3.8+, `/bin/sh` (bash or
dash). Tested on macOS 26 (Apple silicon) and Arch Linux ARM.

Zellij resurrects sessions only if its configuration directory exists
(`~/.config/zellij`, or `ZELLIJ_CONFIG_DIR`); without it every attach starts
an empty session. `mkdir -p ~/.config/zellij` is enough, and `zjr doctor`
checks for it.

### Which Python

The installer records one interpreter in the launchers: `--python PATH` if
given, else `/usr/bin/python3` (on macOS only when the Command Line Tools are
installed, since without them that stub opens an installer dialog), else the
`python3` on PATH, with a pyenv/mise/asdf shim resolved to the interpreter it
points at. It warns when that is a virtualenv or a single versioned install
(uv, mise, Homebrew Cellar), which go away on upgrade. Any CPython 3.8+ works;
the core is one file using only the standard library.

The interpreter is never part of what Zellij saves, so upgrading or removing
it cannot break resurrection: if the recorded one is gone, the launcher uses
`/usr/bin/python3` or `python3` on PATH, and with no usable Python at all it
starts the plain CLI with a notice.

## How it works

Zellij saves, for each pane, the command running in its foreground, and on
resurrection offers to run it again. For a `zclaude` pane that command is

    /bin/sh ~/.local/bin/zclaude --zj-pane <token>

```
zsh (your pane's shell)
└─ /bin/sh zclaude --zj-pane <token>    ← what Zellij saves; stays alive
   └─ python3 -I zjr.py run claude ...  ← tracks the conversation
      └─ claude --session-id <id> ...   ← in the foreground of your terminal
```

- The token names a state file holding the arguments you gave and the
  conversation the pane is on now. Your arguments are not in the saved command,
  so quoting and spaces survive (Zellij splits saved commands on whitespace).
- The saved command starts with `/bin/sh` because a Python interpreter's path
  carries its version and moves on upgrade.
- A resurrected pane saves the same command again, so it survives any number
  of crashes. No `post_command_discovery_hook` is needed.
- The CLI talks to your terminal directly: no pseudo-terminal proxy and no
  key interception. Ctrl+C, Ctrl+Z / `fg` and resizing behave as with the
  bare CLI.

**Claude Code** gets `--session-id <uuid>` on first launch, so the conversation
is known from the start. Claude Code keeps `~/.claude/sessions/<pid>.json` up to
date with the conversation each process is on; the launcher reads it once a
second, so `/clear`, `/resume` and `--continue` are followed exactly.

**Codex** chooses its own id and keeps no such registry. The launcher looks
for the rollout file (`~/.codex/sessions/YYYY/MM/DD/rollout-*-<id>.jsonl`)
that the pane's own processes hold open, which is exact and follows `/new`
and `/resume` inside the pane. When Codex's shared daemon writes the file
instead, nothing in the pane holds it, and the launcher falls back to a
guess: the first interactive rollout created after the CLI started, in its
working directory, that no other `zcodex` pane has claimed. A guess is made
once, never replaces a conversation already found, and is not made for
`--worktree` or the `codex resume` picker. Two daemon-backed Codex sessions
started in one directory within seconds of each other can still be swapped.

`CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `XDG_STATE_HOME` are honoured.

## What a resumed pane runs

| Situation | Command |
|---|---|
| first launch | `claude --session-id <new> <your arguments>` / `codex <your arguments>` |
| resurrected, conversation on disk | `claude --resume <id> <your options>` / `codex resume <your options> <id>` |
| the conversation is open in another pane or process | the same with `--fork-session` / `codex fork`: a copy, never two writers |
| conversation gone (Claude Code deletes transcripts after 30 days), never written, or the resume failed at once | a new conversation with your options, and a note saying why |

"Your options" are the options known to be settings (`--model`,
`--dangerously-skip-permissions`, `--add-dir`, `-c key=value`, ...). Positional
prompts, images, `--name`, `--worktree`, `--resume`/`--continue` and options this
version does not recognise are not repeated, so a crash never re-sends a prompt.
The pane prints which words were left out.

## Security

- No network access, no `shell=True`, no `eval`; the CLIs are run with argument
  vectors. The core runs as `python3 -I`, ignoring `PYTHONPATH` and friends.
- State lives in a 0700 directory that must be a real directory owned by you
  (symlinks are refused); files are 0600, written atomically, opened with
  `O_NOFOLLOW`, size-limited and validated field by field. Pane tokens must be
  uuid4 strings and conversation ids UUIDs before they reach a path or a
  command line.
- Resurrection keeps Zellij's own gate: nothing runs until you press Enter.
- `events.log` records ids, directories and exit codes, never prompts.
- If anything in the launcher's own machinery fails, it says so and runs the
  plain CLI instead of standing in your way. A `claude` on PATH that is really
  `zclaude` (a symlink, or a wrapper) is skipped or stopped, never looped.
- A pane is never resumed into a conversation that another pane, or any live
  Claude Code process, has open: it continues in a copy (`--fork-session`,
  `codex fork`) instead, so a transcript never has two writers.

## zjr

```
zjr list              panes that can be restored, and the conversation of each
zjr doctor            check the installation, PATH, Zellij config, CLIs
zjr gc [--days N]     forget panes untouched for N days (default 30)
zjr forget PANE       forget one pane
```

## Limitations

- Only `zclaude` / `zcodex` started directly from the pane's shell are tracked:
  in a nested shell or over ssh, Zellij saves the outer program instead.
- Zellij must keep `session_serialization` on (the default) and have a
  configuration directory; `zjr doctor` checks both.
- A Zellij server started from inside Claude Code passes `CLAUDECODE` to every
  pane; Claude Code then runs as a child session and does not report
  conversation switches. `zjr doctor` warns about it.
- Uninstalling while saved sessions still refer to `zclaude` leaves those panes
  unable to start.

## Tests

```sh
cd tests
python3 -m unittest test_unit test_launcher          # about a minute
ZJR_E2E=1 python3 -m unittest test_zellij            # real Zellij: crash, resurrect, Enter, twice
tests/run-linux.sh                                   # all of it on Arch Linux ARM, bash and dash as /bin/sh
```

`run-linux.sh` uses Apple's `container` (`brew install container`); the host
downloads the packages, so it works where the VM has no network of its own.

The tests use fake `claude` / `codex` executables that behave like the real
ones on disk (registry, transcripts, rollout files), real processes on
pseudo-terminals, SIGKILL for crashes, and a real interactive zsh for job
control. The Zellij test runs servers with a throwaway HOME, configuration
and socket directory, so it never touches your own sessions.

## Compared with [zellij-claude-enhance](https://github.com/Shengfeng233/zellij-claude-enhance)

This is a rewrite rather than a port. It keeps the idea (a token in the
pane's saved command) and drops what needed dotfile edits, ran on every
discovery tick, or could not work on macOS: the rc-file colour fix, the
command-discovery hook, the Linux-only Zellij upgrader and dispatcher,
the Ctrl+Y PTY proxies, and the prebuilt status-bar plugin.

## License

MIT, see [LICENSE](LICENSE).
