//! Decision engine. Mirrors `decider.py`.
//!
//! Two paths:
//!   - [`rule_decide`]: cheap, no model call. Returns `Some(Decision)` when
//!     the answer is unambiguous (Recommended marker, cursor-on-Yes,
//!     canonical Yes/No).
//!   - [`llm_decide`]: HTTP call to a local Ollama text model.
//!
//! [`decide`] is the top-level: rule path first, LLM as fallback.

use std::sync::OnceLock;

use regex::Regex;
use serde::Serialize;

use crate::parser::{Kind, Option, PromptFrame};

pub const ALLOWED_ACTIONS: &[&str] = &["1", "2", "3", "4", "5", "y", "n", "a", "enter", "none"];

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Decision {
    pub action: String,
    pub confidence: f32,
    pub reason: String,
    pub source: Source,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Source {
    Rule,
    Llm,
    Fallback,
}

impl Decision {
    pub fn rule(action: &str, confidence: f32, reason: impl Into<String>) -> Self {
        Self { action: action.into(), confidence, reason: reason.into(), source: Source::Rule }
    }
    pub fn fallback(action: &str, confidence: f32, reason: impl Into<String>) -> Self {
        Self { action: action.into(), confidence, reason: reason.into(), source: Source::Fallback }
    }
}

fn safe_first_label() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"(?i)^\s*(yes|allow|ok|continue|proceed)\b").unwrap())
}
fn unsafe_first_label() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"(?i)don'?t ask again|for the rest of|always allow|never ask").unwrap()
    })
}
fn canonical_yes_label() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"(?i)^(yes|yes,?\s*proceed|yes,?\s*continue|allow|ok|continue|proceed)$")
            .unwrap()
    })
}
fn canonical_no_prefix() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"(?i)^(no|cancel|abort)\b").unwrap())
}

/// Returns the safest option to auto-pick, or None.
fn safe_default_option(frame: &PromptFrame) -> std::option::Option<&Option> {
    let cand = frame
        .options
        .iter()
        .find(|o| o.key == "1")
        .or_else(|| frame.options.iter().find(|o| o.key == "y"))?;
    if unsafe_first_label().is_match(&cand.label) {
        return None;
    }
    let cand_label = cand.label.trim().to_lowercase();
    let keys: std::collections::BTreeSet<&str> =
        frame.options.iter().map(|o| o.key.as_str()).collect();

    // (a) Inline (y/n) — synthesised by the parser with labels "yes"/"no".
    if frame.options.len() == 2
        && keys.contains("y")
        && keys.contains("n")
        && cand.key == "y"
        && cand_label == "yes"
    {
        return Some(cand);
    }

    // (b) Cursor on a Yes-style option.
    if cand.is_cursor && safe_first_label().is_match(&cand.label) {
        return Some(cand);
    }

    // (c) Exactly two options, canonical Yes / No-prefixed labels.
    if frame.options.len() == 2
        && cand.key == "1"
        && canonical_yes_label().is_match(&cand_label)
    {
        if let Some(other) = frame.options.iter().find(|o| o.key == "2") {
            if canonical_no_prefix().is_match(other.label.trim()) {
                return Some(cand);
            }
        }
    }
    None
}

/// Cheap, deterministic decisions. Returns `None` if the LLM is needed.
pub fn rule_decide(frame: &PromptFrame) -> std::option::Option<Decision> {
    if frame.kind != Kind::Choice {
        return Some(Decision::rule(
            "none",
            1.0,
            format!("kind={:?} → no auto-keypress", frame.kind),
        ));
    }
    if frame.destructive {
        return Some(Decision::rule(
            "none",
            1.0,
            "destructive keyword detected — handing to human",
        ));
    }
    if frame.options.is_empty() {
        return Some(Decision::rule("none", 1.0, "no options parsed"));
    }

    let recommended: Vec<&Option> = frame.options.iter().filter(|o| o.recommended).collect();
    if recommended.len() == 1 {
        let r = recommended[0];
        return Some(Decision::rule(
            &r.key,
            0.95,
            format!("option {:?} marked recommended", r.key),
        ));
    }

    if let Some(safe) = safe_default_option(frame) {
        return Some(Decision::rule(
            &safe.key,
            0.85,
            format!("safe single-shot Yes on key {:?}", safe.key),
        ));
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parser::parse_prompt;

    fn lines(s: &[&str]) -> Vec<String> {
        s.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn recommended_marker_decides() {
        let f = parse_prompt(&lines(&[
            "Choose?",
            "  1. Yes",
            "  2. Yes, allow all (recommended)",
            "  3. No",
        ]));
        let d = rule_decide(&f).expect("rule should decide");
        assert_eq!(d.action, "2");
        assert!(d.confidence > 0.9);
    }

    #[test]
    fn cursor_on_yes_decides() {
        let f = parse_prompt(&lines(&[
            "Proceed?",
            "❯ 1. Yes",
            "  2. Yes, and don't ask again",
            "  3. No, with feedback",
        ]));
        let d = rule_decide(&f).expect("rule should decide");
        assert_eq!(d.action, "1");
    }

    #[test]
    fn yn_inline_decides_y() {
        let f = parse_prompt(&lines(&["Apply changes? (y/N)"]));
        let d = rule_decide(&f).expect("rule should decide");
        assert_eq!(d.action, "y");
    }

    #[test]
    fn destructive_returns_none() {
        let f = parse_prompt(&lines(&[
            "Force push to main?",
            "❯ 1. Yes",
            "  2. No",
        ]));
        let d = rule_decide(&f).expect("rule short-circuits destructive");
        assert_eq!(d.action, "none");
        assert!(d.reason.to_lowercase().contains("destructive"));
    }

    #[test]
    fn text_kind_returns_none() {
        let f = parse_prompt(&lines(&["What is your name?", "> "]));
        let d = rule_decide(&f).expect("text returns none");
        assert_eq!(d.action, "none");
    }

    #[test]
    fn ambiguous_falls_through_to_llm() {
        // Three options, no recommended marker, no cursor, label #1 has "don't ask again" → unsafe.
        let f = parse_prompt(&lines(&[
            "Pick one?",
            "  1. Yes, and don't ask again",
            "  2. Yes",
            "  3. No",
        ]));
        assert!(rule_decide(&f).is_none(), "must delegate to LLM");
    }

    #[test]
    fn two_choice_yes_no_decides() {
        let f = parse_prompt(&lines(&[
            "Continue?",
            "  1. Yes",
            "  2. No",
        ]));
        let d = rule_decide(&f).expect("canonical yes/no");
        assert_eq!(d.action, "1");
    }
}
