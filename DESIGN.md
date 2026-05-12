# AutoNod — Smart Mode design

An agent extension that monitors the Claude Code CLI inside VS Code at zero cost while it waits for user input, then automatically picks the appropriate option to keep things moving.

## 1. Limitations of the current implementation

The existing `agent.py` has two modes.
- **OCR mode** — presses Enter on a single regex match `^[❯>]\s*1\.\s*Yes\b`. Fast, but a single pattern.
- **VLM mode** — calls a vision model every cycle. Accurate, but takes 6–65 seconds.

Within this structure, the four problems users have reported stem from the causes below.

| User-visible symptom | Root cause |
|---|---|
| Misses the prompt entirely | regex is locked to a single pattern (`1. Yes`); cannot read "Recommended" markers, `(y/n)`, or multi-choice menus |
| Presses Enter at the wrong moment | decides immediately from a single OCR frame — mid-render frames or scrollback can be mistaken for a prompt |
| Cannot branch beyond Yes/No | OCR→regex→`enter` is a one-way pipeline; no module to decide which of N options to press |
| VLM too slow | calling the VLM every cycle is the only option; no cheap text OCR + text LLM routing |

## 2. Design principles

1. **Tiered** — run the cheap, fast checks first; escalate to expensive models only when confidence is low.
2. **If you can read text, use a text LLM** — if characters are OCR-readable on screen, don't use the VLM. The VLM is a fallback for OCR failure.
3. **Press only what you've seen twice** — a stable-frame check blocks false positives.
4. **Hands off when risky** — free-text input prompts, destructive-operation confirmations, or low confidence → return `none`.
5. **Preserve existing behavior** — isolated behind the `--smart` flag. Existing `--ocr`, VLM mode, and `--dry-run` continue to work.

## 3. Architecture

```
                 ┌──── loop every 1–2 seconds ────┐
                 ▼                                 │
[1] Screen capture (existing mss + crop)           │
        │                                          │
        ▼                                          │
[2] macOS Vision OCR (~0.3s, existing ocr.py)      │
        │  lines: list[str]                        │
        ▼                                          │
[3] Stable-frame gate (state.py, new)              │
    - same OCR text N=2 cycles in a row → pass     │
    - if the same prompt was handled within        │
      the last N seconds → blocked by cooldown     │
        │                                          │
        ▼                                          │
[4] PromptFrame parser (parser.py, new)            │
    - kind: 'choice' | 'text' | 'none'             │
    - question                                     │
    - options: [{key, label, recommended,          │
                 is_cursor}]                       │
        │                                          │
        ├── kind == 'none'  ──► action='none'  ────┘
        ├── kind == 'text'  ──► action='none' (free input → human)
        │
        ▼
[5] Rule decider (decider.py, new) — conservative
    - exactly one "Recommended" option → that key (confidence 0.95)
    - inline (y/n) → "y" (confidence 0.85)
    - cursor on a Yes/Allow/OK/Continue/Proceed option → that key
    - exactly 2 options, label #1 is a "Yes" variant + #2 starts with "No" → 1
    - anything else (3+ options, "Yes, …" variants, etc.) is delegated to the LLM
        │
        ▼
[6] Text LLM (qwen3:latest, ~10–30s on demand, timeout 30s)
    - input: PromptFrame + project context (--project-context)
    - output JSON: {action, confidence, reason}
    - confidence < 0.7 → 'none'
        │
        ▼
[7] Optional: VLM verification (--smart-verify, --model qwen2.5vl:7b, …)
    - only when OCR returned 0 lines (prompt rendered graphically)
    - parse the VLM response, then apply cooldown keyed on a hash of the screen bytes
        │
        ▼
[8] Key press (existing press(), focus, click logic, unchanged)
```

## 4. Data structures

```python
@dataclass
class PromptFrame:
    kind: Literal['choice', 'text', 'none']
    question: str | None              # "Do you want to proceed?"
    options: list[Option]
    cursor_idx: int | None            # index of the option the ❯ points at
    raw_lines: list[str]              # raw text, for debug/LLM input

@dataclass
class Option:
    key: str           # '1', '2', 'y', 'a', ...
    label: str         # 'Yes', 'Yes, allow all', 'No, with feedback'
    recommended: bool  # "(recommended)" or "❯" cursor
    is_cursor: bool    # currently at the ❯ position
```

## 5. Parser heuristics

Scan OCR lines top to bottom:

| Pattern | Meaning |
|---|---|
| line ending with `?` | candidate question |
| `^\s*[❯>]\s*(\d+|[a-zA-Z])[.)]\s*(.+)` | option + cursor |
| `^\s*(\d+|[a-zA-Z])[.)]\s*(.+)` | option (no cursor) |
| substring `(recommended)`, `(default)`, `(suggested)` | that option's `recommended=True` |
| inline `\([yY]/[nN]\)` | y/n choice. Synthesize options `[{y, "yes"}, {n, "no"}]` |
| no options at all, only `?` plus a `›`/`>` input line | kind='text' (free input) |
| nothing matches | kind='none' |

## 6. LLM prompt (qwen3:latest)

System:
```
You are a deciding agent for a CLI assistant.
You will be given a parsed prompt and a project context.
Choose exactly one of: '1','2','3','4','5','y','n','a','enter','none'.

Hard rules:
- If the question asks for free-text input, return 'none'.
- If the action being confirmed is destructive (delete, drop, rm -rf,
  force push, overwrite without backup), return 'none'.
- If exactly one option is annotated "(recommended)" or has the cursor,
  prefer that option's key.
- If options are equivalent and the project context does not disambiguate,
  prefer the safest (typically 'Yes, allow once' / option 1).
- If you are not at least 0.7 confident, return 'none' with reason.

Output STRICT JSON, single line:
{"action":"...","confidence":0.0,"reason":"..."}
```

User:
```
PROJECT_CONTEXT:
<excerpt of CLAUDE.md or README, max ~1500 chars>

PROMPT_QUESTION: <question>
PROMPT_OPTIONS:
  [1] Yes  (cursor)
  [2] Yes, and don't ask again
  [3] No, with feedback   (recommended)
RAW_OCR:
<original lines, for tie-breaking>
```

Ollama API: `POST /api/chat` with `format: "json"`, `temperature: 0.0`.

## 7. Stable frames + cooldown

`PromptStateTracker` in `state.py`:
- `prompt_hash` = `sha1(question + sorted(options.label))[:12]`
- the same hash observed in **2 consecutive frames** → "stable"
- once stable, decide + act, then record that hash in the `recently_acted` map with `now+15s`
- if the same hash reappears within the cooldown, immediately return `none` (the already-handled prompt is just still on screen)

This blocks both false positives (flicker) and double-presses (hitting the same prompt twice).

## 8. CLI interface

```bash
# simplest usage — auto-handle the default Claude Code prompts
.venv/bin/python agent.py --smart \
  --app "Visual Studio Code" --crop 0,0.7,1,1 --interval 2

# give the LLM project goals as context
.venv/bin/python agent.py --smart \
  --project-context CLAUDE.md \
  --app "Visual Studio Code" --crop 0,0.7,1,1 --interval 2

# safe mode — dry-run every decision, log only
.venv/bin/python agent.py --smart --dry-run \
  --app "Visual Studio Code" --crop 0,0.7,1,1 --interval 2

# fall back to the VLM when OCR fails
.venv/bin/python agent.py --smart --smart-verify \
  --app "Visual Studio Code" --crop 0,0.7,1,1 --interval 5
```

New flags:
- `--smart` — enable the tiered decider (mutually exclusive with `--ocr`/`--blind`)
- `--smart-llm-model` (default: `qwen3:latest`)
- `--smart-llm-timeout` (default: 30) — LLM HTTP timeout in seconds
- `--smart-verify` — invoke `--model` (VLM) when OCR returns 0 lines
- `--project-context FILE` — file to inject into the LLM context
- `--smart-confidence` (default: 0.7)
- `--smart-stable-frames` (default: 2)
- `--smart-cooldown-sec` (default: 15)

## 9. Safety guards

1. **Destructive-keyword blacklist** (checked in both parser and LLM):
   `delete`, `drop`, `rm -rf`, `force push`, `force-push`, `--no-verify`, `overwrite`, `discard`, `reset --hard`
   → even when `kind='choice'`, always return `none` and hand off to a human.
2. **Allowlist of keys**: pressable keys are only `{1,2,3,4,5,y,n,a,enter,none}`. If the LLM returns anything else → `none`.
3. **--dry-run first** — start out dry-run only for a few days and review the logs.
4. **Preserve FAILSAFE** — moving the mouse to the top-left corner exits immediately (existing behavior).

## 10. File change plan

| File | Status | Contents |
|---|---|---|
| `parser.py` | new | `parse_prompt(lines) -> PromptFrame` |
| `decider.py` | new | `decide(frame, ctx, *, llm_client) -> Decision` (rule path + LLM path) |
| `state.py` | new | `PromptStateTracker` (debounce/stable-frame) |
| `prompts/decide_system.txt` | new | LLM system prompt |
| `agent.py` | modified | `--smart` flag + new branch, existing modes unchanged |
| `tests/test_parser.py` | new | 8–10 cases |
| `tests/test_decider_rule.py` | new | rule path only (LLM is mocked) |
| `README.md` | modified | add a smart-mode section |

Existing `ocr.py`, `ocr_paddle.py`, `picker.py`, and the existing OCR/VLM modes are unchanged.

## 11. Acceptance criteria

- [ ] `--smart --dry-run` produces the correct key in the decision log on a screen actually showing a Claude Code permission prompt (across 3+ prompt cases).
- [ ] 0 triggers across 100 cycles on a normal code-editing screen with no prompt.
- [ ] Presses only once while the same prompt remains on screen (cooldown verification).
- [ ] Returns `none` for prompts that contain words like `delete`/`force push`.
- [ ] Average cycle time < 5 seconds (cycles that call the LLM may take up to ~15–30s).
- [ ] Unit tests pass.

## 12. Step-by-step implementation order

1. `parser.py` + tests
2. `state.py` + tests
3. `decider.py` (rule path only) + tests
4. `decider.py` LLM path
5. `agent.py` `--smart` wiring
6. Live-screen smoke test
7. README update
