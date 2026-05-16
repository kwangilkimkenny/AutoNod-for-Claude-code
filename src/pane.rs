//! Per-pane polling loop.
//!
//! Owns one [`PromptStateTracker`] per pane. On every tick it captures the
//! pane's current screen, parses it, runs the gate, and — if the gate
//! passes — calls the decider and (unless dry-run) injects the chosen key
//! via tmux send-keys.

use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use tokio::sync::Notify;
use tokio::time::{interval, MissedTickBehavior};
use tracing::{debug, error, info, warn};

use crate::decider::Source;
use crate::llm::{self, LlmConfig};
use crate::parser::{parse_prompt, Kind};
use crate::state::PromptStateTracker;
use crate::tmux::{self, PaneInfo};

#[derive(Clone)]
pub struct PaneRuntime {
    pub interval: Duration,
    pub scrollback: u32,
    pub dry_run: bool,
    pub stable_frames: usize,
    pub cooldown: Duration,
    pub llm: Arc<LlmConfig>,
    pub project_context: Option<String>,
    pub shutdown: Arc<Notify>,
}

impl PaneRuntime {
    pub fn make_tracker(&self) -> PromptStateTracker {
        PromptStateTracker::new(self.stable_frames, self.cooldown)
    }
}

pub async fn run_pane(pane: PaneInfo, rt: PaneRuntime) -> Result<()> {
    info!(
        pane = %pane.pane_id,
        target = %pane.target,
        cmd = %pane.command,
        "watching pane"
    );
    let mut tracker = rt.make_tracker();
    let mut ticker = interval(rt.interval);
    ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
    // Skip the immediate first fire — the very first capture is the
    // "stability streak primer" and acting on it would defeat the gate.
    ticker.tick().await;

    loop {
        tokio::select! {
            _ = ticker.tick() => {
                if let Err(e) = tick_once(&pane, &rt, &mut tracker).await {
                    warn!(pane = %pane.pane_id, error = %e, "tick failed");
                }
            }
            _ = rt.shutdown.notified() => {
                info!(pane = %pane.pane_id, "shutdown signal — exiting pane loop");
                return Ok(());
            }
        }
    }
}

async fn tick_once(
    pane: &PaneInfo,
    rt: &PaneRuntime,
    tracker: &mut PromptStateTracker,
) -> Result<()> {
    let snap = match tmux::capture_pane(&pane.pane_id, rt.scrollback).await {
        Ok(s) => s,
        Err(e) => {
            // Likely the pane closed; surface and exit by returning the err.
            return Err(e);
        }
    };

    let raw_lines: Vec<String> = snap.lines().map(|l| l.to_string()).collect();
    let frame = parse_prompt(&raw_lines);

    let hash = if frame.kind == Kind::Choice {
        Some(frame.hash())
    } else {
        None
    };
    let gate = tracker.observe(hash.as_deref());
    if !gate.proceed {
        debug!(
            pane = %pane.pane_id,
            kind = ?frame.kind,
            gate = %gate.reason,
            "skip"
        );
        return Ok(());
    }

    let decision = llm::decide(&frame, rt.project_context.as_deref(), &rt.llm).await;
    let tag = match decision.source {
        Source::Rule => "rule",
        Source::Llm => "llm",
        Source::Fallback => "fallback",
    };
    info!(
        pane = %pane.pane_id,
        action = %decision.action,
        confidence = decision.confidence,
        source = tag,
        reason = %decision.reason,
        "decision"
    );

    if decision.action == "none" {
        return Ok(());
    }
    if rt.dry_run {
        info!(pane = %pane.pane_id, action = %decision.action, "DRY RUN — not sending key");
        // Still record cooldown so we don't repeatedly log the same decision.
        if let Some(h) = hash {
            tracker.mark_acted(&h);
        }
        return Ok(());
    }

    if let Err(e) = inject(&pane.pane_id, &decision.action).await {
        error!(pane = %pane.pane_id, error = %e, "send-keys failed");
        return Err(e);
    }
    if let Some(h) = hash {
        tracker.mark_acted(&h);
    }
    Ok(())
}

/// Translate a Decision `action` into the actual tmux send-keys sequence and dispatch it.
async fn inject(pane_id: &str, action: &str) -> Result<()> {
    match action {
        "enter" => tmux::send_keys(pane_id, "Enter").await,
        // Digits and letters: type the character, then press Enter.
        _ => tmux::send_keys_many(pane_id, &[action, "Enter"]).await,
    }
}
