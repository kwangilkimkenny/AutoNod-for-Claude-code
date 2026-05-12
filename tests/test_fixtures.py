"""Fixture-driven regression tests. Each fixture is a real-world-shaped OCR
output (one observation per line) — closer to what Vision actually emits
than the hand-crafted inline strings in the unit tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser import parse_prompt  # noqa: E402
from decider import rule_decide  # noqa: E402


FIX_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[str]:
    return FIX_DIR.joinpath(name).read_text(encoding="utf-8").splitlines()


def test_recommended_marker_split_across_lines_is_merged():
    """Vision often emits '(recommended)' as a separate observation —
    parser must merge it back so the rule path picks the right key.
    """
    f = parse_prompt(_load("recommended_split_across_lines.txt"))
    assert f.kind == "choice"
    assert len(f.options) == 3
    # The 3rd option carries the (recommended) marker after merge.
    assert f.options[2].key == "3"
    assert f.options[2].recommended is True
    assert "recommended" not in f.options[2].label.lower()
    d = rule_decide(f)
    assert d is not None
    assert d.action == "3"


def test_scrollback_above_live_prompt():
    f = parse_prompt(_load("scrollback_above_live_prompt.txt"))
    assert f.kind == "choice"
    # The live (bottom) question wins.
    assert f.question == "Continue?"
    assert len(f.options) == 2
    assert f.options[0].is_cursor is True


def test_stray_one_dot_one_in_output_is_not_a_prompt():
    """'Note: 1. Install ...' must NOT trigger a choice frame — no cursor,
    single 'option' → reject as natural text.
    """
    f = parse_prompt(_load("noise_line_with_one_dot_one.txt"))
    assert f.kind == "none"


def test_claude_code_three_options_picks_cursor_yes():
    f = parse_prompt(_load("claude_code_three_options.txt"))
    assert f.kind == "choice"
    assert len(f.options) == 3
    assert f.options[0].is_cursor is True
    d = rule_decide(f)
    assert d is not None
    assert d.action == "1"
    assert "cursor" in d.reason.lower() or "yes" in d.reason.lower()


def test_inline_yn_fixture():
    f = parse_prompt(_load("inline_yn.txt"))
    assert f.kind == "choice"
    assert [o.key for o in f.options] == ["y", "n"]
    d = rule_decide(f)
    assert d is not None
    assert d.action == "y"


def test_free_text_fixture_returns_none():
    f = parse_prompt(_load("free_text_input.txt"))
    assert f.kind == "text"
    d = rule_decide(f)
    assert d is not None
    assert d.action == "none"


# ---- additional parser regressions for the hardening pass ----

def test_single_option_without_cursor_is_rejected():
    f = parse_prompt(["1. Foo"])
    assert f.kind == "none"


def test_single_option_with_cursor_is_accepted():
    f = parse_prompt(["❯ 1. Yes"])
    assert f.kind == "choice"
    assert len(f.options) == 1
    assert f.options[0].is_cursor is True


def test_multiple_recommended_markers_stripped_idempotently():
    f = parse_prompt([
        "Choose?",
        "  1. A (recommended) (default)",
        "  2. B",
    ])
    assert f.options[0].recommended is True
    assert "recommended" not in f.options[0].label.lower()
    assert "default" not in f.options[0].label.lower()


def test_hash_distinguishes_prompts_when_question_is_missing():
    """If OCR misses the '?' line, the hash must still discriminate using
    surrounding raw text — otherwise two unrelated Yes/No prompts collide
    and the cooldown blocks the second one wrongly.
    """
    a = parse_prompt(["doing thing X", "  1. Yes", "  2. No"])
    b = parse_prompt(["doing thing Y", "  1. Yes", "  2. No"])
    assert a.question is None and b.question is None
    assert a.hash() != b.hash()


def test_hash_distinguishes_prompts_when_question_is_present():
    a = parse_prompt(["Question A?", "  1. Yes", "  2. No"])
    b = parse_prompt(["Question B?", "  1. Yes", "  2. No"])
    assert a.hash() != b.hash()
