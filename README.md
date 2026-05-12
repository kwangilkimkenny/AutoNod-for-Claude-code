# AutoNod

An agent that reads the screen via OCR on macOS and automatically responds to **Claude Code permission prompts** inside VS Code.

- **Cheap, fast decisions first**: macOS Vision OCR (~0.3s) + rule-based decider. Falls back to a local LLM only when ambiguous.
- **Safety guards**: same frame stable twice + 15s cooldown after handling + destructive keyword blocking + free-text input avoidance.
- **Window lock**: at startup, click once with the mouse to designate the single VS Code window to operate on.

## Quick start

### 1. Prerequisites (one-time)

```bash
# Python dependencies
.venv/bin/pip install -r requirements.txt

# Local LLM (Ollama)
brew install ollama
ollama serve &
ollama pull qwen3
```

macOS permissions — open System Settings → Privacy & Security, add your current terminal (Terminal/iTerm) to **both** **Screen Recording** and **Accessibility**, then **fully quit and relaunch the terminal**.

### 2. Verify — confirm the capture region once (no keys pressed)

```bash
.venv/bin/python agent.py --smart --dry-run --once \
  --pick-by-click --app Code --crop 0.2,0.7,1,0.97 \
  --smart-stable-frames 1 --save-shot /tmp/shot.png
```

After running, **click the VS Code window you want to operate within 30 seconds** to lock it.

Two things to check:
```bash
open /tmp/shot.png                       # visually confirm only the terminal panel was cropped
.venv/bin/python ocr.py /tmp/shot.png    # confirm OCR read the prompt line
```

If `/tmp/shot.png` is off, adjust the `--crop` ratios (see [Tuning the capture region](#tuning-the-capture-region) below).

### 3. Repeated dry-run — false-positive regression check

```bash
.venv/bin/python agent.py --smart --dry-run \
  --pick-by-click --app Code --crop 0.2,0.7,1,0.97 --interval 2
```

On normal screens with no prompt, you should only see `smart kind=none` repeating. If a spurious `action='1'` appears, the region contains text that looks like an option — narrow `--crop`.

### 4. Real operation — keys are actually pressed

```bash
.venv/bin/python agent.py --smart \
  --pick-by-click --app Code --crop 0.2,0.7,1,0.97 --interval 2
```

**Emergency stop**: move the mouse to the top-left corner of the screen → pyautogui FAILSAFE exits immediately. Or `Ctrl+C` in the terminal.

## How it works

```
every 1–2 seconds ──┐
                    ▼
[Capture] mss captures only the designated region of the locked VS Code window
                    ▼
[OCR] macOS Vision (~0.3s). Sorted by boundingBox, fragments on the same row are joined.
                    ▼
[Parser] '1. Yes', '(recommended)', '(y/n)', etc. → PromptFrame
                    ▼
[Stability gate] same frame twice in a row + 15s cooldown
                    ▼
[Rule decider] (a) single recommended marker (b) cursor on Yes
               (c) exactly Yes/No 2-choice → immediate key decision
                    ▼
[LLM decider] qwen3:latest returns JSON within 30s. If confidence<0.7, 'none'.
                    ▼
[Key press] one of 1/2/3/y/n/enter via pyautogui
```

Free-text input prompts (only `?`, no options) or destructive keywords (`delete`, `rm -rf`, `force push`, `--no-verify`, `reset --hard`, …) are always handed off to a human.

For design details, see [`DESIGN.md`](DESIGN.md).

## Key flags

| Flag | Default | Description |
|---|---|---|
| `--smart` | off | enable tiered decider (mutually exclusive with `--ocr`/`--blind`) |
| `--dry-run` | off | log decisions only, do not press keys |
| `--once` | off | exit after a single cycle |
| `--pick-by-click` | off | lock the target window via mouse click at startup |
| `--pick-timeout` | `30` | seconds to wait for the click above |
| `--app NAME` | (none) | partial match on window owner (VS Code = `Code`) |
| `--crop l,t,r,b` | (none) | capture region. All values in [0,1] = ratios; otherwise pixels |
| `--interval` | `5.0` | loop interval (seconds). `2` recommended for Smart |
| `--smart-stable-frames` | `2` | N consecutive identical frames required before deciding |
| `--smart-cooldown-sec` | `15` | seconds to block re-pressing for the same prompt |
| `--smart-llm-model` | `qwen3:latest` | Ollama text model |
| `--smart-llm-timeout` | `30` | LLM HTTP timeout (seconds) |
| `--smart-confidence` | `0.7` | LLM confidence threshold |
| `--smart-verify` | off | fall back to VLM (`--model`) only when OCR returns 0 lines |
| `--project-context` | (none) | file to include in the LLM context (e.g. `CLAUDE.md`) |
| `--save-shot PATH` | (none) | save the captured PNG (debug) |

For all options, run `.venv/bin/python agent.py --help`.

## Tuning the capture region

`--crop` supports two notations.

- **Ratio** (`l,t,r,b` — all 0–1): left, top, right, bottom ratios within the window. e.g. `0.2,0.7,1,0.97` = "from 20% left, from 70% top, with a 3% margin from the bottom-right".
- **Pixel** (`x,y,w,h`): pixel coordinates relative to the top-left of the window.

Recommended: capture once with `--save-shot /tmp/shot.png`, inspect via `open /tmp/shot.png`, and adjust the ratios by however much it's off. In VS Code, excluding the left sidebar + status bar tends to crop the terminal panel cleanly.

## Reading the logs

| Log | Meaning |
|---|---|
| `smart kind=none` | no prompt (normal) |
| `smart hash=… gate=need 2 stable frames (have 1)` | 1st frame, waiting for the next cycle (normal) |
| `kind=choice action='1' [rule c=0.85] safe single-shot Yes …` | rule path decided immediately |
| `[rule c=0.95] option 'N' marked recommended` | decided via the (recommended) marker |
| `[llm c=0.82] …` | decided after an LLM call |
| `[fallback …] llm error: …` | Ollama down / model not installed → handed off to a human |
| `[rule c=1.00] destructive keyword detected` | destructive keyword blocked |
| `gate=cooldown (12.4s left)` | prompt just handled, re-press blocked (normal) |

## Troubleshooting

| Symptom | Action |
|---|---|
| `no window found for app='Visual Studio Code'` | the owner name is `Code`. Use `--app Code` or check via `--list-windows` |
| Capture is black | Screen Recording permission missing → add the terminal and fully relaunch it |
| Keys don't press | Accessibility permission missing → same action |
| `connection refused` | check that `ollama serve` is running |
| `model not found: qwen3:latest` | `ollama pull qwen3`, or use `--smart-llm-model llama3.2` etc. |
| Spurious `action='1'` on normal screens | narrow `--crop` to exclude the sidebar/status bar |
| Click selects the wrong window | drop `--app` or specify it differently. Or use `--pick-window --gui` for the GUI picker |
| Captures the wrong area after moving the window | restart the agent (window coordinates are measured once at startup) |

## Tests

```bash
.venv/bin/python -m pytest tests/ -q   # 38 passed
```

## Appendix: simple OCR / VLM modes

For when you want to auto-respond to a single pattern without Smart mode.

```bash
# regex match on one line (~0.5s, no hallucinations)
.venv/bin/python agent.py --ocr \
  --app Code --crop 0.2,0.7,1,0.97 --interval 1
# default pattern: ^[❯>]\s*1\.\s*Yes\b

# custom regex
.venv/bin/python agent.py --ocr --ocr-pattern '\([Yy]/[Nn]\)' ...

# vision LLM (slow, for visual reasoning)
ollama pull qwen2.5vl:7b
.venv/bin/python agent.py --model qwen2.5vl:7b \
  --prompt-file prompts/yes_to_proceed.txt \
  --app Code --crop 0.2,0.7,1,0.97 --interval 8
```

`--smart`, `--ocr`, and `--blind` cannot be enabled simultaneously (mutually exclusive).

## Limitations / caveats

- In multi-monitor setups, moving the window shifts the coordinates → restart.
- Because it sends global key input, clicking another window during operation may send the keys there. `--pick-by-click` locks the window coordinates at startup, but does not force focus to stay (`press()` attempts a click+focus right before the keystroke, but it's not 100% safe).
- Recommended to shut it down when you step away. It's an automation tool, not an unattended system.
