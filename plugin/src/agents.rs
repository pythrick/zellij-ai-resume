//! What an agent CLI is doing, read from its pane's title and the bottom of
//! its screen: the signs the CLIs draw for people.
//!
//! - Claude Code (2.1.3xx) keeps "✳ <conversation>" as its title whatever it
//!   does (older versions put a spinner there while working). While it works,
//!   its status line says "esc to interrupt"; a permission or a question ends
//!   with "Esc to cancel" under numbered choices.
//! - Codex puts a braille spinner before its title while it works, and
//!   "[ ! ] Action Required" while it waits for an answer.
//!
//! The patterns follow Herdr's agent detection manifests
//! (https://github.com/herdrdev/herdr, Apache-2.0), reduced to plain text
//! matching on the last lines of the screen.

use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::cli::Tool;

/// A turn counts as finished once the CLI has been idle this long (so a
/// pause between two steps of one turn does not).
pub const DONE_AFTER: Duration = Duration::from_secs(2);

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Activity {
    /// Waiting for a new prompt.
    Idle,
    /// Thinking or running tools.
    Working,
    /// Waiting for the user: a permission, a question, a choice.
    Blocked,
    /// Finished a turn the user has not looked at yet (never detected, only
    /// derived from working followed by idle).
    Done,
}

impl Activity {
    /// The mark a tab name starts with.
    pub fn glyph(self) -> &'static str {
        match self {
            Activity::Idle => "",
            Activity::Working => "◐ ",
            Activity::Blocked => "● ",
            Activity::Done => "✓ ",
        }
    }

    pub fn describe(self) -> &'static str {
        match self {
            Activity::Idle => "idle",
            Activity::Working => "working",
            Activity::Blocked => "needs you",
            Activity::Done => "finished",
        }
    }

    /// Which comes first when several agents compete for attention.
    pub fn urgency(self) -> u8 {
        match self {
            Activity::Blocked => 3,
            Activity::Done => 2,
            Activity::Working => 1,
            Activity::Idle => 0,
        }
    }
}

/// How many lines from the bottom of the screen are read.
const BOTTOM: usize = 25;

fn is_braille(c: char) -> bool {
    ('\u{2800}'..='\u{28FF}').contains(&c)
}

/// The last non-empty lines of a screen, lowercased.
fn bottom(screen: &[String], count: usize) -> Vec<String> {
    let mut lines: Vec<String> =
        screen.iter().rev().map(|line| line.trim_end()).filter(|line| !line.trim().is_empty()).take(count).map(str::to_lowercase).collect();
    lines.reverse();
    lines
}

/// A numbered choice such as "❯ 1. Yes" or "  1. Yes, allow once".
fn first_choice_is_yes(lines: &[String]) -> bool {
    lines.iter().any(|line| line.trim_start().trim_start_matches(['❯', '›', '>']).trim_start().starts_with("1. yes"))
}

fn any(lines: &[String], needle: &str) -> bool {
    lines.iter().any(|line| line.contains(needle))
}

/// What the CLI in a pane is doing now (never `Done`, see `Activity`).
pub fn detect(tool: Tool, title: &str, screen: &[String]) -> Activity {
    let lines = bottom(screen, BOTTOM);
    match tool {
        Tool::Claude => {
            let mut first = title.chars();
            let spinner = first.next().map_or(false, |c| is_braille(c) || ('\u{25D0}'..='\u{25D3}').contains(&c)) && first.next() == Some(' ');
            let dialog = any(&lines, "esc to cancel")
                && (first_choice_is_yes(&lines) || any(&lines, "enter to confirm") || (any(&lines, "enter to select") && any(&lines, "navigate")));
            if dialog || any(&lines, "waiting for permission") || any(&lines, "do you want to allow this connection?") {
                Activity::Blocked
            } else if spinner || any(&lines[lines.len().saturating_sub(12)..], "esc to interrupt") {
                Activity::Working
            } else {
                Activity::Idle
            }
        }
        Tool::Codex => {
            let spinner = title.split_whitespace().any(|word| word.chars().count() == 1 && word.chars().all(is_braille));
            let update = any(&lines, "update available") && (any(&lines, "update now") || any(&lines, "esc skip"));
            let asks = ["press enter to confirm or esc to cancel", "allow command?", "enter to submit answer", "enter to submit all", "[y/n]",
                        "do you trust the contents of this directory?", "trust this folder?"];
            if title.contains("Action Required") || update || asks.iter().any(|needle| any(&lines, needle)) {
                Activity::Blocked
            } else if spinner || any(&lines[lines.len().saturating_sub(12)..], "esc to interrupt") {
                Activity::Working
            } else {
                Activity::Idle
            }
        }
    }
}

/// What an agent does next, from what it did, what its pane shows (`seen`),
/// whether its hook just reported the end of a turn (`finished`), whether
/// the user is looking at it (`here`), and since when it has looked idle in
/// the middle of a turn. A finished turn stays `Done` until the user looks.
pub fn advance(current: Activity, seen: Activity, finished: bool, here: bool, idle_since: Option<Instant>, now: Instant)
               -> (Activity, Option<Instant>) {
    let done_or_seen = if here { Activity::Idle } else { Activity::Done };
    match (current, seen) {
        (_, Activity::Working) => (Activity::Working, None),
        (_, Activity::Blocked) => (Activity::Blocked, None),
        (Activity::Working, _) => {
            let since = idle_since.unwrap_or(now);
            if finished || now.duration_since(since) >= DONE_AFTER {
                (done_or_seen, None)
            } else {
                (Activity::Working, Some(since))
            }
        }
        (Activity::Done, _) => (done_or_seen, None),
        _ if finished => (done_or_seen, None), // a turn too short to be seen working
        _ => (Activity::Idle, None),
    }
}

/// The CLI's own title without its status marks ("✳ ", a spinner,
/// "[ ! ] Action Required | "), or "" when it says nothing of its own.
pub fn label(tool: Tool, title: &str) -> String {
    let clean: String = title.chars().filter(|c| !c.is_control()).collect();
    let mut rest = clean.trim();
    match tool {
        Tool::Claude => {
            let mut chars = rest.chars();
            if let (Some(mark), Some(' ')) = (chars.next(), chars.next()) {
                if is_braille(mark) || "✳✻✽✶✢·*◐◑◒◓".contains(mark) {
                    rest = chars.as_str().trim_start();
                }
            }
            if rest == "Claude Code" {
                rest = "";
            }
        }
        Tool::Codex => {
            if rest.starts_with('[') {
                if let Some(end) = rest.find(']') {
                    if end <= 6 {
                        rest = rest[end + 1..].trim_start();
                    }
                }
            }
            rest = rest.strip_prefix("Action Required").map_or(rest, |after| after.trim_start().trim_start_matches('|').trim_start());
            let mut chars = rest.chars();
            if let (Some(mark), Some(' ')) = (chars.next(), chars.next()) {
                if is_braille(mark) {
                    rest = chars.as_str().trim_start();
                }
            }
        }
    }
    let label: String = rest.chars().take(80).collect();
    label.trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn screen(text: &str) -> Vec<String> {
        text.lines().map(String::from).collect()
    }

    // As Claude Code 2.1.323 draws them (captured from a real session).
    const CLAUDE_WORKING: &str = "❯ Run the shell command: sleep 6 && echo finished\n\n✽ Incubating… (4s · ↓ 23 tokens)\n\
        ────────────────\n❯ \n────────────────\n  ⏸ manual mode on · esc to interrupt · ← 1 agent\n";
    const CLAUDE_DONE: &str = "⏺ It waited 6 seconds and printed finished.\n✻ Churned for 11s · done 3:04 PM\n────────────────\n❯ \n\
        ────────────────\n  ⏸ manual mode on · ? for shortcuts · ← 1 agent\n";
    const CLAUDE_PERMISSION: &str = "⏺ Write(zjr-perm-test.txt)\n────────────────\n Create file\n zjr-perm-test.txt\n  1 hi\n\
        Do you want to create zjr-perm-test.txt?\n ❯ 1. Yes\n   2. Yes, and switch to accept edits for this session (shift+tab)\n   3. No\n\
        Esc to cancel · Tab to amend\n";
    // As Codex 0.167.2 draws them.
    const CODEX_APPROVAL: &str = "  1. Yes, proceed\n  2. Yes, and don't ask again\n  3. No, and tell Codex what to do differently (esc)\n\
        \n  Press enter to confirm or esc to cancel\n";
    const CODEX_UPDATE: &str = "  Update available · 0.167.2 → 0.169.0\n  Release notes: https://github.com/openai/codex/releases/latest\n\
        › 1. Update now (runs `pnpm add -g @openai/codex`)\n  2. Skip\n  3. Skip until next version\n  enter continue · esc skip\n";

    #[test]
    fn claude() {
        assert_eq!(detect(Tool::Claude, "✳ Sleep 6 echo command", &screen(CLAUDE_WORKING)), Activity::Working);
        assert_eq!(detect(Tool::Claude, "✳ Sleep 6 echo command", &screen(CLAUDE_DONE)), Activity::Idle);
        assert_eq!(detect(Tool::Claude, "✳ Sleep 6 echo command", &screen(CLAUDE_PERMISSION)), Activity::Blocked);
        assert_eq!(detect(Tool::Claude, "◐ Older version", &screen(CLAUDE_DONE)), Activity::Working); // a spinner in the title
        // A conversation that only talks about such things is not a dialog.
        let talk = "⏺ The prompt says \"Do you want to proceed?\" and offers 1. Yes\n────\n❯ \n────\n  ? for shortcuts\n";
        assert_eq!(detect(Tool::Claude, "✳ x", &screen(talk)), Activity::Idle);
        // Long ago "esc to interrupt" scrolled up: only the last lines count.
        let old = format!("  esc to interrupt\n{}", "line\n".repeat(30));
        assert_eq!(detect(Tool::Claude, "✳ x", &screen(&old)), Activity::Idle);
    }

    #[test]
    fn codex() {
        assert_eq!(detect(Tool::Codex, "codex3", &screen("› Ask Codex to do anything\n")), Activity::Idle);
        assert_eq!(detect(Tool::Codex, "⠙ codex3", &screen("› Ask Codex to do anything\n")), Activity::Working);
        assert_eq!(detect(Tool::Codex, "[ ! ] Action Required | Create x | codex3", &screen("")), Activity::Blocked);
        assert_eq!(detect(Tool::Codex, "Create x | codex3", &screen(CODEX_APPROVAL)), Activity::Blocked);
        assert_eq!(detect(Tool::Codex, "codex", &screen(CODEX_UPDATE)), Activity::Blocked);
        assert_eq!(detect(Tool::Codex, "a ⠿⠿ word", &screen("")), Activity::Idle); // only a lone spinner character counts
    }

    #[test]
    fn turns() {
        use Activity::*;
        let t0 = Instant::now();
        let later = |seconds: u64| t0 + Duration::from_secs(seconds);
        assert_eq!(advance(Idle, Working, false, false, None, t0), (Working, None));
        // Idle in the middle of a turn: not finished before DONE_AFTER.
        assert_eq!(advance(Working, Idle, false, false, None, t0), (Working, Some(t0)));
        assert_eq!(advance(Working, Idle, false, false, Some(t0), later(1)), (Working, Some(t0)));
        assert_eq!(advance(Working, Idle, false, false, Some(t0), later(2)), (Done, None));
        assert_eq!(advance(Working, Idle, false, true, Some(t0), later(2)), (Idle, None)); // watched: nothing to point out
        assert_eq!(advance(Working, Idle, true, false, None, t0), (Done, None)); // a hook said so: at once
        assert_eq!(advance(Done, Idle, false, false, None, later(60)), (Done, None)); // until looked at
        assert_eq!(advance(Done, Idle, false, true, None, later(60)), (Idle, None));
        assert_eq!(advance(Done, Working, false, false, None, t0), (Working, None));
        assert_eq!(advance(Working, Blocked, false, true, None, t0), (Blocked, None));
        assert_eq!(advance(Blocked, Idle, false, false, None, t0), (Idle, None)); // answered, or cancelled
        assert_eq!(advance(Idle, Idle, true, false, None, t0), (Done, None));
        assert_eq!(advance(Idle, Idle, false, false, None, t0), (Idle, None));
    }

    #[test]
    fn labels() {
        assert_eq!(label(Tool::Claude, "✳ Blaze Service Issues Teams thread"), "Blaze Service Issues Teams thread");
        assert_eq!(label(Tool::Claude, "◑ Working title"), "Working title");
        assert_eq!(label(Tool::Claude, "✳ Claude Code"), "");
        assert_eq!(label(Tool::Codex, "[ ! ] Action Required | Create zjr-perm-test.txt | codex3"), "Create zjr-perm-test.txt | codex3");
        assert_eq!(label(Tool::Codex, "⠙ codex3"), "codex3");
        assert_eq!(label(Tool::Codex, "codex3"), "codex3");
        assert_eq!(label(Tool::Claude, "✳ evil\u{7}\u{1b}]0;x"), "evil]0;x");
        assert_eq!(Activity::Blocked.urgency() > Activity::Done.urgency(), true);
    }
}
