"""Tests for the rule path of decider.decide.

LLM path is exercised separately with a fake chat function below.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser import parse_prompt  # noqa: E402
from decider import decide, rule_decide, llm_decide  # noqa: E402


def _frame(*lines):
    return parse_prompt(list(lines))


# ---- rule path ----

def test_rule_picks_recommended_option():
    f = _frame(
        "Continue?",
        "  1. Yes",
        "  2. No (recommended)",
    )
    d = rule_decide(f)
    assert d is not None
    assert d.action == "2"
    assert d.source == "rule"
    assert d.confidence >= 0.9


def test_rule_picks_safe_yes_when_no_marker():
    f = _frame(
        "Run the script?",
        "  1. Yes",
        "  2. No",
    )
    d = rule_decide(f)
    assert d is not None
    assert d.action == "1"


def test_rule_defers_three_option_yesno_cancel_to_llm():
    """Three options without marker/cursor → no longer auto-pressed; LLM
    decides. Prevents the old aggressive behavior of pressing '1' just
    because its label starts with 'Yes'.
    """
    f = _frame(
        "Continue?",
        "  1. Yes",
        "  2. No",
        "  3. Cancel",
    )
    d = rule_decide(f)
    assert d is None  # falls through


def test_rule_defers_when_first_option_is_unsafe_variant():
    f = _frame(
        "Allow Bash?",
        "  1. Yes, and don't ask again",
        "  2. Yes, just this once",
        "  3. No",
    )
    d = rule_decide(f)
    assert d is None


def test_rule_skips_destructive_prompt():
    f = _frame("Delete the branch? (y/n)")
    d = rule_decide(f)
    assert d is not None
    assert d.action == "none"


def test_rule_skips_text_input():
    f = _frame("What is your name?", "› ")
    d = rule_decide(f)
    assert d is not None
    assert d.action == "none"


def test_rule_falls_through_when_options_lack_marker_and_safe_default():
    """Three options, all 'Yes <variant>', no marker — needs LLM."""
    f = _frame(
        "Allow Bash?",
        "  1. Yes, and don't ask again for npm",
        "  2. Yes, and don't ask again for git",
        "  3. Yes, and don't ask again for any command",
    )
    d = rule_decide(f)
    assert d is None  # fall through to LLM


# ---- LLM path with a fake chat function ----

def _fake_chat(response_obj):
    def _fn(_system, _user, _model):
        return json.dumps(response_obj)
    return _fn


def test_llm_path_used_when_rule_falls_through():
    f = _frame(
        "Allow Bash?",
        "  1. Yes, and don't ask again for npm",
        "  2. Yes, and don't ask again for git",
        "  3. Yes, and don't ask again for any command",
    )
    d = decide(
        f,
        project_context="This project uses npm scripts heavily.",
        chat_fn=_fake_chat({"action": "1", "confidence": 0.9,
                            "reason": "project uses npm"}),
    )
    assert d.action == "1"
    assert d.source == "llm"


def test_llm_low_confidence_is_treated_as_none():
    f = _frame(
        "Allow Bash?",
        "  1. Yes, and don't ask again for npm",
        "  2. Yes, and don't ask again for git",
    )
    d = llm_decide(
        f,
        chat_fn=_fake_chat({"action": "1", "confidence": 0.4, "reason": "guess"}),
    )
    assert d.action == "none"


def test_llm_invalid_action_rejected():
    f = _frame(
        "Allow?",
        "  1. Foo",
        "  2. Bar",
        "  3. Baz",
    )
    d = llm_decide(
        f,
        chat_fn=_fake_chat({"action": "99", "confidence": 0.99, "reason": "x"}),
    )
    assert d.action == "none"
    assert d.source == "fallback"


def test_llm_unparseable_output():
    f = _frame(
        "Allow?",
        "  1. Foo",
        "  2. Bar",
        "  3. Baz",
    )
    d = llm_decide(f, chat_fn=lambda *a, **k: "blah blah no json")
    assert d.action == "none"
    assert d.source == "fallback"
