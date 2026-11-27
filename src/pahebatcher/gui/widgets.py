"""Small flat widgets: a black ground with one pink accent."""

from __future__ import annotations

from collections.abc import Callable
import tkinter as tk
from tkinter import font as tkfont
from typing import Any

BG, PANEL, LINE, TEXT = "#000000", "#0b0b0f", "#26262e", "#ececf1"
MUTED, PINK, BAR, TRACK = "#8a8a96", "#ff3f90", "#d5015a", "#1c1c22"
OK, WARN, ERR = "#3ddc97", "#ffb020", "#ff5c5c"

STYLE_COLORS = {
    "brand": PINK, "ok": OK, "warn": WARN, "muted": MUTED, "red": ERR
}


def pick_family(root: tk.Misc, names: list[str]) -> str:
    """Picks the first installed font family from a list.

    Args:
        root: Any Tk widget, used to query the installed families.
        names: Family names in order of preference.

    Returns:
        The first installed name, or the last name if none is installed.
    """
    have = set(tkfont.families(root))
    return next((n for n in names if n in have), names[-1])


class Fonts:
    """The fonts used by the interface."""

    def __init__(self, root: tk.Misc) -> None:
        """Chooses and creates the fonts.

        Args:
            root: Any Tk widget, used to query the installed families.
        """
        ui = pick_family(
            root,
            [
                "Segoe UI",
                "SF Pro Text",
                "Helvetica Neue",
                "Ubuntu",
                "DejaVu Sans",
            ],
        )
        head = pick_family(root, ["Manrope", ui])
        mono = pick_family(
            root,
            [
                "Cascadia Mono",
                "Consolas",
                "SF Mono",
                "Menlo",
                "DejaVu Sans Mono",
            ],
        )
        self.ui = (ui, 10)
        self.small = (ui, 9)
        self.bold = (ui, 10, "bold")
        self.title = (head, 18, "bold")
        self.logo = (head, 22, "bold")
        self.label = (ui, 8, "bold")
        self.mono = (mono, 10)


class Button(tk.Label):
    """A flat button.

    A primary button is solid pink; otherwise it is a quiet outlined ghost.
    """

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None],
        fonts: Fonts,
        primary: bool = False,
        **kw: Any,
    ) -> None:
        """Initializes the button.

        Args:
            master: The parent widget.
            text: The label.
            command: Called when the button is clicked.
            fonts: The interface fonts.
            primary: Whether to draw a solid pink button.
            **kw: Extra options passed to ``tk.Label``.
        """
        self._primary, self._cmd, self._enabled = primary, command, True
        super().__init__(
            master,
            text=text,
            font=fonts.bold,
            padx=16,
            pady=7,
            cursor="hand2",
            highlightthickness=1,
            **kw,
        )
        self._paint(False)
        self.bind("<Enter>", lambda _e: self._paint(True))
        self.bind("<Leave>", lambda _e: self._paint(False))
        self.bind(
            "<Button-1>", lambda _e: self._cmd() if self._enabled else None
        )

    def _paint(self, hover: bool) -> None:
        """Applies the colors for the current state.

        Args:
            hover: Whether the pointer is over the button.
        """
        if not self._enabled:
            self.configure(
                bg=BG, fg="#55555e", highlightbackground=TRACK, cursor="arrow"
            )
        elif self._primary:
            c = PINK if hover else BAR
            self.configure(
                bg=c, fg="#ffffff", highlightbackground=c, cursor="hand2"
            )
        else:
            self.configure(
                bg=BG,
                fg=PINK if hover else TEXT,
                highlightbackground=PINK if hover else LINE,
                cursor="hand2",
            )

    def enable(self, on: bool) -> None:
        """Enables or disables the button.

        Args:
            on: True to enable it.
        """
        self._enabled = on
        self._paint(False)

    def set_command(self, command: Callable[[], None]) -> None:
        self._cmd = command


class Link(tk.Label):
    """A text-only clickable label."""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None],
        fonts: Fonts,
        **kw: Any,
    ) -> None:
        """Initializes the link.

        Args:
            master: The parent widget.
            text: The label.
            command: Called when the link is clicked.
            fonts: The interface fonts.
            **kw: Extra options passed to ``tk.Label``.
        """
        kw.setdefault("bg", BG)
        super().__init__(
            master, text=text, font=fonts.ui, fg=MUTED, cursor="hand2", **kw
        )
        self.bind("<Enter>", lambda _e: self.configure(fg=PINK))
        self.bind("<Leave>", lambda _e: self.configure(fg=MUTED))
        self.bind("<Button-1>", lambda _e: command())


class ThinBar(tk.Canvas):
    """A thin progress bar."""

    def __init__(self, master: tk.Misc, height: int = 4, **kw: Any) -> None:
        """Initializes the bar at 0%.

        Args:
            master: The parent widget.
            height: The bar's height in pixels.
            **kw: Extra options passed to ``tk.Canvas``.
        """
        super().__init__(
            master, height=height, bg=TRACK, highlightthickness=0, bd=0, **kw
        )
        self._frac = 0.0
        self._fill = self.create_rectangle(0, 0, 0, height, fill=BAR, width=0)
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, frac: float) -> None:
        """Sets the progress.

        Args:
            frac: The fraction complete, from 0.0 to 1.0.
        """
        self._frac = max(0.0, min(1.0, frac))
        self._draw()

    def _draw(self) -> None:
        """Redraws the bar."""
        self.coords(
            self._fill,
            0,
            0,
            self.winfo_width() * self._frac,
            self.winfo_height(),
        )


class Segmented(tk.Frame):
    """An inline choice like ``SUB · DUB``.

    The selected value is pink.
    """

    def __init__(
        self,
        master: tk.Misc,
        options: list[tuple[str, Any]],
        value: Any,
        on_change: Callable[[Any], None],
        fonts: Fonts,
    ) -> None:
        """Initializes the control.

        Args:
            master: The parent widget.
            options: ``(label, value)`` pairs.
            value: The initially selected value.
            on_change: Called with the new value when the user picks another.
            fonts: The interface fonts.
        """
        super().__init__(master, bg=BG)
        self._labels: list[tuple[tk.Label, Any]] = []
        self._on_change = on_change
        for i, (text, val) in enumerate(options):
            if i:
                tk.Label(self, text="·", bg=BG, fg=MUTED, font=fonts.ui).pack(
                    side="left", padx=4
                )
            lb = tk.Label(
                self, text=text, bg=BG, font=fonts.bold, cursor="hand2"
            )
            lb.pack(side="left")
            lb.bind("<Button-1>", self._handler(val))
            self._labels.append((lb, val))
        self.set(value)

    def _handler(self, val: Any) -> Callable[[tk.Event[Any]], None]:
        def _on_click(_e: tk.Event[Any]) -> None:
            self.set(val, notify=True)
        return _on_click

    def set(self, value: Any, notify: bool = False) -> None:
        """Selects a value.

        Args:
            value: The value to select.
            notify: Whether to call ``on_change``.
        """
        for lb, val in self._labels:
            lb.configure(fg=PINK if val == value else MUTED)
        if notify:
            self._on_change(value)


def make_entry(master: tk.Misc, fonts: Fonts, **kw: Any) -> tk.Entry:
    """Creates a styled text entry.

    Args:
        master: The parent widget.
        fonts: The interface fonts.
        **kw: Extra options passed to ``tk.Entry``.

    Returns:
        The entry.
    """
    return tk.Entry(
        master,
        font=fonts.ui,
        bg="#15151b",
        fg=TEXT,
        insertbackground=PINK,
        relief="flat",
        highlightthickness=1,
        highlightbackground=LINE,
        highlightcolor=BAR,
        bd=6,
        **kw,
    )


class SlimScrollbar(tk.Canvas):
    """A thin, flat, rounded scrollbar.

    It only shows itself when the content overflows.
    """

    WIDTH = 8

    def __init__(
        self,
        master: tk.Misc,
        command: Callable[..., Any],
        **pack_kw: Any,
    ) -> None:
        """Initializes the scrollbar.

        Args:
            master: The parent widget.
            command: The scroll command of the scrolled widget.
            **pack_kw: Packing options used when the bar is shown.
        """
        super().__init__(
            master,
            width=self.WIDTH + 4,
            bg=BG,
            highlightthickness=0,
            bd=0,
            cursor="arrow",
        )
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
        """Updates the thumb, as the scrolled widget requests.

        Args:
            lo: The first visible fraction.
            hi: The last visible fraction.
        """
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
        """Redraws the thumb."""
        self.delete("all")
        if not self._shown:
            return
        top, bot = self._thumb()
        x0, x1 = 2, 2 + self.WIDTH
        r = self.WIDTH / 2
        color = PINK if (self._hover or self._drag is not None) else "#3a3a44"
        self.create_oval(x0, top, x1, top + 2 * r, fill=color, outline=color)
        self.create_oval(x0, bot - 2 * r, x1, bot, fill=color, outline=color)
        self.create_rectangle(
            x0, top + r, x1, bot - r, fill=color, outline=color
        )

    def _press(self, e: tk.Event[Any]) -> None:
        """Handles a mouse press on the bar.

        Args:
            e: The event.
        """
        top, bot = self._thumb()
        if top <= e.y <= bot:
            self._drag = e.y - top
        else:  # click in the track: jump so the thumb centres on the click
            h = max(self.winfo_height(), 1)
            self._command(
                "moveto", max(0.0, e.y / h - (self._hi - self._lo) / 2)
            )
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
    """Creates a styled listbox with a slim scrollbar.

    Args:
        master: The parent widget.
        fonts: The interface fonts.

    Returns:
        A tuple ``(frame, listbox)``. Pack the frame, and use the listbox.
    """
    frame = tk.Frame(master, bg=BG)
    lb = tk.Listbox(
        frame,
        font=fonts.mono,
        bg=BG,
        fg=TEXT,
        bd=0,
        highlightthickness=0,
        activestyle="none",
        selectbackground="#1a0710",
        selectforeground=PINK,
        exportselection=False,
    )
    sb = SlimScrollbar(frame, lb.yview, side="right", fill="y")
    lb.configure(yscrollcommand=sb.set)
    # claim its strip first so a wide listbox can't squeeze it out
    sb._pack_kw["before"] = lb
    lb.pack(side="left", fill="both", expand=True)
    return frame, lb


class Slider(tk.Canvas):
    """A flat horizontal slider for seeking and volume.

    Clicking jumps to that point and dragging scrubs. ``on_drag`` is called
    with the fraction while the pointer moves, and ``on_commit`` once when it
    is released, so a seek can wait until the user lets go.
    """

    KNOB = 6

    def __init__(
        self,
        master: tk.Misc,
        on_drag: Callable[[float], None] | None = None,
        on_commit: Callable[[float], None] | None = None,
        height: int = 20,
        **kw: Any,
    ) -> None:
        """Initializes the slider at 0.

        Args:
            master: The parent widget.
            on_drag: Called with the fraction while dragging, or None.
            on_commit: Called with the fraction on release, or None.
            height: The widget's height in pixels.
            **kw: Extra options passed to ``tk.Canvas``.
        """
        super().__init__(
            master,
            height=height,
            bg=BG,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            **kw,
        )
        self._on_drag = on_drag
        self._on_commit = on_commit
        self._frac = 0.0
        self._dragging = False
        self._hover = False
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))

    @property
    def dragging(self) -> bool:
        """Whether the user is holding the knob."""
        return self._dragging

    def set(self, frac: float) -> None:
        """Moves the knob, unless the user is dragging it.

        Args:
            frac: The position, from 0.0 to 1.0.
        """
        if self._dragging:
            return
        self._frac = max(0.0, min(1.0, frac))
        self._draw()

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self._draw()

    def _frac_at(self, x: float) -> float:
        span = max(self.winfo_width() - 2 * self.KNOB, 1)
        return max(0.0, min(1.0, (x - self.KNOB) / span))

    def _press(self, e: tk.Event[Any]) -> None:
        self._dragging = True
        self._frac = self._frac_at(e.x)
        self._draw()
        if self._on_drag:
            self._on_drag(self._frac)

    def _move(self, e: tk.Event[Any]) -> None:
        if not self._dragging:
            return
        self._frac = self._frac_at(e.x)
        self._draw()
        if self._on_drag:
            self._on_drag(self._frac)

    def _release(self, _e: tk.Event[Any]) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self._draw()
        if self._on_commit:
            self._on_commit(self._frac)

    def _draw(self) -> None:
        """Redraws the track, the filled part and the knob."""
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            return
        k = self.KNOB
        mid = h / 2
        x = k + (w - 2 * k) * self._frac
        self.create_rectangle(k, mid - 2, w - k, mid + 2, fill=TRACK, width=0)
        self.create_rectangle(k, mid - 2, x, mid + 2, fill=BAR, width=0)
        if self._hover or self._dragging:
            self.create_oval(
                x - k, mid - k, x + k, mid + k, fill=PINK, outline=PINK
            )
