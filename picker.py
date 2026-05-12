"""GUI window picker (Tkinter). Shows a list of windows + live preview."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import mss
from PIL import Image, ImageTk


def pick_window_gui(candidates: list[dict],
                    multi: bool = False) -> list[dict]:
    """
    Show candidates in a Tk window. Returns a list of chosen windows.
    multi=False: at most one chosen (legacy behavior, single-element list).
    multi=True: zero or more chosen via Cmd/Shift-click.
    Each candidate: {owner, title, x, y, w, h}.
    """
    if not candidates:
        return []

    root = tk.Tk()
    root.title("AutoNod — pick window(s)" if multi else "AutoNod — pick a window")
    root.geometry("980x620")

    selected: dict[str, list[dict]] = {"wins": []}
    img_ref: dict[str, ImageTk.PhotoImage | None] = {"img": None}

    paned = ttk.Panedwindow(root, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=10, pady=10)

    # Left: list
    left = ttk.Frame(paned)
    paned.add(left, weight=1)
    hint = ("여러 창 선택: Cmd/Shift-click" if multi else "단일 창 선택")
    ttk.Label(left, text=f"Windows  ({hint})",
              font=("", 13, "bold")).pack(anchor="w")
    listbox = tk.Listbox(
        left, width=44, activestyle="dotbox",
        selectmode=(tk.EXTENDED if multi else tk.BROWSE),
    )
    listbox.pack(fill="both", expand=True, pady=(4, 0))
    for c in candidates:
        title = c["title"] or "(no title)"
        listbox.insert("end",
                       f"{c['owner']}  •  {title}  ({c['w']}×{c['h']})")

    # Right: preview
    right = ttk.Frame(paned)
    paned.add(right, weight=2)
    info = ttk.Label(right, text="Click a window to preview", anchor="w")
    info.pack(fill="x")
    preview = tk.Label(right, background="#222", anchor="center")
    preview.pack(fill="both", expand=True, pady=(4, 0))

    def render_preview(_=None) -> None:
        sel = listbox.curselection()
        if not sel:
            return
        c = candidates[sel[0]]
        bbox = {"left": c["x"], "top": c["y"],
                "width": c["w"], "height": c["h"]}
        try:
            with mss.MSS() as sct:
                shot = sct.grab(bbox)
                img = Image.frombytes("RGB", shot.size, shot.bgra,
                                      "raw", "BGRX")
            preview.update_idletasks()
            avail_w = max(200, preview.winfo_width() - 8)
            avail_h = max(200, preview.winfo_height() - 8)
            img.thumbnail((avail_w, avail_h), Image.LANCZOS)
            tkimg = ImageTk.PhotoImage(img)
            img_ref["img"] = tkimg
            preview.configure(image=tkimg, text="")
            info.configure(text=f"{c['owner']} — {c['title'] or '(no title)'}  "
                                f"@ {c['x']},{c['y']}  {c['w']}×{c['h']}")
        except Exception as e:
            preview.configure(image="", text=f"Preview failed: {e}",
                              foreground="white")

    listbox.bind("<<ListboxSelect>>", render_preview)
    listbox.bind("<Double-Button-1>", lambda _e: on_select())

    # Buttons
    btnbar = ttk.Frame(root)
    btnbar.pack(fill="x", padx=10, pady=(0, 10))

    def on_select() -> None:
        sel = list(listbox.curselection())
        if sel:
            selected["wins"] = [candidates[i] for i in sel]
            root.destroy()

    def on_cancel() -> None:
        root.destroy()

    ttk.Button(btnbar, text="Cancel", command=on_cancel).pack(side="right")
    ttk.Button(btnbar, text="Select", command=on_select).pack(
        side="right", padx=(0, 6))

    listbox.selection_set(0)
    root.after(50, render_preview)
    root.mainloop()
    return selected["wins"]
