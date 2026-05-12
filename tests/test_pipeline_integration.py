"""Offline integration test: simulate the smart loop end-to-end with
synthetic OCR lines. Proves parser → state → rule decider compose
correctly without needing a real screen or LLM.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser import parse_prompt
from decider import decide
from state import PromptStateTracker


def _step(state: PromptStateTracker, lines: list[str]) -> tuple[str, str]:
    frame = parse_prompt(lines)
    h = None if frame.kind == "none" else frame.hash()
    gate = state.observe(h)
    if not gate.proceed:
        return "none", f"gated: {gate.reason}"
    decision = decide(frame, project_context=None,
                     # Force rule-only path by failing the chat call.
                     chat_fn=lambda *a, **k: "")
    if decision.action != "none":
        state.mark_acted(h)
    return decision.action, f"[{decision.source}] {decision.reason}"


def test_two_frame_stability_blocks_first_then_acts():
    state = PromptStateTracker(stable_frames=2, cooldown_sec=15)
    lines = ["Run it?", "  1. Yes", "  2. No"]
    a1, _ = _step(state, lines)
    assert a1 == "none"  # first sighting → must wait for second frame
    a2, _ = _step(state, lines)
    assert a2 == "1"


def test_cooldown_blocks_double_press():
    state = PromptStateTracker(stable_frames=2, cooldown_sec=15)
    lines = ["Run it?", "  1. Yes", "  2. No"]
    _step(state, lines)  # first frame: gated
    a, _ = _step(state, lines)  # second frame: pressed
    assert a == "1"
    a3, msg = _step(state, lines)  # third+ frames: cooldown
    assert a3 == "none"
    assert "cooldown" in msg


def test_destructive_prompt_never_pressed():
    state = PromptStateTracker(stable_frames=1, cooldown_sec=15)
    lines = ["Delete the local branch? (y/n)"]
    a, msg = _step(state, lines)
    assert a == "none"
    assert "destructive" in msg


def test_no_prompt_resets_streak():
    state = PromptStateTracker(stable_frames=2, cooldown_sec=15)
    lines = ["Run it?", "  1. Yes", "  2. No"]
    _step(state, lines)  # frame 1
    _step(state, ["import foo"])  # blank screen — streak resets
    a, _ = _step(state, lines)  # back to prompt — needs another frame
    assert a == "none"


def test_recommended_marker_takes_priority_over_first_option():
    state = PromptStateTracker(stable_frames=1, cooldown_sec=15)
    lines = [
        "Continue?",
        "  1. Yes",
        "  2. No (recommended)",
    ]
    a, _ = _step(state, lines)
    assert a == "2"


def test_text_input_prompt_returns_none():
    state = PromptStateTracker(stable_frames=1, cooldown_sec=15)
    lines = ["What name?", "› "]
    a, _ = _step(state, lines)
    assert a == "none"
