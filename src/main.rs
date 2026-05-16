//! `autonod` — tmux pane monitoring daemon that auto-responds to Claude
//! Code permission prompts.
//!
//! Subcommands:
//!   - `list`            enumerate visible panes
//!   - `attach`          start the monitor loop
//!   - `decide-once`     one-shot capture + decide for a single pane (debug)
//!   - `test-parser`     parse a text file and print the resulting frame

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use clap::{Args, Parser, Subcommand};
use tokio::sync::Notify;
use tracing::{info, warn};
use tracing_subscriber::EnvFilter;

use autonod::llm::{self, LlmConfig};
use autonod::pane::{run_pane, PaneRuntime};
use autonod::parser::parse_prompt;
use autonod::tmux;

#[derive(Parser, Debug)]
#[command(
    name = "autonod",
    version,
    about = "tmux pane monitor that auto-replies to Claude Code permission prompts"
)]
struct Cli {
    /// Increase log verbosity (-v = debug, -vv = trace).
    #[arg(short, long, action = clap::ArgAction::Count, global = true)]
    verbose: u8,

    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// List all tmux panes the server can see.
    List,

    /// Watch one or more panes and auto-press keys for recognised prompts.
    Attach(AttachArgs),

    /// Capture one pane, decide once, and exit. Useful for debugging.
    DecideOnce(DecideOnceArgs),

    /// Parse a local text file as if it were a captured screen.
    TestParser(TestParserArgs),
}

#[derive(Args, Debug)]
struct AttachArgs {
    /// Pane targets: pane id (%23), `session:window.pane`, or `session`
    /// for "all panes in that session". Repeatable. If omitted, all panes
    /// are watched.
    #[arg(short = 't', long = "target")]
    targets: Vec<String>,

    /// Log decisions but do not actually press keys.
    #[arg(long)]
    dry_run: bool,

    /// Poll interval, seconds.
    #[arg(long, default_value_t = 2.0)]
    interval: f64,

    /// Scrollback lines to include in each capture.
    #[arg(long, default_value_t = 50)]
    scrollback: u32,

    /// Consecutive identical frames required before acting.
    #[arg(long, default_value_t = 2)]
    stable_frames: usize,

    /// Seconds to suppress re-action on the same prompt hash.
    #[arg(long, default_value_t = 15.0)]
    cooldown_sec: f64,

    #[command(flatten)]
    llm: LlmArgs,
}

#[derive(Args, Debug)]
struct DecideOnceArgs {
    /// Pane id or `session:win.pane`.
    target: String,

    #[arg(long, default_value_t = 50)]
    scrollback: u32,

    #[command(flatten)]
    llm: LlmArgs,
}

#[derive(Args, Debug)]
struct TestParserArgs {
    /// Path to a text file containing the captured screen.
    path: PathBuf,
}

#[derive(Args, Debug, Clone)]
struct LlmArgs {
    /// Ollama model used for the LLM fallback path.
    #[arg(long, default_value = llm::DEFAULT_MODEL)]
    llm_model: String,

    /// HTTP timeout (seconds) for the Ollama call.
    #[arg(long, default_value_t = llm::DEFAULT_TIMEOUT_SEC as f64)]
    llm_timeout: f64,

    /// Confidence threshold below which the LLM decision becomes "none".
    #[arg(long, default_value_t = 0.7)]
    confidence: f32,

    /// File whose contents are passed to the LLM as project context.
    #[arg(long)]
    project_context: Option<PathBuf>,

    /// Override the Ollama endpoint.
    #[arg(long, default_value = llm::OLLAMA_CHAT_URL)]
    llm_endpoint: String,
}

impl LlmArgs {
    fn build(&self) -> Result<(LlmConfig, Option<String>)> {
        let cfg = LlmConfig {
            model: self.llm_model.clone(),
            timeout: Duration::from_secs_f64(self.llm_timeout),
            confidence_threshold: self.confidence,
            system_prompt: llm::DEFAULT_SYSTEM_PROMPT.to_string(),
            endpoint: self.llm_endpoint.clone(),
        };
        let ctx = match &self.project_context {
            None => None,
            Some(p) => Some(
                std::fs::read_to_string(p)
                    .with_context(|| format!("read project context {}", p.display()))?,
            ),
        };
        Ok((cfg, ctx))
    }
}

fn init_logging(verbosity: u8) {
    let level = match verbosity {
        0 => "info",
        1 => "debug",
        _ => "trace",
    };
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new(format!("autonod={level}")));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .compact()
        .init();
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    init_logging(cli.verbose);

    match cli.cmd {
        Cmd::List => cmd_list().await,
        Cmd::Attach(a) => cmd_attach(a).await,
        Cmd::DecideOnce(a) => cmd_decide_once(a).await,
        Cmd::TestParser(a) => cmd_test_parser(a),
    }
}

async fn cmd_list() -> Result<()> {
    if !tmux::server_running().await {
        anyhow::bail!("no running tmux server (try `tmux new-session`)");
    }
    let panes = tmux::list_panes().await?;
    if panes.is_empty() {
        println!("(no panes)");
        return Ok(());
    }
    println!("{:<8} {:<24} {:<24} TITLE", "PANE", "TARGET", "COMMAND");
    for p in panes {
        println!("{:<8} {:<24} {:<24} {}", p.pane_id, p.target, p.command, p.title);
    }
    Ok(())
}

async fn cmd_attach(a: AttachArgs) -> Result<()> {
    if !tmux::server_running().await {
        anyhow::bail!("no running tmux server");
    }
    let (llm_cfg, ctx) = a.llm.build()?;
    let panes = tmux::resolve_targets(&a.targets).await?;
    if panes.is_empty() {
        anyhow::bail!("no panes matched the given targets");
    }
    info!(
        count = panes.len(),
        dry_run = a.dry_run,
        interval_s = a.interval,
        "starting monitor"
    );

    let shutdown = Arc::new(Notify::new());
    {
        let sh = shutdown.clone();
        tokio::spawn(async move {
            if tokio::signal::ctrl_c().await.is_ok() {
                warn!("SIGINT received — shutting down");
                sh.notify_waiters();
            }
        });
    }

    let rt = PaneRuntime {
        interval: Duration::from_secs_f64(a.interval),
        scrollback: a.scrollback,
        dry_run: a.dry_run,
        stable_frames: a.stable_frames,
        cooldown: Duration::from_secs_f64(a.cooldown_sec),
        llm: Arc::new(llm_cfg),
        project_context: ctx,
        shutdown: shutdown.clone(),
    };

    let mut tasks = Vec::with_capacity(panes.len());
    for p in panes {
        let rt = rt.clone();
        tasks.push(tokio::spawn(async move {
            if let Err(e) = run_pane(p, rt).await {
                warn!(error = %e, "pane loop exited with error");
            }
        }));
    }
    for t in tasks {
        let _ = t.await;
    }
    Ok(())
}

async fn cmd_decide_once(a: DecideOnceArgs) -> Result<()> {
    let (llm_cfg, ctx) = a.llm.build()?;
    let panes = tmux::resolve_targets(std::slice::from_ref(&a.target)).await?;
    let p = panes.into_iter().next().context("target not found")?;
    let snap = tmux::capture_pane(&p.pane_id, a.scrollback).await?;
    let lines: Vec<String> = snap.lines().map(String::from).collect();
    let frame = parse_prompt(&lines);
    println!("--- frame ---");
    println!("{}", serde_json::to_string_pretty(&frame)?);
    let decision = llm::decide(&frame, ctx.as_deref(), &llm_cfg).await;
    println!("--- decision ---");
    println!("{}", serde_json::to_string_pretty(&decision)?);
    Ok(())
}

fn cmd_test_parser(a: TestParserArgs) -> Result<()> {
    let text = std::fs::read_to_string(&a.path)
        .with_context(|| format!("read {}", a.path.display()))?;
    let lines: Vec<String> = text.lines().map(String::from).collect();
    let frame = parse_prompt(&lines);
    println!("{}", serde_json::to_string_pretty(&frame)?);
    Ok(())
}
