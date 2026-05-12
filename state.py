"""Stability gate for the smart loop.

Two responsibilities:
  1. Require N consecutive identical frames before letting a decision
     through. Filters out mid-render flicker that otherwise produces
     false-positive Enter presses.
  2. Suppress a re-press of the same prompt within a cooldown window —
     because after we press, the prompt may stay on screen for a moment
     while the next render happens.

Pure logic, no I/O.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque


@dataclass
class GateDecision:
    proceed: bool
    reason: str


class PromptStateTracker:
    def __init__(self, stable_frames: int = 2, cooldown_sec: float = 15.0):
        self._stable_required = max(1, stable_frames)
        self._cooldown = cooldown_sec
        self._recent: Deque[str] = deque(maxlen=self._stable_required)
        self._acted: dict[str, float] = {}

    def observe(self, prompt_hash: str | None) -> GateDecision:
        """Record one frame's hash. Return whether the caller may act on it.

        Pass None when no actionable prompt is on screen — that resets the
        stability streak.
        """
        now = time.time()
        self._gc(now)

        if prompt_hash is None:
            self._recent.clear()
            return GateDecision(False, "no actionable prompt")

        self._recent.append(prompt_hash)

        if len(self._recent) < self._stable_required:
            return GateDecision(
                False,
                f"need {self._stable_required} stable frames "
                f"(have {len(self._recent)})",
            )
        if any(h != prompt_hash for h in self._recent):
            return GateDecision(False, "frame not yet stable")

        last = self._acted.get(prompt_hash)
        if last is not None and now - last < self._cooldown:
            return GateDecision(
                False,
                f"cooldown ({self._cooldown - (now - last):.1f}s left)",
            )
        return GateDecision(True, "stable")

    def mark_acted(self, prompt_hash: str) -> None:
        self._acted[prompt_hash] = time.time()

    def _gc(self, now: float) -> None:
        # Expire entries older than 5x cooldown to keep memory bounded.
        cutoff = now - self._cooldown * 5
        stale = [k for k, t in self._acted.items() if t < cutoff]
        for k in stale:
            del self._acted[k]
