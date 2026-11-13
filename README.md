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

It also follows what each agent is doing ([Agents](#agents)):
- **Its tab says it:** "◐ Fixing the parser" while it works, "● …" when it
  needs you, "✓ …" when it finished and you have not looked yet.
- **Alt u** goes to the agent that has waited longest for you.
- **Alt a** lists every agent in the session, and goes to the one you pick.
- **A desktop notification** says when one needs you or finishes while you
  look elsewhere.

And **Alt r** restarts the focused pane's CLI in place, on its current
conversation. Use it to pick up a new Claude Code or Codex version, new MCP
servers or a changed setting.

```sh
./install.sh --edit-config      # builds the plugin, adds it to load_plugins
# then once, inside Zellij (and press y in the pane that opens):
zellij plugin -c grant=true -- 'file:~/.config/zellij/plugins/zellij-ai-resume.wasm'
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
run `zellij plugin -c grant=true -- 'file:~/.config/zellij/plugins/zellij-ai-resume.wasm'`
inside Zellij and press `y`. Zellij remembers the answer for that file. The
pane shows the plugin's status and can be closed.

Granted before the plugin followed agents (without `-c grant=true`), it
lacks the permission to read pane contents. Everything else works, but it
cannot see Claude Code at work or waiting. Run the command above once more to
add it.

Zellij resurrects sessions only if its configuration directory exists.
Installing creates it, since the plugin lives there.

To upgrade, pull and run `./install.sh` again. Sessions started from then on
use the new build, and the grant still holds. In a running session,
`zellij action start-or-reload-plugin 'file:~/.config/zellij/plugins/zellij-ai-resume.wasm'`
loads the new build at once.

To uninstall, run `./uninstall.sh`. It removes the plugin and prints the line
to delete from `config.kdl`, or deletes it with `--edit-config` (which also
removes the hooks `--claude-hooks` added). `--purge` also deletes the saved
pane maps.

| Path | What |
|---|---|
| `~/.config/zellij/plugins/zellij-ai-resume.wasm` | the plugin (or under your `ZELLIJ_CONFIG_DIR`) |
| `config.kdl` | one line in `load_plugins`, added by you or by `--edit-config` |
| Zellij's cache folder | Zellij's own `permissions.kdl` entry, and the plugin's folder: one small JSON map per session (deleted after 30 days unused), the agents' state for the inbox, and a note that reading pane contents was granted |
| Claude Code's `settings.json` | only with `--claude-hooks`: three hooks (see [Agents](#agents)) |

Nothing else changes: no shell rc files, no Codex configuration, no
wrappers on PATH.

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

It starts again on the conversation it was on. Claude Code writes a
conversation only with its first message, so one that has none yet starts
afresh, with the same options.

How it starts again depends on the pane:
- In a pane where you started the CLI from your shell, the command is typed
  at the shell's prompt, so it lands in your shell history. It is preceded
  by a `cd` if the shell is not in the CLI's directory.
- A resurrected pane is replaced in place.

The binding is added to the running session, not written to your
configuration.

## Agents

The plugin follows every Claude Code and Codex pane it tracks, from what the
CLI shows people:
- **Claude Code** keeps "✳ <conversation>" as its pane's title. While it works,
  its status line reads "esc to interrupt". A permission or a question ends
  with "Esc to cancel" under numbered choices ("❯ 1. Yes …").
- **Codex** puts a spinner before its title while it works, and
  "[ ! ] Action Required" while it waits for an answer. Its approval and
  update prompts end with "Press enter to confirm or esc to cancel" or
  "esc skip".

Those patterns follow [Herdr's](https://github.com/herdrdev/herdr) agent
detection rules (Apache-2.0), checked against Claude Code 2.1.323 and Codex
0.167. Only the last lines of the screen count, so a conversation that merely
talks about such prompts is not mistaken for one.

From that, each agent is:

| State | Tab mark | What it means |
|---|---|---|
| working | `◐ ` | thinking or running tools |
| needs you | `● ` | a permission, a question, a choice |
| finished | `✓ ` | a turn ended while you looked elsewhere; cleared once you look |
| idle | none | waiting for a prompt |

**Tab names.** An agent's tab is named after it: the conversation title
Claude Code or Codex gives it (else the CLI and its folder), with the mark.
With several agents in one tab, the most pressing one names it.
- Only tabs with a name Zellij gave them, or one the plugin gave them, are
  named. A tab you renamed stays as you named it.
- The names survive a resurrection like any tab name. The plugin keeps
  naming them afterwards.

**Alt u** goes to the agent that has waited longest: one that needs you,
else one that finished.

**Alt a** opens the inbox, a floating list of every agent in the session:
its state and for how long, its tab and its title. Pick one with ↑/↓ and
Enter (or its number) to go there. Esc, or leaving it, closes it.

**Notifications.** When an agent comes to need you, or finishes, while you
look at another pane (or nothing is attached), a desktop notification says
so: "Claude Code · tab 3 — Fixing the parser: needs you". macOS shows them
through `osascript`, Linux through `notify-send`. At most one per agent and
kind every 30 seconds.

**Hooks (optional).** Reading the screen needs no setup, but Claude Code can
also say it outright. `./install.sh --claude-hooks` adds three hooks to its
`settings.json` (keeping a copy of the file as it was):

| Hook | Reports |
|---|---|
| `Notification`, for `permission_prompt` and `elicitation_dialog` only | needs you |
| `Stop` | finished |
| `UserPromptSubmit` | working |

Each runs `zellij pipe --name zjr-status -- "$ZELLIJ_PANE_ID <state>"`. It
does nothing outside Zellij, prints nothing, and always succeeds, so it
never slows down or changes a conversation. Any program can report the same
way; `<state>` is `blocked`, `done`, `working` or `idle`.

## Configuration

Settings go in a block after the plugin's line in `load_plugins`:

```kdl
load_plugins {
    "file:~/.config/zellij/plugins/zellij-ai-resume.wasm" {
        reload_key "Ctrl Alt r"
        next_key "Alt u"
        inbox_key "Alt a"
        tab_status "true"
        notifications "true"
        exit_grace "60"
        tab_titles "true"
    }
}
```

| Setting | Default | What |
|---|---|---|
| `reload_key` | `Alt r` | the reload key; `""` for none |
| `next_key` | `Alt u` | the key that goes to the agent waiting longest; `""` for none |
| `inbox_key` | `Alt a` | the key that lists the agents; `""` for none |
| `tab_status` | `true` | name agents' tabs after them, with their state; `"false"` to leave tab names alone |
| `notifications` | `true` | desktop notifications; `"false"` for none |
| `exit_grace` | `60` | seconds an exited CLI is still brought back if its session ends meanwhile (see [Shutdowns](#shutdowns-and-logouts)) |
| `tab_titles` | `true` | after a resurrection, let tabs show their pane's title again (see [Tab names](#tab-names)); `"false"` to leave tabs as Zellij restores them |

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
kept in the plugin's cache folder, which outlives the server. When a CLI
appears or changes conversation, or panes open or close, the plugin asks
Zellij to save the session, so the layout and the map describe the same
moment.

When a session is resurrected, the plugin looks at the panes Zellij holds
for Enter. If a pane's position is in the map and the same CLI is waiting
there, the plugin checks that the conversation still exists. It then replaces
the pane in place with the command that resumes it, in its directory. For a
directory Zellij would remap for plugins (`/tmp`, `/data`, `/cache`, `/host`),
the command enters the directory itself:
`/bin/sh -c 'cd -- "$1" && shift && exec "$@"' zellij-ai-resume DIR claude ...`.

### Shutdowns and logouts

A restart, shutdown or logout sends SIGTERM to every process. The CLIs exit
at once, and Zellij and the shells outlive them by a moment. Zellij's last
save can then show a shell where a CLI ran.

So a CLI that exits keeps its entry in the map for `exit_grace` (60 seconds),
and the plugin never asks Zellij to save because a CLI exited. If the session
ends within that time, the CLI comes back with the others.

If Zellij brought such a pane back as a shell, the plugin replaces the shell
too, but only when all of these hold:
- **another server wrote the map**, so a plugin restart inside a running
  session never does it;
- **Zellij resurrected the session.** The plugin checks that Zellij had a
  saved layout for it when the session started; a new session that reuses a
  deleted session's name has none;
- **every tab has the same number of panes** as when the map was written;
- **the pane is an idle shell.** That is a plain shell with nothing running,
  or a shell Zellij holds waiting for Enter.

A CLI you quit more than `exit_grace` before the session ended stays quit.

### Tab names

Zellij 0.45 shows the title of a tab's only pane as the tab's name while the
tab still has its default name. Claude Code titles its pane with the
conversation, so such tabs read "Blaze Service Issues" rather than "Tab #3".

Zellij saves each tab's name, the default "Tab #3" included, and a
resurrected tab gets it back as a name of its own. Its tab bar entry then
stays "Tab #3" for good.

Once a resurrected session is restored (after the 30-second restore window),
the plugin moves the pane of each tab still called "Tab #<n>" into a new tab.
Zellij gives a new tab a default name, so the tab shows its pane's title
again. Nothing else changes:
- the panes, and the CLIs in them, keep running;
- the order of the tabs stays the same, and the focused tab stays focused;
- the new tabs get new numbers (Zellij numbers tabs as it creates them).

This only applies to tabs with a single pane and no floating panes, as
Zellij shows titles only for those. New tabs go last, so only the run of such
tabs at the end of the tab bar is moved: a tab you named, or one with
splits, keeps every tab before it as it is.

`zellij pipe --name zjr-tabs` does the same at any time, in a session the
plugin did not restore itself (after upgrading the plugin, say).

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

The plugin asks for seven permissions:

| Permission | Used for |
|---|---|
| ReadApplicationState | panes, tabs and the session name |
| RunCommands | the read-only scripts above, and `kill` for reload |
| WriteToStdin | typing the command at a shell prompt on reload |
| Reconfigure | binding the reload key for the session |
| ReadCliPipes | `zellij pipe --name zjr-reload`, `zjr-tabs` and `zjr-status` |
| ChangeApplicationState | replacing a pane in place, moving a pane to a new tab (for its title), naming tabs, going to an agent, saving the session, waking the background copy after the grant |
| ReadPaneContents | reading the last lines of an agent's screen, to see it at work or waiting |

- **No network access.**
- **Screens are read, not kept.** Only the last lines of an agent's pane are
  read, to see its state, and nothing of them is written anywhere. The inbox
  file holds states, tab numbers and the CLIs' own titles.
- **Notifications show conversation titles** on your desktop (and in the
  notification centre). `notifications "false"` turns them off.
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
  This applies only to panes whose position matches what the plugin
  recorded, in the first 30 seconds after the session loads. The pane must
  hold the same CLI, or be an idle shell under the conditions in
  [Shutdowns](#shutdowns-and-logouts). It runs a command the plugin builds,
  never the saved command line.
- **A Claude Code process counts as live only if its pid still runs Claude
  Code.** A reboot leaves registry entries behind whose pids get reused, and
  a conversation open in a live process is continued in a copy instead.

## Limitations

- What an agent is doing is read from what its CLI draws today. A new
  Claude Code or Codex version that words things differently can leave a
  state unseen until the plugin catches up; Claude Code's hooks
  (`--claude-hooks`) say it regardless.

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
- A CLI you quit less than `exit_grace` before its session ended comes back
  with the others.
- A shell pane brought back as a CLI is a Zellij command pane, as Zellij's
  own resurrection makes it. When the CLI exits, Zellij offers to run it
  again rather than returning to a shell.

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
