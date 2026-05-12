"""Parser unit tests. Run with:  .venv/bin/python -m pytest tests/ -q"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser import parse_prompt  # noqa: E402


def test_simple_yes_no():
    lines = [
        "Some scrollback line",
        "",
        "Do you want to proceed?",
        "❯ 1. Yes",
        "  2. No",
    ]
    f = parse_prompt(lines)
    assert f.kind == "choice"
    assert f.question == "Do you want to proceed?"
    assert [(o.key, o.label, o.is_cursor) for o in f.options] == [
        ("1", "Yes", True),
        ("2", "No", False),
    ]
    assert f.cursor_idx == 0
    assert not f.destructive


def test_three_options_with_recommended_marker_on_third():
    lines = [
        "Allow Bash command for this session?",
        "❯ 1. Yes",
        "  2. Yes, and don't ask again for npm",
        "  3. No, with feedback (recommended)",
    ]
    f = parse_prompt(lines)
    assert f.kind == "choice"
    assert len(f.options) == 3
    assert f.options[2].recommended is True
    # The "(recommended)" marker should be stripped from the label.
    assert "recommended" not in f.options[2].label.lower()
    assert f.options[0].recommended is False
    assert f.options[0].is_cursor is True


def test_inline_yn_prompt():
    lines = [
        "Run npm install? (y/n)",
    ]
    f = parse_prompt(lines)
    assert f.kind == "choice"
    assert [o.key for o in f.options] == ["y", "n"]


def test_free_text_input_is_text_kind():
    lines = [
        "What name would you like to use?",
        "› ",
    ]
    f = parse_prompt(lines)
    assert f.kind == "text"


def test_no_prompt_screen():
    lines = [
        "import foo",
        "def bar():",
        "    return 1",
    ]
    f = parse_prompt(lines)
    assert f.kind == "none"


def test_destructive_keyword_flagged():
    lines = [
        "Delete the local branch and force push? (y/n)",
    ]
    f = parse_prompt(lines)
    assert f.kind == "choice"
    assert f.destructive is True


def test_scrollback_above_real_prompt_is_ignored():
    lines = [
        "Do you want to proceed?",  # past prompt in scrollback
        "❯ 1. Yes",
        "  2. No",
        "[user pressed 1]",
        "running...",
        "Do you want to delete the file?",  # the LIVE prompt
        "❯ 1. Yes",
        "  2. No",
    ]
    f = parse_prompt(lines)
    assert f.kind == "choice"
    # The bottom-most question should win.
    assert f.question == "Do you want to delete the file?"
    assert f.destructive is True


def test_cursor_on_second_option():
    lines = [
        "Pick one?",
        "  1. Foo",
        "❯ 2. Bar",
        "  3. Baz",
    ]
    f = parse_prompt(lines)
    assert f.cursor_idx == 1


def test_hash_is_stable_across_cursor_movement():
    a = parse_prompt(["Q?", "❯ 1. Yes", "  2. No"])
    b = parse_prompt(["Q?", "  1. Yes", "❯ 2. No"])
    # Same question + same options → same hash. Cursor pos doesn't affect.
    assert a.hash() == b.hash()


def test_letter_keyed_options():
    lines = [
        "Continue?",
        "❯ a. Approve all",
        "  b. Skip",
    ]
    f = parse_prompt(lines)
    assert f.kind == "choice"
    assert [o.key for o in f.options] == ["a", "b"]
