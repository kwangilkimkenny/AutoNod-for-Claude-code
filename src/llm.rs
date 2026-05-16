//! Ollama text LLM client. Mirrors `decider.py` LLM path.
//!
//! POST /api/chat with `format: "json"`, `temperature: 0`, then parse a
//! single-line JSON Decision out of the reply.

use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use regex::Regex;
use serde::{Deserialize, Serialize};

use crate::decider::{Decision, Source, ALLOWED_ACTIONS};
use crate::parser::PromptFrame;

pub const OLLAMA_CHAT_URL: &str = "http://localhost:11434/api/chat";
pub const DEFAULT_MODEL: &str = "qwen3:latest";
pub const DEFAULT_TIMEOUT_SEC: u64 = 30;
pub const DEFAULT_SYSTEM_PROMPT: &str = include_str!("../prompts/decide_system.txt");

#[derive(Serialize)]
struct ChatMessage<'a> {
    role: &'a str,
    content: &'a str,
}

#[derive(Serialize)]
struct ChatOptions {
    temperature: f32,
}

#[derive(Serialize)]
struct ChatRequest<'a> {
    model: &'a str,
    messages: Vec<ChatMessage<'a>>,
    stream: bool,
    format: &'a str,
    options: ChatOptions,
}

#[derive(Deserialize)]
struct ChatReplyMessage {
    content: String,
}

#[derive(Deserialize)]
struct ChatReply {
    message: ChatReplyMessage,
}

pub struct LlmConfig {
    pub model: String,
    pub timeout: Duration,
    pub confidence_threshold: f32,
    pub system_prompt: String,
    pub endpoint: String,
}

impl Default for LlmConfig {
    fn default() -> Self {
        Self {
            model: DEFAULT_MODEL.into(),
            timeout: Duration::from_secs(DEFAULT_TIMEOUT_SEC),
            confidence_threshold: 0.7,
            system_prompt: DEFAULT_SYSTEM_PROMPT.to_string(),
            endpoint: OLLAMA_CHAT_URL.into(),
        }
    }
}

pub fn format_frame_for_llm(frame: &PromptFrame, project_context: Option<&str>) -> String {
    let mut out = String::new();
    if let Some(ctx) = project_context {
        out.push_str("PROJECT_CONTEXT:\n");
        let trimmed = ctx.trim();
        let cap = trimmed.char_indices().nth(1500).map(|(i, _)| i).unwrap_or(trimmed.len());
        out.push_str(&trimmed[..cap]);
        out.push_str("\n\n");
    }
    out.push_str("PROMPT_QUESTION: ");
    out.push_str(frame.question.as_deref().unwrap_or("(none)"));
    out.push_str("\nPROMPT_OPTIONS:\n");
    for o in &frame.options {
        let mut marks: Vec<&str> = Vec::new();
        if o.is_cursor {
            marks.push("cursor");
        }
        if o.recommended {
            marks.push("recommended");
        }
        let suffix = if marks.is_empty() {
            String::new()
        } else {
            format!("  ({})", marks.join(", "))
        };
        out.push_str(&format!("  [{}] {}{}\n", o.key, o.label, suffix));
    }
    out.push_str("\nRAW_OCR:\n");
    let start = frame.raw_lines.len().saturating_sub(30);
    for ln in &frame.raw_lines[start..] {
        out.push_str(ln);
        out.push('\n');
    }
    out
}

async fn call_ollama(cfg: &LlmConfig, user: &str) -> Result<String> {
    let client = reqwest::Client::builder()
        .timeout(cfg.timeout)
        .build()
        .context("build http client")?;
    let req = ChatRequest {
        model: &cfg.model,
        messages: vec![
            ChatMessage { role: "system", content: &cfg.system_prompt },
            ChatMessage { role: "user", content: user },
        ],
        stream: false,
        format: "json",
        options: ChatOptions { temperature: 0.0 },
    };
    let resp = client
        .post(&cfg.endpoint)
        .json(&req)
        .send()
        .await
        .context("ollama POST failed")?
        .error_for_status()
        .context("ollama returned non-2xx")?;
    let parsed: ChatReply = resp.json().await.context("ollama reply not JSON")?;
    Ok(parsed.message.content)
}

fn extract_json(text: &str) -> Option<String> {
    let re = Regex::new(r"(?s)\{.*\}").ok()?;
    re.find(text).map(|m| m.as_str().to_string())
}

pub fn parse_llm_response(text: &str) -> Result<Decision> {
    let json = extract_json(text).ok_or_else(|| anyhow!("no JSON object in reply"))?;
    let obj: serde_json::Value =
        serde_json::from_str(&json).context("LLM reply is not valid JSON")?;
    let action = obj
        .get("action")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_lowercase();
    if !ALLOWED_ACTIONS.contains(&action.as_str()) {
        return Err(anyhow!("action {:?} not in allowed set", action));
    }
    let confidence = obj.get("confidence").and_then(|v| v.as_f64()).unwrap_or(0.0) as f32;
    let reason = obj
        .get("reason")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .chars()
        .take(200)
        .collect::<String>();
    Ok(Decision { action, confidence, reason, source: Source::Llm })
}

pub async fn llm_decide(
    frame: &PromptFrame,
    project_context: Option<&str>,
    cfg: &LlmConfig,
) -> Decision {
    if frame.kind != crate::parser::Kind::Choice {
        return Decision::fallback("none", 1.0, format!("kind={:?}", frame.kind));
    }
    let user = format_frame_for_llm(frame, project_context);
    let raw = match call_ollama(cfg, &user).await {
        Ok(r) => r,
        Err(e) => return Decision::fallback("none", 0.0, format!("llm error: {e:#}")),
    };
    let parsed = match parse_llm_response(&raw) {
        Ok(d) => d,
        Err(e) => {
            let snippet: String = raw.chars().take(120).collect();
            return Decision::fallback(
                "none",
                0.0,
                format!("unparseable llm output ({e}): {snippet:?}"),
            );
        }
    };
    if parsed.confidence < cfg.confidence_threshold {
        return Decision {
            action: "none".into(),
            confidence: parsed.confidence,
            reason: format!("low confidence ({:.2}): {}", parsed.confidence, parsed.reason),
            source: Source::Llm,
        };
    }
    parsed
}

pub async fn decide(
    frame: &PromptFrame,
    project_context: Option<&str>,
    cfg: &LlmConfig,
) -> Decision {
    if let Some(d) = crate::decider::rule_decide(frame) {
        return d;
    }
    llm_decide(frame, project_context, cfg).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_clean_json() {
        let d = parse_llm_response(
            r#"{"action":"1","confidence":0.92,"reason":"recommended"}"#,
        )
        .unwrap();
        assert_eq!(d.action, "1");
        assert!((d.confidence - 0.92).abs() < 1e-4);
    }

    #[test]
    fn extracts_json_from_chatter() {
        let d = parse_llm_response(
            "Sure! Here you go:\n{\"action\":\"y\",\"confidence\":0.8,\"reason\":\"yn\"}\n",
        )
        .unwrap();
        assert_eq!(d.action, "y");
    }

    #[test]
    fn rejects_disallowed_action() {
        let err = parse_llm_response(r#"{"action":"rm -rf","confidence":1.0}"#);
        assert!(err.is_err());
    }
}
