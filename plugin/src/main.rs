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
//!
//! A shutdown or logout stops the CLIs a moment before Zellij, so a CLI that
//! exits keeps its place in the map for a while (`exit_grace`), and Zellij's
//! last save may show a shell where it ran. Such a shell is replaced too, but
//! only in a session Zellij resurrected, laid out as the map describes.
//!
//! Zellij shows the title of a tab's only pane (Claude Code's conversation
//! title) as the tab's name while the tab has its default name. A resurrected
//! tab gets "Tab #3" back as a name of its own, so its tab bar entry stops
//! following the pane; once the session is restored, such tabs get new ones.
//!
//! It also follows what each agent is doing (see `agents`): an agent's tab is
//! named after it with a mark ("◐ " working, "● " needs you, "✓ " finished
//! and not seen yet), a key jumps to the agent that has waited longest, a
//! second key opens a list of all of them (this plugin again, in a floating
//! pane: the "inbox"), and a desktop notification says when one needs you or
//! finishes while you look elsewhere.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::PathBuf;
use std::str::FromStr;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use zellij_ai_resume::agents::{self, Activity};
use zellij_ai_resume::cli::{self, Tool};
use zellij_tile::prelude::*;

const TICK: f64 = 2.0;
const FAST_TICK: f64 = 0.3;
const RESTORE_WINDOW: Duration = Duration::from_secs(30);
const RELOAD_GRACE: Duration = Duration::from_secs(5);
const RELOAD_GIVE_UP: Duration = Duration::from_secs(12);
const STALE_COMMAND: Duration = Duration::from_secs(15);
/// How long panes moving to new tabs take to settle; meanwhile the layout is
/// half old, half new, and must not be saved.
const MOVE_SETTLE: Duration = Duration::from_secs(3);
/// How long an exited CLI keeps its entry (configurable as `exit_grace`).
const EXIT_GRACE: Duration = Duration::from_secs(60);
const MAP_FORMAT: u32 = 1;
const MAP_MAX_AGE: Duration = Duration::from_secs(60 * 60 * 24 * 30);
const PIPE_NAME: &str = "zjr-reload";
const TABS_PIPE: &str = "zjr-tabs";
/// What an agent's hook reports: "<pane id> blocked|done|working|idle".
const STATUS_PIPE: &str = "zjr-status";
/// Focus the agent that has waited longest for the user.
const NEXT_PIPE: &str = "zjr-next";
/// What the plugin needs: to read panes and tabs, run its scripts, type a
/// command on reload, bind its keys, hear `zellij pipe`, change panes.
const PERMISSIONS: [PermissionType; 6] = [
    PermissionType::ReadApplicationState,
    PermissionType::RunCommands,
    PermissionType::WriteToStdin,
    PermissionType::Reconfigure,
    PermissionType::ReadCliPipes,
    PermissionType::ChangeApplicationState,
];
/// And to read what agents draw, which came later. A background plugin
/// cannot show Zellij's permission prompt, and asking it for a permission not
/// granted yet holds its events back until someone answers; asking for fewer
/// than granted makes Zellij forget the rest. So the grant pane (`zellij
/// plugin -c grant=true -- ...`) asks for all of them and, granted, leaves
/// SCREENS_GRANTED in the cache all instances share; from then on every
/// instance asks for all of them.
const READ_SCREENS: PermissionType = PermissionType::ReadPaneContents;
const SCREENS_GRANTED: &str = "/cache/read-screens";
/// At most one notification per agent and kind of news this often.
const NOTIFY_EVERY: Duration = Duration::from_secs(30);
/// Marks the commands that enter their directory themselves (see `command_in`).
const MARKER: &str = "zellij-ai-resume";
/// What an idle pane runs: a shell, which a restore may replace.
const SHELLS: &[&str] = &[
    "sh", "ash", "bash", "dash", "zsh", "fish", "ksh", "mksh", "oksh", "yash", "csh", "tcsh", "nu", "elvish", "xonsh", "pwsh",
];

/// One host command per tick: the process table (with process groups, to
/// tell a shell's foreground job from its background ones), Claude Code's
/// registry, the files the given process trees hold open, and recent Codex
/// rollouts.
const PROBE: &str = r#"
table=$(ps -A -o pid=,ppid=,pgid=,tpgid=)
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
            printf 'p%s\nfcwd\nn%s\n' "$p" "$(readlink /proc/"$p"/cwd)"
            for f in /proc/"$p"/fd/*; do printf 'f%s\nn%s\n' "${f##*/}" "$(readlink "$f")"; done
        done 2>/dev/null
    else
        lsof -n -P -w -F pfn -p "$pids" 2>/dev/null
    fi
    echo '--rollouts--'
    find "${CODEX_HOME:-$HOME/.codex}/sessions" -name 'rollout-*.jsonl' -mmin -720 2>/dev/null | while read -r f; do
        head -c 8192 "$f" | awk 'NR == 1 { print; exit }' | sed -n 's/.*"timestamp": *"\([^"]*\)".*"cwd": *"\([^"]*\)".*/\1	\2/p' | sed "s|^|$f	|"
    done
fi
"#;

/// Where this server comes from, asked once at load. Run in the plugin's
/// /data folder, which Zellij keeps at <cache>/<server id>/<plugin url>/...:
/// prints `server <id>` (a fresh id per server process), and `layout` if
/// Zellij has a saved layout for the session named in $1, which it has when
/// it resurrected the session and not when it created a new one (a new
/// session saves its first layout only after `serialization_interval`).
const ORIGIN: &str = r#"
dir=$(pwd -P)
while [ -n "$dir" ] && [ "$dir" != / ]; do
    cache=${dir%/*}
    found=
    for info in "$cache"/contract_version_*/session_info; do
        [ -d "$info" ] || continue
        found=1
        [ -f "$info/$1/session-layout.kdl" ] && echo layout
    done
    if [ -n "$found" ]; then
        echo "server ${dir##*/}"
        exit 0
    fi
    dir=$cache
done
"#;

/// A desktop notification: title in $1, text in $2, both arguments (never
/// code), through osascript on macOS or notify-send elsewhere.
const NOTIFY: &str = r#"
if command -v osascript >/dev/null 2>&1; then
    osascript -e 'on run argv' -e 'display notification (item 2 of argv) with title (item 1 of argv)' -e 'end run' "$1" "$2"
elif command -v notify-send >/dev/null 2>&1; then
    notify-send --app-name=zellij-ai-resume "$1" "$2"
fi
"#;

/// Which conversations still exist, and which Claude Code ones are open in a
/// live process: `claude:<id>` / `codex:<id>` lines, then `live:<id>` lines.
/// A registry entry counts only if its pid is still a Claude Code process: a
/// reboot leaves entries behind whose pids are reused.
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
    ps -o args= -p "$pid" 2>/dev/null | grep -q claude || continue
    sed -n 's/.*"sessionId" *: *"\([^"]*\)".*/live:\1/p' "$f"
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
    /// The server that wrote it, to tell a resurrected session from the
    /// plugin restarting in a running one.
    #[serde(default)]
    server: Option<String>,
    /// Terminal panes per tab when it was written.
    #[serde(default)]
    shape: Vec<usize>,
    /// The tabs (positions) this plugin names after their agent.
    #[serde(default)]
    owned_tabs: Vec<usize>,
}

/// What this server is, learned once at load (see ORIGIN).
#[derive(Clone, Debug, Default, PartialEq)]
struct Origin {
    server: Option<String>, // Zellij's id for this server process
    layout: bool,           // Zellij had a saved layout of this session: it resurrected it
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
    shell: bool, // Zellij brought the pane back as a shell, not as the CLI waiting for Enter
}

/// What the plugin knows of what an agent is doing.
#[derive(Clone, Debug)]
struct Agent {
    activity: Activity,
    since: Instant,
    since_epoch: u64,
    idle_since: Option<Instant>, // looked idle in the middle of a turn since then
    reported: Option<Activity>,  // what its hook said last: blocked, or done
    notified: Option<(Activity, Instant)>,
}

/// An agent as the inbox lists it; the working instance writes them to /cache.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct Row {
    pane: u32,
    tab: usize,
    tool: String,
    label: String,
    activity: Activity,
    since: u64,
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
    exit_grace: Duration,
    tab_titles: bool,
    tabs_renewed: bool, // this server's tabs were given back their titles (or needed not)
    renew_tabs: bool,   // asked for through `zellij pipe --name zjr-tabs`
    moving_until: Option<Instant>, // panes are moving to new tabs until then
    save_after_move: bool,
    plugin_id: u32,
    granted: bool,
    session: Option<String>,
    panes: HashMap<usize, Vec<PaneInfo>>,
    tabs: Vec<TabInfo>,
    loaded: Option<Instant>,
    saved: Option<Map>,
    origin: Option<Origin>,
    origin_sent: Option<Instant>,
    handled: HashSet<String>,
    restored: HashSet<String>, // positions replaced by a resumed CLI
    restores: Vec<Restore>,
    live: HashMap<u32, Live>,
    ended: HashMap<u32, (Live, Instant)>, // CLIs that exited, kept for `exit_grace`
    probing: Vec<Running>,
    probe_sent: Option<Instant>,
    check_sent: Option<Instant>,
    reloads: Vec<Reload>,
    reload_checks: HashMap<u32, Live>, // reloads waiting to learn whether their conversation is on disk
    written: Option<String>,
    written_panes: BTreeMap<String, Entry>,
    written_shape: Vec<usize>,
    told: bool, // whether the restore decision has been logged
    bound: bool,
    notes: Vec<String>,
    // Following the agents.
    tab_status: bool,
    notifications: bool,
    next_key: String,
    inbox_key: String,
    agents: HashMap<u32, Agent>,
    reports: HashMap<u32, Activity>, // from hooks, for agents not followed yet
    tab_names: HashMap<usize, Vec<String>>, // tab id -> the names this plugin gave it, latest last
    adopt: Option<HashSet<usize>>, // tab positions named by this plugin before it started
    clients: usize,
    rows_written: Option<String>,
    last_notice: Option<Instant>,
    screens: bool,     // reading pane contents is granted
    ask_screens: bool, // and was asked for
    // The inbox: this instance, opened by inbox_key, lists the agents.
    inbox: bool,
    rows: Vec<Row>,
    selected: usize,
    shown: bool,
}

register_plugin!(State);

impl ZellijPlugin for State {
    fn load(&mut self, configuration: BTreeMap<String, String>) {
        self.reload_key = configuration.get("reload_key").cloned().unwrap_or_else(|| "Alt r".to_string());
        self.exit_grace = EXIT_GRACE;
        self.tab_titles = configuration.get("tab_titles").map_or(true, |value| value.trim() != "false");
        self.tab_status = configuration.get("tab_status").map_or(true, |value| value.trim() != "false");
        self.notifications = configuration.get("notifications").map_or(true, |value| value.trim() != "false");
        self.next_key = configuration.get("next_key").cloned().unwrap_or_else(|| "Alt u".to_string());
        self.inbox_key = configuration.get("inbox_key").cloned().unwrap_or_else(|| "Alt a".to_string());
        self.inbox = configuration.get("mode").map(String::as_str) == Some("inbox");
        if let Some(text) = configuration.get("exit_grace") {
            match text.trim().parse::<u64>() {
                Ok(seconds) => self.exit_grace = Duration::from_secs(seconds),
                Err(_) => self.note(format!("exit_grace {text:?} is not a number of seconds")),
            }
        }
        self.plugin_id = get_plugin_ids().plugin_id;
        self.ask_screens = configuration.get("grant").map(String::as_str) == Some("true") || fs::metadata(SCREENS_GRANTED).is_ok();
        if self.ask_screens {
            let mut all = PERMISSIONS.to_vec();
            all.push(READ_SCREENS);
            request_permission(&all);
        } else {
            request_permission(&PERMISSIONS);
        }
        if self.inbox {
            subscribe(&[EventType::PermissionRequestResult, EventType::ModeUpdate, EventType::PaneUpdate, EventType::Key, EventType::Timer]);
            set_timeout(0.1);
            return;
        }
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
        if self.inbox {
            return self.inbox_update(event);
        }
        match event {
            Event::PermissionRequestResult(status) => {
                self.granted = status == PermissionStatus::Granted;
                self.note(format!("permissions {status:?}"));
                self.screens = self.granted && self.ask_screens;
                if self.screens && fs::metadata(SCREENS_GRANTED).is_err() {
                    let _ = fs::write(SCREENS_GRANTED, "");
                }
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
                    if current.connected_clients > self.clients {
                        self.bound = false; // an attaching client starts from config.kdl: bind again
                    }
                    self.clients = current.connected_clients;
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
                    Some("reload") => {
                        if let Some(pane) = context.get("pane").and_then(|pane| pane.parse().ok()) {
                            self.on_reload_check(pane, &output);
                        }
                    }
                    Some("origin") => {
                        let origin = parse_origin(&output);
                        self.note(format!("server {}, {}", origin.server.as_deref().unwrap_or("(unknown)"),
                                          if origin.layout { "resurrected" } else { "not resurrected" }));
                        if !self.screens {
                            self.note("reading pane contents is not granted: Claude Code at work or waiting shows only once \
                                       `zellij plugin -c grant=true -- <this plugin>` is run and answered y".to_string());
                        }
                        self.origin = Some(origin);
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
        if self.inbox || ![PIPE_NAME, TABS_PIPE, STATUS_PIPE, NEXT_PIPE].contains(&message.name.as_str()) {
            return false;
        }
        if let PipeSource::Cli(pipe_id) = &message.source {
            unblock_cli_pipe_input(pipe_id); // or `zellij pipe` waits forever
        }
        if message.name == TABS_PIPE {
            self.renew_tabs = true; // done at the next tick, once nothing is being restored
            return false;
        }
        if message.name == NEXT_PIPE {
            if self.granted && self.is_leader() {
                self.next_agent();
            }
            return false;
        }
        if message.name == STATUS_PIPE {
            let words: Vec<&str> = message.payload.as_deref().unwrap_or("").split_whitespace().collect();
            if let [pane, event] = words[..] {
                let activity = match event {
                    "blocked" => Some(Activity::Blocked),
                    "done" => Some(Activity::Done),
                    "working" | "idle" => Some(Activity::Idle), // clears what was reported before
                    _ => None,
                };
                if let (Ok(pane), Some(activity)) = (pane.strip_prefix("terminal_").unwrap_or(pane).parse::<u32>(), activity) {
                    self.reports.insert(pane, activity);
                }
            }
            return false;
        }
        // `zellij pipe` with a non-terminal stdin (`! zellij pipe ...` inside
        // Claude Code) ends with an empty message: not a second request.
        let Some(payload) = message.payload.as_deref().map(str::trim).filter(|payload| !payload.is_empty()) else {
            return false;
        };
        // `reload` for the focused pane, or a pane id: 3 or terminal_3.
        if payload == "reload" {
            self.reload(None);
        } else if let Ok(pane) = payload.strip_prefix("terminal_").unwrap_or(payload).parse::<u32>() {
            self.reload(Some(pane));
        } else {
            self.note(format!("{PIPE_NAME}: {payload:?} is neither `reload` nor a pane id"));
        }
        true
    }

    fn render(&mut self, rows: usize, cols: usize) {
        if self.inbox {
            return self.render_inbox(rows, cols);
        }
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
            self.bind_keys();
            self.adopt = Some(self.saved.as_ref().map(|saved| saved.owned_tabs.iter().copied().collect()).unwrap_or_default());
            let mut context = BTreeMap::new();
            context.insert("kind".to_string(), "origin".to_string());
            run_command_with_env_variables_and_cwd(&["/bin/sh", "-c", ORIGIN, "origin", &session], BTreeMap::new(),
                                                   PathBuf::from("/data"), context);
            self.origin_sent = Some(Instant::now());
        }
        self.bind_keys();
        if self.origin.is_none() && self.origin_sent.map_or(false, |sent| sent.elapsed() > STALE_COMMAND) {
            self.note("no answer about this server: restoring only panes Zellij kept waiting".to_string());
            self.origin = Some(Origin::default());
        }
        if self.restoring() {
            self.restore();
        }
        self.advance_reloads();
        if !self.restoring() && self.origin.is_some() {
            let resurrected = self.origin.as_ref().map_or(false, |origin| origin.layout) && !self.same_server();
            if self.renew_tabs || (resurrected && self.tab_titles && !self.tabs_renewed) {
                self.renew_tabs = false;
                self.tabs_renewed = true;
                self.give_tabs_their_titles();
            }
            self.tabs_renewed = true;
        }
        if self.probe_sent.map_or(true, |sent| sent.elapsed() > STALE_COMMAND) {
            self.probe();
        }
        if self.origin.is_some() && self.moving_until.map_or(true, |until| Instant::now() >= until) {
            self.follow_agents();
            if self.tab_status {
                self.name_tabs();
            }
            self.write_rows(&session);
        }
    }

    /// Bind the plugin's keys for this session (not written to the config
    /// file): reload, next agent, inbox. Zellij keeps such bindings for one
    /// client only (the plugin's), and a client that attaches starts from
    /// config.kdl, so they are bound again whenever one attaches; install.sh
    /// --edit-config puts them in config.kdl, for every client. Through
    /// `reconfigure`: Zellij 0.45 drops a KeybindPipe's name and payload when
    /// a plugin binds it with `rebind_keys`.
    fn bind_keys(&mut self) {
        if self.bound {
            return;
        }
        let url = self.panes.values().flatten().find(|pane| pane.is_plugin && pane.id == self.plugin_id).and_then(|pane| pane.plugin_url.clone());
        let kdl_string = |text: &str| text.replace('\\', "\\\\").replace('"', "\\\"");
        let mut binds = String::new();
        for (setting, key, action) in [
            ("reload_key", self.reload_key.clone(), format!("MessagePlugin {{ name \"{PIPE_NAME}\"; payload \"reload\"; }};")),
            ("next_key", self.next_key.clone(), format!("MessagePlugin {{ name \"{NEXT_PIPE}\"; payload \"next\"; }};")),
            ("inbox_key", self.inbox_key.clone(), match &url {
                Some(url) => format!("LaunchOrFocusPlugin \"{}\" {{ floating true; move_to_focused_tab true; mode \"inbox\"; }};", kdl_string(url)),
                None => String::new(),
            }),
        ] {
            if key.is_empty() || action.is_empty() {
                continue;
            }
            let plain = key.chars().all(|c| c.is_ascii_alphanumeric() || " +-".contains(c));
            if !plain || KeyWithModifier::from_str(&key).is_err() {
                self.note(format!("{setting} {key:?} is not a key"));
                continue;
            }
            binds.push_str(&format!("        bind \"{key}\" {{ {action} }}\n"));
        }
        if !binds.is_empty() {
            reconfigure(format!("keybinds {{\n    shared_except \"locked\" {{\n{binds}    }}\n}}\n"), false);
        }
        self.bound = true;
    }

    // --- restoring a resurrected session ------------------------------------------------

    /// Whether this server wrote the saved map: the plugin restarted in a
    /// running session.
    fn same_server(&self) -> bool {
        let server = self.origin.as_ref().and_then(|origin| origin.server.as_ref());
        server.is_some() && self.saved.as_ref().and_then(|saved| saved.server.as_ref()) == server
    }

    /// Whether this plugin may restore anything: not when this server wrote
    /// the map.
    fn may_restore(&self) -> bool {
        self.origin.is_some() && self.saved.is_some() && !self.same_server()
    }

    /// Whether panes Zellij brought back as shells may be restored: only in a
    /// session Zellij resurrected, with the panes of each tab the map knew.
    fn may_restore_shells(&self) -> bool {
        let layout = self.origin.as_ref().map_or(false, |origin| origin.layout);
        layout && self.may_restore() && self.saved.as_ref().map_or(false, |saved| saved.shape == shape(&self.panes))
    }

    fn restore(&mut self) {
        if self.check_sent.map_or(false, |sent| sent.elapsed() < STALE_COMMAND) || !self.may_restore() {
            return;
        }
        let shells = self.may_restore_shells();
        if !self.told {
            self.told = true;
            let layout = self.origin.as_ref().map_or(false, |origin| origin.layout);
            let saved_shape = self.saved.as_ref().map(|saved| saved.shape.clone()).unwrap_or_default();
            self.note(format!("restoring held panes{}; tabs {:?}, saved {:?}, layout {layout}",
                              if shells { " and shells" } else { "" }, shape(&self.panes), saved_shape));
        }
        let Some(saved) = &self.saved else { return };
        let mut todo = Vec::new();
        for (key, pane) in positions(&self.panes) {
            if self.handled.contains(&key) {
                continue;
            }
            let Some(entry) = saved.panes.get(&key) else { continue };
            let Some(tool) = Tool::from_name(&entry.tool) else { continue };
            let shell = if pane.is_held && waits_with(&pane, tool) {
                false
            } else if shells && (holds_shell(&pane) || idle_shell(&pane)) {
                true
            } else {
                continue; // something else is at this position now
            };
            todo.push(Restore { pane: pane.id, key, tool, entry: entry.clone(), shell });
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
            // Still as it was? Enter may have been pressed meanwhile, or the
            // shell be busy with its startup files (then try again).
            let pane = self.panes.values().flatten().find(|pane| !pane.is_plugin && pane.id == restore.pane).cloned();
            let ready = match &pane {
                Some(pane) if restore.shell => holds_shell(pane) || idle_shell(pane),
                Some(pane) => pane.is_held,
                None => false,
            };
            if !ready && restore.shell && pane.is_some() {
                continue;
            }
            self.handled.insert(restore.key.clone());
            if !ready {
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
            let opened = open_command_pane_in_place_of_pane_id(PaneId::Terminal(restore.pane), command_in(tool, &args, &entry.cwd),
                                                               true, BTreeMap::new());
            self.restored.insert(restore.key.clone());
            let shell = if restore.shell { " (Zellij kept a shell there)" } else { "" };
            self.note(format!("restored {}{shell} as {} {} in {} ({opened:?})", restore.key, tool.name(), args.join(" "), entry.cwd));
        }
    }

    /// Move the pane of each tab still called "Tab #<n>" with that pane alone
    /// in it to a new tab, which Zellij names by default again, so the tab bar
    /// shows the pane's title. New tabs go last: only the run of such tabs at
    /// the end moves, which keeps the order. The focused tab stays focused.
    fn give_tabs_their_titles(&mut self) {
        let mut tabs = self.tabs.clone();
        tabs.sort_by_key(|tab| tab.position);
        let mut moving: Vec<(u32, bool)> = Vec::new();
        for tab in tabs.iter().rev() {
            match lone_pane(tab, self.panes.get(&tab.position).map(Vec::as_slice).unwrap_or(&[])) {
                Some(pane) => moving.push((pane, tab.active)),
                None => break,
            }
        }
        moving.reverse();
        if moving.is_empty() {
            let seen: Vec<String> = tabs.iter().map(|tab| {
                let panes = self.panes.get(&tab.position).map(Vec::as_slice).unwrap_or(&[]);
                let summary: Vec<String> = panes.iter().map(|pane| format!("{}{}{}{}", if pane.is_plugin { "plugin" } else { "terminal" },
                    if pane.is_floating { ",floating" } else { "" }, if pane.is_suppressed { ",suppressed" } else { "" },
                    if pane.is_plugin && pane.is_selectable { ",selectable" } else { "" })).collect();
                format!("{:?} [{}]", tab.name, summary.join(" "))
            }).collect();
            self.note(format!("no tab at the end to give its pane's title back: {}", seen.join(", ")));
            return;
        }
        for (pane, active) in &moving {
            break_panes_to_new_tab(&[PaneId::Terminal(*pane)], None, *active);
        }
        if let Some((pane, _)) = moving.iter().find(|(_, active)| *active) {
            focus_terminal_pane(*pane, false, false);
        }
        self.moving_until = Some(Instant::now() + MOVE_SETTLE);
        self.save_after_move = true;
        self.note(format!("{} tab(s) show their pane's title again", moving.len()));
    }

    // --- following the agents -----------------------------------------------------------

    /// Follow what each CLI is doing, from its pane's title and the bottom of
    /// its screen, and from what its hooks reported (`zellij pipe --name
    /// zjr-status`). A turn that ends while the user looks elsewhere is
    /// `Done` until they look; becoming `Blocked` or `Done` that way is news.
    fn follow_agents(&mut self) {
        let focused = if self.clients > 0 { self.focused_pane() } else { None };
        // CLIs coming back in a resurrected session are no news.
        let quiet = self.restoring() && self.may_restore() && self.saved.as_ref().map_or(false, |saved| !saved.panes.is_empty());
        let now = Instant::now();
        let epoch = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |elapsed| elapsed.as_secs());
        let ids: Vec<u32> = self.live.keys().copied().collect();
        self.agents.retain(|id, _| ids.contains(id));
        let panes: HashSet<u32> = self.panes.values().flatten().filter(|pane| !pane.is_plugin).map(|pane| pane.id).collect();
        self.reports.retain(|pane, _| panes.contains(pane));
        let mut news = Vec::new();
        // Zellij answers a screen read from its screen thread, and may take a
        // while when that is busy: past a second in a tick, the rest wait.
        let reading = Instant::now();
        for id in ids {
            if self.screens && reading.elapsed() > Duration::from_secs(1) {
                break;
            }
            let tool = self.live[&id].running.tool;
            let Some(title) = self.panes.values().flatten().find(|pane| !pane.is_plugin && pane.id == id).map(|pane| pane.title.clone()) else {
                continue;
            };
            let screen = if self.screens {
                get_pane_scrollback(PaneId::Terminal(id), false).map(|contents| contents.viewport).unwrap_or_default()
            } else {
                vec![]
            };
            let mut seen = agents::detect(tool, &title, &screen);
            let report = self.reports.remove(&id);
            let agent = self.agents.entry(id).or_insert(Agent {
                activity: Activity::Idle, since: now, since_epoch: epoch, idle_since: None, reported: None, notified: None,
            });
            if let Some(report) = report {
                agent.reported = (report != Activity::Idle).then_some(report);
            }
            if seen == Activity::Working && agent.reported == Some(Activity::Blocked) {
                agent.reported = None; // answered
            }
            if agent.reported == Some(Activity::Blocked) {
                seen = Activity::Blocked;
            }
            let finished = agent.reported == Some(Activity::Done);
            if finished {
                agent.reported = None;
            }
            let here = focused == Some(id);
            let (next, idle_since) = agents::advance(agent.activity, seen, finished, here, agent.idle_since, now);
            agent.idle_since = idle_since;
            if next != agent.activity {
                agent.activity = next;
                agent.since = now;
                agent.since_epoch = epoch;
                if matches!(next, Activity::Blocked | Activity::Done) && !here && !quiet {
                    news.push(id);
                }
            }
        }
        for id in news {
            self.notify(id);
        }
    }

    /// What to call an agent: its CLI's title, else the CLI and its folder.
    fn label_of(&self, pane: u32) -> String {
        let Some(live) = self.live.get(&pane) else { return String::new() };
        let title = self.panes.values().flatten().find(|info| !info.is_plugin && info.id == pane).map_or("", |info| info.title.as_str());
        let label = agents::label(live.running.tool, title);
        if label.is_empty() {
            let folder = live.running.cwd.rsplit('/').next().unwrap_or("");
            format!("{} · {folder}", live.running.tool.name())
        } else {
            label
        }
    }

    /// Name each agent's tab after it, with a mark for what it does (see
    /// `Activity::glyph`), the most pressing agent first when a tab has
    /// several. Only tabs that keep a name Zellij gave them, or one this
    /// plugin gave them: a tab the user renamed is theirs.
    fn name_tabs(&mut self) {
        let adopt = self.adopt.take().unwrap_or_default();
        for tab in self.tabs.clone() {
            let panes: Vec<PaneInfo> = self.panes.get(&tab.position).map_or(vec![], |panes| {
                panes.iter().filter(|pane| !pane.is_plugin && !pane.is_suppressed).cloned().collect()
            });
            let ours = match self.tab_names.get(&tab.tab_id) {
                Some(names) => names.contains(&tab.name),
                None => is_default_tab_name(&tab.name) || (panes.len() == 1 && tab.name == panes[0].title) || adopt.contains(&tab.position),
            };
            if !ours {
                self.tab_names.remove(&tab.tab_id);
                continue;
            }
            let focused = panes.iter().find(|pane| pane.is_focused).map(|pane| pane.id);
            let best = panes.iter().filter_map(|pane| self.agents.get(&pane.id).map(|agent| (pane.id, agent.activity)))
                .max_by_key(|(pane, activity)| (activity.urgency(), Some(*pane) == focused));
            let name = match best {
                Some((pane, activity)) => format!("{}{}", activity.glyph(), self.label_of(pane)),
                None => match self.tab_names.get(&tab.tab_id).and_then(|names| names.last()) {
                    Some(name) => strip_mark(name).to_string(), // its agent is gone: no mark any more
                    None if adopt.contains(&tab.position) => tab.name.clone(), // named before; its agent comes back soon
                    None => continue, // Zellij's to name
                },
            };
            if name.is_empty() {
                continue;
            }
            if name != tab.name {
                rename_tab_with_id(tab.tab_id as u64, &name);
            }
            let names = self.tab_names.entry(tab.tab_id).or_default();
            if names.last() != Some(&name) {
                names.push(name);
                if names.len() > 4 {
                    names.remove(0); // a few, for renames Zellij has not reported back yet
                }
            }
        }
        let tab_ids: HashSet<usize> = self.tabs.iter().map(|tab| tab.tab_id).collect();
        self.tab_names.retain(|id, _| tab_ids.contains(id));
    }

    /// Focus the agent that has waited longest for the user: one that needs
    /// an answer, else one that finished.
    fn next_agent(&mut self) {
        let focused = self.focused_pane();
        let next = self.agents.iter()
            .filter(|(pane, agent)| Some(**pane) != focused && matches!(agent.activity, Activity::Blocked | Activity::Done))
            .max_by_key(|(_, agent)| (agent.activity.urgency(), std::cmp::Reverse(agent.since)))
            .map(|(pane, _)| *pane);
        match next {
            Some(pane) => focus_terminal_pane(pane, false, false),
            None => self.note("next: no agent waits for you".to_string()),
        }
    }

    /// Tell the desktop that an agent needs the user or finished.
    fn notify(&mut self, pane: u32) {
        let Some(agent) = self.agents.get(&pane) else { return };
        let activity = agent.activity;
        let repeated = agent.notified.map_or(false, |(was, at)| was == activity && at.elapsed() < NOTIFY_EVERY);
        if !self.notifications || repeated || self.last_notice.map_or(false, |at| at.elapsed() < Duration::from_secs(2)) {
            return;
        }
        let Some(live) = self.live.get(&pane) else { return };
        let tab = self.panes.iter().find(|(_, panes)| panes.iter().any(|info| !info.is_plugin && info.id == pane)).map(|(tab, _)| *tab);
        let tool = match live.running.tool {
            Tool::Claude => "Claude Code",
            Tool::Codex => "Codex",
        };
        let title = match tab {
            Some(tab) => format!("{tool} · tab {}", tab + 1),
            None => tool.to_string(),
        };
        let text = format!("{}: {}", self.label_of(pane), activity.describe());
        let clean = |text: String| -> String { text.chars().filter(|c| !c.is_control()).collect() };
        run(&["/bin/sh".to_string(), "-c".to_string(), NOTIFY.to_string(), "notify".to_string(), clean(title), clean(text)], "notify");
        self.last_notice = Some(Instant::now());
        if let Some(agent) = self.agents.get_mut(&pane) {
            agent.notified = Some((activity, Instant::now()));
        }
    }

    /// The agents as the inbox lists them, written when they change.
    fn write_rows(&mut self, session: &str) {
        let mut rows: Vec<Row> = self.agents.iter().filter_map(|(pane, agent)| {
            let tab = self.panes.iter().find(|(_, panes)| panes.iter().any(|info| !info.is_plugin && info.id == *pane)).map(|(tab, _)| *tab)?;
            let tool = self.live.get(pane)?.running.tool.name().to_string();
            Some(Row { pane: *pane, tab, tool, label: self.label_of(*pane), activity: agent.activity, since: agent.since_epoch })
        }).collect();
        rows.sort_by_key(|row| (std::cmp::Reverse(row.activity.urgency()), row.since, row.tab));
        let Ok(text) = serde_json::to_string(&rows) else { return };
        if self.rows_written.as_deref() == Some(text.as_str()) {
            return;
        }
        let path = rows_path(session);
        let temporary = path.with_extension("tmp");
        if fs::write(&temporary, &text).and_then(|_| fs::rename(&temporary, &path)).is_ok() {
            self.rows_written = Some(text);
        }
    }

    // --- the inbox ----------------------------------------------------------------------

    fn inbox_update(&mut self, event: Event) -> bool {
        match event {
            Event::PermissionRequestResult(status) => {
                self.granted = status == PermissionStatus::Granted;
                if self.granted {
                    rename_plugin_pane(self.plugin_id, "Agents");
                }
            }
            Event::ModeUpdate(info) => {
                if info.session_name.is_some() {
                    self.session = info.session_name;
                }
            }
            Event::PaneUpdate(manifest) => {
                // Closed when left (or when brought back by a resurrection, unseen).
                let me = manifest.panes.values().flatten().find(|pane| pane.is_plugin && pane.id == self.plugin_id).cloned();
                match me {
                    Some(pane) if pane.is_focused => self.shown = true,
                    Some(_) if self.shown => close_self(),
                    _ => {}
                }
                self.panes = manifest.panes;
            }
            Event::Timer(_) => {
                if !self.shown && self.loaded.get_or_insert_with(Instant::now).elapsed() > Duration::from_secs(3) {
                    close_self();
                }
                if let Some(session) = &self.session {
                    let rows: Vec<Row> = fs::read_to_string(rows_path(session)).ok().and_then(|text| serde_json::from_str(&text).ok()).unwrap_or_default();
                    self.rows = rows;
                    self.selected = self.selected.min(self.rows.len().saturating_sub(1));
                }
                set_timeout(1.0);
            }
            Event::Key(key) => {
                let pick = |state: &mut State, index: usize| {
                    if let Some(row) = state.rows.get(index) {
                        focus_terminal_pane(row.pane, false, false);
                        close_self();
                    }
                };
                match key.bare_key {
                    BareKey::Down | BareKey::Char('j') if key.has_no_modifiers() => {
                        self.selected = (self.selected + 1).min(self.rows.len().saturating_sub(1));
                    }
                    BareKey::Up | BareKey::Char('k') if key.has_no_modifiers() => self.selected = self.selected.saturating_sub(1),
                    BareKey::Enter => {
                        let index = self.selected;
                        pick(self, index);
                    }
                    BareKey::Char(digit @ '1'..='9') if key.has_no_modifiers() => pick(self, digit as usize - '1' as usize),
                    BareKey::Esc | BareKey::Char('q') => close_self(),
                    _ => return false,
                }
            }
            _ => return false,
        }
        true
    }

    fn render_inbox(&mut self, rows: usize, cols: usize) {
        let fit = |text: String| -> String { text.chars().take(cols).collect() };
        println!("{}", fit(format!("Agents  {}", "↑↓ select · Enter go · 1-9 go · Esc close")));
        println!();
        if self.rows.is_empty() {
            println!("{}", fit("No Claude Code or Codex in this session.".to_string()));
            return;
        }
        let now = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |elapsed| elapsed.as_secs());
        for (index, row) in self.rows.iter().enumerate().take(rows.saturating_sub(2)) {
            let mark = row.activity.glyph();
            let mark = if mark.is_empty() { "  " } else { mark };
            let line = format!("{} {}{mark}tab {:<3} {:<6} {}  ({}, {})", if index < 9 { (index + 1).to_string() } else { " ".to_string() },
                               if index == self.selected { "▸ " } else { "  " }, row.tab + 1, row.tool, row.label, row.activity.describe(),
                               ago(now.saturating_sub(row.since)));
            if index == self.selected {
                println!("\u{1b}[7m{}\u{1b}[0m", fit(line));
            } else {
                println!("{}", fit(line));
            }
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
            self.set_live(HashMap::new());
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
        let mut foreground: HashSet<i32> = HashSet::new();
        for line in sections.get("").map(String::as_str).unwrap_or("").lines() {
            let fields: Vec<i32> = line.split_whitespace().map_while(|field| field.parse().ok()).collect();
            if let [pid, ppid, ref rest @ ..] = fields[..] {
                parent.insert(pid, ppid);
                if let [pgid, tpgid] = rest[..] {
                    if pgid > 0 && pgid == tpgid {
                        foreground.insert(pid); // in its terminal's foreground job
                    }
                }
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
            // The CLI's processes: the pane's own process and its tree for a
            // command pane; for a shell, the tree of its foreground job (not
            // the shell, nor jobs left in the background).
            let roots: Vec<i32> = if running.command_pane {
                vec![running.pane_pid]
            } else {
                let children: Vec<i32> = parent.iter().filter(|(_, ppid)| **ppid == running.pane_pid).map(|(pid, _)| *pid).collect();
                let jobs: Vec<i32> = children.iter().copied().filter(|pid| foreground.contains(pid)).collect();
                if jobs.is_empty() { children } else { jobs }
            };
            let mut pids: Vec<i32> = parent.keys().copied().filter(|pid| roots.iter().any(|root| descends_from(*pid, *root, &parent))).collect();
            pids.sort_unstable();
            let mut running = running;
            // Until the CLI says where it runs, or while it exits (its registry
            // entry gone, its files closed), what was known stands.
            if let Some(known) = previous {
                running.cwd = known.running.cwd.clone();
            }
            let (session_id, exact) = match running.tool {
                Tool::Claude => {
                    let found = registry
                        .iter()
                        .filter(|entry| entry.get("pid").and_then(Value::as_i64).map_or(false, |pid| pids.contains(&(pid as i32))))
                        .max_by_key(|entry| entry.get("startedAt").and_then(Value::as_i64).unwrap_or(0));
                    if let Some(cwd) = found.and_then(|entry| entry.get("cwd")).and_then(Value::as_str) {
                        if cwd.starts_with('/') {
                            running.cwd = cwd.to_string(); // where the conversation runs, from Claude Code itself
                        }
                    }
                    match found.and_then(|entry| entry.get("sessionId")).and_then(Value::as_str) {
                        Some(id) if cli::is_uuid(id) => (Some(id.to_string()), true),
                        _ => (previous.and_then(|known| known.session_id.clone()), false),
                    }
                }
                Tool::Codex => {
                    // The CLI is the pane's process (a restored pane) or the
                    // shell's foreground job; its own directory, as -C is
                    // relative to it.
                    let cli = roots.iter().copied().max();
                    if let Some(cwd) = cli.and_then(|pid| open.cwd.get(&pid)) {
                        if cwd.starts_with('/') {
                            running.cwd = cwd.clone();
                        }
                    }
                    let held = pids
                        .iter()
                        .flat_map(|pid| open.files.get(pid).into_iter().flatten())
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
        self.set_live(live);
        self.persist();
    }

    /// Take in what the latest probe found. A CLI that is gone keeps its
    /// entry for `exit_grace`, in case its session is ending too: a shutdown
    /// or logout stops the CLIs a moment before Zellij.
    fn set_live(&mut self, live: HashMap<u32, Live>) {
        let present: HashSet<u32> = self.panes.values().flatten().filter(|pane| !pane.is_plugin).map(|pane| pane.id).collect();
        for (pane, gone) in std::mem::take(&mut self.live) {
            if !live.contains_key(&pane) && present.contains(&pane) {
                self.ended.entry(pane).or_insert((gone, Instant::now()));
            }
        }
        let grace = self.exit_grace;
        self.ended.retain(|pane, (_, since)| !live.contains_key(pane) && present.contains(pane) && since.elapsed() < grace);
        self.live = live;
    }

    /// Write the position -> conversation map, and have Zellij save the layout
    /// when a CLI appeared or changed, or the panes did, so the two describe the
    /// same moment. Not when a CLI exited: a shutdown is the moment not to save.
    fn persist(&mut self) {
        let Some(session) = self.session.clone() else { return };
        let Some(server) = self.origin.as_ref().map(|origin| origin.server.clone()) else {
            return; // not before knowing what there is to restore
        };
        if self.moving_until.map_or(false, |until| Instant::now() < until) {
            return; // nor while tabs are half moved
        }
        if self.loaded.is_none() {
            return;
        }
        let shells = self.may_restore_shells();
        let mut panes = BTreeMap::new();
        for (key, pane) in positions(&self.panes) {
            let known = self.live.get(&pane.id).or_else(|| self.ended.get(&pane.id).map(|(live, _)| live));
            if let Some(live) = known {
                let running = &live.running;
                panes.insert(key, Entry {
                    tool: running.tool.name().to_string(),
                    args: running.args.clone(),
                    cwd: running.cwd.clone(),
                    session_id: live.session_id.clone(),
                });
            } else if self.restoring() && self.may_restore() {
                // To be restored still, or restored and not seen running yet.
                let pending = !self.handled.contains(&key) && (pane.is_held || shells);
                if pending || self.restored.contains(&key) {
                    if let Some(entry) = self.saved.as_ref().and_then(|saved| saved.panes.get(&key)) {
                        panes.insert(key, entry.clone());
                    }
                }
            }
        }
        let gained = panes.iter().any(|(key, entry)| self.written_panes.get(key) != Some(entry));
        let owned_tabs: Vec<usize> = self.tabs.iter()
            .filter(|tab| self.tab_names.get(&tab.tab_id).and_then(|names| names.last()) == Some(&tab.name))
            .map(|tab| tab.position).collect();
        let map = Map { format: MAP_FORMAT, panes, server, shape: shape(&self.panes), owned_tabs };
        let moved = map.shape != self.written_shape;
        let Ok(text) = serde_json::to_string_pretty(&map) else { return };
        if std::mem::take(&mut self.save_after_move) {
            let _ = save_session(); // the layout as the moves left it
        }
        if self.written.as_deref() == Some(text.as_str()) {
            return;
        }
        let path = map_path(&session);
        let temporary = path.with_extension("tmp");
        match fs::write(&temporary, &text).and_then(|_| fs::rename(&temporary, &path)) {
            Ok(()) => {
                self.written = Some(text);
                self.written_panes = map.panes;
                self.written_shape = map.shape;
                if gained || moved {
                    let _ = save_session();
                }
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
        if self.reloads.iter().any(|reload| reload.pane == pane) || self.reload_checks.contains_key(&pane) {
            return;
        }
        let Some(live) = self.live.get(&pane).cloned() else {
            self.note(format!("reload: pane {pane} runs no tracked CLI"));
            return;
        };
        // Claude Code names its conversation at start but writes it only with
        // the first message: resume it only if it is on disk.
        let Some(id) = live.session_id.clone() else {
            return self.begin_reload(pane, live);
        };
        let mut command = vec!["/bin/sh".to_string(), "-c".to_string(), CHECK.to_string(), "check".to_string()];
        command.push(format!("{}:{id}", live.running.tool.name()));
        let mut context = BTreeMap::new();
        context.insert("kind".to_string(), "reload".to_string());
        context.insert("pane".to_string(), pane.to_string());
        let words: Vec<&str> = command.iter().map(String::as_str).collect();
        run_command(&words, context);
        self.reload_checks.insert(pane, live);
    }

    fn on_reload_check(&mut self, pane: u32, output: &str) {
        let Some(mut live) = self.reload_checks.remove(&pane) else { return };
        let item = live.session_id.as_ref().map(|id| format!("{}:{id}", live.running.tool.name()));
        if !output.lines().any(|line| Some(line) == item.as_deref()) {
            live.session_id = None; // no message yet: a new conversation, with the same options
        }
        self.begin_reload(pane, live);
    }

    fn begin_reload(&mut self, pane: u32, live: Live) {
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
            open_command_pane_in_place_of_pane_id(id, command_in(tool, &args, &live.running.cwd), true, BTreeMap::new());
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

/// The command pane that runs the CLI in `cwd`. Zellij maps a plugin's paths
/// under /host, /data, /cache and /tmp to the plugin's own folders, so a
/// directory there (a project in /tmp, a server's /data) is entered by the
/// command itself instead.
fn command_in(tool: Tool, args: &[String], cwd: &str) -> CommandToRun {
    let first = cwd.trim_start_matches('/').split('/').next().unwrap_or("");
    if ["host", "data", "cache", "tmp"].contains(&first) {
        let mut wrapped = vec!["-c".to_string(), r#"cd -- "$1" && shift && exec "$@""#.to_string(), MARKER.to_string(),
                               cwd.to_string(), tool.name().to_string()];
        wrapped.extend(args.iter().cloned());
        return CommandToRun { path: PathBuf::from("/bin/sh"), args: wrapped, cwd: None };
    }
    CommandToRun { path: PathBuf::from(tool.name()), args: args.to_vec(), cwd: Some(PathBuf::from(cwd)) }
}

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

/// "Tab #3": the name Zellij gives a tab (and a resurrection gives back).
fn is_default_tab_name(name: &str) -> bool {
    name.strip_prefix("Tab #").map_or(false, |n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
}

/// A tab name without the mark this plugin puts before it.
fn strip_mark(name: &str) -> &str {
    [Activity::Working, Activity::Blocked, Activity::Done].iter().find_map(|activity| name.strip_prefix(activity.glyph())).unwrap_or(name)
}

/// "12s", "5m", "3h", "2d".
fn ago(seconds: u64) -> String {
    match seconds {
        0..=59 => format!("{seconds}s"),
        60..=3599 => format!("{}m", seconds / 60),
        3600..=86399 => format!("{}h", seconds / 3600),
        _ => format!("{}d", seconds / 86400),
    }
}

fn rows_path(session: &str) -> PathBuf {
    map_path(session).with_file_name(format!("agents-{}", map_path(session).file_name().map_or(String::new(), |name| name.to_string_lossy().trim_start_matches("session-").to_string())))
}

/// The pane of a tab that Zellij would name after it: a tab still called by
/// a default name, with one tiled terminal pane and no other pane to select.
fn lone_pane(tab: &TabInfo, panes: &[PaneInfo]) -> Option<u32> {
    let default_name = is_default_tab_name(&tab.name);
    let mut terminals = panes.iter().filter(|pane| !pane.is_plugin && !pane.is_suppressed);
    let pane = terminals.next()?;
    let alone = terminals.next().is_none() && panes.iter().all(|other| !other.is_plugin || !other.is_selectable || other.is_suppressed);
    (default_name && alone && !pane.is_floating).then_some(pane.id)
}

/// Whether a pane Zellij holds waits to run `tool`.
fn waits_with(pane: &PaneInfo, tool: Tool) -> bool {
    let words: Vec<String> = pane.terminal_command.as_deref().unwrap_or("").split_whitespace().map(String::from).collect();
    let ours = words.iter().any(|word| word == MARKER) && words.iter().any(|word| word == tool.name());
    ours || cli::identify(&words).map(|found| found.0) == Some(tool)
}

/// Whether Zellij holds a pane waiting to start a plain shell: it saves a
/// shell as the pane's command when it is not exactly the default one.
fn holds_shell(pane: &PaneInfo) -> bool {
    let words: Vec<&str> = pane.terminal_command.as_deref().unwrap_or("").split_whitespace().collect();
    pane.is_held && words.first().map_or(false, |program| is_shell(program)) && !words.contains(&"-c")
}

/// Whether a pane is a plain shell with nothing running in it.
fn idle_shell(pane: &PaneInfo) -> bool {
    if pane.is_held || pane.exited || pane.terminal_command.is_some() {
        return false;
    }
    let Ok(argv) = get_pane_running_command(PaneId::Terminal(pane.id)) else { return false };
    argv.first().map_or(false, |program| is_shell(program))
}

fn is_shell(program: &str) -> bool {
    let name = program.rsplit('/').next().unwrap_or(program).trim_start_matches('-');
    SHELLS.contains(&name)
}

/// Terminal panes per tab, in tab order.
fn shape(panes: &HashMap<usize, Vec<PaneInfo>>) -> Vec<usize> {
    let mut tabs: Vec<(usize, usize)> =
        panes.iter().map(|(tab, panes)| (*tab, panes.iter().filter(|pane| !pane.is_plugin && !pane.is_suppressed).count())).collect();
    tabs.sort_unstable();
    tabs.into_iter().map(|(_, count)| count).collect()
}

fn parse_origin(output: &str) -> Origin {
    let mut origin = Origin::default();
    for line in output.lines() {
        if let Some(id) = line.strip_prefix("server ") {
            origin.server = Some(id.trim().to_string()).filter(|id| cli::is_uuid(id));
        } else if line.trim() == "layout" {
            origin.layout = true;
        }
    }
    origin
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

/// What processes have open, lsof -F pfn style ("p<pid>", then "f<fd>" and
/// "n<path>" pairs): the files of each, and the directory each runs in.
#[derive(Default)]
struct OpenFiles {
    files: HashMap<i32, Vec<String>>,
    cwd: HashMap<i32, String>,
}

fn parse_open_files(text: &str) -> OpenFiles {
    let mut open = OpenFiles::default();
    let (mut pid, mut fd) = (0, String::new());
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix('p') {
            pid = rest.parse().unwrap_or(0);
        } else if let Some(rest) = line.strip_prefix('f') {
            fd = rest.to_string();
        } else if let Some(path) = line.strip_prefix('n') {
            if pid <= 0 {
                continue;
            }
            if fd == "cwd" {
                open.cwd.insert(pid, path.to_string());
            } else {
                open.files.entry(pid).or_default().push(path.to_string());
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
    let mut map: Map = serde_json::from_str(&fs::read_to_string(map_path(session)).ok()?).ok()?;
    for entry in map.panes.values_mut() {
        // Only ids this plugin could have written reach a command line.
        entry.session_id = entry.session_id.take().filter(|id| cli::is_uuid(id));
    }
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
    fn directories_zellij_would_remap_are_entered_by_the_command() {
        let args = vec!["--verbose".to_string()];
        let plain = command_in(Tool::Claude, &args, "/home/me/project");
        assert_eq!((plain.path, plain.args, plain.cwd), (PathBuf::from("claude"), args.clone(), Some(PathBuf::from("/home/me/project"))));
        let wrapped = command_in(Tool::Claude, &args, "/tmp/project");
        assert_eq!(wrapped.path, PathBuf::from("/bin/sh"));
        assert_eq!(wrapped.args[2..], [MARKER, "/tmp/project", "claude", "--verbose"].map(String::from));
        assert_eq!(wrapped.cwd, None);
        assert_eq!(command_in(Tool::Codex, &[], "/datastore/x").path, PathBuf::from("codex")); // only whole names
    }

    const SERVER: &str = "3dd72776-f6cd-47d3-9fda-a6409f9b73fd";

    fn terminal(id: u32) -> PaneInfo {
        PaneInfo { id, ..Default::default() }
    }

    fn live(pane: u32) -> Live {
        let running = Running { pane, tool: Tool::Claude, args: vec![], cwd: "/w".into(), pane_pid: 1, command_pane: false };
        Live { running, session_id: Some("11111111-2222-4333-8444-555555555555".into()), exact: true, pids: vec![], first_seen: 0 }
    }

    fn state(origin: Option<Origin>, saved: Option<Map>, tabs: &[u32]) -> State {
        let mut state = State { origin, saved, exit_grace: EXIT_GRACE, ..Default::default() };
        for (tab, count) in tabs.iter().enumerate() {
            state.panes.insert(tab, (0..*count).map(|index| terminal(tab as u32 * 10 + index)).collect());
        }
        state
    }

    fn map(server: Option<&str>, shape: &[usize]) -> Map {
        Map { format: MAP_FORMAT, panes: BTreeMap::new(), server: server.map(String::from), shape: shape.to_vec(), owned_tabs: vec![] }
    }

    #[test]
    fn restores_only_what_another_server_left() {
        let ours = Some(Origin { server: Some(SERVER.into()), layout: true });
        // The plugin restarted in the running session that wrote the map.
        assert!(!state(ours.clone(), Some(map(Some(SERVER), &[1])), &[1]).may_restore());
        // A server before this one, or a map from before servers were recorded.
        assert!(state(ours.clone(), Some(map(Some("another"), &[1])), &[1]).may_restore());
        assert!(state(ours.clone(), Some(map(None, &[])), &[1]).may_restore());
        // Not before knowing, nor without a map.
        assert!(!state(None, Some(map(None, &[1])), &[1]).may_restore());
        assert!(!state(ours, None, &[1]).may_restore());
    }

    #[test]
    fn shells_only_in_a_resurrected_session_laid_out_as_the_map() {
        let resurrected = Some(Origin { server: Some(SERVER.into()), layout: true });
        let created = Some(Origin { server: Some(SERVER.into()), layout: false });
        assert!(state(resurrected.clone(), Some(map(Some("before"), &[2, 1])), &[2, 1]).may_restore_shells());
        assert!(!state(resurrected.clone(), Some(map(Some("before"), &[2, 1])), &[1]).may_restore_shells());
        assert!(!state(resurrected.clone(), Some(map(None, &[])), &[1]).may_restore_shells()); // an older map: no shape
        assert!(!state(created, Some(map(Some("before"), &[1])), &[1]).may_restore_shells()); // a new session, same name
        assert!(!state(resurrected, Some(map(Some(SERVER), &[1])), &[1]).may_restore_shells());
        assert!(!state(Some(Origin::default()), Some(map(Some("before"), &[1])), &[1]).may_restore_shells()); // unknown
    }

    #[test]
    fn an_exited_cli_is_kept_for_a_while_then_dropped() {
        let mut state = state(None, None, &[2]);
        state.set_live(HashMap::from([(0, live(0)), (1, live(1))]));
        state.set_live(HashMap::from([(1, live(1))])); // pane 0's CLI exited
        assert!(state.ended.contains_key(&0) && !state.ended.contains_key(&1));
        state.set_live(HashMap::from([(0, live(0)), (1, live(1))])); // and started again
        assert!(state.ended.is_empty());
        state.set_live(HashMap::new());
        assert_eq!(state.ended.len(), 2);
        state.panes.get_mut(&0).unwrap().retain(|pane| pane.id != 1); // pane 1 closed
        state.set_live(HashMap::new());
        assert_eq!(state.ended.keys().copied().collect::<Vec<_>>(), vec![0]);
        state.exit_grace = Duration::ZERO; // time is up
        state.set_live(HashMap::new());
        assert!(state.ended.is_empty());
    }

    #[test]
    fn origin_and_shells() {
        assert_eq!(parse_origin(&format!("layout\nserver {SERVER}\n")), Origin { server: Some(SERVER.into()), layout: true });
        assert_eq!(parse_origin("server ../../etc\n"), Origin::default());
        assert_eq!(parse_origin(""), Origin::default());
        assert!(is_shell("-zsh") && is_shell("/bin/bash") && is_shell("fish"));
        assert!(!is_shell("claude") && !is_shell("/usr/bin/vim") && !is_shell("zsh-helper"));
        let held = |command: &str| PaneInfo { id: 1, is_held: true, terminal_command: Some(command.into()), ..Default::default() };
        assert!(holds_shell(&held("/bin/bash --norc")) && holds_shell(&held("-zsh")));
        assert!(!holds_shell(&held("/bin/sh -c make watch")) && !holds_shell(&held("claude --resume x")));
        assert!(!holds_shell(&PaneInfo { is_held: false, ..held("/bin/bash") }));
        let mut panes = HashMap::new();
        panes.insert(1, vec![terminal(3), PaneInfo { id: 9, is_plugin: true, ..Default::default() }]);
        panes.insert(0, vec![terminal(1), terminal(2)]);
        assert_eq!(shape(&panes), vec![2, 1]);
        // Maps written before the server and shape were recorded still load.
        let old: Map = serde_json::from_str(r#"{"format": 1, "panes": {}}"#).unwrap();
        assert_eq!((old.server, old.shape), (None, vec![]));
    }

    #[test]
    fn tabs_named_by_default_with_one_pane() {
        let tab = |name: &str| TabInfo { name: name.into(), ..Default::default() };
        let bar = PaneInfo { id: 5, is_plugin: true, is_selectable: false, ..Default::default() };
        let one = vec![terminal(3), bar.clone()];
        assert_eq!(lone_pane(&tab("Tab #3"), &one), Some(3));
        assert_eq!(lone_pane(&tab("Tab #12"), &one), Some(3));
        assert_eq!(lone_pane(&tab("work"), &one), None); // named by the user
        assert_eq!(lone_pane(&tab("Tab #"), &one), None);
        assert_eq!(lone_pane(&tab("Tab #3"), &[terminal(3), terminal(4), bar.clone()]), None); // a split
        let floating = PaneInfo { is_floating: true, ..terminal(4) };
        assert_eq!(lone_pane(&tab("Tab #3"), &[terminal(3), floating]), None);
        let grant = PaneInfo { id: 6, is_plugin: true, is_selectable: true, is_floating: true, ..Default::default() };
        assert_eq!(lone_pane(&tab("Tab #3"), &[terminal(3), grant]), None);
        let background = PaneInfo { id: 0, is_plugin: true, is_selectable: true, is_suppressed: true, ..Default::default() };
        assert_eq!(lone_pane(&tab("Tab #1"), &[terminal(3), bar, background]), Some(3));
    }

    #[test]
    fn tab_names_and_marks() {
        assert!(is_default_tab_name("Tab #3") && is_default_tab_name("Tab #14"));
        assert!(!is_default_tab_name("Tab #") && !is_default_tab_name("work") && !is_default_tab_name("Tab #3 x"));
        assert_eq!(strip_mark("◐ Fixing bugs"), "Fixing bugs");
        assert_eq!(strip_mark("● Fixing bugs"), "Fixing bugs");
        assert_eq!(strip_mark("✓ Fixing bugs"), "Fixing bugs");
        assert_eq!(strip_mark("Fixing bugs"), "Fixing bugs");
        assert_eq!((ago(5), ago(125), ago(7300), ago(200000)), ("5s".into(), "2m".into(), "2h".into(), "2d".into()));
        assert_eq!(rows_path("llm"), PathBuf::from("/cache/agents-llm.json"));
    }

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
        let open = parse_open_files("p12\nfcwd\nn/work\nf3\nn/a\nf4\nn/b\np13\nf1\nn/c\n");
        assert_eq!(open.files[&12], vec!["/a", "/b"]);
        assert_eq!(open.cwd[&12], "/work");
        assert!(!open.cwd.contains_key(&13));
        let sections = split_sections("1 0\n--claude--\n{}\n--files--\np1\n");
        assert_eq!(sections[""], "1 0\n");
        assert_eq!(sections["files"], "p1\n");
    }
}
