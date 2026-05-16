# AutoNod

Auto-press **Enter** (or `1`, `y`, …) on **Claude Code permission prompts** running inside tmux. Watches as many panes as you want, in parallel, safely.

```
$ autonod attach -t work
INFO  watching pane pane=%0 target=work:0.0 cmd=claude
INFO  watching pane pane=%1 target=work:1.0 cmd=claude
INFO  decision pane=%0 action=1 confidence=0.85 source=rule reason=safe single-shot Yes on key "1"
INFO  decision pane=%1 action=2 confidence=0.95 source=rule reason=option "2" marked recommended
```

- **Zero macOS permissions.** No Screen Recording, no Accessibility, no Input Monitoring.
- **Always the right pane.** `tmux send-keys -t <pane_id>` is exact — no wrong-window misfires.
- **Safe by default.** Same prompt must appear twice + 15-second cooldown + destructive-keyword blocklist (`delete`, `rm -rf`, `force push`, …) always abstains.
- **Cheap.** Rule decider handles the common Claude Code prompts without any LLM call. Falls back to a local Ollama model only when ambiguous.

> **Two versions in this repo.** `v0.2` (Rust + tmux — described here) and the legacy `v0.1` (macOS OCR + Python — see [legacy section](#legacy-v01--macos-ocr--python)). New users should pick v0.2.

---

## 5-minute quick start

### 1. Install

```bash
# tmux (one-time)
brew install tmux

# autonod
git clone https://github.com/kwangilkimkenny/AutoNod-for-Claude-code.git
cd AutoNod-for-Claude-code
cargo install --path .                    # installs to ~/.cargo/bin/autonod
```

### 2. Run Claude Code inside tmux

```bash
tmux new -s work       # start a tmux session named "work"
# inside that pane, start whatever CLI you want autonod to babysit:
claude                 # or: aider, gemini, etc.
```

You can split the window or open more tmux windows — each pane running Claude is one autonod target.

### 3. Watch in dry-run first

In a **separate** terminal:

```bash
autonod list                                          # what does tmux see?
autonod attach -t work --dry-run --interval 2        # log only, no keys pressed
```

Use Claude Code as you normally would. autonod will log what it *would* press without actually pressing.

### 4. Once you trust it, drop --dry-run

```bash
autonod attach -t work --interval 2                  # real keypresses
```

Stop with `Ctrl+C`. Need to watch every session at once?

```bash
autonod attach                                       # all panes the tmux server can see
```

That's it. The rest of this README is reference material.

---

## Recipes

```bash
# Watch one specific pane by id
autonod attach -t %3

# Watch every pane in a session
autonod attach -t work

# Watch a single pane: session:window.pane
autonod attach -t work:1.0

# Watch several at once (flag is repeatable)
autonod attach -t work -t experiments -t %9

# More conservative — wait for 3 stable frames before pressing
autonod attach -t work --stable-frames 3 --interval 1

# Inject CLAUDE.md as context for the LLM fallback
autonod attach -t work --project-context CLAUDE.md
```

### Debug helpers

```bash
autonod list                              # panes the tmux server reports
autonod decide-once work:0.0              # one capture + decision dump (JSON)
autonod test-parser path/to/screen.txt    # parse a saved screen
autonod -v attach -t work --dry-run       # debug-level logs
```

### Self-verification before trusting it on real work

A bundled regression suite paints 9 fake Claude Code prompts into a throwaway tmux session and asserts the decisions match. Run it any time:

```bash
cargo build --release
bash tests/smoke_tmux.sh
# expected: 9 scenarios, 9 pass, 0 fail, ALL PASS
```

For real-world confidence before unattended use, run `autonod attach -t <your-session> --dry-run` against a real Claude session for ~30 minutes of normal coding. If the log shows zero `action=…` lines for screens that *weren't* live prompts, you're safe to drop `--dry-run`.

---

## Flags (`autonod attach`)

| Flag                | Default                              | Description                                                           |
| ------------------- | ------------------------------------ | --------------------------------------------------------------------- |
| `-t, --target`      | (all panes)                          | pane id (`%23`), `session:win.pane`, or session name. Repeatable.     |
| `--dry-run`         | off                                  | log decisions, do not press keys                                      |
| `--interval`        | `2.0`                                | poll period in seconds                                                |
| `--scrollback`      | `50`                                 | history lines included in each snapshot                               |
| `--stable-frames`   | `2`                                  | identical frames required before acting (anti-flicker)                |
| `--cooldown-sec`    | `15`                                 | seconds to block re-action on the same prompt hash                    |
| `--llm-model`       | `qwen3:latest`                       | Ollama model for the ambiguous-case fallback                          |
| `--llm-timeout`     | `30`                                 | LLM HTTP timeout (seconds)                                            |
| `--confidence`      | `0.7`                                | LLM confidence threshold                                              |
| `--project-context` | (none)                               | file (e.g. `CLAUDE.md`) injected into the LLM context                 |
| `--llm-endpoint`    | `http://localhost:11434/api/chat`    | override Ollama URL                                                   |

Global: `-v` once → debug logs, `-vv` → trace. `RUST_LOG=autonod=debug` works too.

---

## How it works

Every `--interval` seconds, for each watched pane:

```
 capture-pane ─► parse ─► state gate ─► rule decider ─► (LLM if ambiguous) ─► send-keys
```

1. **Capture.** `tmux capture-pane -p -J` grabs the visible screen plus `--scrollback` history.
2. **Parse.** Scans bottom-up for an option block (`1. Yes`, `❯ 1. Yes`, `(y/N)`) and the nearest preceding `?` line. Output: `PromptFrame { kind, question, options[], cursor_idx, destructive }`.
3. **Stability gate.** Requires `--stable-frames` (default 2) consecutive identical frames before letting any decision through. Suppresses re-action on the same hash for `--cooldown-sec` (default 15s).
4. **Rule decider** acts immediately when one of these is true:
   - exactly one option marked `(recommended)` / `(default)` → that key (confidence 0.95)
   - `❯` cursor sits on a Yes / Allow / OK / Continue option → that key (0.85)
   - exactly two options with canonical Yes / No labels → `1` (0.85)
   - inline `(y/N)` shorthand → `y` (0.85)
5. **LLM fallback** (Ollama `qwen3:latest`) for ambiguous prompts — strict JSON `{action, confidence, reason}`, temperature 0. Below `--confidence` → `none`.
6. **Safety**: questions containing `delete`, `rm -rf`, `force push`, `--no-verify`, `reset --hard`, `overwrite`, `discard`, `wipe`, `destroy` always return `none`. Free-text prompts (no options) always return `none`.
7. **Action**: chosen key (one of `1`–`5`, `y`, `n`, `a`, `enter`) is sent via `tmux send-keys -t <pane_id>`, followed by `Enter`.

The full design — including data structures and parser heuristics — is in [`DESIGN.md`](DESIGN.md).

---

## Troubleshooting

| Symptom                                       | Action                                                                 |
| --------------------------------------------- | ---------------------------------------------------------------------- |
| `no running tmux server`                      | Start one: `tmux new -s work`                                           |
| `autonod list` shows nothing                  | You're inside the tmux pane — `list` looks at the *server*. Open another terminal. |
| `tmux send-keys ... failed`                   | Pane closed mid-run. Restart the attach.                              |
| `llm error: connection refused`               | Ollama isn't running. `ollama serve &` and `ollama pull qwen3`. Or rely on the rule path. |
| Wrong key gets sent for a custom prompt       | Run `autonod decide-once <target>` and read the parser output. Open an issue with the screen dump. |
| Reaction is too aggressive                    | `--stable-frames 3 --interval 2` is calmer.                            |
| Reaction is too slow                          | `--interval 1` polls twice as fast (still rule-only when possible).    |

---

## Build, test, hack

```bash
cargo build --release           # ~30s clean build
cargo test                      # 27 unit tests (parser, state, decider, llm)
cargo clippy --all-targets      # zero warnings expected
bash tests/smoke_tmux.sh        # 9 end-to-end scenarios in a real tmux server
```

Crate layout:

```
src/
├── lib.rs       module exports
├── main.rs      CLI (list / attach / decide-once / test-parser)
├── parser.rs    PromptFrame parser  (port of parser.py)
├── state.rs     stability gate + cooldown
├── decider.rs   rule decisions
├── llm.rs       Ollama HTTP client
├── tmux.rs      list-panes / capture-pane / send-keys wrappers
└── pane.rs      per-pane polling loop
```

---

## Legacy: v0.1 — macOS OCR + Python

The original implementation reads VS Code's terminal panel via macOS Vision OCR and presses keys with pyautogui. It still works, and remains in the repo for users on VS Code who don't want to move their workflow into tmux. Its main limitations — wrong-window key delivery, OCR misreads on small fonts, macOS permission overhead — were the reason for the v0.2 rewrite.

To use v0.1:

```bash
.venv/bin/pip install -r requirements.txt
ollama serve & ollama pull qwen3
.venv/bin/python agent.py --smart --pick-window --gui --app Code --crop 0.2,0.7,1,0.97 --interval 2
```

Detailed setup and design notes are kept under tag [`v0.1.0`](https://github.com/kwangilkimkenny/AutoNod-for-Claude-code/releases/tag/v0.1.0).

---

## Limitations / what autonod is not

- It is an **automation tool, not an unattended system**. Even with the safety guards, leave it running only while you can occasionally glance at the terminal.
- It cannot type free-form answers — questions that need typed input always return `none`.
- It does not push keys to anything outside tmux. If your Claude session is in a plain terminal window, run it under tmux first.
- The cooldown is per-prompt-hash, not per-pane. If two panes show the same prompt in the same 15-second window, only one will be acted on first.

## License

MIT.
