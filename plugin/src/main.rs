//! Proof of concept: a background Zellij plugin that brings Claude Code panes
//! back on their conversation after a session is resurrected, with the CLI
//! started as plain `claude` (no wrapper).
//!
//! While the session runs it maps each pane running `claude` to the
//! conversation it is on (Claude Code's ~/.claude/sessions/<pid>.json, found
//! through the pane's process tree) and keeps that map in /cache, which
//! survives the server. Panes are identified by position: tab, then order by
//! (floating, y, x), which a resurrected session reproduces. After every
//! change it asks Zellij to save the session, so the saved layout and the map
//! describe the same moment. When the session is resurrected, each restored
//! `claude` pane is replaced in place by `claude --resume <id> <its options>`
//! in the directory the conversation ran in.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use zellij_tile::prelude::*;

const TICK_SECONDS: f64 = 2.0;
const RESTORE_TICKS: u32 = 10; // how long restored panes are looked for after loading

/// What runs in one pane, enough to start it again.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct Tracked {
    tool: String,
    args: Vec<String>,
    cwd: String,
    session_id: String,
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct Saved {
    panes: BTreeMap<String, Tracked>, // "<tab>:<index>" -> what runs there
}

#[derive(Default)]
struct State {
    granted: bool,
    session: Option<String>,
    panes: HashMap<usize, Vec<PaneInfo>>,
    tracked: HashMap<u32, Tracked>,
    restore_from: Option<Saved>,
    replaced: HashSet<String>,
    ticks: u32,
    written: Option<String>,
    probing: Vec<(u32, Vec<String>, i32)>, // panes running claude: (pane id, argv, pane pid)
}

register_plugin!(State);

impl ZellijPlugin for State {
    fn load(&mut self, _configuration: BTreeMap<String, String>) {
        request_permission(&[PermissionType::ReadApplicationState, PermissionType::RunCommands]);
        subscribe(&[
            EventType::PermissionRequestResult,
            EventType::ModeUpdate,
            EventType::PaneUpdate,
            EventType::Timer,
            EventType::RunCommandResult,
            EventType::SessionUpdate,
        ]);
        set_timeout(1.0);
    }

    fn update(&mut self, event: Event) -> bool {
        match event {
            Event::PermissionRequestResult(status) => {
                self.granted = status == PermissionStatus::Granted;
                eprintln!("zjr: permissions {:?}", status);
            }
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
            Event::Timer(_) => {
                self.tick();
                set_timeout(TICK_SECONDS);
            }
            Event::RunCommandResult(code, stdout, stderr, context) => {
                if context.get("kind").map(String::as_str) == Some("probe") {
                    if code == Some(0) {
                        self.on_probe(&String::from_utf8_lossy(&stdout));
                    } else {
                        eprintln!("zjr: probe failed: {}", String::from_utf8_lossy(&stderr));
                    }
                }
            }
            _ => {}
        }
        false
    }
}

impl State {
    fn tick(&mut self) {
        // Background plugins get no ModeUpdate, so the session name comes from SessionUpdate.
        let Some(session) = self.session.clone() else { return };
        if !self.granted || self.panes.is_empty() {
            return;
        }
        self.ticks += 1;
        if self.ticks == 1 {
            self.restore_from = read_saved(&session);
            eprintln!("zjr: session {session}, saved panes: {}",
                      self.restore_from.as_ref().map_or(0, |saved| saved.panes.len()));
        }
        if self.ticks <= RESTORE_TICKS {
            self.restore();
        }
        self.probe();
    }

    /// Terminal panes keyed by position: tab, then (floating, y, x).
    fn positions(&self) -> Vec<(String, PaneInfo)> {
        let mut out = Vec::new();
        for (tab, panes) in &self.panes {
            let mut terminals: Vec<&PaneInfo> =
                panes.iter().filter(|pane| !pane.is_plugin && !pane.is_suppressed).collect();
            terminals.sort_by_key(|pane| (pane.is_floating, pane.pane_y, pane.pane_x));
            for (index, pane) in terminals.into_iter().enumerate() {
                out.push((format!("{tab}:{index}"), pane.clone()));
            }
        }
        out
    }

    /// Replace each restored (held) claude pane with one resuming its conversation.
    fn restore(&mut self) {
        let Some(saved) = &self.restore_from else { return };
        for (key, pane) in self.positions() {
            if !pane.is_held || self.replaced.contains(&key) {
                continue;
            }
            let Some(entry) = saved.panes.get(&key) else { continue };
            let command = pane.terminal_command.clone().unwrap_or_default();
            if identify(&command.split_whitespace().map(String::from).collect::<Vec<_>>()).map(|found| found.0).as_deref()
                != Some(entry.tool.as_str())
            {
                eprintln!("zjr: {key} now runs {command:?}, not {}: left alone", entry.tool);
                continue;
            }
            let mut args = vec!["--resume".to_string(), entry.session_id.clone()];
            args.extend(resume_options(&entry.args));
            let cwd = PathBuf::from(&entry.cwd);
            let command_to_run = CommandToRun { path: PathBuf::from(&entry.tool), args: args.clone(), cwd: Some(cwd) };
            let new = open_command_pane_in_place_of_pane_id(PaneId::Terminal(pane.id), command_to_run, true, BTreeMap::new());
            eprintln!("zjr: {key} (pane {}) -> {} {:?} in {} => {:?}", pane.id, entry.tool, args, entry.cwd, new);
            self.replaced.insert(key);
        }
    }

    /// Find the panes running claude, then (in one host command) the process
    /// table and Claude Code's session registry.
    fn probe(&mut self) {
        self.probing.clear();
        for (_, pane) in self.positions() {
            if pane.is_held || pane.exited {
                continue;
            }
            let id = PaneId::Terminal(pane.id);
            let Ok(argv) = get_pane_running_command(id) else { continue };
            if identify(&argv).is_none() {
                continue;
            }
            if let Ok(pid) = get_pane_pid(id) {
                self.probing.push((pane.id, argv, pid));
            }
        }
        if self.probing.is_empty() {
            self.tracked.clear();
            self.persist();
            return;
        }
        let script = r#"ps -A -o pid=,ppid=; echo '--registry--'
for f in "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/sessions/*.json; do [ -f "$f" ] && cat "$f" && echo; done"#;
        let mut context = BTreeMap::new();
        context.insert("kind".to_string(), "probe".to_string());
        run_command(&["/bin/sh", "-c", script], context);
    }

    fn on_probe(&mut self, output: &str) {
        let (table, registry) = output.split_once("--registry--").unwrap_or((output, ""));
        let mut parent: HashMap<i32, i32> = HashMap::new();
        for line in table.lines() {
            let mut fields = line.split_whitespace();
            if let (Some(Ok(pid)), Some(Ok(ppid))) = (fields.next().map(str::parse), fields.next().map(str::parse)) {
                parent.insert(pid, ppid);
            }
        }
        let entries: Vec<Value> = serde_json::Deserializer::from_str(registry).into_iter::<Value>().flatten().collect();
        let mut tracked = HashMap::new();
        for (pane_id, argv, pane_pid) in &self.probing {
            // The registry entry of a live process at or below the pane's process.
            let found = entries.iter().find(|entry| {
                let Some(pid) = entry.get("pid").and_then(Value::as_i64).map(|pid| pid as i32) else { return false };
                parent.contains_key(&pid) && descends_from(pid, *pane_pid, &parent)
            });
            let Some(entry) = found else { continue };
            let (Some(session_id), Some(cwd)) =
                (entry.get("sessionId").and_then(Value::as_str), entry.get("cwd").and_then(Value::as_str))
            else {
                continue;
            };
            tracked.insert(*pane_id, Tracked {
                tool: "claude".to_string(),
                args: argv.iter().skip(identify(argv).map_or(1, |found| found.1)).cloned().collect(),
                cwd: cwd.to_string(),
                session_id: session_id.to_string(),
            });
        }
        self.tracked = tracked;
        self.persist();
    }

    /// Write the position -> conversation map, and have Zellij save the layout
    /// with it. Not before the restore window ends: the saved map is still needed.
    fn persist(&mut self) {
        if self.ticks <= RESTORE_TICKS && self.restore_from.as_ref().map_or(false, |saved| !saved.panes.is_empty()) {
            return;
        }
        let Some(session) = self.session.clone() else { return };
        let mut saved = Saved::default();
        for (key, pane) in self.positions() {
            if let Some(entry) = self.tracked.get(&pane.id) {
                saved.panes.insert(key, entry.clone());
            }
        }
        let Ok(text) = serde_json::to_string_pretty(&saved) else { return };
        if self.written.as_deref() == Some(text.as_str()) {
            return;
        }
        let path = saved_path(&session);
        let temporary = path.with_extension("tmp");
        match fs::write(&temporary, &text).and_then(|_| fs::rename(&temporary, &path)) {
            Ok(()) => {
                eprintln!("zjr: saved {} pane(s)", saved.panes.len());
                self.written = Some(text);
                let _ = save_session();
            }
            Err(error) => eprintln!("zjr: cannot write {}: {error}", path.display()),
        }
    }
}

fn descends_from(mut pid: i32, ancestor: i32, parent: &HashMap<i32, i32>) -> bool {
    for _ in 0..32 {
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

/// ("claude", where its own arguments start) for Claude Code's argv: the
/// native binary, or a script run by an interpreter (npm's node cli.js, shims).
fn identify(argv: &[String]) -> Option<(String, usize)> {
    let base = |word: &String| word.rsplit('/').next().unwrap_or("").to_string();
    let first = argv.first().map(base)?;
    if first == "claude" || first == "claude.exe" {
        return Some(("claude".to_string(), 1));
    }
    let interpreter = first.starts_with("python") || ["Python", "node", "bun", "deno", "sh", "bash"].contains(&first.as_str());
    let script = argv.get(1)?;
    if interpreter && (base(script) == "claude" || script.contains("/claude-code/")) {
        return Some(("claude".to_string(), 2));
    }
    None
}

/// The options to repeat on resume: settings only, never a prompt (the same
/// rules as the zclaude launcher, abridged for the proof of concept).
fn resume_options(args: &[String]) -> Vec<String> {
    const VALUE: &[&str] = &["--model", "--agent", "--agents", "--effort", "--fallback-model", "--permission-mode",
        "--settings", "--setting-sources", "--append-system-prompt", "--system-prompt", "--plugin-dir",
        "--debug-file", "--session-id", "-n", "--name"];
    const MANY: &[&str] = &["--add-dir", "--allowedTools", "--allowed-tools", "--disallowedTools",
        "--disallowed-tools", "--mcp-config", "--tools", "--betas", "--file"];
    const OPTIONAL: &[&str] = &["-r", "--resume", "-d", "--debug", "-w", "--worktree", "--from-pr"];
    const DROP: &[&str] = &["--session-id", "-r", "--resume", "-c", "--continue", "--fork-session", "--from-pr",
        "-n", "--name", "-w", "--worktree", "--file"];
    let mut kept = Vec::new();
    let mut index = 0;
    while index < args.len() {
        let word = &args[index];
        if word == "--" || !word.starts_with('-') || word.len() < 2 {
            break; // a prompt (and anything after --) is never repeated
        }
        let name = word.split('=').next().unwrap_or(word);
        let mut end = index + 1;
        if !word.contains('=') {
            if VALUE.contains(&name) {
                end = (end + 1).min(args.len());
            } else if MANY.contains(&name) {
                while end < args.len() && !args[end].starts_with('-') {
                    end += 1;
                }
            } else if OPTIONAL.contains(&name) && end < args.len() && !args[end].starts_with('-') {
                end += 1;
            }
        }
        if !DROP.contains(&name) {
            kept.extend(args[index..end].iter().cloned());
        }
        index = end;
    }
    kept
}

fn saved_path(session: &str) -> PathBuf {
    let safe: String = session.chars().map(|c| if c.is_ascii_alphanumeric() || "-_.".contains(c) { c } else { '_' }).collect();
    PathBuf::from("/cache").join(format!("session-{safe}.json"))
}

fn read_saved(session: &str) -> Option<Saved> {
    serde_json::from_str(&fs::read_to_string(saved_path(session)).ok()?).ok()
}
