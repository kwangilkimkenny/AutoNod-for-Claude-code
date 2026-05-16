//! Parse terminal lines from a CLI prompt into a structured [`PromptFrame`].
//!
//! Pure function — no I/O, no model calls. Mirrors the behaviour of the
//! Python `parser.py` in this repo so the test corpus can be shared.

use std::sync::OnceLock;

use regex::Regex;
use serde::Serialize;
use sha1::{Digest, Sha1};

pub const CURSOR_GLYPHS: &str = "❯>›»";

pub const RECOMMENDED_MARKERS: &[&str] = &[
    "(recommended)",
    "(default)",
    "(suggested)",
    "[recommended]",
];

pub const DESTRUCTIVE_KEYWORDS: &[&str] = &[
    "delete",
    "drop table",
    "rm -rf",
    "force push",
    "force-push",
    "--no-verify",
    "overwrite",
    "discard",
    "reset --hard",
    "wipe",
    "destroy",
];

fn opt_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        // Cursor glyphs are non-ASCII; escape per char.
        let cursor_class: String =
            CURSOR_GLYPHS.chars().map(|c| regex::escape(&c.to_string())).collect();
        Regex::new(&format!(
            r"^\s*(?P<cursor>[{cursor_class}]\s*)?(?P<key>\d{{1,2}}|[a-zA-Z])\s*[.)]\s+(?P<label>.+?)\s*$"
        ))
        .expect("opt_re compiles")
    })
}

fn yn_inline_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"\(\s*([yY])\s*/\s*([nN])\s*\)").unwrap())
}

fn question_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r".+\?\s*$").unwrap())
}

fn marker_only_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(r"(?i)^\s*[\(\[]\s*(recommended|default|suggested)\s*[\)\]]\s*$").unwrap()
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum Kind {
    Choice,
    Text,
    None,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Option {
    pub key: String,
    pub label: String,
    pub recommended: bool,
    pub is_cursor: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PromptFrame {
    pub kind: Kind,
    pub question: std::option::Option<String>,
    pub options: Vec<Option>,
    pub cursor_idx: std::option::Option<usize>,
    pub raw_lines: Vec<String>,
    pub destructive: bool,
}

impl PromptFrame {
    pub fn none(raw: Vec<String>) -> Self {
        Self {
            kind: Kind::None,
            question: None,
            options: Vec::new(),
            cursor_idx: None,
            raw_lines: raw,
            destructive: false,
        }
    }

    /// 12-char hex digest used as the cooldown/stability key.
    /// Mirrors `PromptFrame.hash()` in parser.py.
    pub fn hash(&self) -> String {
        let mut h = Sha1::new();
        h.update(self.question.as_deref().unwrap_or("").as_bytes());
        let mut sorted = self.options.clone();
        sorted.sort_by(|a, b| a.key.cmp(&b.key));
        for o in &sorted {
            h.update(b"|");
            h.update(o.key.as_bytes());
            h.update(b"=");
            h.update(o.label.as_bytes());
        }
        if self.question.is_none() {
            // Mix in the surrounding raw text, stripping cursor glyphs.
            let cursor_chars: Vec<char> = CURSOR_GLYPHS.chars().collect();
            let start = self.raw_lines.len().saturating_sub(5);
            for ln in &self.raw_lines[start..] {
                h.update(b"|R|");
                let stripped: String = ln
                    .trim()
                    .chars()
                    .filter(|c| !cursor_chars.contains(c))
                    .collect();
                h.update(stripped.as_bytes());
            }
        }
        let digest = h.finalize();
        hex::encode(&digest[..6])
    }
}

fn has_destructive_keyword(text: &str) -> bool {
    let lower = text.to_lowercase();
    DESTRUCTIVE_KEYWORDS.iter().any(|kw| lower.contains(kw))
}

/// Returns (cleaned_label, found_marker).
fn strip_recommended(label: &str) -> (String, bool) {
    let mut out = label.to_string();
    let mut found = false;
    loop {
        let low = out.to_lowercase();
        let mut hit_idx: std::option::Option<usize> = None;
        let mut hit_marker: &str = "";
        for marker in RECOMMENDED_MARKERS {
            if let Some(i) = low.find(marker) {
                if hit_idx.is_none_or(|cur| i < cur) {
                    hit_idx = Some(i);
                    hit_marker = marker;
                }
            }
        }
        match hit_idx {
            None => break,
            Some(i) => {
                let end = i + hit_marker.len();
                let combined = format!("{}{}", &out[..i], &out[end..]);
                out = combined.trim_matches(|c: char| c.is_whitespace() || "-–—".contains(c)).to_string();
                found = true;
            }
        }
    }
    (out, found)
}

/// Merge orphan `(recommended)` / `(default)` lines into the previous option line.
fn coalesce_marker_lines(raw: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::with_capacity(raw.len());
    for line in raw {
        if marker_only_re().is_match(line) {
            if let Some(last) = out.last() {
                if opt_re().is_match(last) {
                    let merged = format!("{} {}", last.trim_end(), line.trim());
                    let n = out.len();
                    out[n - 1] = merged;
                    continue;
                }
            }
        }
        out.push(line.clone());
    }
    out
}

/// Parse terminal lines → [`PromptFrame`]. Robust to scrollback noise above the prompt.
///
/// Strategy: scan from the bottom up. The live prompt is always the last
/// block on screen. We collect contiguous option lines, then look upward
/// for the nearest question line.
pub fn parse_prompt(lines: &[String]) -> PromptFrame {
    let raw_nonblank: Vec<String> = lines
        .iter()
        .map(|l| l.trim_end().to_string())
        .filter(|l| !l.trim().is_empty())
        .collect();

    if raw_nonblank.is_empty() {
        return PromptFrame::none(lines.to_vec());
    }

    let raw = coalesce_marker_lines(&raw_nonblank);

    // ---- pass 1: contiguous option block from the bottom ----
    let mut options: Vec<Option> = Vec::new();
    let mut last_opt_line_idx: std::option::Option<usize> = None;
    let mut first_opt_line_idx: std::option::Option<usize> = None;

    for i in (0..raw.len()).rev() {
        if let Some(caps) = opt_re().captures(&raw[i]) {
            if last_opt_line_idx.is_none() {
                last_opt_line_idx = Some(i);
            }
            first_opt_line_idx = Some(i);
            let cursor = caps.name("cursor").is_some();
            let key = caps.name("key").unwrap().as_str().to_lowercase();
            let label_raw = caps.name("label").unwrap().as_str();
            let (label, recommended) = strip_recommended(label_raw);
            options.insert(
                0,
                Option {
                    key,
                    label,
                    recommended,
                    is_cursor: cursor,
                },
            );
        } else if last_opt_line_idx.is_some() {
            break;
        }
    }

    // Reject single anonymous option lines like "Step 1. install …".
    if options.len() == 1 && !options.iter().any(|o| o.is_cursor) {
        options.clear();
        first_opt_line_idx = None;
        last_opt_line_idx = None;
    }

    if !options.is_empty() {
        let cursor_idx = options.iter().position(|o| o.is_cursor);

        let mut question: std::option::Option<String> = None;
        if let Some(start) = first_opt_line_idx {
            for j in (0..start).rev() {
                if question_re().is_match(&raw[j]) {
                    question = Some(raw[j].trim().to_string());
                    break;
                }
            }
        }

        let mut blob = question.clone().unwrap_or_default();
        blob.push(' ');
        for o in &options {
            blob.push_str(&o.label);
            blob.push(' ');
        }
        let destructive = has_destructive_keyword(&blob);

        let _ = last_opt_line_idx; // currently unused, kept for parity

        return PromptFrame {
            kind: Kind::Choice,
            question,
            options,
            cursor_idx,
            raw_lines: lines.to_vec(),
            destructive,
        };
    }

    // ---- pass 2: inline (y/n) shorthand ----
    for i in (0..raw.len()).rev() {
        if yn_inline_re().is_match(&raw[i]) {
            let question = raw[i].trim().to_string();
            let destructive = has_destructive_keyword(&question);
            return PromptFrame {
                kind: Kind::Choice,
                question: Some(question),
                options: vec![
                    Option { key: "y".into(), label: "yes".into(), recommended: false, is_cursor: false },
                    Option { key: "n".into(), label: "no".into(), recommended: false, is_cursor: false },
                ],
                cursor_idx: None,
                raw_lines: lines.to_vec(),
                destructive,
            };
        }
    }

    // ---- pass 3: free-text input prompt ----
    for i in (0..raw.len()).rev() {
        if question_re().is_match(&raw[i]) {
            return PromptFrame {
                kind: Kind::Text,
                question: Some(raw[i].trim().to_string()),
                options: Vec::new(),
                cursor_idx: None,
                raw_lines: lines.to_vec(),
                destructive: false,
            };
        }
    }

    PromptFrame::none(lines.to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lines(s: &[&str]) -> Vec<String> {
        s.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn classic_three_option_with_cursor() {
        let f = parse_prompt(&lines(&[
            "Do you want to proceed?",
            "❯ 1. Yes",
            "  2. Yes, and don't ask again",
            "  3. No, with feedback",
        ]));
        assert_eq!(f.kind, Kind::Choice);
        assert_eq!(f.question.as_deref(), Some("Do you want to proceed?"));
        assert_eq!(f.options.len(), 3);
        assert_eq!(f.options[0].key, "1");
        assert!(f.options[0].is_cursor);
        assert_eq!(f.cursor_idx, Some(0));
        assert!(!f.destructive);
    }

    #[test]
    fn recommended_marker_on_label() {
        let f = parse_prompt(&lines(&[
            "Which one?",
            "  1. Yes",
            "  2. Yes, allow all (recommended)",
            "  3. No",
        ]));
        assert_eq!(f.kind, Kind::Choice);
        let rec: Vec<_> = f.options.iter().filter(|o| o.recommended).collect();
        assert_eq!(rec.len(), 1);
        assert_eq!(rec[0].key, "2");
        assert_eq!(rec[0].label, "Yes, allow all");
    }

    #[test]
    fn orphan_marker_line_merges_into_prior_option() {
        let f = parse_prompt(&lines(&[
            "Choose?",
            "  1. Yes",
            "  2. Yes, allow all",
            "(recommended)",
            "  3. No",
        ]));
        let rec: Vec<_> = f.options.iter().filter(|o| o.recommended).collect();
        assert_eq!(rec.len(), 1);
        assert_eq!(rec[0].key, "2");
    }

    #[test]
    fn inline_yn() {
        let f = parse_prompt(&lines(&["Proceed with apply? (y/N)"]));
        assert_eq!(f.kind, Kind::Choice);
        assert_eq!(f.options.len(), 2);
        assert_eq!(f.options[0].key, "y");
        assert_eq!(f.options[1].key, "n");
    }

    #[test]
    fn free_text_question() {
        let f = parse_prompt(&lines(&["What is your name?", "> "]));
        assert_eq!(f.kind, Kind::Text);
    }

    #[test]
    fn empty_screen() {
        let f = parse_prompt(&lines(&["", "   ", ""]));
        assert_eq!(f.kind, Kind::None);
    }

    #[test]
    fn destructive_question_flagged() {
        let f = parse_prompt(&lines(&[
            "Force push to main?",
            "❯ 1. Yes",
            "  2. No",
        ]));
        assert_eq!(f.kind, Kind::Choice);
        assert!(f.destructive);
    }

    #[test]
    fn single_anonymous_option_rejected() {
        let f = parse_prompt(&lines(&[
            "Step 1. install the package using pip",
        ]));
        assert_eq!(f.kind, Kind::None);
    }

    #[test]
    fn single_cursor_option_accepted() {
        let f = parse_prompt(&lines(&[
            "Continue?",
            "❯ 1. Yes",
        ]));
        assert_eq!(f.kind, Kind::Choice);
        assert_eq!(f.options.len(), 1);
    }

    #[test]
    fn scrollback_noise_ignored() {
        let f = parse_prompt(&lines(&[
            "running tests...",
            "all 14 passed",
            "Do you want to proceed?",
            "❯ 1. Yes",
            "  2. No",
        ]));
        assert_eq!(f.kind, Kind::Choice);
        assert_eq!(f.question.as_deref(), Some("Do you want to proceed?"));
    }

    #[test]
    fn hash_stable_under_cursor_movement() {
        let a = parse_prompt(&lines(&[
            "Do you want to proceed?",
            "❯ 1. Yes",
            "  2. No",
        ]));
        let b = parse_prompt(&lines(&[
            "Do you want to proceed?",
            "  1. Yes",
            "❯ 2. No",
        ]));
        assert_eq!(a.hash(), b.hash(), "hash must be invariant under cursor moves");
    }

    #[test]
    fn hash_differs_for_different_prompts() {
        let a = parse_prompt(&lines(&[
            "Do you want to proceed?",
            "❯ 1. Yes",
            "  2. No",
        ]));
        let b = parse_prompt(&lines(&[
            "Delete the file?",
            "❯ 1. Yes",
            "  2. No",
        ]));
        assert_ne!(a.hash(), b.hash());
    }
}
