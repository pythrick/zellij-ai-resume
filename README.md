# zellij-ai-resume

A Zellij plugin that brings **Claude Code** and **Codex** panes back on their
conversations when a session is resurrected, on macOS and Linux. You keep
running plain `claude` and `codex`, with no wrapper and no aliases.

When a Zellij session dies (crash, reboot, `kill -9`), Zellij can resurrect
it: every pane gets its last command back, waiting for Enter. With the
plugin loaded, each Claude Code and Codex pane comes back on its own: on the
conversation that pane was on (after a `/clear` or `/resume` too), in the
directory it ran in, with the options it was started with. Other panes come
back as Zellij restores them.

It also adds a reload key: **Alt r** restarts the focused pane's CLI in
place, on its current conversation. Use it to pick up a new Claude Code or
Codex version, new MCP servers or a changed setting.

```sh
./install.sh --edit-config      # builds the plugin, adds it to load_plugins
# then once, inside Zellij (and press y in the pane that opens):
zellij plugin -- 'file:~/.config/zellij/plugins/zellij-ai-resume.wasm'
```

## Install

Requirements:
- Zellij 0.45 on macOS or Linux.
- To build the plugin, Rust through [rustup](https://rustup.rs). The first
  build fetches the Rust version pinned in `plugin/rust-toolchain.toml`, for
  `plugin/` only.

`./install.sh` takes these steps:
1. It builds the plugin, or takes a built one with `--wasm FILE`.
2. It copies it to `plugins/zellij-ai-resume.wasm` in Zellij's configuration
   directory, which it asks Zellij for (`zellij setup --check`).
3. Then it prints what to add to `config.kdl`:

   ```kdl
   load_plugins {
       "file:~/.config/zellij/plugins/zellij-ai-resume.wasm"
   }
   ```

   Zellij reads only the first `load_plugins` block, so if you already have
   one, put the line inside it.

`--edit-config` does that step for you:
- It fills in an existing `load_plugins { ... }` block, adds one, or creates
  `config.kdl`.
- It keeps the file as it was in `config.kdl.before-zellij-ai-resume`.
- It leaves a symlinked `config.kdl` alone (for managed dotfiles), and any
  block it cannot edit safely.

Last step: grant the plugin its permissions, once. A plugin loaded in the
background cannot show Zellij's permission prompt, so open it in a pane:
run `zellij plugin -- 'file:~/.config/zellij/plugins/zellij-ai-resume.wasm'`
inside Zellij and press `y`. Zellij remembers the answer for that file. The
pane shows the plugin's status and can be closed.

Zellij resurrects sessions only if its configuration directory exists.
Installing creates it, since the plugin lives there.

To upgrade, pull and run `./install.sh` again. Sessions started from then on
use the new build, and the grant still holds.

To uninstall, run `./uninstall.sh`. It removes the plugin and prints the line
to delete from `config.kdl`, or deletes it with `--edit-config`. `--purge`
also deletes the saved pane maps.

| Path | What |
|---|---|
| `~/.config/zellij/plugins/zellij-ai-resume.wasm` | the plugin (or under your `ZELLIJ_CONFIG_DIR`) |
| `config.kdl` | one line in `load_plugins`, added by you or by `--edit-config` |
| Zellij's cache folder | Zellij's own `permissions.kdl` entry, and the plugin's folder with one small JSON map per session (deleted after 30 days unused) |

Nothing else changes: no shell rc files, no Claude Code or Codex
configuration, no hooks, no wrappers on PATH.

## Reload

- **Alt r** works in every mode except locked. It reloads the focused pane.
- **`zellij pipe --name zjr-reload -- reload`** does the same from a script.
  Inside Claude Code, type `! zellij pipe --name zjr-reload -- reload`.
- **`zellij pipe --name zjr-reload -- <pane id>`** reloads a given pane. The
  id can be `3` or `terminal_3`, as `zellij action list-panes` lists them.

The CLI gets SIGTERM, and SIGKILL if it is still running 5 seconds later.
In a shell pane that is the foreground job's process tree, not the shell
itself or jobs you left running in the background.
Claude Code exits cleanly on SIGTERM. Codex exits at once and leaves its
terminal modes on, so they are reset before it starts again.

How it starts again depends on the pane:
- In a pane where you started the CLI from your shell, the command is typed
  at the shell's prompt, so it lands in your shell history. It is preceded
  by a `cd` if the shell is not in the CLI's directory.
- A resurrected pane is replaced in place.

The binding is added to the running session, not written to your
configuration. To change or drop it, give the plugin a `reload_key`
(`""` for none):

```kdl
load_plugins {
    "file:~/.config/zellij/plugins/zellij-ai-resume.wasm" {
        reload_key "Ctrl Alt r"
    }
}
```

## How it works

While a session runs, the plugin runs one small shell script on the host
every 2 seconds. The script reads:
- the process table;
- Claude Code's process registry;
- for Codex panes, the files the pane's processes hold open (`lsof` on
  macOS, `/proc` on Linux), and the recent rollout files.

From that it keeps a map: for each pane running `claude` or `codex`, the
conversation, the directory and the arguments.

- **Claude Code** writes `~/.claude/sessions/<pid>.json`, with the
  conversation id and directory of each running process. That makes
  `/clear`, `/resume` and `--continue` exact.
- **Codex** keeps its rollout file
  (`~/.codex/sessions/YYYY/MM/DD/rollout-*-<id>.jsonl`) open, which is also
  exact. Sometimes Codex's shared daemon writes the file instead, and nothing
  in the pane holds it open. The plugin then guesses: the first rollout
  started in the pane's directory since the CLI was first seen, that no other
  pane has claimed. A guess is made once and never replaces a conversation
  already found. Two daemon-backed Codex sessions started in one directory
  within seconds of each other can still be swapped.

Panes are identified by position: tab, then floating or tiled, top to bottom,
left to right. A resurrected layout reproduces those positions. The map is
kept in the plugin's cache folder, which outlives the server. After every
change the plugin asks Zellij to save the session, so the layout and the map
always describe the same moment.

When a session is resurrected, the plugin looks at the panes Zellij holds
for Enter. If a pane's position is in the map and the same CLI is waiting
there, the plugin checks that the conversation still exists. It then replaces
the pane in place with the command that resumes it, in its directory. For a
directory Zellij would remap for plugins (`/tmp`, `/data`, `/cache`, `/host`),
the command enters the directory itself:
`/bin/sh -c 'cd -- "$1" && shift && exec "$@"' zellij-ai-resume DIR claude ...`.

`CLAUDE_CONFIG_DIR` and `CODEX_HOME` are honoured. When several copies of
the plugin run in one session (the background one and one opened in a pane),
the one with the lowest id does the work.

## What a resumed pane runs

| Situation | Command |
|---|---|
| the conversation is on disk | `claude --resume <id> <your options>` / `codex resume <your options> <id>` |
| it is open in a live Claude Code process, or another pane resumes it too | the same with `--fork-session` / `codex fork`: a copy, never two writers |
| it is gone (Claude Code deletes transcripts after 30 days), or never had a message | a new conversation with your options |

"Your options" are the options known to be settings: `--model`,
`--dangerously-skip-permissions`, `--add-dir`, `-c key=value` and the like.
The following are never repeated, so a crash never re-sends a prompt:
- positional prompts and images;
- `--name`, `--worktree`, `--resume` and `--continue`;
- options this version does not recognise.

## Security

The plugin asks for six permissions:

| Permission | Used for |
|---|---|
| ReadApplicationState | panes, tabs and the session name |
| RunCommands | the read-only scripts above, and `kill` for reload |
| WriteToStdin | typing the command at a shell prompt on reload |
| Reconfigure | binding the reload key for the session |
| ReadCliPipes | `zellij pipe --name zjr-reload` |
| ChangeApplicationState | replacing a pane in place, saving the session, waking the background copy after the grant |

- **No network access.**
- **Values never become shell code.** Commands are argument vectors. The
  scripts are fixed text, and values reach them as arguments, never spliced
  into the script. The command typed at a shell prompt on reload is quoted
  word by word.
- **What is saved is checked before it is used again.**
  - Conversation ids must be UUIDs before they reach a command line.
  - The tool must be `claude` or `codex`.
  - Saved arguments are filtered again through the tables of known options.
- **Reload signals only the CLI's own processes**: the pane's foreground
  job and its children, from the same listing.
- **Unlike Zellij's default, resurrected CLI panes start without Enter.**
  This applies only to panes whose position and CLI match what the plugin
  recorded, in the first 20 seconds after the session loads. It runs a
  command the plugin builds, never the saved command line.

## Limitations

- Only CLIs running in the pane itself are seen. In an ssh session or a
  nested multiplexer, Zellij and the plugin see the outer program.
- Zellij must keep `session_serialization` on (the default).
- Built and tested with Zellij 0.45.1. Plugin API changes in later versions
  may need a rebuild.
- Sometimes a Zellij server is started from inside Claude Code. It then
  passes `CLAUDECODE` to every pane, and Claude Code runs as a child session
  that does not report conversation switches.
- Zellij keys the plugin's cache folder by the URL as written, and the grant
  by the file. Changing how `config.kdl` names the plugin (`~/` or an
  absolute path) starts a new map. Moving the file also needs the grant
  again.
- Reload types the command into the pane's shell as `cd -- '<dir>' && claude
  ...`, which bash, zsh, dash and fish accept. Other shells may not.

## Tests

```sh
(cd plugin && cargo test)                         # argument handling, parsing
cd tests
python3 -m unittest test_install                  # install.sh / uninstall.sh
ZJR_E2E=1 python3 -m unittest test_plugin         # real Zellij: crash, resurrect, reload
tests/run-linux.sh                                # all of it on Arch Linux ARM, bash and dash as /bin/sh
```

The end-to-end tests have a setup of their own:
- Each starts real Zellij servers with a throwaway HOME, configuration and
  socket directory. They never touch your own sessions.
- The plugin is installed with `install.sh --edit-config`.
- Fake `claude` and `codex` executables behave like the real ones on disk:
  the registry, transcripts and rollout files.
- A crash is SIGKILL to the server, and the session is then attached again.

`run-linux.sh` uses Apple's `container` (`brew install container`). The host
downloads the packages, so it works where the VM has no network of its own.

## History

Earlier versions were wrappers (`zclaude`, `zcodex`) that you started instead
of the CLIs. They are retired. If you installed them, `./uninstall.sh` from
commit `ad7b25c` removes them.

Compared with
[zellij-claude-enhance](https://github.com/Shengfeng233/zellij-claude-enhance):
there are no shell rc changes, no command-discovery hook, no wrappers and no
PTY proxy. It works on macOS, and it covers Codex as well as Claude Code.

## License

MIT, see [LICENSE](LICENSE).
