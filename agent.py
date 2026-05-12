"""
Local VLM screen agent.

Loop:
  1. Capture screen (full / app window / sub-region) with mss
  2. Send to local Ollama VLM (default: qwen2.5vl:7b)
  3. Parse the model's chosen action: 1, 2, 3, enter, or none
  4. Press the key with pyautogui (skipped when --dry-run)

macOS permissions required:
  - Screen Recording (System Settings > Privacy & Security > Screen Recording)
  - Accessibility (same panel) for key input

Usage:
  python agent.py --dry-run                            # full screen, no keys
  python agent.py --list-windows                       # list app windows + bounds
  python agent.py --app Code --dry-run                 # capture only VS Code
  python agent.py --app Code --crop 0,0.7,1,1 --once   # bottom 30% (terminal)
  python agent.py --app Code --crop 0,720,1600,400     # absolute pixel crop
  python agent.py --save-shot /tmp/shot.png --once     # save what was sent
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

import mss
import pyautogui
import requests
from PIL import Image

from decider import (
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TIMEOUT_SEC,
    decide as smart_decide,
)
from parser import parse_prompt
from state import PromptStateTracker

OLLAMA_URL = "http://localhost:11434/api/generate"

DEFAULT_PROMPT = (
    "You are controlling a screen. Look at the image and decide which single key "
    "to press: '1', '2', '3', 'enter', or 'none' (if no action is needed).\n"
    "Respond ONLY in this exact JSON format, nothing else:\n"
    '{"action": "1" | "2" | "3" | "enter" | "none", "reason": "short reason"}'
)

VALID_ACTIONS = {"1", "2", "3", "enter", "none"}


# ---------- macOS window discovery (Quartz) ----------

def list_windows(app_filter: str | None = None) -> list[dict]:
    """Return on-screen windows with owner/title/bounds (logical coords)."""
    from Quartz import (  # type: ignore
        CGWindowListCopyWindowInfo,
        kCGNullWindowID,
        kCGWindowListExcludeDesktopElements,
        kCGWindowListOptionOnScreenOnly,
    )
    options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    out: list[dict] = []
    for w in CGWindowListCopyWindowInfo(options, kCGNullWindowID) or []:
        owner = w.get("kCGWindowOwnerName", "") or ""
        title = w.get("kCGWindowName", "") or ""
        b = w.get("kCGWindowBounds", {}) or {}
        if app_filter and app_filter.lower() not in owner.lower():
            continue
        out.append({
            "owner": owner,
            "title": title,
            "x": int(b.get("X", 0)),
            "y": int(b.get("Y", 0)),
            "w": int(b.get("Width", 0)),
            "h": int(b.get("Height", 0)),
        })
    return out


def find_window(app: str, title_substr: str | None = None) -> dict | None:
    cands = [c for c in list_windows(app) if c["w"] > 100 and c["h"] > 100]
    if title_substr:
        cands = [c for c in cands if title_substr.lower() in c["title"].lower()]
    if not cands:
        return None
    cands.sort(key=lambda c: c["w"] * c["h"], reverse=True)
    return cands[0]


def find_window_under_click(app_filter: str | None = None,
                            timeout_sec: float = 30.0) -> dict | None:
    """Wait for the user to left-click somewhere, then return the front-most
    window (optionally filtered to `app_filter`) under that click point.

    Useful when several windows of the same app are open and the user wants
    to point at the specific one to operate on.
    """
    from Quartz import (  # type: ignore
        CGEventSourceButtonState,
        kCGEventSourceStateHIDSystemState,
        kCGMouseButtonLeft,
    )
    msg = ("[agent] click the target window now"
           + (f" (app filter: {app_filter!r})" if app_filter else "")
           + f" — timeout {timeout_sec:.0f}s")
    print(msg)
    t0 = time.time()
    last = bool(CGEventSourceButtonState(
        kCGEventSourceStateHIDSystemState, kCGMouseButtonLeft))
    while time.time() - t0 < timeout_sec:
        cur = bool(CGEventSourceButtonState(
            kCGEventSourceStateHIDSystemState, kCGMouseButtonLeft))
        if cur and not last:
            # Wait a beat for the OS to bring the clicked window to the
            # front, then snapshot the cursor and the z-ordered window list.
            time.sleep(0.10)
            pos = pyautogui.position()
            print(f"[agent] click at ({pos.x}, {pos.y})")
            for w in list_windows(app_filter):
                if (w["w"] > 100 and w["h"] > 100
                        and w["x"] <= pos.x < w["x"] + w["w"]
                        and w["y"] <= pos.y < w["y"] + w["h"]):
                    return w
            print(f"[agent] no matching window under cursor "
                  f"(app={app_filter!r})", file=sys.stderr)
            return None
        last = cur
        time.sleep(0.04)
    print("[agent] click timeout", file=sys.stderr)
    return None


def pick_window_interactive(app_filter: str | None = None,
                            title_substr: str | None = None,
                            multi: bool = False) -> list[dict]:
    cands = [c for c in list_windows(app_filter) if c["w"] > 100 and c["h"] > 100]
    if title_substr:
        cands = [c for c in cands if title_substr.lower() in c["title"].lower()]
    if not cands:
        return []
    cands.sort(key=lambda c: (c["owner"].lower(), -(c["w"] * c["h"])))
    print("\nAvailable windows:")
    for i, c in enumerate(cands):
        title = c["title"] or "(no title — grant Screen Recording permission to see)"
        print(f"  [{i}] {c['owner']:<22} {c['x']:>5},{c['y']:>5} "
              f"{c['w']:>5}x{c['h']:<5}  {title}")
    hint = "numbers like '0,2,3'" if multi else "a single number"
    while True:
        choice = input(f"\nPick {hint} (or 'q' to quit): ").strip()
        if choice.lower() == "q":
            return []
        try:
            idxs = [int(x.strip()) for x in choice.split(",") if x.strip()]
            if not multi and len(idxs) != 1:
                print("Enter exactly one number (multi-select disabled).")
                continue
            if all(0 <= i < len(cands) for i in idxs) and idxs:
                return [cands[i] for i in idxs]
        except ValueError:
            pass
        print(f"Enter {hint} in range 0..{len(cands) - 1}, or 'q'.")


def parse_crop(spec: str, win_w: int, win_h: int) -> tuple[int, int, int, int]:
    """
    Parse '--crop x,y,w,h'.
    If all four values are in [0,1], treat as fractions of the window.
    Otherwise treat as logical pixels relative to the window origin.
    Returns (x, y, w, h) in logical pixels relative to window origin.
    """
    parts = [float(p.strip()) for p in spec.split(",")]
    if len(parts) != 4:
        raise ValueError("--crop expects 'x,y,w,h'")
    if all(0.0 <= p <= 1.0 for p in parts):
        x = int(parts[0] * win_w)
        y = int(parts[1] * win_h)
        w = max(1, int(parts[2] * win_w) - x if parts[2] > parts[0] else int(parts[2] * win_w))
        h = max(1, int(parts[3] * win_h) - y if parts[3] > parts[1] else int(parts[3] * win_h))
        # Interpret as left,top,right,bottom when last two > first two; else as x,y,w,h
        if parts[2] > parts[0] and parts[3] > parts[1]:
            return x, y, w, h
        return int(parts[0] * win_w), int(parts[1] * win_h), \
               max(1, int(parts[2] * win_w)), max(1, int(parts[3] * win_h))
    return int(parts[0]), int(parts[1]), max(1, int(parts[2])), max(1, int(parts[3]))


# ---------- Capture ----------

def capture_b64(bbox: dict | None, max_side: int) -> str:
    """bbox=None -> full primary monitor. Otherwise mss-style dict."""
    with mss.MSS() as sct:
        region = bbox if bbox is not None else sct.monitors[1]
        shot = sct.grab(region)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def save_b64_png(b64: str, path: str) -> None:
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))


# ---------- VLM ----------

def ask_vlm(model: str, prompt: str, image_b64: str,
            timeout: int = 120) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"temperature": 0.0},
    }
    r = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json().get("response", "")


DEFAULT_OCR_PATTERN = r"^[❯>]\s*1\.\s*Yes\b"


def ocr_decide(lines: list[str], pattern: str) -> tuple[str, str]:
    """Pure-OCR decision: scan lines for the trigger pattern."""
    rx = re.compile(pattern, re.IGNORECASE)
    for ln in lines:
        s = ln.strip()
        if rx.search(s):
            return "enter", f"matched: {s!r}"
    return "none", f"no line matched /{pattern}/"


def parse_action(text: str) -> tuple[str, str]:
    """Extract action+reason. Tolerates extra text around the JSON."""
    m = re.search(r"\{.*?\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            action = str(obj.get("action", "")).strip().lower()
            reason = str(obj.get("reason", "")).strip()
            if action in VALID_ACTIONS:
                return action, reason
        except json.JSONDecodeError:
            pass

    low = text.lower()
    for cand in ("enter", "1", "2", "3"):
        if re.search(rf"\b{cand}\b", low):
            return cand, "fallback parse"
    return "none", "could not parse"


def focus_app(app_name: str) -> None:
    """Bring the named app to front via AppleScript. Best-effort."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'tell application "{app_name}" to activate'],
            check=False, timeout=2,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.15)  # let focus settle before keystroke
    except Exception as e:
        print(f"[agent] focus_app failed: {e}", file=sys.stderr)


def press(action: str,
          focus_target: str | None = None,
          focus_bbox: dict | None = None) -> None:
    if action == "none":
        return
    # If we have a bbox, a click at that screen location both activates the
    # correct window (under that pixel) and focuses the right pane — in one
    # action. Skip osascript activate in that case, because activating an app
    # by name picks "the most recent window of that app", which may be a
    # different window than the one we're actually watching.
    if focus_bbox:
        prev = pyautogui.position()
        cx = focus_bbox["left"] + focus_bbox["width"] // 2
        cy = focus_bbox["top"] + focus_bbox["height"] // 2
        pyautogui.click(cx, cy)
        time.sleep(0.10)
        try:
            pyautogui.moveTo(prev.x, prev.y, _pause=False)
        except Exception:
            pass
    elif focus_target:
        focus_app(focus_target)
    key = "enter" if action == "enter" else action
    pyautogui.press(key)


# ---------- Main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5vl:7b")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="Decide but do not press keys")
    ap.add_argument("--once", action="store_true",
                    help="Run a single iteration and exit")
    ap.add_argument("--prompt", default=None,
                    help="Prompt text. If omitted and --prompt-file not given, "
                         "uses the built-in default.")
    ap.add_argument("--prompt-file", default=None,
                    help="Path to a text file containing the prompt "
                         "(takes precedence over --prompt)")
    ap.add_argument("--max-side", type=int, default=1280)
    ap.add_argument("--app", default=None,
                    help="App owner name to capture (e.g. 'Code' for VS Code)")
    ap.add_argument("--window-title", default=None,
                    help="Substring to disambiguate among the app's windows")
    ap.add_argument("--crop", default=None,
                    help="Sub-region within the window. "
                         "Either fractions 'l,t,r,b' (each in [0,1]) "
                         "or pixels 'x,y,w,h'.")
    ap.add_argument("--list-windows", action="store_true",
                    help="Print on-screen windows and exit")
    ap.add_argument("--pick-window", action="store_true",
                    help="Interactively pick the window(s) from a numbered list")
    ap.add_argument("--pick-by-click", action="store_true",
                    help="Wait for a mouse click and lock onto the window "
                         "under the click point. Combine with --app to "
                         "filter by app (e.g. --app Code).")
    ap.add_argument("--pick-timeout", type=float, default=30.0,
                    help="Seconds to wait for the --pick-by-click click "
                         "(default: 30)")
    ap.add_argument("--gui", action="store_true",
                    help="With --pick-window, show a GUI picker with previews")
    ap.add_argument("--multi", action="store_true",
                    help="Allow picking multiple windows at once "
                         "(GUI: Cmd/Shift-click, terminal: '0,2,3')")
    ap.add_argument("--save-shot", default=None,
                    help="Save the captured PNG to this path (debug)")
    ap.add_argument("--no-focus", action="store_true",
                    help="Do NOT auto-focus the target app before pressing keys "
                         "(default: focus is enabled when a window is targeted)")
    ap.add_argument("--no-click-focus", action="store_true",
                    help="Do NOT click into the captured region before pressing "
                         "keys (default: click is enabled when --crop is set, "
                         "needed to focus the right pane like VS Code terminal)")
    mode_group = ap.add_mutually_exclusive_group()
    mode_group.add_argument("--blind", action="store_true",
                    help="Skip all detection. Just press --blind-key every "
                         "--interval seconds (still respects --app/--crop "
                         "for focus + click).")
    mode_group.add_argument("--ocr", action="store_true",
                    help="OCR-only mode (no VLM).")
    mode_group.add_argument("--smart", action="store_true",
                    help="Smart tiered mode: OCR → parser → rule → text LLM. "
                         "VLM only as optional fallback (--smart-verify).")
    ap.add_argument("--blind-key", default="enter",
                    help="Key to press in --blind mode (default: enter)")
    ap.add_argument("--ocr-engine", choices=["vision", "paddle"],
                    default="vision",
                    help="OCR backend. 'vision' = macOS Vision (~0.5s, default). "
                         "'paddle' = PaddleOCR (~7s on Apple Silicon).")
    ap.add_argument("--ocr-lang", default="en",
                    help="OCR language code (paddle only, e.g. 'en','korean','ch')")
    ap.add_argument("--ocr-pattern", default=DEFAULT_OCR_PATTERN,
                    help=f"Regex matched against each OCR line. "
                         f"Match → 'enter'. Default: {DEFAULT_OCR_PATTERN!r}")
    ap.add_argument("--smart-llm-model", default=DEFAULT_LLM_MODEL,
                    help=f"Ollama text model for the LLM decision step "
                         f"(default: {DEFAULT_LLM_MODEL})")
    ap.add_argument("--smart-confidence", type=float, default=0.7,
                    help="LLM confidence threshold (default: 0.7)")
    ap.add_argument("--smart-llm-timeout", type=int,
                    default=DEFAULT_LLM_TIMEOUT_SEC,
                    help=f"LLM HTTP timeout in seconds "
                         f"(default: {DEFAULT_LLM_TIMEOUT_SEC})")
    ap.add_argument("--smart-stable-frames", type=int, default=2,
                    help="Require N identical frames before acting "
                         "(default: 2)")
    ap.add_argument("--smart-cooldown-sec", type=float, default=15.0,
                    help="Suppress re-press for the same prompt within N "
                         "seconds (default: 15)")
    ap.add_argument("--smart-verify", action="store_true",
                    help="When OCR returns no text, fall back to the VLM "
                         "(--model) for a verify-pass. Useful when the "
                         "prompt is rendered with graphics rather than text.")
    ap.add_argument("--project-context", default=None,
                    help="Path to a file (e.g. CLAUDE.md) excerpted into "
                         "the LLM context to inform decisions.")
    args = ap.parse_args()

    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            prompt = f.read()
    else:
        prompt = args.prompt if args.prompt is not None else DEFAULT_PROMPT

    if args.list_windows:
        for w in list_windows(args.app):
            print(f"{w['owner']!r:<22} {w['x']:>5},{w['y']:>5} "
                  f"{w['w']:>5}x{w['h']:<5}  title={w['title']!r}")
        return 0

    wins: list[dict] = []
    if args.pick_by_click:
        if args.multi:
            print("[agent] --pick-by-click does not support --multi",
                  file=sys.stderr)
            return 2
        win = find_window_under_click(args.app, timeout_sec=args.pick_timeout)
        if not win:
            print("[agent] no window picked by click", file=sys.stderr)
            return 2
        wins = [win]
    elif args.pick_window:
        cands = [c for c in list_windows(args.app)
                 if c["w"] > 100 and c["h"] > 100]
        if args.window_title:
            cands = [c for c in cands
                     if args.window_title.lower() in c["title"].lower()]
        if args.gui:
            from picker import pick_window_gui
            wins = pick_window_gui(cands, multi=args.multi)
        else:
            wins = pick_window_interactive(args.app, args.window_title,
                                           multi=args.multi)
        if not wins:
            print("[agent] no window picked", file=sys.stderr)
            return 2
    elif args.app:
        win = find_window(args.app, args.window_title)
        if not win:
            print(f"[agent] no window found for app={args.app!r} "
                  f"title~={args.window_title!r}", file=sys.stderr)
            return 2
        wins = [win]
    elif args.crop:
        print("[agent] --crop requires --app, --pick-window, or "
              "--pick-by-click", file=sys.stderr)
        return 2

    targets: list[dict] = []
    if wins:
        for w_ in wins:
            x, y, w, h = w_["x"], w_["y"], w_["w"], w_["h"]
            if args.crop:
                cx, cy, cw, ch = parse_crop(args.crop, w, h)
                x, y, w, h = x + cx, y + cy, cw, ch
            bbox = {"left": x, "top": y, "width": w, "height": h}
            t = {
                "win": w_,
                "bbox": bbox,
                "focus_target": None if args.no_focus else w_["owner"],
                "click_bbox": (bbox if args.crop and not args.no_click_focus
                               else None),
                "label": (w_["title"] or w_["owner"])[:40],
            }
            targets.append(t)
            print(f"[agent] target: {w_['owner']!r} "
                  f"title={w_['title']!r} bbox={bbox}")
    else:
        # Full-screen mode (no window targeted)
        targets.append({
            "win": None, "bbox": None,
            "focus_target": None, "click_bbox": None,
            "label": "fullscreen",
        })

    pyautogui.FAILSAFE = True
    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print(f"[agent] {mode} model={args.model} interval={args.interval}s "
          f"targets={len(targets)} "
          f"(move mouse to top-left corner to abort)")

    project_context: str | None = None
    if args.smart and args.project_context:
        try:
            project_context = Path(args.project_context).read_text(
                encoding="utf-8")
            print(f"[agent] smart: loaded {len(project_context)} chars of "
                  f"project context from {args.project_context}")
        except Exception as e:
            print(f"[agent] WARN: could not read --project-context "
                  f"{args.project_context}: {e}", file=sys.stderr)

    if args.smart:
        for t in targets:
            t["state"] = PromptStateTracker(
                stable_frames=args.smart_stable_frames,
                cooldown_sec=args.smart_cooldown_sec,
            )
        verify = " +vlm-verify" if args.smart_verify else ""
        print(f"[agent] smart mode: llm={args.smart_llm_model!r} "
              f"confidence>={args.smart_confidence} "
              f"timeout={args.smart_llm_timeout}s "
              f"stable_frames={args.smart_stable_frames} "
              f"cooldown={args.smart_cooldown_sec}s{verify}")

    while True:
        t0 = time.time()
        for ti, target in enumerate(targets):
            tag = f"[{ti}:{target['label']}]" if len(targets) > 1 else ""
            try:
                _run_cycle(args, target, tag, prompt, project_context)
            except KeyboardInterrupt:
                print("\n[agent] stopped")
                return 0
            except requests.exceptions.RequestException as e:
                print(f"[agent] {tag} ollama error: {e}", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                # Don't let a single bad cycle (OCR glitch, pyautogui hiccup,
                # weird OS condition) kill a long-running daemon.
                print(f"[agent] {tag} cycle error: {e!r}", file=sys.stderr)
                traceback.print_exc()

        if args.once:
            return 0
        sleep_left = max(0.0, args.interval - (time.time() - t0))
        time.sleep(sleep_left)


def _ocr_lines(img: Image.Image, engine: str, lang: str) -> list[str]:
    if engine == "paddle":
        from ocr_paddle import recognize_lines as _ocr
        return _ocr(img, lang=lang)
    from ocr import recognize_lines as _ocr
    return _ocr(img)


def _run_cycle(args, target: dict, tag: str,
               prompt: str, project_context: str | None) -> None:
    """One iteration over one target. Raises only on KeyboardInterrupt /
    truly fatal errors — the main loop catches everything else.
    """
    bbox = target["bbox"]

    if args.blind:
        action = args.blind_key
        reason = "blind mode"
        print(f"[{time.strftime('%H:%M:%S')}] {tag} "
              f"action={action!r} reason={reason}")
        if not args.dry_run:
            press(action,
                  focus_target=target["focus_target"],
                  focus_bbox=target["click_bbox"])
        return

    t1 = time.time()
    img_b64 = capture_b64(bbox, args.max_side)
    if args.save_shot:
        save_b64_png(img_b64, args.save_shot)

    if args.smart:
        _run_smart_cycle(args, target, tag, img_b64, t1, project_context)
        return

    if args.ocr:
        img = Image.open(io.BytesIO(base64.b64decode(img_b64)))
        lines = _ocr_lines(img, args.ocr_engine, args.ocr_lang)
        action, reason = ocr_decide(lines, args.ocr_pattern)
    else:
        raw = ask_vlm(args.model, prompt, img_b64)
        action, reason = parse_action(raw)
    elapsed = time.time() - t1
    print(f"[{time.strftime('%H:%M:%S')}] {tag} "
          f"action={action!r} ({elapsed:.2f}s) reason={reason}")
    if action != "none" and not args.dry_run:
        press(action,
              focus_target=target["focus_target"],
              focus_bbox=target["click_bbox"])


def _run_smart_cycle(args, target: dict, tag: str,
                     img_b64: str, t1: float,
                     project_context: str | None) -> None:
    img = Image.open(io.BytesIO(base64.b64decode(img_b64)))
    lines = _ocr_lines(img, args.ocr_engine, args.ocr_lang)
    frame = parse_prompt(lines)
    state: PromptStateTracker = target["state"]

    action: str = "none"
    reason: str = ""

    if frame.kind == "none":
        # OCR empty / no actionable prompt. If --smart-verify is on AND OCR
        # returned literally nothing (often: screen is graphical), fall back
        # to the VLM.
        if args.smart_verify and not lines:
            try:
                raw = ask_vlm(args.model, _verify_prompt(), img_b64,
                              timeout=args.smart_llm_timeout)
                v_action, v_reason = parse_action(raw)
            except requests.exceptions.RequestException as e:
                v_action, v_reason = "none", f"vlm error: {e}"
            # Use a screen-bytes hash so the cooldown still applies — we
            # don't want to press the same screen twice in a row.
            h = hashlib.sha1(img_b64[-2000:].encode("ascii")).hexdigest()[:12]
            gate = state.observe(h)
            if gate.proceed and v_action not in ("none", ""):
                action, reason = v_action, f"[vlm-verify] {v_reason}"
                state.mark_acted(h)
            else:
                action = "none"
                reason = (f"[vlm-verify gated:{gate.reason}] {v_reason}"
                          if not gate.proceed else f"[vlm-verify] {v_reason}")
            print(f"[{time.strftime('%H:%M:%S')}] {tag} "
                  f"smart kind=none verify action={action!r} "
                  f"({time.time() - t1:.2f}s) reason={reason}")
        else:
            state.observe(None)
            print(f"[{time.strftime('%H:%M:%S')}] {tag} "
                  f"smart kind=none ({time.time() - t1:.2f}s)")
        if action != "none" and not args.dry_run:
            press(action,
                  focus_target=target["focus_target"],
                  focus_bbox=target["click_bbox"])
        return

    h = frame.hash()
    gate = state.observe(h)
    if not gate.proceed:
        print(f"[{time.strftime('%H:%M:%S')}] {tag} "
              f"smart hash={h} gate={gate.reason} "
              f"({time.time() - t1:.2f}s)")
        return

    decision = smart_decide(
        frame, project_context,
        model=args.smart_llm_model,
        confidence_threshold=args.smart_confidence,
        timeout=args.smart_llm_timeout,
    )
    action = decision.action
    reason = (f"[{decision.source} c={decision.confidence:.2f}] "
              f"{decision.reason}")
    elapsed = time.time() - t1
    print(f"[{time.strftime('%H:%M:%S')}] {tag} "
          f"smart hash={h} kind={frame.kind} "
          f"action={action!r} ({elapsed:.2f}s) "
          f"reason={reason}")
    if action != "none":
        state.mark_acted(h)
        if not args.dry_run:
            press(action,
                  focus_target=target["focus_target"],
                  focus_bbox=target["click_bbox"])


_VERIFY_PROMPT = (
    "A CLI prompt is on screen but OCR could not extract text. "
    "Look at the image. If you see a confirmation prompt with options, "
    "choose one of '1','2','3','enter'. If you cannot tell, answer 'none'. "
    "Respond ONLY in this JSON: "
    '{"action":"1"|"2"|"3"|"enter"|"none","reason":"short"}'
)


def _verify_prompt() -> str:
    return _VERIFY_PROMPT


if __name__ == "__main__":
    sys.exit(main())
