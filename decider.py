"""Decision engine for the smart loop.

Two paths:
  - rule_decide: cheap, no model call. Returns a Decision when the answer
    is unambiguous (Recommended marker, cursor-on-Yes, canonical Yes/No).
  - llm_decide: calls a local Ollama text model with the parsed
    PromptFrame + project context.

The orchestrator (agent.py --smart) tries rule_decide first and only
falls through to llm_decide if the rule path returns None.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from parser import PromptFrame, Option

OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
ALLOWED_ACTIONS = {"1", "2", "3", "4", "5", "y", "n", "a", "enter", "none"}
SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "decide_system.txt"
DEFAULT_LLM_MODEL = "qwen3:latest"
DEFAULT_LLM_TIMEOUT_SEC = 30


@dataclass
class Decision:
    action: str        # one of ALLOWED_ACTIONS
    confidence: float  # 0..1
    reason: str
    source: str        # 'rule' | 'llm' | 'fallback'


SAFE_FIRST_LABEL = re.compile(r"^\s*(yes|allow|ok|continue|proceed)\b", re.IGNORECASE)
UNSAFE_FIRST_LABEL = re.compile(
    r"don'?t ask again|for the rest of|always allow|never ask",
    re.IGNORECASE,
)
# Strict canonical labels — matched as the entire stripped label.
CANONICAL_YES_LABEL = re.compile(
    r"^(yes|yes,?\s*proceed|yes,?\s*continue|allow|ok|continue|proceed)$",
    re.IGNORECASE,
)
CANONICAL_NO_LABEL_PREFIX = re.compile(r"^(no|cancel|abort)\b", re.IGNORECASE)


def _safe_default_option(frame: PromptFrame) -> Option | None:
    """Return the safest option to auto-pick, or None.

    Conservative — only fires when the assistant has clearly indicated the
    safe answer:
      (a) inline (y/n) prompt with literal 'yes' as the y option, OR
      (b) cursor sits on a Yes/Allow/OK/Continue/Proceed option, OR
      (c) exactly two options with canonical 'Yes' / 'No' labels and no
          'don't ask again' variants.

    All other ambiguous cases are deferred to the LLM path.
    """
    by_key = {o.key: o for o in frame.options}
    cand = by_key.get("1") or by_key.get("y")
    if not cand:
        return None
    if UNSAFE_FIRST_LABEL.search(cand.label):
        return None

    cand_label = cand.label.strip().lower()

    # (a) Inline (y/n) — synthesised by the parser with labels "yes"/"no".
    if (len(frame.options) == 2
            and {o.key for o in frame.options} == {"y", "n"}
            and cand.key == "y"
            and cand_label == "yes"):
        return cand

    # (b) Cursor on a Yes-style option.
    if cand.is_cursor and SAFE_FIRST_LABEL.search(cand.label):
        return cand

    # (c) Exactly two options, canonical Yes / No-prefixed labels.
    if (len(frame.options) == 2
            and cand.key == "1"
            and CANONICAL_YES_LABEL.match(cand_label)):
        other = by_key.get("2")
        if other and CANONICAL_NO_LABEL_PREFIX.match(other.label.strip()):
            return cand

    return None


def rule_decide(frame: PromptFrame) -> Decision | None:
    """Cheap, deterministic decisions. Returns None if LLM is needed."""
    if frame.kind != "choice":
        return Decision(
            action="none",
            confidence=1.0,
            reason=f"kind={frame.kind} → no auto-keypress",
            source="rule",
        )
    if frame.destructive:
        return Decision(
            action="none",
            confidence=1.0,
            reason="destructive keyword detected — handing to human",
            source="rule",
        )
    if not frame.options:
        return Decision(
            action="none", confidence=1.0,
            reason="no options parsed", source="rule",
        )

    recommended = [o for o in frame.options if o.recommended]
    if len(recommended) == 1:
        return Decision(
            action=recommended[0].key,
            confidence=0.95,
            reason=f"option {recommended[0].key!r} marked recommended",
            source="rule",
        )

    safe = _safe_default_option(frame)
    if safe:
        return Decision(
            action=safe.key,
            confidence=0.85,
            reason=f"safe single-shot Yes on key {safe.key!r}",
            source="rule",
        )
    return None


# ---------- LLM path ----------

def _format_frame_for_llm(frame: PromptFrame, project_context: str | None) -> str:
    parts = []
    if project_context:
        parts.append("PROJECT_CONTEXT:")
        parts.append(project_context.strip()[:1500])
        parts.append("")
    parts.append(f"PROMPT_QUESTION: {frame.question or '(none)'}")
    parts.append("PROMPT_OPTIONS:")
    for o in frame.options:
        marks = []
        if o.is_cursor:
            marks.append("cursor")
        if o.recommended:
            marks.append("recommended")
        suffix = f"  ({', '.join(marks)})" if marks else ""
        parts.append(f"  [{o.key}] {o.label}{suffix}")
    parts.append("")
    parts.append("RAW_OCR:")
    parts.extend(frame.raw_lines[-30:])  # cap to last 30 lines
    return "\n".join(parts)


def _load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _call_ollama_chat(model: str, system: str, user: str,
                      timeout: int = DEFAULT_LLM_TIMEOUT_SEC) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0},
    }
    r = requests.post(OLLAMA_CHAT_URL, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json().get("message", {}).get("content", "")


def _parse_llm_response(text: str) -> Decision | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    action = str(obj.get("action", "")).strip().lower()
    if action not in ALLOWED_ACTIONS:
        return None
    try:
        conf = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    reason = str(obj.get("reason", "")).strip()[:200]
    return Decision(action=action, confidence=conf, reason=reason, source="llm")


def llm_decide(
    frame: PromptFrame,
    project_context: str | None = None,
    *,
    model: str = DEFAULT_LLM_MODEL,
    confidence_threshold: float = 0.7,
    timeout: int = DEFAULT_LLM_TIMEOUT_SEC,
    chat_fn: Callable[[str, str, str], str] | None = None,
) -> Decision:
    """Call the local text LLM. `chat_fn` is injectable for tests."""
    if frame.kind != "choice":
        return Decision("none", 1.0, f"kind={frame.kind}", "fallback")

    system = _load_system_prompt()
    user = _format_frame_for_llm(frame, project_context)
    fn = chat_fn or (
        lambda s, u, _m=model, _t=timeout: _call_ollama_chat(_m, s, u, _t))
    try:
        raw = fn(system, user, model)
    except Exception as e:  # noqa: BLE001
        return Decision("none", 0.0, f"llm error: {e}", "fallback")

    parsed = _parse_llm_response(raw)
    if parsed is None:
        return Decision("none", 0.0, f"unparseable llm output: {raw[:120]!r}",
                        "fallback")
    if parsed.confidence < confidence_threshold:
        return Decision(
            "none", parsed.confidence,
            f"low confidence ({parsed.confidence:.2f}): {parsed.reason}",
            "llm",
        )
    return parsed


def decide(
    frame: PromptFrame,
    project_context: str | None = None,
    *,
    model: str = DEFAULT_LLM_MODEL,
    confidence_threshold: float = 0.7,
    timeout: int = DEFAULT_LLM_TIMEOUT_SEC,
    chat_fn: Callable[[str, str, str], str] | None = None,
) -> Decision:
    """Top-level: rule path → LLM path."""
    rule = rule_decide(frame)
    if rule is not None:
        return rule
    return llm_decide(
        frame, project_context,
        model=model,
        confidence_threshold=confidence_threshold,
        timeout=timeout,
        chat_fn=chat_fn,
    )
