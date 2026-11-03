//! zellij-ai-resume: a Zellij plugin that brings Claude Code and Codex panes
//! back on their conversation when a dead session is resurrected, and
//! reloads a pane's CLI on demand. The CLIs run as they are: plain `claude`
//! and `codex`, no wrapper.
//!
//! While the session runs, the plugin maps each pane running one of them to
//! the conversation it is on and the directory it runs in. Claude Code keeps a
//! registry of its processes (~/.claude/sessions/<pid>.json); Codex keeps its
//! current rollout file (~/.codex/sessions/.../rollout-*-<id>.jsonl) open.
//! Panes are identified by position (tab, then floating/y/x order), which a
//! resurrected layout reproduces; the map lives in /cache, which survives the
//! server, and Zellij is asked to save the layout whenever the map changes,
//! so the two always describe the same moment.
//!
//! When the session is resurrected, each restored CLI pane is replaced in
//! place by the command that resumes its conversation, in its directory. A
//! pane whose command no longer matches the map is left alone.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::PathBuf;
use std::str::FromStr;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use zellij_ai_resume::cli::{self, Tool};
use zellij_tile::prelude::*;

const TICK: f64 = 2.0;
const FAST_TICK: f64 = 0.3;
const RESTORE_WINDOW: Duration = Duration::from_secs(20);
const RELOAD_GRACE: Duration = Duration::from_secs(5);
const RELOAD_GIVE_UP: Duration = Duration::from_secs(12);
const STALE_COMMAND: Duration = Duration::from_secs(15);
const MAP_FORMAT: u32 = 1;
const MAP_MAX_AGE: Duration = Duration::from_secs(60 * 60 * 24 * 30);
const PIPE_NAME: &str = "zjr-reload";

/// One host command per tick: the process table, Claude Code's registry, the
/// files the given process trees hold open, and recent Codex rollouts.
const PROBE: &str = r#"
table=$(ps -A -o pid=,ppid=)
printf '%s\n' "$table"
echo '--claude--'
for f in "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/sessions/*.json; do
    [ -f "$f" ] && cat "$f" && echo
done
echo '--files--'
if [ $# -gt 0 ]; then
    pids=$(printf '%s\n' "$table" | awk -v roots="$*" '
        { kids[$2] = kids[$2] " " $1 }
        END {
            n = split(roots, queue, " "); out = ""
            for (i = 1; i <= n; i++) seen[queue[i]] = 1
            while (n > 0) {
                pid = queue[n--]; out = out "," pid
                m = split(kids[pid], children, " ")
                for (j = 1; j <= m; j++) if (!(children[j] in seen)) { seen[children[j]] = 1; queue[++n] = children[j] }
            }
            print substr(out, 2)
        }')
    if [ -d /proc/self/fd ]; then
        for p in $(printf '%s' "$pids" | tr ',' ' '); do
            for f in /proc/"$p"/fd/*; do printf 'p%s\nn%s\n' "$p" "$(readlink "$f")"; done
        done 2>/dev/null
    else
        lsof -n -P -w -F pn -p "$pids" 2>/dev/null
    fi
    echo '--rollouts--'
    find "${CODEX_HOME:-$HOME/.codex}/sessions" -name 'rollout-*.jsonl' -mmin -720 2>/dev/null | while read -r f; do
        head -c 8192 "$f" | awk 'NR == 1 { print; exit }' | sed -n 's/.*"timestamp": *"\([^"]*\)".*"cwd": *"\([^"]*\)".*/\1	\2/p' | sed "s|^|$f	|"
    done
fi
"#;

/// Which conversations still exist, and which Claude Code ones are open in a
/// live process: `claude:<id>` / `codex:<id>` lines, then `live:<id>` lines.
const CHECK: &str = r#"
claude="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
codex="${CODEX_HOME:-$HOME/.codex}"
for item in "$@"; do
    id=${item#*:}
    case $item in
        claude:*) ls "$claude"/projects/*/"$id".jsonl >/dev/null 2>&1 && echo "$item" ;;
        codex:*) ls "$codex"/sessions/*/*/*/rollout-*-"$id".jsonl >/dev/null 2>&1 && echo "$item" ;;
    esac
done
for f in "$claude"/sessions/*.json; do
    [ -f "$f" ] || continue
    pid=${f##*/}; pid=${pid%.json}
    kill -0 "$pid" 2>/dev/null && sed -n 's/.*"sessionId" *: *"\([^"]*\)".*/live:\1/p' "$f"
done
"#;

/// What ran at a position, enough to start it again.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct Entry {
    tool: String,
    args: Vec<String>,
    cwd: String,
    session_id: Option<String>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct Map {
    format: u32,
    panes: BTreeMap<String, Entry>,
}

/// A CLI found running in a pane.
#[derive(Clone, Debug)]
struct Running {
    pane: u32,
    tool: Tool,
    args: Vec<String>,
    cwd: String,
    pane_pid: i32,
    command_pane: bool, // the CLI is the pane's own command (a restored pane), not a shell's child
}

/// What the plugin knows about a pane's CLI.
#[derive(Clone, Debug)]
struct Live {
    running: Running,
    session_id: Option<String>,
    exact: bool,     // the session came from the CLI itself, not a guess
    pids: Vec<i32>,  // the CLI's processes (not the shell around it)
    first_seen: u64, // seconds since the epoch
}

#[derive(Clone, Debug)]
struct Restore {
    pane: u32,
    key: String,
    tool: Tool,
    entry: Entry,
}

#[derive(Debug)]
struct Reload {
    pane: u32,
    live: Live,
    began: Instant,
    killed: bool,
}

#[derive(Default)]
struct State {
    reload_key: String,
    plugin_id: u32,
    granted: bool,
    session: Option<String>,
    panes: HashMap<usize, Vec<PaneInfo>>,
    tabs: Vec<TabInfo>,
    loaded: Option<Instant>,
    saved: Option<Map>,
    handled: HashSet<String>,
    restores: Vec<Restore>,
    live: HashMap<u32, Live>,
    probing: Vec<Running>,
    probe_sent: Option<Instant>,
    check_sent: Option<Instant>,
    reloads: Vec<Reload>,
    written: Option<String>,
    bound: bool,
    notes: Vec<String>,
}

register_plugin!(State);

impl ZellijPlugin for State {
    fn load(&mut self, configuration: BTreeMap<String, String>) {
        self.reload_key = configuration.get("reload_key").cloned().unwrap_or_else(|| "Alt r".to_string());
        self.plugin_id = get_plugin_ids().plugin_id;
        request_permission(&[
            PermissionType::ReadApplicationState,
            PermissionType::RunCommands,
            PermissionType::WriteToStdin,
            PermissionType::Reconfigure,
            PermissionType::ReadCliPipes,
            PermissionType::ChangeApplicationState,
        ]);
        subscribe(&[
            EventType::PermissionRequestResult,
            EventType::ModeUpdate,
            EventType::SessionUpdate,
            EventType::PaneUpdate,
            EventType::TabUpdate,
            EventType::Timer,
            EventType::RunCommandResult,
        ]);
        set_timeout(FAST_TICK);
    }

    fn update(&mut self, event: Event) -> bool {
        match event {
            Event::PermissionRequestResult(status) => {
                self.granted = status == PermissionStatus::Granted;
                self.note(format!("permissions {status:?}"));
                if self.granted {
                    // Granted here, in a pane: a background instance that asked
                    // before still waits for an answer; restarted, it finds the grant.
                    let me = self.plugin_id;
                    let stalled: Vec<u32> = self.instances().into_iter().filter(|id| *id < me).collect();
                    for id in stalled {
                        reload_plugin_with_id(id);
                        self.note(format!("restarted instance {id} with the grant"));
                    }
                }
            }
            // Background plugins get no ModeUpdate; SessionUpdate names the session too.
            Event::ModeUpdate(info) => {
                if info.session_name.is_some() {
                    self.session = info.session_name;
                }
            }
            Event::SessionUpdate(sessions, _) => {
                if let Some(current) = sessions.iter().find(|session| session.is_current_session) {
                    self.session = Some(current.name.clone());
                }
            }
            Event::PaneUpdate(manifest) => self.panes = manifest.panes,
            Event::TabUpdate(tabs) => self.tabs = tabs,
            Event::Timer(_) => {
                self.tick();
                let busy = !self.reloads.is_empty() || self.restoring();
                set_timeout(if busy { FAST_TICK } else { TICK });
            }
            Event::RunCommandResult(code, stdout, stderr, context) => {
                let output = String::from_utf8_lossy(&stdout).to_string();
                match context.get("kind").map(String::as_str) {
                    Some("probe") => {
                        self.probe_sent = None;
                        if code == Some(0) {
                            self.on_probe(&output);
                        }
                    }
                    Some("check") => {
                        self.check_sent = None;
                        self.on_check(&output);
                    }
                    _ => {}
                }
                if code != Some(0) && !stderr.is_empty() {
                    self.note(format!("host command failed: {}", String::from_utf8_lossy(&stderr).trim()));
                }
            }
            _ => {}
        }
        true
    }

    fn pipe(&mut self, message: PipeMessage) -> bool {
        if message.name != PIPE_NAME {
            return false;
        }
        if let PipeSource::Cli(pipe_id) = &message.source {
            unblock_cli_pipe_input(pipe_id); // or `zellij pipe` waits forever
        }
        // `zellij pipe` with a non-terminal stdin (`! zellij pipe ...` inside
        // Claude Code) ends with an empty message: not a second request.
        let Some(payload) = message.payload.as_deref().map(str::trim).filter(|payload| !payload.is_empty()) else {
            return false;
        };
        self.reload(payload.parse::<u32>().ok());
        true
    }

    fn render(&mut self, _rows: usize, _cols: usize) {
        // Seen only when the plugin is opened in a pane, e.g. to grant its permissions.
        println!("zellij-ai-resume");
        println!();
        if !self.granted {
            println!("Waiting for permissions: answer the prompt in this pane.");
            return;
        }
        println!("Active{}.", if self.is_leader() { "" } else { " (another instance of the plugin does the work)" });
        println!("Session: {}", self.session.as_deref().unwrap_or("?"));
        let mut tracked: Vec<_> = self.live.values().collect();
        tracked.sort_by_key(|live| live.running.pane);
        println!("Tracking {} pane(s):", tracked.len());
        for live in tracked {
            println!("  pane {}: {} {} in {}", live.running.pane, live.running.tool.name(),
                     live.session_id.as_deref().unwrap_or("(conversation not known yet)"), live.running.cwd);
        }
        if !self.reload_key.is_empty() {
            println!();
            println!("{} (or `zellij pipe --name {PIPE_NAME} -- reload`) restarts the focused CLI on its conversation.", self.reload_key);
        }
        println!();
        println!("Loaded through load_plugins it runs in the background; this pane can be closed.");
        for note in self.notes.iter().rev().take(5) {
            println!("  {note}");
        }
    }
}

impl State {
    fn note(&mut self, message: String) {
        eprintln!("zellij-ai-resume: {message}");
        self.notes.push(message);
        if self.notes.len() > 20 {
            self.notes.remove(0);
        }
    }

    fn restoring(&self) -> bool {
        self.loaded.map_or(true, |loaded| loaded.elapsed() < RESTORE_WINDOW)
    }

    /// The ids of this plugin's instances (a background one, and one opened
    /// in a pane to grant permissions, say).
    fn instances(&self) -> Vec<u32> {
        let all: Vec<&PaneInfo> = self.panes.values().flatten().filter(|pane| pane.is_plugin).collect();
        let Some(url) = all.iter().find(|pane| pane.id == self.plugin_id).and_then(|pane| pane.plugin_url.clone()) else {
            return vec![self.plugin_id];
        };
        all.iter().filter(|pane| pane.plugin_url.as_deref() == Some(url.as_str())).map(|pane| pane.id).collect()
    }

    /// Only one instance acts: the one with the lowest id.
    fn is_leader(&self) -> bool {
        self.instances().into_iter().min() == Some(self.plugin_id)
    }

    fn tick(&mut self) {
        let Some(session) = self.session.clone() else { return };
        if !self.granted || self.panes.is_empty() || !self.is_leader() {
            return;
        }
        if self.loaded.is_none() {
            self.loaded = Some(Instant::now());
            self.saved = read_map(&session);
            let count = self.saved.as_ref().map_or(0, |map| map.panes.len());
            self.note(format!("session {session}: {count} pane(s) saved before"));
            forget_old_maps();
            self.bind_reload_key();
        }
        if self.restoring() {
            self.restore();
        }
        self.advance_reloads();
        if self.probe_sent.map_or(true, |sent| sent.elapsed() > STALE_COMMAND) {
            self.probe();
        }
    }

    /// Bind the reload key for this session (not written to the config file).
    /// Through `reconfigure`: Zellij 0.45 drops a KeybindPipe's name and
    /// payload when a plugin binds it with `rebind_keys`.
    fn bind_reload_key(&mut self) {
        if self.bound || self.reload_key.is_empty() {
            return;
        }
        let plain = self.reload_key.chars().all(|c| c.is_ascii_alphanumeric() || " +-".contains(c));
        if !plain || KeyWithModifier::from_str(&self.reload_key).is_err() {
            self.note(format!("reload_key {:?} is not a key", self.reload_key));
            return;
        }
        reconfigure(
            format!(
                "keybinds {{\n    shared_except \"locked\" {{\n        bind \"{}\" {{ MessagePlugin {{ name \"{PIPE_NAME}\"; payload \"reload\"; }}; }}\n    }}\n}}\n",
                self.reload_key
            ),
            false,
        );
        self.bound = true;
    }

    // --- restoring a resurrected session ------------------------------------------------

    fn restore(&mut self) {
        if self.check_sent.map_or(false, |sent| sent.elapsed() < STALE_COMMAND) {
            return;
        }
        let Some(saved) = &self.saved else { return };
        let mut todo = Vec::new();
        for (key, pane) in positions(&self.panes) {
            if !pane.is_held || self.handled.contains(&key) {
                continue;
            }
            let Some(entry) = saved.panes.get(&key) else { continue };
            let words: Vec<String> = pane.terminal_command.as_deref().unwrap_or("").split_whitespace().map(String::from).collect();
            let Some((tool, _)) = cli::identify(&words) else { continue };
            if Tool::from_name(&entry.tool) != Some(tool) {
                continue; // something else runs at this position now
            }
            todo.push(Restore { pane: pane.id, key, tool, entry: entry.clone() });
        }
        if todo.is_empty() {
            return;
        }
        let items: Vec<String> = todo
            .iter()
            .filter_map(|restore| restore.entry.session_id.as_ref().map(|id| format!("{}:{id}", restore.tool.name())))
            .collect();
        self.restores = todo;
        self.check_sent = Some(Instant::now());
        let mut command = vec!["/bin/sh".to_string(), "-c".to_string(), CHECK.to_string(), "check".to_string()];
        command.extend(items);
        run(&command, "check");
    }

    fn on_check(&mut self, output: &str) {
        let existing: HashSet<&str> = output.lines().filter(|line| !line.starts_with("live:")).collect();
        let live: HashSet<&str> = output.lines().filter_map(|line| line.strip_prefix("live:")).collect();
        let mut resumed: HashSet<String> = HashSet::new();
        for restore in std::mem::take(&mut self.restores) {
            // Still the restored pane, waiting? (Enter may have been pressed meanwhile.)
            let still_held = self.panes.values().flatten().any(|pane| !pane.is_plugin && pane.id == restore.pane && pane.is_held);
            self.handled.insert(restore.key.clone());
            if !still_held {
                continue;
            }
            let entry = &restore.entry;
            let tool = restore.tool;
            let args = match &entry.session_id {
                Some(id) if existing.contains(format!("{}:{id}", tool.name()).as_str()) => {
                    let fork = resumed.contains(id) || (tool == Tool::Claude && live.contains(id.as_str()));
                    resumed.insert(id.clone());
                    cli::resume_args(tool, &entry.args, id, fork)
                }
                _ => cli::fresh_args(tool, &entry.args), // gone, or it never had a message
            };
            let command = CommandToRun { path: PathBuf::from(tool.name()), args: args.clone(), cwd: Some(PathBuf::from(&entry.cwd)) };
            let opened = open_command_pane_in_place_of_pane_id(PaneId::Terminal(restore.pane), command, true, BTreeMap::new());
            self.note(format!("restored {} as {} {} in {} ({opened:?})", restore.key, tool.name(), args.join(" "), entry.cwd));
        }
    }

    // --- following the CLIs -------------------------------------------------------------

    fn probe(&mut self) {
        let mut running = Vec::new();
        for (_, pane) in positions(&self.panes) {
            if pane.is_held || pane.exited {
                continue;
            }
            let id = PaneId::Terminal(pane.id);
            let Ok(argv) = get_pane_running_command(id) else { continue };
            let Some((tool, start)) = cli::identify(&argv) else { continue };
            let args = argv[start.min(argv.len())..].to_vec();
            if !cli::is_interactive(tool, &args) {
                continue;
            }
            let (Ok(pane_pid), Ok(cwd)) = (get_pane_pid(id), get_pane_cwd(id)) else { continue };
            running.push(Running {
                pane: pane.id,
                tool,
                args,
                cwd: cwd.display().to_string(),
                pane_pid,
                command_pane: pane.terminal_command.is_some(),
            });
        }
        if running.is_empty() {
            self.live.clear();
            self.persist();
            return;
        }
        let mut command = vec!["/bin/sh".to_string(), "-c".to_string(), PROBE.to_string(), "probe".to_string()];
        command.extend(running.iter().filter(|r| r.tool == Tool::Codex).map(|r| r.pane_pid.to_string()));
        self.probing = running;
        self.probe_sent = Some(Instant::now());
        run(&command, "probe");
    }

    fn on_probe(&mut self, output: &str) {
        let sections = split_sections(output);
        let mut parent: HashMap<i32, i32> = HashMap::new();
        for line in sections.get("").map(String::as_str).unwrap_or("").lines() {
            let mut fields = line.split_whitespace();
            if let (Some(Ok(pid)), Some(Ok(ppid))) = (fields.next().map(str::parse), fields.next().map(str::parse)) {
                parent.insert(pid, ppid);
            }
        }
        let registry: Vec<Value> = serde_json::Deserializer::from_str(sections.get("claude").map(String::as_str).unwrap_or(""))
            .into_iter::<Value>()
            .flatten()
            .collect();
        let open = parse_open_files(sections.get("files").map(String::as_str).unwrap_or(""));
        let rollouts = parse_rollouts(sections.get("rollouts").map(String::as_str).unwrap_or(""));
        let now = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |elapsed| elapsed.as_secs());

        let mut live = HashMap::new();
        let mut claimed: HashSet<String> = HashSet::new();
        for running in std::mem::take(&mut self.probing) {
            let previous = self.live.get(&running.pane).filter(|known| known.running.tool == running.tool);
            let first_seen = previous.map_or(now, |known| known.first_seen);
            // The CLI's processes: the pane's own process for a command pane,
            // its descendants otherwise (the shell is not the CLI).
            let mut pids: Vec<i32> = parent.keys().copied().filter(|pid| descends_from(*pid, running.pane_pid, &parent)).collect();
            if !running.command_pane {
                pids.retain(|pid| *pid != running.pane_pid);
            }
            let (session_id, exact) = match running.tool {
                Tool::Claude => {
                    let found = registry
                        .iter()
                        .filter(|entry| entry.get("pid").and_then(Value::as_i64).map_or(false, |pid| pids.contains(&(pid as i32))))
                        .max_by_key(|entry| entry.get("startedAt").and_then(Value::as_i64).unwrap_or(0));
                    match found.and_then(|entry| entry.get("sessionId")).and_then(Value::as_str) {
                        Some(id) if cli::is_uuid(id) => (Some(id.to_string()), true),
                        _ => (previous.and_then(|known| known.session_id.clone()), false),
                    }
                }
                Tool::Codex => {
                    let held = pids
                        .iter()
                        .flat_map(|pid| open.get(pid).into_iter().flatten())
                        .filter_map(|path| rollout_id(path).map(|id| (path.rsplit('/').next().unwrap_or("").to_string(), id)))
                        .max();
                    match (held, previous) {
                        (Some((_, id)), _) => (Some(id), true),
                        (None, Some(known)) if known.session_id.is_some() => (known.session_id.clone(), known.exact),
                        // Codex's shared daemon writes the file for its clients:
                        // the first rollout started in this directory since the
                        // CLI was seen, that no other pane has.
                        _ => {
                            let guess = rollouts
                                .iter()
                                .filter(|rollout| rollout.cwd == running.cwd && rollout.started + 5 >= first_seen)
                                .filter(|rollout| !claimed.contains(&rollout.id) && !self.live.values().any(|other| {
                                    other.running.pane != running.pane && other.session_id.as_deref() == Some(rollout.id.as_str())
                                }))
                                .min_by_key(|rollout| rollout.started);
                            (guess.map(|rollout| rollout.id.clone()), false)
                        }
                    }
                }
            };
            if let Some(id) = &session_id {
                claimed.insert(id.clone());
            }
            live.insert(running.pane, Live { running, session_id, exact, pids, first_seen });
        }
        self.live = live;
        self.persist();
    }

    /// Write the position -> conversation map and have Zellij save the layout
    /// with it. Restored positions not yet replaced keep their saved entry.
    fn persist(&mut self) {
        let Some(session) = self.session.clone() else { return };
        if self.loaded.is_none() {
            return;
        }
        let mut map = Map { format: MAP_FORMAT, panes: BTreeMap::new() };
        for (key, pane) in positions(&self.panes) {
            if let Some(live) = self.live.get(&pane.id) {
                let running = &live.running;
                map.panes.insert(key, Entry {
                    tool: running.tool.name().to_string(),
                    args: running.args.clone(),
                    cwd: running.cwd.clone(),
                    session_id: live.session_id.clone(),
                });
            } else if pane.is_held && !self.handled.contains(&key) {
                if let Some(entry) = self.saved.as_ref().and_then(|saved| saved.panes.get(&key)) {
                    map.panes.insert(key, entry.clone());
                }
            }
        }
        let Ok(text) = serde_json::to_string_pretty(&map) else { return };
        if self.written.as_deref() == Some(text.as_str()) {
            return;
        }
        let path = map_path(&session);
        let temporary = path.with_extension("tmp");
        match fs::write(&temporary, &text).and_then(|_| fs::rename(&temporary, &path)) {
            Ok(()) => {
                self.written = Some(text);
                let _ = save_session();
            }
            Err(error) => self.note(format!("cannot write {}: {error}", path.display())),
        }
    }

    // --- reload -------------------------------------------------------------------------

    fn focused_pane(&self) -> Option<u32> {
        if let Ok((_, PaneId::Terminal(id))) = get_focused_pane_info() {
            return Some(id);
        }
        let tab = self.tabs.iter().find(|tab| tab.active)?;
        let panes = self.panes.get(&tab.position)?;
        let focused = |floating: bool| panes.iter().find(|pane| !pane.is_plugin && pane.is_focused && pane.is_floating == floating);
        let pane = if tab.are_floating_panes_visible { focused(true).or_else(|| focused(false)) } else { focused(false) };
        pane.map(|pane| pane.id)
    }

    /// Stop the CLI in a pane (the focused one by default); it is started
    /// again on its conversation once it has exited.
    fn reload(&mut self, pane: Option<u32>) {
        if !self.granted || !self.is_leader() {
            self.note(format!("reload: not acting (granted {}, leader {})", self.granted, self.is_leader()));
            return;
        }
        let Some(pane) = pane.or_else(|| self.focused_pane()) else {
            self.note("reload: no focused terminal pane".to_string());
            return;
        };
        if self.reloads.iter().any(|reload| reload.pane == pane) {
            return;
        }
        let Some(live) = self.live.get(&pane).cloned() else {
            self.note(format!("reload: pane {pane} runs no tracked CLI"));
            return;
        };
        signal(&live.pids, "TERM");
        self.reloads.push(Reload { pane, live, began: Instant::now(), killed: false });
        set_timeout(FAST_TICK);
    }

    fn advance_reloads(&mut self) {
        for mut reload in std::mem::take(&mut self.reloads) {
            let elapsed = reload.began.elapsed();
            let pane = self.panes.values().flatten().find(|pane| !pane.is_plugin && pane.id == reload.pane).cloned();
            let Some(pane) = pane else { continue }; // the pane was closed
            let live = &reload.live;
            let id = PaneId::Terminal(reload.pane);
            let gone = if live.running.command_pane {
                pane.exited || pane.is_held
            } else {
                get_pane_running_command(id).map_or(false, |argv| cli::identify(&argv).is_none())
            };
            if gone {
                self.restart(&reload);
                continue;
            }
            if elapsed > RELOAD_GRACE && !reload.killed {
                signal(&live.pids, "KILL");
                reload.killed = true;
            }
            if elapsed > RELOAD_GIVE_UP {
                self.note(format!("reload: pane {} did not stop", reload.pane));
                continue;
            }
            self.reloads.push(reload);
        }
    }

    fn restart(&mut self, reload: &Reload) {
        let live = &reload.live;
        let tool = live.running.tool;
        let args = match &live.session_id {
            Some(id) => cli::resume_args(tool, &live.running.args, id, false),
            None => cli::fresh_args(tool, &live.running.args),
        };
        let id = PaneId::Terminal(reload.pane);
        if live.running.command_pane {
            let command = CommandToRun { path: PathBuf::from(tool.name()), args: args.clone(), cwd: Some(PathBuf::from(&live.running.cwd)) };
            open_command_pane_in_place_of_pane_id(id, command, true, BTreeMap::new());
        } else {
            // Back at the shell's prompt: type the command, after a cd if the
            // shell is elsewhere, and undo terminal modes a killed CLI left on.
            let mut line = String::new();
            if tool == Tool::Codex {
                line.push_str("printf '\\033[?1049l\\033[?2004l\\033[<u\\033[?25h'; ");
            }
            if get_pane_cwd(id).map(|cwd| cwd.display().to_string()).as_deref() != Ok(live.running.cwd.as_str()) {
                line.push_str(&format!("cd -- {} && ", cli::shell_quote(&live.running.cwd)));
            }
            line.push_str(tool.name());
            for arg in &args {
                line.push(' ');
                line.push_str(&cli::shell_quote(arg));
            }
            line.push('\r');
            write_chars_to_pane_id(&line, id);
        }
        self.note(format!("reloaded pane {} as {} {}", reload.pane, tool.name(), args.join(" ")));
    }
}

// --- helpers --------------------------------------------------------------------------------

fn run(command: &[String], kind: &str) {
    let mut context = BTreeMap::new();
    context.insert("kind".to_string(), kind.to_string());
    let words: Vec<&str> = command.iter().map(String::as_str).collect();
    run_command(&words, context);
}

fn signal(pids: &[i32], name: &str) {
    if pids.is_empty() {
        return;
    }
    let mut command = vec!["kill".to_string(), format!("-{name}")];
    command.extend(pids.iter().map(i32::to_string));
    run(&command, "signal");
}

/// Terminal panes keyed by position: tab, then (floating, y, x).
fn positions(panes: &HashMap<usize, Vec<PaneInfo>>) -> Vec<(String, PaneInfo)> {
    let mut out = Vec::new();
    for (tab, panes) in panes {
        let mut terminals: Vec<&PaneInfo> = panes.iter().filter(|pane| !pane.is_plugin && !pane.is_suppressed).collect();
        terminals.sort_by_key(|pane| (pane.is_floating, pane.pane_y, pane.pane_x));
        for (index, pane) in terminals.into_iter().enumerate() {
            out.push((format!("{tab}:{index}"), pane.clone()));
        }
    }
    out
}

fn descends_from(mut pid: i32, ancestor: i32, parent: &HashMap<i32, i32>) -> bool {
    for _ in 0..64 {
        if pid == ancestor {
            return true;
        }
        match parent.get(&pid) {
            Some(&next) if next > 1 && next != pid => pid = next,
            _ => return false,
        }
    }
    false
}

fn split_sections(output: &str) -> HashMap<String, String> {
    let mut sections = HashMap::new();
    let mut name = String::new();
    let mut body = String::new();
    for line in output.lines() {
        if let Some(next) = line.strip_prefix("--").and_then(|rest| rest.strip_suffix("--")) {
            sections.insert(std::mem::replace(&mut name, next.to_string()), std::mem::take(&mut body));
        } else {
            body.push_str(line);
            body.push('\n');
        }
    }
    sections.insert(name, body);
    sections
}

/// lsof -F pn style: "p<pid>" lines, each followed by its "n<path>" lines.
fn parse_open_files(text: &str) -> HashMap<i32, Vec<String>> {
    let mut open: HashMap<i32, Vec<String>> = HashMap::new();
    let mut pid = 0;
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix('p') {
            pid = rest.parse().unwrap_or(0);
        } else if let Some(path) = line.strip_prefix('n') {
            if pid > 0 {
                open.entry(pid).or_default().push(path.to_string());
            }
        }
    }
    open
}

fn rollout_id(path: &str) -> Option<String> {
    let name = path.rsplit('/').next()?;
    let stem = name.strip_prefix("rollout-")?.strip_suffix(".jsonl")?;
    let id = stem.get(stem.len().checked_sub(36)?..)?;
    (path.contains("/sessions/") && cli::is_uuid(id)).then(|| id.to_lowercase())
}

struct Rollout {
    id: String,
    started: u64,
    cwd: String,
}

fn parse_rollouts(text: &str) -> Vec<Rollout> {
    text.lines()
        .filter_map(|line| {
            let mut fields = line.split('\t');
            let (path, timestamp, cwd) = (fields.next()?, fields.next()?, fields.next()?);
            Some(Rollout { id: rollout_id(path)?, started: parse_utc(timestamp)?, cwd: cwd.to_string() })
        })
        .collect()
}

/// Seconds since the epoch for "YYYY-MM-DDTHH:MM:SS[.fff]Z".
fn parse_utc(text: &str) -> Option<u64> {
    let number = |range: std::ops::Range<usize>| text.get(range)?.parse::<i64>().ok();
    let (year, month, day) = (number(0..4)?, number(5..7)?, number(8..10)?);
    let (hour, minute, second) = (number(11..13)?, number(14..16)?, number(17..19)?);
    // Days from the civil date (Howard Hinnant's algorithm).
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * ((month + 9) % 12) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146097 + doe - 719468;
    u64::try_from(days * 86400 + hour * 3600 + minute * 60 + second).ok()
}

fn map_path(session: &str) -> PathBuf {
    let safe: String = session.chars().map(|c| if c.is_ascii_alphanumeric() || "-_.".contains(c) { c } else { '_' }).collect();
    PathBuf::from("/cache").join(format!("session-{safe}.json"))
}

fn read_map(session: &str) -> Option<Map> {
    let map: Map = serde_json::from_str(&fs::read_to_string(map_path(session)).ok()?).ok()?;
    (map.format == MAP_FORMAT).then_some(map)
}

/// Maps of sessions not seen for a month (Claude Code drops its transcripts by then).
fn forget_old_maps() {
    let Ok(entries) = fs::read_dir("/cache") else { return };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        let old = entry.metadata().and_then(|meta| meta.modified()).ok()
            .and_then(|modified| modified.elapsed().ok())
            .map_or(false, |age| age > MAP_MAX_AGE);
        if name.starts_with("session-") && name.ends_with(".json") && old {
            let _ = fs::remove_file(entry.path());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn utc() {
        assert_eq!(parse_utc("1970-01-01T00:00:00Z"), Some(0));
        assert_eq!(parse_utc("2026-11-03T19:57:32.884Z"), Some(1793735852));
        assert_eq!(parse_utc("nope"), None);
    }

    #[test]
    fn rollouts_and_files() {
        let path = "/h/.codex/sessions/2026/11/03/rollout-2026-11-03T13-57-32-01a020c0-0f51-7e82-9f3b-fab2b4f02036.jsonl";
        assert_eq!(rollout_id(path).as_deref(), Some("01a020c0-0f51-7e82-9f3b-fab2b4f02036"));
        assert_eq!(rollout_id("/h/.codex/log/rollout-x.jsonl"), None);
        let open = parse_open_files("p12\nn/a\nn/b\np13\nn/c\n");
        assert_eq!(open[&12], vec!["/a", "/b"]);
        let sections = split_sections("1 0\n--claude--\n{}\n--files--\np1\n");
        assert_eq!(sections[""], "1 0\n");
        assert_eq!(sections["files"], "p1\n");
    }
}
