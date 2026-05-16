//! Thin wrapper around `tmux` shell invocations.
//!
//! We invoke the user-installed `tmux` binary via `std::process::Command`
//! (async via `tokio::process::Command`). No control-mode protocol — we trade
//! a small amount of fork overhead for simplicity, robustness, and zero
//! extra dependencies.

use anyhow::{anyhow, bail, Context, Result};
use serde::Serialize;
use tokio::process::Command;

/// Identity of a single tmux pane.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PaneInfo {
    /// Unique pane id like `%23`. Stable across the tmux server's lifetime.
    pub pane_id: String,
    /// Human address like `mysession:0.1`.
    pub target: String,
    pub session: String,
    pub window_index: u32,
    pub pane_index: u32,
    pub title: String,
    pub command: String,
}

const LIST_FORMAT: &str =
    "#{pane_id}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_title}\t#{pane_current_command}";

/// List all panes across all sessions.
pub async fn list_panes() -> Result<Vec<PaneInfo>> {
    let out = Command::new("tmux")
        .args(["list-panes", "-a", "-F", LIST_FORMAT])
        .output()
        .await
        .context("spawn tmux list-panes")?;
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        bail!("tmux list-panes failed: {stderr}");
    }
    let text = String::from_utf8(out.stdout).context("tmux list-panes stdout is not UTF-8")?;
    let mut panes = Vec::new();
    for line in text.lines() {
        if line.is_empty() {
            continue;
        }
        let parts: Vec<&str> = line.splitn(6, '\t').collect();
        if parts.len() < 6 {
            continue;
        }
        let pane_id = parts[0].to_string();
        let session = parts[1].to_string();
        let window_index: u32 = parts[2].parse().unwrap_or(0);
        let pane_index: u32 = parts[3].parse().unwrap_or(0);
        let title = parts[4].to_string();
        let command = parts[5].to_string();
        let target = format!("{session}:{window_index}.{pane_index}");
        panes.push(PaneInfo {
            pane_id,
            target,
            session,
            window_index,
            pane_index,
            title,
            command,
        });
    }
    Ok(panes)
}

/// Capture the visible buffer (plus N lines of scrollback) of `pane_id` as plain text.
/// `-p` writes to stdout, `-J` joins wrapped lines, `-S -N` extends history.
pub async fn capture_pane(pane_id: &str, scrollback: u32) -> Result<String> {
    let s_flag = format!("-{scrollback}");
    let out = Command::new("tmux")
        .args(["capture-pane", "-p", "-J", "-t", pane_id, "-S", &s_flag, "-E", "-"])
        .output()
        .await
        .context("spawn tmux capture-pane")?;
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        if stderr.contains("can't find pane") {
            return Err(anyhow!("pane {pane_id} not found"));
        }
        bail!("tmux capture-pane failed: {stderr}");
    }
    String::from_utf8(out.stdout).context("capture-pane stdout is not UTF-8")
}

/// Send a single keystroke to the given pane.
///
/// `key` is interpreted by tmux — e.g. `"1"`, `"y"`, `"Enter"`. Multiple
/// keys can be sent by chaining calls (or by using [`send_keys_many`]).
pub async fn send_keys(pane_id: &str, key: &str) -> Result<()> {
    let status = Command::new("tmux")
        .args(["send-keys", "-t", pane_id, key])
        .status()
        .await
        .context("spawn tmux send-keys")?;
    if !status.success() {
        bail!("tmux send-keys {key} → {pane_id} failed");
    }
    Ok(())
}

pub async fn send_keys_many(pane_id: &str, keys: &[&str]) -> Result<()> {
    let mut cmd = Command::new("tmux");
    cmd.args(["send-keys", "-t", pane_id]);
    for k in keys {
        cmd.arg(k);
    }
    let status = cmd.status().await.context("spawn tmux send-keys")?;
    if !status.success() {
        bail!("tmux send-keys {keys:?} → {pane_id} failed");
    }
    Ok(())
}

/// True if `tmux` is on PATH and a server is reachable.
pub async fn server_running() -> bool {
    Command::new("tmux")
        .arg("list-sessions")
        .output()
        .await
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Resolve user-supplied pane targets (e.g. `"mysess:0.1"`, `"%23"`, or
/// `"mysess"` for "all panes in this session") to concrete [`PaneInfo`]s.
pub async fn resolve_targets(specs: &[String]) -> Result<Vec<PaneInfo>> {
    let all = list_panes().await?;
    if specs.is_empty() {
        return Ok(all);
    }
    let mut out: Vec<PaneInfo> = Vec::new();
    for spec in specs {
        let mut matched = false;
        for p in &all {
            if &p.pane_id == spec || &p.target == spec || p.session == *spec {
                if !out.iter().any(|q| q.pane_id == p.pane_id) {
                    out.push(p.clone());
                }
                matched = true;
            }
        }
        if !matched {
            return Err(anyhow!("no pane matched target {spec:?}"));
        }
    }
    Ok(out)
}
