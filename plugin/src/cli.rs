//! What runs in a pane, and how to start it again: recognising Claude Code and
//! Codex from a process's argv, and building the command that resumes (or
//! forks, or freshly restarts) a conversation with the options it ran with.
//!
//! Only options known to be settings are repeated. A positional prompt would
//! be sent to the resumed conversation a second time, so it, one-shot options
//! and anything unrecognised are left out. The option tables mirror the CLIs'
//! own parsers (commander for Claude Code, clap for Codex).

use std::collections::HashSet;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Tool {
    Claude,
    Codex,
}

impl Tool {
    pub fn name(self) -> &'static str {
        match self {
            Tool::Claude => "claude",
            Tool::Codex => "codex",
        }
    }

    pub fn from_name(name: &str) -> Option<Tool> {
        match name {
            "claude" => Some(Tool::Claude),
            "codex" => Some(Tool::Codex),
            _ => None,
        }
    }
}

fn basename(word: &str) -> &str {
    word.rsplit('/').next().unwrap_or(word)
}

/// The tool an argv runs and the index of its own first argument: the native
/// binary, or a script run by an interpreter (npm's node entry points, shims).
pub fn identify(argv: &[String]) -> Option<(Tool, usize)> {
    let first = basename(argv.first()?);
    match first {
        "claude" | "claude.exe" => return Some((Tool::Claude, 1)),
        "codex" | "codex.exe" => return Some((Tool::Codex, 1)),
        _ => {}
    }
    let interpreter = first.starts_with("python") || ["Python", "node", "bun", "deno", "sh", "bash"].contains(&first);
    let script = argv.get(1)?;
    if !interpreter {
        return None;
    }
    let name = basename(script);
    if name == "claude" || script.contains("/claude-code/") {
        return Some((Tool::Claude, 2));
    }
    if name == "codex" || name == "codex.js" || script.contains("/@openai/codex/") {
        return Some((Tool::Codex, 2));
    }
    None
}

pub fn is_uuid(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 36
        && bytes.iter().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => *byte == b'-',
            _ => byte.is_ascii_hexdigit(),
        })
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Arity {
    Flag,
    Value,
    Optional,
    Many,
}
use Arity::*;

const CLAUDE_OPTIONS: &[(&str, Arity)] = &[
    ("--add-dir", Many), ("--agent", Value), ("--agents", Value), ("--allow-dangerously-skip-permissions", Flag),
    ("--allowedTools", Many), ("--allowed-tools", Many), ("--append-system-prompt", Value),
    ("--append-system-prompt-file", Value), ("--autocompact", Value), ("--ax-screen-reader", Flag), ("--bg", Flag),
    ("--background", Flag), ("--bare", Flag), ("--betas", Many), ("--brief", Flag), ("--chrome", Flag),
    ("--no-chrome", Flag), ("--cloud", Optional), ("-c", Flag), ("--continue", Flag),
    ("--dangerously-skip-permissions", Flag), ("-d", Optional), ("--debug", Optional), ("--debug-file", Value),
    ("--desktop", Flag), ("--disable-slash-commands", Flag), ("--disallowedTools", Many), ("--disallowed-tools", Many),
    ("--effort", Value), ("--environment", Value), ("--exclude-dynamic-system-prompt-sections", Flag),
    ("--fallback-model", Value), ("--file", Many), ("--fork-session", Flag), ("--forward-subagent-text", Flag),
    ("--from-pr", Optional), ("-h", Flag), ("--help", Flag), ("--ide", Flag), ("--include-hook-events", Flag),
    ("--include-partial-messages", Flag), ("--input-format", Value), ("--json-schema", Value),
    ("--max-budget-usd", Value), ("--max-turns", Value), ("--mcp-config", Many), ("--mcp-debug", Flag),
    ("--model", Value), ("-n", Value), ("--name", Value), ("--no-session-persistence", Flag),
    ("--output-format", Value), ("--permission-mode", Value), ("--permission-prompt-tool", Value),
    ("--permission-prompts", Value), ("--plugin-dir", Value), ("--plugin-url", Value), ("-p", Flag), ("--print", Flag),
    ("--prompt-suggestions", Optional), ("--remote-control", Optional), ("--remote-control-session-name-prefix", Value),
    ("--replay-user-messages", Flag), ("--restricted", Flag), ("-r", Optional), ("--resume", Optional),
    ("--safe-mode", Flag), ("--session-id", Value), ("--setting-sources", Value), ("--settings", Value),
    ("--strict-mcp-config", Flag), ("--system-prompt", Value), ("--system-prompt-file", Value),
    ("--system-prompt-snapshot", Value), ("--teleport", Optional), ("--tmux", Flag), ("--tools", Many),
    ("--verbose", Flag), ("-v", Flag), ("--version", Flag), ("-w", Optional), ("--worktree", Optional),
];
/// Options that pick or create the conversation, or are one-shot: never repeated.
const CLAUDE_DROP: &[&str] = &[
    "--session-id", "-r", "--resume", "-c", "--continue", "--from-pr", "--fork-session", "-n", "--name", "-w",
    "--worktree", "--tmux", "--file",
];
/// Not an interactive session in this terminal.
const CLAUDE_NOT_INTERACTIVE: &[&str] = &[
    "-p", "--print", "-h", "--help", "-v", "--version", "--bg", "--background", "--cloud", "--teleport", "--desktop",
    "--environment",
];
const CLAUDE_COMMANDS: &[&str] = &[
    "agents", "attach", "auth", "auto-mode", "config", "doctor", "gateway", "help", "import", "install", "kill", "logs",
    "mcp", "migrate-installer", "plugin", "plugins", "purge", "respawn", "rm", "setup-token", "stop", "ultrareview",
    "update", "upgrade",
];

const CODEX_OPTIONS: &[(&str, Arity)] = &[
    ("-c", Value), ("--config", Value), ("--enable", Value), ("--disable", Value), ("--remote", Value),
    ("--remote-auth-token-env", Value), ("--strict-config", Flag), ("-i", Many), ("--image", Many), ("-m", Value),
    ("--model", Value), ("--oss", Flag), ("--local-provider", Value), ("-p", Value), ("--profile", Value),
    ("-s", Value), ("--sandbox", Value), ("--approve-for-me", Flag),
    ("--dangerously-bypass-approvals-and-sandbox", Flag), ("--yolo", Flag), ("--full-auto", Flag),
    ("--dangerously-bypass-hook-trust", Flag), ("-C", Value), ("--cd", Value), ("--worktree", Flag),
    ("--add-dir", Value), ("-a", Value), ("--ask-for-approval", Value), ("--search", Flag), ("--no-alt-screen", Flag),
    ("--no-daemon", Flag), ("-h", Flag), ("--help", Flag), ("-V", Flag), ("--version", Flag), ("--last", Flag),
    ("--all", Flag), ("--include-non-interactive", Flag),
];
const CODEX_DROP: &[&str] = &["-i", "--image", "--worktree", "--last", "--all", "--include-non-interactive"];
const CODEX_NOT_INTERACTIVE: &[&str] = &["-h", "--help", "-V", "--version", "--remote"];
const CODEX_COMMANDS: &[&str] = &[
    "a", "agents", "app", "app-server", "apply", "archive", "cloud", "completion", "debug", "delete", "doctor", "e",
    "exec", "exec-server", "features", "generate-ts", "help", "login", "logout", "mcp", "mcp-server",
    "migrate-nfs-db", "migrate-rollouts", "plugin", "queue", "remote-control", "responses-api-proxy", "review",
    "sandbox", "unarchive", "update",
];

#[derive(Debug, PartialEq)]
enum Kind {
    Option,
    Unknown,
    Positional,
    Separator,
}

#[derive(Debug)]
struct Word {
    kind: Kind,
    name: String,
    words: Vec<String>,
}

fn is_option(word: &str) -> bool {
    word.len() > 1 && word.starts_with('-')
}

fn arity(table: &[(&str, Arity)], name: &str) -> Option<Arity> {
    table.iter().find(|(option, _)| *option == name).map(|(_, arity)| *arity)
}

fn tokenize(args: &[String], table: &[(&str, Arity)]) -> Vec<Word> {
    let mut items = Vec::new();
    let mut index = 0;
    while index < args.len() {
        let word = &args[index];
        if word == "--" {
            items.push(Word { kind: Kind::Separator, name: String::new(), words: vec![word.clone()] });
            for rest in &args[index + 1..] {
                items.push(Word { kind: Kind::Positional, name: String::new(), words: vec![rest.clone()] });
            }
            break;
        }
        if !is_option(word) {
            items.push(Word { kind: Kind::Positional, name: String::new(), words: vec![word.clone()] });
            index += 1;
            continue;
        }
        let (name, has_value) = match word.split_once('=') {
            Some((name, _)) => (name.to_string(), true),
            None => (word.clone(), false),
        };
        let found = arity(table, &name);
        if found.is_none() && !word.starts_with("--") && word.len() > 2 {
            if let Some(short) = arity(table, &word[..2]) {
                if short != Flag {
                    // A short option with its value attached: -mo3, -m=o3.
                    items.push(Word { kind: Kind::Option, name: word[..2].to_string(), words: vec![word.clone()] });
                    index += 1;
                    continue;
                }
            }
        }
        let mut end = index + 1;
        let kind = match found {
            None => {
                // Not an option we know: whether it takes a value cannot be told
                // apart from a prompt, so it owns the words up to the next option.
                if !has_value {
                    while end < args.len() && !is_option(&args[end]) {
                        end += 1;
                    }
                }
                Kind::Unknown
            }
            Some(arity) => {
                if !has_value {
                    match arity {
                        Value => end = (end + 1).min(args.len()),
                        Optional if end < args.len() && !is_option(&args[end]) => end += 1,
                        Many => {
                            while end < args.len() && !is_option(&args[end]) {
                                end += 1;
                            }
                        }
                        _ => {}
                    }
                }
                Kind::Option
            }
        };
        items.push(Word { kind, name, words: args[index..end].to_vec() });
        index = end;
    }
    items
}

fn tables(tool: Tool) -> (&'static [(&'static str, Arity)], &'static [&'static str], &'static [&'static str], &'static [&'static str]) {
    match tool {
        Tool::Claude => (CLAUDE_OPTIONS, CLAUDE_DROP, CLAUDE_NOT_INTERACTIVE, CLAUDE_COMMANDS),
        Tool::Codex => (CODEX_OPTIONS, CODEX_DROP, CODEX_NOT_INTERACTIVE, CODEX_COMMANDS),
    }
}

fn positionals(items: &[Word]) -> Vec<&str> {
    items
        .iter()
        .take_while(|item| item.kind != Kind::Separator)
        .filter(|item| item.kind == Kind::Positional)
        .map(|item| item.words[0].as_str())
        .collect()
}

/// Whether these arguments start an interactive session in the terminal
/// (not `-p`, `--version`, a subcommand such as `mcp` or `codex exec`, ...).
pub fn is_interactive(tool: Tool, args: &[String]) -> bool {
    let (table, _, not_interactive, commands) = tables(tool);
    let items = tokenize(args, table);
    if items.iter().any(|item| item.kind == Kind::Option && not_interactive.contains(&item.name.as_str())) {
        return false;
    }
    !positionals(&items).first().map_or(false, |command| commands.contains(command))
}

/// The options to repeat when starting the conversation again.
pub fn kept_options(tool: Tool, args: &[String]) -> Vec<String> {
    let (table, drop, _, _) = tables(tool);
    let drop: HashSet<&str> = drop.iter().copied().collect();
    tokenize(args, table)
        .into_iter()
        .filter(|item| item.kind == Kind::Option && !drop.contains(item.name.as_str()))
        .flat_map(|item| item.words)
        .collect()
}

/// A conversation the arguments name explicitly (`--resume <id>`,
/// `--session-id <id>`, `codex resume <id>`).
pub fn named_session(tool: Tool, args: &[String]) -> Option<String> {
    let (table, _, _, _) = tables(tool);
    let items = tokenize(args, table);
    match tool {
        Tool::Claude => items.iter().find_map(|item| {
            let value = if item.words.len() > 1 { item.words[1].clone() } else { item.words[0].split_once('=')?.1.to_string() };
            (item.kind == Kind::Option && ["--session-id", "-r", "--resume"].contains(&item.name.as_str()) && is_uuid(&value))
                .then_some(value)
        }),
        Tool::Codex => {
            let words = positionals(&items);
            (words.first() == Some(&"resume")).then(|| words.get(1).copied()).flatten()
                .filter(|word| is_uuid(word)).map(String::from)
        }
    }
}

/// The argv (after the program) that continues `session`; `fork` continues
/// in a copy instead, when the conversation is open somewhere else.
pub fn resume_args(tool: Tool, args: &[String], session: &str, fork: bool) -> Vec<String> {
    let kept = kept_options(tool, args);
    match tool {
        Tool::Claude => {
            let mut out = vec!["--resume".to_string(), session.to_string()];
            if fork {
                out.push("--fork-session".to_string());
            }
            out.extend(kept);
            out
        }
        Tool::Codex => {
            let mut out = vec![if fork { "fork" } else { "resume" }.to_string()];
            out.extend(kept);
            out.push(session.to_string());
            out
        }
    }
}

/// A new conversation with the same options (the old one is gone).
pub fn fresh_args(tool: Tool, args: &[String]) -> Vec<String> {
    kept_options(tool, args)
}

/// A word quoted for a POSIX shell.
pub fn shell_quote(word: &str) -> String {
    if !word.is_empty() && word.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"-_./=:@%+,".contains(&byte)) {
        word.to_string()
    } else {
        format!("'{}'", word.replace('\'', "'\\''"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(words: &[&str]) -> Vec<String> {
        words.iter().map(|word| word.to_string()).collect()
    }

    const ID: &str = "11111111-2222-4333-8444-555555555555";

    #[test]
    fn identifies_binaries_and_scripts() {
        assert_eq!(identify(&args(&["/opt/mise/claude-code/bin/claude.exe", "--verbose"])), Some((Tool::Claude, 1)));
        assert_eq!(identify(&args(&["node", "/x/node_modules/@anthropic-ai/claude-code/cli.js"])), Some((Tool::Claude, 2)));
        assert_eq!(identify(&args(&["/x/Python", "/tmp/fakes/claude", "-p"])), Some((Tool::Claude, 2)));
        assert_eq!(identify(&args(&["/x/vendor/aarch64-apple-darwin/bin/codex", "-m", "o3"])), Some((Tool::Codex, 1)));
        assert_eq!(identify(&args(&["node", "/x/@openai/codex/bin/codex.js"])), Some((Tool::Codex, 2)));
        assert_eq!(identify(&args(&["/bin/zsh"])), None);
        assert_eq!(identify(&args(&["vim", "claude"])), None);
    }

    #[test]
    fn prompts_and_one_shot_options_are_not_repeated() {
        let ran = args(&["--dangerously-skip-permissions", "--model", "opus", "--name", "x", "fix the bug"]);
        assert_eq!(resume_args(Tool::Claude, &ran, ID, false), args(&["--resume", ID, "--dangerously-skip-permissions", "--model", "opus"]));
        assert_eq!(resume_args(Tool::Claude, &ran, ID, true)[..3], args(&["--resume", ID, "--fork-session"])[..]);
        assert_eq!(fresh_args(Tool::Claude, &args(&["--add-dir", "a", "b", "--brand-new", "v", "--", "-x"])), args(&["--add-dir", "a", "b"]));
        assert_eq!(fresh_args(Tool::Claude, &args(&["--model=opus", "-c", "--resume", "named"])), args(&["--model=opus"]));
    }

    #[test]
    fn codex_resume_and_fork() {
        let ran = args(&["-m", "o3", "-C", "sub", "-i", "a.png", "b.png", "write tests"]);
        assert_eq!(resume_args(Tool::Codex, &ran, ID, false), args(&["resume", "-m", "o3", "-C", "sub", ID]));
        assert_eq!(resume_args(Tool::Codex, &args(&["resume", "--last", "-mo3"]), ID, true), args(&["fork", "-mo3", ID]));
    }

    #[test]
    fn interactive_or_not() {
        assert!(is_interactive(Tool::Claude, &args(&["--verbose", "update the docs"])));
        assert!(!is_interactive(Tool::Claude, &args(&["-p", "hi"])));
        assert!(!is_interactive(Tool::Claude, &args(&["--model", "opus", "mcp", "list"])));
        assert!(is_interactive(Tool::Codex, &args(&["resume", "--last"])));
        assert!(!is_interactive(Tool::Codex, &args(&["exec", "ls"])));
    }

    #[test]
    fn named_sessions() {
        assert_eq!(named_session(Tool::Claude, &args(&["--resume", ID, "--verbose"])), Some(ID.to_string()));
        assert_eq!(named_session(Tool::Claude, &args(&[&format!("--session-id={ID}")])), Some(ID.to_string()));
        assert_eq!(named_session(Tool::Claude, &args(&["--resume", "named"])), None);
        assert_eq!(named_session(Tool::Codex, &args(&["resume", "-m", "o3", ID])), Some(ID.to_string()));
        assert_eq!(named_session(Tool::Codex, &args(&["fork", ID])), None);
    }

    #[test]
    fn quoting() {
        assert_eq!(shell_quote("--model=opus"), "--model=opus");
        assert_eq!(shell_quote("it's here"), "'it'\\''s here'");
        assert_eq!(shell_quote(""), "''");
        assert!(is_uuid(ID) && !is_uuid("x") && !is_uuid(&ID.replace('-', "_")));
    }
}
