"""Parse OCR lines from a CLI prompt into a structured PromptFrame.

Pure function — no I/O, no model calls. Unit-testable.

Recognises three kinds of frames:
  - 'choice': numbered/lettered options (e.g. Claude Code permission prompts)
  - 'text':   a question with an empty input line / cursor (free-form input)
  - 'none':   no live prompt visible
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Literal


CURSOR_GLYPHS = "❯>›»"
RECOMMENDED_MARKERS = ("(recommended)", "(default)", "(suggested)", "[recommended]")
DESTRUCTIVE_KEYWORDS = (
    "delete", "drop table", "rm -rf", "force push", "force-push",
    "--no-verify", "overwrite", "discard", "reset --hard",
    "wipe", "destroy",
)

# Numbered/lettered option lines: optional cursor, key (1-9 or a-z), . or ),
# then the label.
_OPT_RE = re.compile(
    rf"^\s*(?P<cursor>[{re.escape(CURSOR_GLYPHS)}]\s*)?"
    r"(?P<key>\d{1,2}|[a-zA-Z])\s*[.)]\s+"
    r"(?P<label>.+?)\s*$"
)
# Inline (y/n) prompts.
_YN_INLINE_RE = re.compile(r"\(\s*([yY])\s*/\s*([nN])\s*\)")
# Question line: ends with '?'.
_Q_RE = re.compile(r".+\?\s*$")
# Marker-only line (Vision OCR sometimes splits "(recommended)" off its host
# option line).
_MARKER_ONLY_RE = re.compile(
    r"^\s*[\(\[]\s*(recommended|default|suggested)\s*[\)\]]\s*$",
    re.IGNORECASE,
)


Kind = Literal["choice", "text", "none"]


@dataclass
class Option:
    key: str
    label: str
    recommended: bool = False
    is_cursor: bool = False


@dataclass
class PromptFrame:
    kind: Kind
    question: str | None = None
    options: list[Option] = field(default_factory=list)
    cursor_idx: int | None = None
    raw_lines: list[str] = field(default_factory=list)
    destructive: bool = False

    def hash(self) -> str:
        h = hashlib.sha1()
        h.update((self.question or "").encode("utf-8"))
        for o in sorted(self.options, key=lambda o: o.key):
            h.update(f"|{o.key}={o.label}".encode("utf-8"))
        if not self.question:
            # Without a question line, distinct prompts that share their
            # option labels (e.g. two different "Yes/No" prompts) would
            # collide and trip the cooldown. Mix in the surrounding raw
            # text — but strip cursor glyphs so a cursor-only movement
            # within the same prompt still hashes identically.
            cursor_re = re.compile(rf"[{re.escape(CURSOR_GLYPHS)}]")
            for ln in self.raw_lines[-5:]:
                h.update(b"|R|")
                h.update(cursor_re.sub("", ln.strip()).encode("utf-8"))
        return h.hexdigest()[:12]


def _norm(s: str) -> str:
    return s.strip().lower()


def _has_destructive_keyword(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in DESTRUCTIVE_KEYWORDS)


def _strip_recommended(label: str) -> tuple[str, bool]:
    """Remove every '(recommended)' / '(default)' / etc. marker from the
    label. Returns (cleaned_label, True if any marker was present).
    """
    found = False
    out = label
    while True:
        low = out.lower()
        hit_idx = -1
        hit_marker = ""
        for marker in RECOMMENDED_MARKERS:
            i = low.find(marker)
            if i >= 0 and (hit_idx < 0 or i < hit_idx):
                hit_idx = i
                hit_marker = marker
        if hit_idx < 0:
            break
        out = (out[:hit_idx] + out[hit_idx + len(hit_marker):]).strip(" -–—\t")
        found = True
    return out, found


def _coalesce_marker_lines(raw: list[str]) -> list[str]:
    """Merge orphan '(recommended)' / '(default)' lines into the previous
    option line. Vision OCR sometimes emits the marker as its own
    observation when it sits at the right edge of an option row.
    """
    out: list[str] = []
    for line in raw:
        if _MARKER_ONLY_RE.match(line) and out and _OPT_RE.match(out[-1]):
            out[-1] = out[-1].rstrip() + " " + line.strip()
        else:
            out.append(line)
    return out


def parse_prompt(lines: list[str]) -> PromptFrame:
    """Parse OCR lines → PromptFrame. Robust to scrollback noise above the prompt.

    Strategy: scan from the bottom up. The live prompt is always the last
    block on screen. We collect contiguous option lines, then look upward
    for the nearest question line.
    """
    raw_nonblank = [ln for ln in (l.rstrip() for l in lines) if ln.strip()]
    if not raw_nonblank:
        return PromptFrame(kind="none", raw_lines=lines)

    raw = _coalesce_marker_lines(raw_nonblank)

    # ---- pass 1: find option block (contiguous from the bottom) ----
    options: list[Option] = []
    cursor_idx: int | None = None
    last_opt_line_idx: int | None = None
    first_opt_line_idx: int | None = None

    for i in range(len(raw) - 1, -1, -1):
        m = _OPT_RE.match(raw[i])
        if m:
            if last_opt_line_idx is None:
                last_opt_line_idx = i
            first_opt_line_idx = i
            label_raw = m.group("label")
            label, recommended = _strip_recommended(label_raw)
            opt = Option(
                key=m.group("key").lower(),
                label=label,
                recommended=recommended,
                is_cursor=bool(m.group("cursor")),
            )
            options.insert(0, opt)
        else:
            # Stop the option block as soon as we hit a non-option after
            # finding at least one option.
            if last_opt_line_idx is not None:
                break

    # Reject single anonymous option lines like "Step 1. install …" that
    # are almost always natural text rather than a real prompt. A genuine
    # CLI prompt block has either a cursor glyph or 2+ options.
    if options:
        has_cursor = any(o.is_cursor for o in options)
        if len(options) == 1 and not has_cursor:
            options = []
            first_opt_line_idx = None
            last_opt_line_idx = None

    if options:
        # Cursor index — the option line whose match had a cursor glyph.
        for idx, o in enumerate(options):
            if o.is_cursor:
                cursor_idx = idx
                break

        # Question = the nearest line above the option block that ends with '?'.
        question: str | None = None
        if first_opt_line_idx is not None:
            for j in range(first_opt_line_idx - 1, -1, -1):
                if _Q_RE.match(raw[j]):
                    question = raw[j].strip()
                    break

        destructive = _has_destructive_keyword(
            (question or "") + " " + " ".join(o.label for o in options)
        )

        return PromptFrame(
            kind="choice",
            question=question,
            options=options,
            cursor_idx=cursor_idx,
            raw_lines=lines,
            destructive=destructive,
        )

    # ---- pass 2: inline (y/n) shorthand ----
    for i in range(len(raw) - 1, -1, -1):
        if _YN_INLINE_RE.search(raw[i]):
            question = raw[i].strip()
            destructive = _has_destructive_keyword(question)
            return PromptFrame(
                kind="choice",
                question=question,
                options=[
                    Option(key="y", label="yes"),
                    Option(key="n", label="no"),
                ],
                cursor_idx=None,
                raw_lines=lines,
                destructive=destructive,
            )

    # ---- pass 3: free-text input prompt ----
    # A trailing '?' question with no options below it = waiting for typed text.
    for i in range(len(raw) - 1, -1, -1):
        if _Q_RE.match(raw[i]):
            return PromptFrame(
                kind="text",
                question=raw[i].strip(),
                raw_lines=lines,
            )

    return PromptFrame(kind="none", raw_lines=lines)
