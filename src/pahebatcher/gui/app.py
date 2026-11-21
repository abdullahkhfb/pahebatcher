"""Desktop app (Tkinter).

Search, pick episodes, and watch the downloads finish.
"""

from __future__ import annotations

from collections.abc import Callable
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Any
import webbrowser

from pahebatcher import config, ffmpeg_setup, models, updater, utils
from pahebatcher.gui import bridge, widgets
from pahebatcher.ui import dashboard

PIP_CMD = 'pip install "pahebatcher[browser]"'
EST_MB = {360: 50, 720: 90, 1080: 150}
MAX_CONTENT = 1280  # content stays readable on wide / full-screen windows


class GuiDashboard(dashboard.Dashboard):
    """The terminal dashboard's state machine without the terminal.

    The GUI polls its state instead of rendering it.
    """

    def start(self) -> None:  # no Rich Live
        return

    def stop(self) -> None:
        return


def clean_title(title: str) -> str:
    """Cleans a title for display.

    Args:
        title: The raw title, which may be empty.

    Returns:
        The title without a trailing SUB or DUB marker, or an em dash if
        nothing is left.
    """
    return (
        re.sub(r"\s+(?:DUB|SUB)\s*$", "", title or "", flags=re.I).strip()
        or "—"
    )


# ── download rows ────────────────────────────────────────────────────────────
class EpRow(tk.Frame):
    """One episode's progress row in the download view."""

    def __init__(self, master: tk.Misc, key: str, fonts: widgets.Fonts) -> None:
        """Builds the row.

        Args:
            master: The parent widget.
            key: The episode's key, shown as the row name.
            fonts: The interface fonts.
        """
        super().__init__(master, bg=widgets.BG)
        self.columnconfigure(2, weight=1)
        self.name = tk.Label(
            self,
            text=f"EP {key}",
            font=fonts.mono,
            fg=widgets.MUTED,
            bg=widgets.BG,
            width=7,
            anchor="w",
        )
        self.state = tk.Label(
            self, font=fonts.mono, bg=widgets.BG, width=15, anchor="w"
        )
        self.bar = widgets.ThinBar(self, height=3)
        self.detail = tk.Label(
            self,
            font=fonts.mono,
            fg=widgets.MUTED,
            bg=widgets.BG,
            width=24,
            anchor="e",
        )
        self.name.grid(row=0, column=0, pady=5)
        self.state.grid(row=0, column=1, padx=(8, 12), pady=5)
        self.bar.grid(row=0, column=2, sticky="ew", pady=5)
        self.detail.grid(row=0, column=3, padx=(12, 0), pady=5)

    def update_from(self, ep: Any, now: float) -> None:
        """Updates the row from the dashboard's state.

        Args:
            ep: The dashboard's state for this episode.
            now: The current monotonic time.
        """
        glyph, style = dashboard._STATES.get(ep.state, (ep.state, "muted"))
        self.state.configure(
            text=ep.note or glyph,
            fg=widgets.STYLE_COLORS.get(style, widgets.MUTED),
        )
        self.bar.set(ep.fraction)
        self.detail.configure(text=dashboard.Dashboard._detail(ep, now).plain)


class DownloadView(tk.Frame):
    """The screen showing download progress."""

    def __init__(self, app: App) -> None:
        """Builds the view.

        Args:
            app: The application window.
        """
        super().__init__(app.body, bg=widgets.BG)
        f = app.fonts
        self.app = app
        self.title = tk.Label(
            self, font=f.title, fg=widgets.TEXT, bg=widgets.BG, anchor="w"
        )
        self.title.pack(fill="x")
        self.sub = tk.Label(
            self, font=f.small, fg=widgets.MUTED, bg=widgets.BG, anchor="w"
        )
        self.sub.pack(fill="x", pady=(2, 14))
        bar_row = tk.Frame(self, bg=widgets.BG)
        bar_row.pack(fill="x", pady=(0, 18))
        self.overall = widgets.ThinBar(bar_row, height=5)
        self.overall.pack(side="left", fill="x", expand=True)
        self.pct = tk.Label(
            bar_row,
            font=f.bold,
            fg=widgets.PINK,
            bg=widgets.BG,
            width=5,
            anchor="e",
        )
        self.pct.pack(side="left")

        holder = tk.Frame(self, bg=widgets.BG)
        holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            holder, bg=widgets.BG, highlightthickness=0, bd=0
        )
        sb = widgets.SlimScrollbar(
            holder, self.canvas.yview, side="right", fill="y"
        )
        self.inner = tk.Frame(self.canvas, bg=widgets.BG)
        win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._sb = sb
        self.inner.bind("<Configure>", lambda _e: self._sync_scroll())
        self.canvas.bind("<Configure>", lambda e: self._resize(win, e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb._pack_kw["before"] = self.canvas
        self.canvas.bind_all("<MouseWheel>", self._wheel)

        foot = tk.Frame(self, bg=widgets.BG)
        foot.pack(fill="x", pady=(14, 0))
        self.status = tk.Label(
            foot, font=f.small, fg=widgets.MUTED, bg=widgets.BG, anchor="w"
        )
        self.status.pack(side="left")
        self.btn_back = widgets.Button(
            foot, "Back to search", app.show_search, f
        )
        self.btn_open = widgets.Button(foot, "Open folder", self.open_folder, f)
        self.btn_cancel = widgets.Button(foot, "Cancel", self.cancel, f)
        self.rows: dict[str, EpRow] = {}
        self._dash_id = 0
        self.active = False
        self.future: Any = None
        self.out_dir = ""
        self._total = 0

    def _resize(self, win: int, width: int) -> None:
        self.canvas.itemconfigure(win, width=width)
        self._sync_scroll()

    def _sync_scroll(self) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        # re-emit so the bar shows/hides itself
        self.canvas.yview_moveto(self.canvas.yview()[0])
        if self.inner.winfo_reqheight() <= self.canvas.winfo_height():
            self.canvas.yview_moveto(0)

    def _wheel(self, e: tk.Event[Any]) -> None:
        if self.winfo_ismapped() and self._sb.winfo_ismapped():
            self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    def begin(self, title: str, total: int, out_dir: str) -> None:
        """Prepares the view for a new download.

        Args:
            title: The series title.
            total: The number of episodes.
            out_dir: The directory the files are written to.
        """
        self.title.configure(text=title)
        self.sub.configure(text=f"0 of {total} · starting")
        self.status.configure(text="")
        self.overall.set(0)
        self.pct.configure(text="0%")
        for w in self.inner.winfo_children():
            w.destroy()
        self.rows.clear()
        self._dash_id, self._total, self.out_dir, self.active = (
            0, total, out_dir, True
        )
        for b in (self.btn_back, self.btn_open, self.btn_cancel):
            b.pack_forget()
        self.btn_cancel.pack(side="right")

    def refresh(self) -> None:
        """Redraws the rows and totals from the dashboard state."""
        dash = self.app.backend.current_dash
        if dash is None:
            return
        if id(dash) != self._dash_id:  # a retry pass starts a fresh dashboard
            self._dash_id = id(dash)
            for w in self.inner.winfo_children():
                w.destroy()
            self.rows.clear()
        now = time.monotonic()
        items = list(dash._eps.items())
        for key, ep in items:
            row = self.rows.get(key)
            if row is None:
                row = EpRow(self.inner, key, self.app.fonts)
                row.pack(fill="x")
                self.rows[key] = row
            row.update_from(ep, now)
        frac, speed, eta = dash._overall(now)
        self.overall.set(frac)
        self.pct.configure(text=f"{frac * 100:.0f}%")
        text = f"{dash._done_eps} of {self._total}"
        if speed > 0:
            text += (
                f" · {utils.fmt_bytes(int(speed))}/s · eta "
                f"{dashboard._fmt_eta(eta)}"
            )
        self.sub.configure(text=text)

    def finished(self, results: dict[str, pathlib.Path | None]) -> None:
        """Shows the outcome of a finished download.

        Args:
            results: A dict mapping episode sessions to files, or to None for
                failures.
        """
        self.refresh()
        self.active = False
        ok = sum(1 for v in results.values() if v is not None)
        fail = len(results) - ok
        self.overall.set(1.0 if not fail else self.overall._frac)
        self.sub.configure(
            text=f"{ok} of {self._total} completed"
            + (f" · {fail} failed" if fail else "")
        )
        done_text, done_color = (
            ("✓ Finished", widgets.OK)
            if not fail
            else ("Finished with errors", widgets.WARN)
        )
        self.status.configure(text=done_text, fg=done_color)
        self._end_buttons(show_open=ok > 0)

    def failed(self, exc: BaseException) -> None:
        """Shows that the download failed.

        Args:
            exc: The error.
        """
        self.active = False
        self.status.configure(text=f"✗ {exc}", fg=widgets.ERR)
        self._end_buttons(show_open=False)

    def cancel(self) -> None:
        """Cancels the running download and returns to the search."""
        if self.future is not None:
            self.future.cancel()
        self.active = False
        self.status.configure(
            text="Cancelled. Progress is kept; start again to resume.",
            fg=widgets.WARN,
        )
        self._end_buttons(show_open=False)

    def _end_buttons(self, show_open: bool) -> None:
        self.btn_cancel.pack_forget()
        self.btn_back.pack(side="right")
        if show_open:
            self.btn_open.pack(side="right", padx=(0, 10))

    def open_folder(self) -> None:
        """Opens the output folder in the system file manager."""
        path = self.out_dir
        if not path or not pathlib.Path(path).exists():
            return
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])


# ── episode picker ───────────────────────────────────────────────────────────
class EpisodesView(tk.Frame):
    """The screen for picking which episodes to download."""

    def __init__(self, app: App) -> None:
        """Builds the view.

        Args:
            app: The application window.
        """
        super().__init__(app.body, bg=widgets.BG)
        f = app.fonts
        self.app = app
        self.anime: models.AnimeInfo | None = None
        self.eps: list[models.EpisodeInfo] = []
        self.sel: set[float] = set()
        widgets.Link(self, "← search", app.show_search, f).pack(anchor="w")
        self.title = tk.Label(
            self, font=f.title, fg=widgets.TEXT, bg=widgets.BG, anchor="w"
        )
        self.title.pack(fill="x", pady=(8, 0))
        self.sub = tk.Label(
            self, font=f.small, fg=widgets.MUTED, bg=widgets.BG, anchor="w"
        )
        self.sub.pack(fill="x", pady=(2, 12))

        ctl = tk.Frame(self, bg=widgets.BG)
        ctl.pack(fill="x", pady=(0, 10))
        widgets.Button(ctl, "All", self.select_all, f).pack(side="left")
        widgets.Button(ctl, "None", self.select_none, f).pack(
            side="left", padx=8
        )
        self.range = widgets.make_entry(ctl, f, width=18)
        self.range.pack(side="left", padx=(12, 8))
        self.range.bind("<Return>", lambda _e: self.select_range())
        widgets.Button(ctl, "Select range", self.select_range, f).pack(
            side="left"
        )
        tk.Label(
            ctl,
            text="e.g. 1-12 · 1,4,7 · 5-",
            font=f.small,
            fg=widgets.MUTED,
            bg=widgets.BG,
        ).pack(side="left", padx=10)

        frame, self.lb = widgets.make_listbox(self, f)
        frame.pack(fill="both", expand=True)
        self.lb.bind("<ButtonRelease-1>", self._click)
        self.lb.bind(
            "<space>", lambda _e: self._toggle(self.lb.index("active"))
        )

        foot = tk.Frame(self, bg=widgets.BG)
        foot.pack(fill="x", pady=(14, 0))
        self.summary = tk.Label(
            foot, font=f.ui, fg=widgets.MUTED, bg=widgets.BG
        )
        self.summary.pack(side="left")
        self.go = widgets.Button(foot, "Download", self.start, f, primary=True)
        self.go.pack(side="right")

    def load(self, anime: models.AnimeInfo) -> None:
        """Fills the list with a series' episodes.

        Args:
            anime: The series.
        """
        self.anime = anime
        by_num = {ep.number: ep for ep in anime.episodes}
        self.eps = [by_num[n] for n in sorted(by_num)]
        self.sel = set()
        self.title.configure(text=anime.title)
        first, last = (
            (self.eps[0].ep_str, self.eps[-1].ep_str)
            if self.eps
            else ("-", "-")
        )
        self.sub.configure(text=f"{len(self.eps)} episodes · {first} to {last}")
        self.lb.delete(0, "end")
        for ep in self.eps:
            self.lb.insert("end", self._line(ep, False))
            self.lb.itemconfig("end", fg=widgets.MUTED)
        self._summary()

    @staticmethod
    def _line(ep: models.EpisodeInfo, on: bool) -> str:
        return (
            f" {'[x]' if on else '[ ]'}  Episode {ep.ep_str:<6} "
            f"{clean_title(ep.title)}"
        )

    def _paint(self, i: int) -> None:
        on = self.eps[i].number in self.sel
        self.lb.delete(i)
        self.lb.insert(i, self._line(self.eps[i], on))
        self.lb.itemconfig(i, fg=widgets.PINK if on else widgets.MUTED)
        self.lb.selection_clear(0, "end")

    def _toggle(self, i: int) -> None:
        if not 0 <= i < len(self.eps):
            return
        n = self.eps[i].number
        self.sel.symmetric_difference_update({n})
        self._paint(i)
        self._summary()

    def _click(self, e: tk.Event[Any]) -> None:
        self._toggle(int(self.lb.nearest(e.y)))  # type: ignore[no-untyped-call]

    def _set_all(self, numbers: set[float]) -> None:
        self.sel = set(numbers)
        for i in range(len(self.eps)):
            self._paint(i)
        self._summary()

    def select_all(self) -> None:
        self._set_all({e.number for e in self.eps})

    def select_none(self) -> None:
        self._set_all(set())

    def select_range(self) -> None:
        """Selects the episodes in the range entered by the user."""
        from pahebatcher.ui import prompts

        raw = self.range.get().strip()
        if raw:
            self._set_all(set(prompts._parse_ep_range(raw, self.eps)))

    def _summary(self) -> None:
        """Updates the summary of what is selected."""
        n = len(self.sel)
        mb = n * EST_MB.get(int(self.app.cfg.get("quality")), 120)
        self.summary.configure(
            text=f"{n} selected · ≈ {mb} MB" if n else "nothing selected"
        )
        self.go.configure(
            text=f"Download {n} episode{'s' if n != 1 else ''}"
            if n
            else "Download"
        )
        self.go.enable(n > 0)

    def start(self) -> None:
        """Starts downloading the selected episodes."""
        if self.anime is None or not self.sel:
            return
        chosen = [e for e in self.eps if e.number in self.sel]
        anime = self.anime
        self.app.ensure_ffmpeg(lambda: self.app.start_download(anime, chosen))


# ── search ───────────────────────────────────────────────────────────────────
class SearchView(tk.Frame):
    """The screen for searching for a series and setting options."""

    def __init__(self, app: App) -> None:
        """Builds the view.

        Args:
            app: The application window.
        """
        super().__init__(app.body, bg=widgets.BG)
        f = app.fonts
        self.app = app
        self.results: list[dict[str, Any]] = []
        self._token = 0
        row = tk.Frame(self, bg=widgets.BG)
        row.pack(fill="x")
        self.entry = widgets.make_entry(row, f)
        self.entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.entry.bind("<Return>", lambda _e: self.search())
        self.btn = widgets.Button(row, "Search", self.search, f, primary=True)
        self.btn.pack(side="left", padx=(10, 0))
        self.status = tk.Label(
            self,
            text="Search for an anime, then double-click a result.",
            font=f.small,
            fg=widgets.MUTED,
            bg=widgets.BG,
            anchor="w",
        )
        self.status.pack(fill="x", pady=(10, 8))
        frame, self.lb = widgets.make_listbox(self, f)
        frame.pack(fill="both", expand=True)
        self.lb.bind("<Double-Button-1>", lambda _e: self.open())
        self.lb.bind("<Return>", lambda _e: self.open())

        s = tk.Frame(self, bg=widgets.BG, highlightthickness=0)
        s.pack(fill="x", pady=(16, 0))
        cfg = app.cfg

        def setter(key: str) -> Callable[[Any], None]:
            """Creates a callback that stores a value in the settings.

            Args:
                key: The setting name.

            Returns:
                A callable taking the new value.
            """
            def _set(v: Any) -> None:
                cfg.set(key, v)
                bridge.save_settings(cfg)
            return _set

        def group(label: str, widget: tk.Widget) -> None:
            """Adds a labeled widget to the options area.

            Args:
                label: The label text.
                widget: The widget to add.
            """
            tk.Label(
                s,
                text=label.upper(),
                font=f.label,
                fg=widgets.MUTED,
                bg=widgets.BG,
            ).pack(side="left", padx=(0, 8))
            widget.pack(side="left", padx=(0, 26))

        group(
            "Quality",
            widgets.Segmented(
                s,
                [("360p", 360), ("720p", 720), ("1080p", 1080)],
                int(cfg.get("quality")),
                setter("quality"),
                f,
            ),
        )
        group(
            "Audio",
            widgets.Segmented(
                s,
                [("SUB", "jpn"), ("DUB", "eng")],
                str(cfg.get("audio_lang")),
                setter("audio_lang"),
                f,
            ),
        )
        group(
            "Parallel",
            widgets.Segmented(
                s,
                [(str(n), n) for n in (1, 2, 3, 4)],
                int(cfg.get("max_parallel")),
                setter("max_parallel"),
                f,
            ),
        )
        self.folder = tk.Label(
            s, font=f.small, fg=widgets.MUTED, bg=widgets.BG, anchor="e"
        )
        widgets.Link(s, "change", self.pick_folder, f).pack(side="right")
        self.folder.pack(side="right", padx=(0, 8))
        self._show_folder()

    def _show_folder(self) -> None:
        p = str(self.app.cfg.get("output_dir"))
        self.folder.configure(text=("…" + p[-38:]) if len(p) > 40 else p)

    def pick_folder(self) -> None:
        """Asks the user for an output folder."""
        d = filedialog.askdirectory(
            initialdir=str(self.app.cfg.get("output_dir")),
            title="Download folder",
        )
        if d:
            self.app.cfg.set("output_dir", d)
            bridge.save_settings(self.app.cfg)
            self._show_folder()

    def search(self) -> None:
        """Runs the search for the entered text."""
        q = self.entry.get().strip()
        if not q:
            return
        if not self.app.solver_ok:
            self.status.configure(
                text="No Cloudflare solver yet — see the notice above.",
                fg=widgets.WARN,
            )
            return
        self._token += 1
        token = self._token
        self.status.configure(text=f"Searching “{q}”…", fg=widgets.MUTED)
        self.app.bridge.run(
            self.app.backend.search(q),
            ok=lambda r: self._results(token, q, r),
            err=lambda e: self._fail(e),
        )

    def _results(
        self,
        token: int,
        q: str,
        results: list[dict[str, Any]],
    ) -> None:
        """Shows search results, unless a newer search has replaced them.

        Args:
            token: The id of the search that produced the results.
            q: The search text.
            results: The raw search results.
        """
        if token != self._token:
            return
        self.results = results
        self.lb.delete(0, "end")
        if not results:
            self.status.configure(
                text=f"No results for “{q}”.", fg=widgets.WARN
            )
            return
        for r in results:
            title = str(r.get("title", "Unknown"))
            title = title if len(title) <= 46 else title[:45] + "…"
            kind, year, n = (
                str(r.get(k, "-")) for k in ("type", "year", "episodes")
            )
            self.lb.insert("end", f" {title:<46}  {kind:<7}{year:<6}{n:>4} eps")
        self.lb.selection_set(0)
        self.status.configure(
            text=f"{len(results)} results — double-click to open",
            fg=widgets.MUTED,
        )

    def open(self) -> None:
        """Opens the selected search result's episode list."""
        sel = self.lb.curselection()  # type: ignore[no-untyped-call]
        if not sel:
            return
        session = self.results[sel[0]].get("session")
        if not session:
            return
        self.status.configure(text="Loading episodes…", fg=widgets.MUTED)
        self.app.bridge.run(
            self.app.backend.scan(str(session)),
            ok=self._opened,
            err=lambda e: self._fail(e),
        )

    def _fail(self, exc: BaseException) -> None:
        self.status.configure(text=f"✗ {exc}", fg=widgets.ERR)

    def _opened(self, anime: models.AnimeInfo) -> None:
        self.status.configure(text="", fg=widgets.MUTED)
        self.app.show_episodes(anime)


# ── main window ──────────────────────────────────────────────────────────────
class App(tk.Tk):
    """The main application window."""

    def __init__(self) -> None:
        """Builds the window and its views, and starts the backend."""
        super().__init__()
        self.title("pahebatcher")
        self.geometry("1000x720")
        self.minsize(860, 600)
        self.configure(bg=widgets.BG)
        self._set_icon()
        self.fonts = widgets.Fonts(self)
        self.bridge = bridge.Bridge()
        # Remove what a previous update left behind.
        self.bridge.thread(updater.cleanup_leftovers)
        self.cfg = bridge.load_settings()
        self.backend = bridge.Backend(self.cfg)
        self.solver_ok = False
        self._ff_busy = False
        self._upd_busy = False
        self._ff_cancel: threading.Event | None = None
        self._after_ff: list[Callable[[], None]] = []
        self._pinging = False

        from pahebatcher.ui import dashboard as dashboard_ui

        def factory(
            total_eps: int,
            console: Any = None,
            title: str = "",
        ) -> GuiDashboard:
            """Creates the view for a screen on demand."""
            d = GuiDashboard(total_eps, title=title)
            self.backend.current_dash = d
            return d

        dashboard_ui.Dashboard = factory  # type: ignore[assignment,misc,attr-defined,unused-ignore]

        self._fullscreen = False
        self._pad = 32
        self._build_header()
        self.notices = tk.Frame(self, bg=widgets.BG)
        self.notices.pack(fill="x", padx=32)
        self._build_ffmpeg_notice()
        self._build_solver_notice()
        self.body = tk.Frame(self, bg=widgets.BG)
        self.body.pack(fill="both", expand=True, padx=32, pady=(18, 24))
        self.v_search, self.v_eps, self.v_dl = (
            SearchView(self), EpisodesView(self), DownloadView(self)
        )
        self.show_search()

        self.refresh_ffmpeg_status()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<F11>", self.toggle_fullscreen)
        self.bind("<Alt-Return>", self.toggle_fullscreen)
        self.bind("<Escape>", self._exit_fullscreen)
        self.bind("<Configure>", self._on_resize)
        self.after(60, self._pump)
        self.bridge.run(
            self._boot(),
            ok=self._set_solver,
            err=lambda _e: self._set_solver(False),
        )

    def _set_icon(self) -> None:
        """Sets the window icon."""
        # also correct inside a PyInstaller bundle
        assets = pathlib.Path(__file__).parent / "assets"
        try:
            # keep a reference or Tk drops it
            self._icon = tk.PhotoImage(file=str(assets / "icon.png"))
            self.iconphoto(True, self._icon)
        except Exception:
            pass  # a missing icon must never stop the app
        if sys.platform == "win32":
            self.update_idletasks()
            _win_set_icon(self, assets / "icon.ico")


    # header + status ------------------------------------------------------
    def _build_header(self) -> None:
        """Builds the header with the logo and window controls."""
        f = self.fonts
        hdr = self.hdr = tk.Frame(self, bg=widgets.BG)
        hdr.pack(fill="x", padx=32, pady=(24, 0))
        # colored anime mascot + pink wordmark (assets/logo, cropped and
        # recoloured)
        try:
            self._logo = tk.PhotoImage(
                file=str(
                    pathlib.Path(__file__).parent / "assets" / "header_logo.png"
                )
            )
            tk.Label(hdr, image=self._logo, bg=widgets.BG, bd=0).pack(
                side="left"
            )
        except Exception:
            tk.Label(
                hdr,
                text="PaheBatcher",
                font=f.logo,
                fg=widgets.PINK,
                bg=widgets.BG,
            ).pack(side="left")
        self.fs_link = widgets.Link(
            hdr,
            "Full screen",
            lambda: self.set_fullscreen(not self._fullscreen),
            f,
        )
        self.fs_link.pack(side="right", padx=(16, 0))
        self.upd_link = widgets.Link(
            hdr, "Check for updates", self.check_updates, f
        )
        self.upd_link.pack(side="right", padx=(16, 0))
        tk.Label(
            hdr,
            text=f"v{config.VERSION}",
            font=f.small,
            fg=widgets.MUTED,
            bg=widgets.BG,
        ).pack(side="right", padx=(16, 0))
        self.dot_solver = tk.Label(
            hdr, text="● Solver", font=f.small, fg=widgets.MUTED, bg=widgets.BG
        )
        self.dot_solver.pack(side="right", padx=(16, 0))
        self.dot_ffmpeg = tk.Label(
            hdr, text="● FFmpeg", font=f.small, fg=widgets.MUTED, bg=widgets.BG
        )
        self.dot_ffmpeg.pack(side="right")

    # updates --------------------------------------------------------------
    def check_updates(self) -> None:
        """Checks for a newer release and offers to install it."""
        if self._upd_busy:
            return
        self._upd_busy = True
        self.upd_link.configure(text="Checking…")
        self.bridge.thread(
            updater.check,
            ok=self._update_checked,
            err=self._update_failed,
        )

    def _update_idle(self) -> None:
        """Returns the update link to its resting state."""
        self._upd_busy = False
        self.upd_link.configure(text="Check for updates")

    def _update_checked(self, info: updater.UpdateInfo | None) -> None:
        """Reports the result of a check and offers the update.

        Args:
            info: The newer release, or None if this version is current.
        """
        self._update_idle()
        if info is None:
            messagebox.showinfo(
                "Up to date",
                f"You already have the latest version (v{config.VERSION}).",
            )
            return
        notes = info.notes[:600] + ("…" if len(info.notes) > 600 else "")
        text = (
            f"Version {info.version} is available. You have v{config.VERSION}."
        )
        if notes:
            text += f"\n\n{notes}"
        if updater.can_self_update() and info.asset_url:
            if messagebox.askyesno(
                "Update available", f"{text}\n\nDownload and install it now?"
            ):
                self._update_install(info)
        elif messagebox.askyesno(
            "Update available", f"{text}\n\nOpen the download page?"
        ):
            webbrowser.open(info.page_url)

    def _update_install(self, info: updater.UpdateInfo) -> None:
        """Downloads and installs a release in the background.

        Args:
            info: The release to install.
        """
        self._upd_busy = True
        self.upd_link.configure(text="Downloading…")

        def progress(stage: str, frac: float | None) -> None:
            text = f"{stage} {frac:.0%}" if frac is not None else f"{stage}…"
            self.bridge.post(lambda: self.upd_link.configure(text=text))

        self.bridge.thread(
            lambda: updater.install(updater.download(info, progress=progress)),
            ok=lambda _path: self._update_installed(info),
            err=self._update_failed,
        )

    def _update_installed(self, info: updater.UpdateInfo) -> None:
        """Offers to restart into the version that was just installed.

        Args:
            info: The release that was installed.
        """
        self._update_idle()
        if messagebox.askyesno(
            "Update installed",
            f"Updated to v{info.version}. Restart now?\n\n"
            "A download in progress will stop.",
        ):
            updater.restart()
            self.on_close()
        else:
            messagebox.showinfo(
                "Update installed",
                "The new version starts the next time you open the app.",
            )

    def _update_failed(self, exc: BaseException) -> None:
        """Shows that checking or installing failed.

        Args:
            exc: The error.
        """
        self._update_idle()
        messagebox.showerror("Update failed", str(exc))

    # full screen ----------------------------------------------------------
    def set_fullscreen(self, on: bool) -> None:
        """Enters or leaves full screen.

        Args:
            on: True for full screen.
        """
        self._fullscreen = on
        try:
            self.attributes("-fullscreen", on)
        except tk.TclError:
            # a window manager without full-screen support: the maximize button
            # still works
            pass
        self.fs_link.configure(text="Exit full screen" if on else "Full screen")

    def toggle_fullscreen(self, _e: tk.Event[Any] | None = None) -> str:
        """Toggles full screen.

        Args:
            _e: The key event, if called from a binding.

        Returns:
            ``"break"``, to stop further handling of the event.
        """
        self.set_fullscreen(not self._fullscreen)
        return "break"

    def _exit_fullscreen(self, _e: tk.Event[Any] | None = None) -> None:
        if self._fullscreen:
            self.set_fullscreen(False)

    def _on_resize(self, e: tk.Event[Any]) -> None:
        """Keeps content centered with a sensible maximum width.

        This matters when the window is very wide.

        Args:
            e: The configure event.
        """
        if e.widget is not self:
            return
        pad = max(32, (e.width - MAX_CONTENT) // 2)
        if pad != self._pad:
            self._pad = pad
            for w in (self.hdr, self.notices, self.body):
                w.pack_configure(padx=pad)

    def _notice(self, text: str) -> tuple[tk.Frame, tk.Label, tk.Frame]:
        """Builds a notice bar at the top of the window.

        Args:
            text: The notice text.

        Returns:
            A tuple ``(frame, label, button_frame)``.
        """
        box = tk.Frame(
            self.notices,
            bg=widgets.PANEL,
            highlightthickness=1,
            highlightbackground=widgets.LINE,
        )
        inner = tk.Frame(box, bg=widgets.PANEL)
        inner.pack(fill="x", padx=16, pady=12)
        # packed first so the buttons keep their full size
        actions = tk.Frame(inner, bg=widgets.PANEL)
        actions.pack(side="right", padx=(16, 0))
        msg = tk.Label(
            inner,
            text=text,
            font=self.fonts.ui,
            fg=widgets.TEXT,
            bg=widgets.PANEL,
            anchor="w",
            justify="left",
        )
        msg.pack(side="left", fill="x", expand=True)
        # wrap, don't clip
        msg.bind(
            "<Configure>",
            lambda e: msg.configure(wraplength=max(e.width - 4, 160)),
        )
        return box, msg, actions

    # FFmpeg ---------------------------------------------------------------
    def _build_ffmpeg_notice(self) -> None:
        """Builds the notice shown when FFmpeg is missing."""
        f = self.fonts
        self.ff_box, self.ff_msg, actions = self._notice(
            "FFmpeg isn't installed. It joins the downloaded pieces into a "
            "single .mp4."
        )
        self.ff_bar = widgets.ThinBar(self.ff_box, height=3)
        self.ff_btn = widgets.Button(
            actions, "Install FFmpeg", self.install_ffmpeg, f, primary=True
        )
        self.ff_btn.pack(side="right")
        self.ff_cancel = widgets.Link(
            actions, "cancel", self._cancel_ffmpeg, f, bg=widgets.PANEL
        )

    def refresh_ffmpeg_status(self) -> None:
        """Shows or hides the FFmpeg notice."""
        ok = ffmpeg_setup.find_ffmpeg(refresh=True) is not None
        self.dot_ffmpeg.configure(fg=widgets.OK if ok else widgets.WARN)
        if ok:
            self.ff_box.pack_forget()
        elif not self._ff_busy:
            self.ff_box.pack(fill="x", pady=(16, 0))

    def install_ffmpeg(self) -> None:
        """Downloads and installs FFmpeg in the background."""
        if self._ff_busy:
            return
        self._ff_busy = True
        self._ff_cancel = threading.Event()
        cancel = self._ff_cancel
        self.ff_btn.pack_forget()
        self.ff_cancel.pack(side="right")
        self.ff_bar.pack(fill="x", side="bottom")
        self.ff_msg.configure(
            text="Downloading FFmpeg… this happens once", fg=widgets.TEXT
        )

        def progress(stage: str, frac: float | None) -> None:
            self.bridge.post(lambda: self._ff_progress(stage, frac))

        self.bridge.thread(
            lambda: ffmpeg_setup.install(progress, cancel),
            ok=self._ff_done,
            err=self._ff_failed,
        )

    def _ff_progress(self, stage: str, frac: float | None) -> None:
        """Shows FFmpeg installation progress.

        Args:
            stage: The name of the current stage.
            frac: The fraction complete from 0.0 to 1.0, or None if unknown.
        """
        if not self._ff_busy:
            return
        pct = (
            f" {frac * 100:.0f}%"
            if frac is not None and stage == "Downloading"
            else "…"
        )
        self.ff_msg.configure(text=f"{stage}{pct}", fg=widgets.TEXT)
        if frac is not None:
            self.ff_bar.set(frac)

    def _end_ffmpeg(self) -> None:
        self._ff_busy = False
        self.ff_cancel.pack_forget()
        self.ff_bar.pack_forget()

    def _ff_done(self, _path: str) -> None:
        self._end_ffmpeg()
        self.refresh_ffmpeg_status()
        callbacks, self._after_ff = self._after_ff, []
        for cb in callbacks:
            cb()

    def _ff_failed(self, exc: BaseException) -> None:
        """Shows that the FFmpeg installation failed.

        Args:
            exc: The error.
        """
        self._end_ffmpeg()
        self._after_ff.clear()
        cancelled = str(exc) == "Cancelled"
        self.ff_msg.configure(
            text="Installation cancelled." if cancelled else f"✗ {exc}",
            fg=widgets.MUTED if cancelled else widgets.ERR,
        )
        self.ff_btn.configure(text="Try again")
        self.ff_btn.pack(side="right")

    def _cancel_ffmpeg(self) -> None:
        if self._ff_cancel:
            self._ff_cancel.set()

    def ensure_ffmpeg(self, then: Callable[[], None]) -> None:
        """Runs a callback once FFmpeg is available.

        Runs it now if FFmpeg is present, otherwise installs FFmpeg first and
        then continues.

        Args:
            then: The callback to run.
        """
        if ffmpeg_setup.find_ffmpeg() is not None:
            then()
            return
        self._after_ff.append(then)
        self.install_ffmpeg()

    # Cloudflare solver ----------------------------------------------------
    def _build_solver_notice(self) -> None:
        """Builds the notice shown when the browser solver is unavailable."""
        f = self.fonts
        self.fs_box, self.fs_msg, actions = self._notice(
            "The built-in browser solver isn't available. It needs Chrome or "
            "Edge installed, "
            "plus the browser add-on to get past animepahe's browser check."
        )
        widgets.Button(actions, "Check again", self.recheck_solver, f).pack(
            side="right"
        )
        self.fs_pip = widgets.Button(
            actions, "Copy pip command", self._copy_pip, f, primary=True
        )
        self.fs_pip.pack(side="right", padx=(0, 10))

    async def _boot(self) -> bool:
        await self.backend.start()
        return await self.backend.ping()

    def recheck_solver(self) -> None:
        """Checks again whether the browser solver can start."""
        if self._pinging:
            return
        self._pinging = True
        self.bridge.run(
            self.backend.ping(),
            ok=self._set_solver,
            err=lambda _e: self._set_solver(False),
        )

    def _set_solver(self, ok: bool) -> None:
        """Records whether the solver works and updates the notice.

        Args:
            ok: True if the solver can start.
        """
        self._pinging = False
        self.solver_ok = bool(ok)
        self.dot_solver.configure(fg=widgets.OK if ok else widgets.ERR)
        name = getattr(self.backend.solver, "backend_name", "") if ok else ""
        self.dot_solver.configure(text=f"● {name}" if name else "● Solver")
        if ok:
            self.fs_box.pack_forget()
        else:
            self.fs_box.pack(fill="x", pady=(10, 0))
            # keep checking quietly until it's up
            self.after(6000, self.recheck_solver)

    def _copy_pip(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(PIP_CMD)
        self.fs_pip.configure(text="Copied ✓")
        self.after(2000, lambda: self.fs_pip.configure(text="Copy pip command"))

    # navigation -----------------------------------------------------------
    def _show(self, view: tk.Frame) -> None:
        for v in (self.v_search, self.v_eps, self.v_dl):
            v.pack_forget()
        view.pack(fill="both", expand=True)

    def show_search(self) -> None:
        """Shows the search screen."""
        self._show(self.v_search)
        self.v_search.entry.focus_set()

    def show_episodes(self, anime: models.AnimeInfo) -> None:
        """Shows the episode picker.

        Args:
            anime: The series to pick episodes from.
        """
        self.v_eps.load(anime)
        self._show(self.v_eps)

    def start_download(
        self,
        anime: models.AnimeInfo,
        episodes: list[models.EpisodeInfo],
    ) -> None:
        """Starts downloading episodes, installing FFmpeg first if needed.

        Args:
            anime: The series.
            episodes: The episodes to download.
        """
        self.backend.current_dash = None
        self._show(self.v_dl)
        self.v_dl.begin(
            anime.title,
            len(episodes),
            self.backend.context(anime.title).output_dir,
        )
        self.v_dl.future = self.bridge.run(
            self.backend.download(anime, episodes),
            ok=self.v_dl.finished,
            err=self.v_dl.failed,
        )

    # loop -----------------------------------------------------------------
    def _pump(self) -> None:
        self.bridge.drain()
        if self.v_dl.active:
            self.v_dl.refresh()
        self.after(250, self._pump)

    def on_close(self) -> None:
        """Shuts down the backend and closes the window."""
        if (
            self.v_dl.active
            and not messagebox.askyesno(
                "pahebatcher", "A download is running. Quit anyway?"
            )
        ):
            return
        if self.v_dl.future is not None:
            self.v_dl.future.cancel()
        try:
            self.bridge.run(self.backend.close()).result(timeout=6)
        except Exception:
            pass
        self.bridge.shutdown()
        self.destroy()


def _win_set_icon(win: tk.Tk, ico: pathlib.Path) -> None:
    """Gives the title bar and taskbar the exact icon the .exe has.

    Tk's own ``iconbitmap`` cannot read PNG-compressed .ico files (what our
    multi-size icon uses), so it silently falls back to the feather.
    Windows itself reads them fine: the sizes are loaded directly and sent
    to the window. When frozen, the icon embedded in the .exe is used
    first.

    Args:
        win: The window.
        ico: The .ico file.
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32  # type: ignore[attr-defined]
        user32.LoadImageW.restype = wintypes.HANDLE
        # separate taskbar identity so Windows doesn't group us under python.exe
        try:
            shell32.SetCurrentProcessExplicitAppUserModelID("pahebatcher.gui")
        except Exception:
            pass
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()

        def load(size: int) -> Any:
            """Loads one icon size from the file.

            Args:
                size: The icon size in pixels.

            Returns:
                The Windows icon handle.
            """
            h = None
            # icon resource embedded in our own .exe
            if getattr(sys, "frozen", False):
                big, small = wintypes.HICON(), wintypes.HICON()
                if shell32.ExtractIconExW(
                    sys.executable, 0, ctypes.byref(big), ctypes.byref(small), 1
                ):
                    h = big if size > 20 else small
            if not h and ico.exists():
                # IMAGE_ICON, LR_LOADFROMFILE
                h = user32.LoadImageW(None, str(ico), 1, size, size, 0x10)
            return h

        # BIG, SMALL
        for which, size in (
            (1, user32.GetSystemMetrics(11)), (0, user32.GetSystemMetrics(49))
        ):
            h = load(size)
            if h:
                user32.SendMessageW(hwnd, 0x0080, which, h)  # WM_SETICON
    except Exception:
        pass


def _prepare_process() -> None:
    """Prepares a windowed build, which has no stdout or stderr.

    Points Rich and ``print()`` somewhere harmless.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8", errors="replace"))  # noqa: SIM115
    from pahebatcher.ui import console as ui_console

    ui_console.console.file = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:
            pass


def main() -> None:
    """Runs the desktop app."""
    _prepare_process()
    App().mainloop()


if __name__ == "__main__":
    main()
