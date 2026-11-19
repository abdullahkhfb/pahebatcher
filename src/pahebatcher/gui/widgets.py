"""Small flat widgets in the animepahe palette: black ground, one pink accent."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import font as tkfont
from typing import Any

BG, PANEL, LINE, TEXT = "#000000", "#0b0b0f", "#26262e", "#ececf1"
MUTED, PINK, BAR, TRACK = "#8a8a96", "#ff3f90", "#d5015a", "#1c1c22"
OK, WARN, ERR = "#3ddc97", "#ffb020", "#ff5c5c"

STYLE_COLORS = {"brand": PINK, "ok": OK, "warn": WARN, "muted": MUTED, "red": ERR}


def pick_family(root: tk.Misc, names: list[str]) -> str:
    have = set(tkfont.families(root))
    return next((n for n in names if n in have), names[-1])


class Fonts:
    def __init__(self, root: tk.Misc) -> None:
        ui = pick_family(root, ["Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "DejaVu Sans"])
        head = pick_family(root, ["Manrope", ui])
        mono = pick_family(root, ["Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono"])
        self.ui = (ui, 10)
        self.small = (ui, 9)
        self.bold = (ui, 10, "bold")
        self.title = (head, 18, "bold")
        self.logo = (head, 22, "bold")
        self.label = (ui, 8, "bold")
        self.mono = (mono, 10)


class Button(tk.Label):
    """Flat button. ``primary`` = solid pink, otherwise a quiet outlined ghost."""

    def __init__(self, master: tk.Misc, text: str, command: Callable[[], None], fonts: Fonts,
                 primary: bool = False, **kw: Any) -> None:
        self._primary, self._cmd, self._enabled = primary, command, True
        super().__init__(master, text=text, font=fonts.bold, padx=16, pady=7, cursor="hand2",
                         highlightthickness=1, **kw)
        self._paint(False)
        self.bind("<Enter>", lambda _e: self._paint(True))
        self.bind("<Leave>", lambda _e: self._paint(False))
        self.bind("<Button-1>", lambda _e: self._cmd() if self._enabled else None)

    def _paint(self, hover: bool) -> None:
        if not self._enabled:
            self.configure(bg=BG, fg="#55555e", highlightbackground=TRACK, cursor="arrow")
        elif self._primary:
            c = PINK if hover else BAR
            self.configure(bg=c, fg="#ffffff", highlightbackground=c, cursor="hand2")
        else:
            self.configure(bg=BG, fg=PINK if hover else TEXT,
                           highlightbackground=PINK if hover else LINE, cursor="hand2")

    def enable(self, on: bool) -> None:
        self._enabled = on
        self._paint(False)

    def set_command(self, command: Callable[[], None]) -> None:
        self._cmd = command


class Link(tk.Label):
    """Text-only clickable label."""

    def __init__(
        self, master: tk.Misc, text: str, command: Callable[[], None], fonts: Fonts, **kw: Any,
    ) -> None:
        kw.setdefault("bg", BG)
        super().__init__(master, text=text, font=fonts.ui, fg=MUTED, cursor="hand2", **kw)
        self.bind("<Enter>", lambda _e: self.configure(fg=PINK))
        self.bind("<Leave>", lambda _e: self.configure(fg=MUTED))
        self.bind("<Button-1>", lambda _e: command())


class ThinBar(tk.Canvas):
    def __init__(self, master: tk.Misc, height: int = 4, **kw: Any) -> None:
        super().__init__(master, height=height, bg=TRACK, highlightthickness=0, bd=0, **kw)
        self._frac = 0.0
        self._fill = self.create_rectangle(0, 0, 0, height, fill=BAR, width=0)
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, frac: float) -> None:
        self._frac = max(0.0, min(1.0, frac))
        self._draw()

    def _draw(self) -> None:
        self.coords(self._fill, 0, 0, self.winfo_width() * self._frac, self.winfo_height())


class Segmented(tk.Frame):
    """Inline choice like ``SUB · DUB`` — the selected value is pink."""

    def __init__(self, master: tk.Misc, options: list[tuple[str, Any]], value: Any,
                 on_change: Callable[[Any], None], fonts: Fonts) -> None:
        super().__init__(master, bg=BG)
        self._labels: list[tuple[tk.Label, Any]] = []
        self._on_change = on_change
        for i, (text, val) in enumerate(options):
            if i:
                tk.Label(self, text="·", bg=BG, fg=MUTED, font=fonts.ui).pack(side="left", padx=4)
            lb = tk.Label(self, text=text, bg=BG, font=fonts.bold, cursor="hand2")
            lb.pack(side="left")
            lb.bind("<Button-1>", self._handler(val))
            self._labels.append((lb, val))
        self.set(value)

    def _handler(self, val: Any) -> Callable[[tk.Event[Any]], None]:
        def _on_click(_e: tk.Event[Any]) -> None:
            self.set(val, notify=True)
        return _on_click

    def set(self, value: Any, notify: bool = False) -> None:
        for lb, val in self._labels:
            lb.configure(fg=PINK if val == value else MUTED)
        if notify:
            self._on_change(value)


def make_entry(master: tk.Misc, fonts: Fonts, **kw: Any) -> tk.Entry:
    return tk.Entry(master, font=fonts.ui, bg="#15151b", fg=TEXT, insertbackground=PINK, relief="flat",
                    highlightthickness=1, highlightbackground=LINE, highlightcolor=BAR, bd=6, **kw)


class SlimScrollbar(tk.Canvas):
    """Thin, flat, rounded scrollbar that only shows itself when the content overflows."""

    WIDTH = 8

    def __init__(self, master: tk.Misc, command: Callable[..., Any], **pack_kw: Any) -> None:
        super().__init__(master, width=self.WIDTH + 4, bg=BG, highlightthickness=0, bd=0, cursor="arrow")
        self._command = command
        self._pack_kw = pack_kw or {"side": "right", "fill": "y"}
        self._lo, self._hi = 0.0, 1.0
        self._shown = False
        self._drag: float | None = None
        self._hover = False
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))

    # the widget being scrolled calls this (yscrollcommand)
    def set(self, lo: Any, hi: Any) -> None:
        self._lo, self._hi = float(lo), float(hi)
        need = self._lo > 0.0 or self._hi < 1.0
        if need and not self._shown:
            self.pack(**self._pack_kw)
            self._shown = True
        elif not need and self._shown:
            self.pack_forget()
            self._shown = False
        self._draw()

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self._draw()

    def _thumb(self) -> tuple[float, float]:
        h = max(self.winfo_height(), 1)
        top = self._lo * h
        bot = max(self._hi * h, top + 28)
        if bot > h:
            top, bot = max(h - (bot - top), 0), h
        return top, bot

    def _draw(self) -> None:
        self.delete("all")
        if not self._shown:
            return
        top, bot = self._thumb()
        x0, x1 = 2, 2 + self.WIDTH
        r = self.WIDTH / 2
        color = PINK if (self._hover or self._drag is not None) else "#3a3a44"
        self.create_oval(x0, top, x1, top + 2 * r, fill=color, outline=color)
        self.create_oval(x0, bot - 2 * r, x1, bot, fill=color, outline=color)
        self.create_rectangle(x0, top + r, x1, bot - r, fill=color, outline=color)

    def _press(self, e: tk.Event[Any]) -> None:
        top, bot = self._thumb()
        if top <= e.y <= bot:
            self._drag = e.y - top
        else:  # click in the track: jump so the thumb centres on the click
            h = max(self.winfo_height(), 1)
            self._command("moveto", max(0.0, e.y / h - (self._hi - self._lo) / 2))
            self._drag = (bot - top) / 2
        self._draw()

    def _move(self, e: tk.Event[Any]) -> None:
        if self._drag is None:
            return
        h = max(self.winfo_height(), 1)
        self._command("moveto", min(max((e.y - self._drag) / h, 0.0), 1.0))

    def _release(self, _e: tk.Event[Any]) -> None:
        self._drag = None
        self._draw()


def make_listbox(master: tk.Misc, fonts: Fonts) -> tuple[tk.Frame, tk.Listbox]:
    frame = tk.Frame(master, bg=BG)
    lb = tk.Listbox(
        frame, font=fonts.mono, bg=BG, fg=TEXT, bd=0, highlightthickness=0, activestyle="none",
        selectbackground="#1a0710", selectforeground=PINK, exportselection=False,
    )
    sb = SlimScrollbar(frame, lb.yview, side="right", fill="y")
    lb.configure(yscrollcommand=sb.set)
    sb._pack_kw["before"] = lb  # claim its strip first so a wide listbox can't squeeze it out
    lb.pack(side="left", fill="both", expand=True)
    return frame, lb
