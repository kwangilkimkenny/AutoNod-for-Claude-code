# OCR fixtures

Each `*.txt` is one line per macOS Vision OCR observation, in the order
the parser will actually see them. These mirror real-world quirks that
synthetic in-test strings tend to hide:

- `recommended_split_across_lines.txt` — Vision often emits the
  `(recommended)` marker as its own observation, separate from the
  option line it visually sits on. The parser must merge them.
- `scrollback_above_live_prompt.txt` — an earlier resolved prompt sits
  above the current live one. Only the live one (bottom-most) should
  drive the decision.
- `noise_line_with_one_dot_one.txt` — a stray "1. Install …" line in
  output text. Must NOT be treated as a choice prompt (no cursor, single
  option).
- `claude_code_three_options.txt` — canonical Claude Code permission
  prompt with three numbered options, cursor on first.
- `inline_yn.txt` — `(y/n)` inline shorthand.
- `free_text_input.txt` — a question waiting for typed text.
