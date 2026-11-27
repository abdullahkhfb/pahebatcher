"""The built-in video player screen.

It replaces MPV in the desktop app. The picture is drawn on a Tk canvas,
the sound goes to the default audio device, and ``engine.MediaEngine`` keeps
the two in step. The screen has the usual controls: pause, seek, volume,
previous and next episode and full screen. The audio language is the one
chosen on the search screen.
"""

from __future__ import annotations

import dataclasses
import re
import time
import tkinter as tk
from typing import TYPE_CHECKING, Any

from pahebatcher import ffmpeg_setup, models
from pahebatcher.gui import bridge, engine, widgets

if TYPE_CHECKING:
    from PIL import Image, ImageTk

    from pahebatcher.gui.app import App

try:
    # Lazy-tolerant: a missing Pillow must disable the player, not the app
    from PIL import Image, ImageTk

    PIL_ERROR: str | None = None
except ImportError as _exc:  # pragma: no cover
    PIL_ERROR = str(_exc)

TICK_MS = 12
UI_REFRESH_S = 0.2
SEEK_DELAY_MS = 300
HIDE_AFTER_MS = 2500
SEEK_STEP = 5.0
SEEK_STEP_LONG = 10.0
VOLUME_STEP = 0.05
DEFAULT_DECODE_WIDTH = 960


def fmt_time(seconds: float) -> str:
    """Formats a time as ``m:ss`` or ``h:mm:ss``.

    Args:
        seconds: The time in seconds.

    Returns:
        The formatted time.
    """
    total = max(0, int(seconds))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _display_title(ep: models.EpisodeInfo) -> str:
    return re.sub(r"\s+(?:DUB|SUB)\s*$", "", ep.title or "", flags=re.I).strip()


class PlayerView(tk.Frame):
    """The screen that plays the chosen episodes one after another."""

    def __init__(self, app: App) -> None:
        """Builds the screen. Nothing plays until ``start`` is called.

        Args:
            app: The application window.
        """
        super().__init__(app.body, bg=widgets.BG)
        f = app.fonts
        self.app = app
        self._fonts = f
        self.anime: models.AnimeInfo | None = None
        self.base: list[models.EpisodeInfo] = []
        self.playlist: list[models.EpisodeInfo] = []
        self.index = 0
        self.audio = "jpn"
        self.session: bridge.StreamSession | None = None
        self.eng: engine.MediaEngine | None = None
        self._info: engine.MediaInfo | None = None
        self._token = 0
        self._future: Any = None
        self._status = ""
        self._status_error = False
        self._flash_until = 0.0
        self._failed = False
        self._finished = False
        self._pending: float | None = None
        self._seek_job: str | None = None
        self._hide_job: str | None = None
        self._resize_job: str | None = None
        self._tick_job: str | None = None
        self._ui_at = 0.0
        self._decoded_w = 0
        self._photo: ImageTk.PhotoImage | None = None
        self._pre_mute = 1.0
        self._controls_hidden = False
        self._volume_frac = int(app.cfg.get("volume")) / 100

        # header: back link and titles
        self.top = tk.Frame(self, bg=widgets.BG)
        self.top.pack(fill="x")
        widgets.Link(self.top, "← episodes", self.back, f).pack(anchor="w")
        row = tk.Frame(self.top, bg=widgets.BG)
        row.pack(fill="x", pady=(6, 10))
        names = tk.Frame(row, bg=widgets.BG)
        names.pack(side="left", fill="x", expand=True)
        self.title = tk.Label(
            names, font=f.bold, fg=widgets.TEXT, bg=widgets.BG, anchor="w"
        )
        self.title.pack(fill="x")
        self.subtitle = tk.Label(
            names, font=f.small, fg=widgets.MUTED, bg=widgets.BG, anchor="w"
        )
        self.subtitle.pack(fill="x")
        # the picture
        self.stage = tk.Frame(
            self,
            bg="#000000",
            highlightthickness=1,
            highlightbackground=widgets.LINE,
        )
        self.stage.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            self.stage, bg="#000000", highlightthickness=0, bd=0
        )
        self.canvas.pack(fill="both", expand=True)
        self._image_item = self.canvas.create_image(0, 0, anchor="center")
        self._text_item = self.canvas.create_text(
            0,
            0,
            text="",
            fill=widgets.TEXT,
            font=f.title,
            justify="center",
            width=600,
        )
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Button-1>", lambda _e: self.toggle_pause())
        self.canvas.bind(
            "<Double-Button-1>", lambda _e: self.toggle_fullscreen()
        )
        self.canvas.bind("<MouseWheel>", self._wheel)
        # X11 reports the wheel as buttons 4 and 5
        self.canvas.bind(
            "<Button-4>", lambda _e: self._nudge_volume(VOLUME_STEP)
        )
        self.canvas.bind(
            "<Button-5>", lambda _e: self._nudge_volume(-VOLUME_STEP)
        )
        self.canvas.bind("<Motion>", self._activity)

        # controls
        self.controls = tk.Frame(self, bg=widgets.BG)
        self.controls.pack(fill="x", pady=(10, 0))
        self.controls.bind("<Motion>", self._activity)
        self.seek = widgets.Slider(
            self.controls,
            on_drag=self._preview_seek,
            on_commit=lambda fr: self._seek_to(fr * self._duration(), True),
        )
        self.seek.pack(fill="x")
        bar = tk.Frame(self.controls, bg=widgets.BG)
        bar.pack(fill="x", pady=(8, 0))
        self.btn_prev = widgets.Button(bar, "◀ Prev", lambda: self.go(-1), f)
        self.btn_prev.pack(side="left")
        self.btn_play = widgets.Button(
            bar, "Play", self.toggle_pause, f, primary=True
        )
        self.btn_play.pack(side="left", padx=8)
        self.btn_next = widgets.Button(bar, "Next ▶", lambda: self.go(1), f)
        self.btn_next.pack(side="left")
        self.clock = tk.Label(
            bar,
            font=f.mono,
            fg=widgets.MUTED,
            bg=widgets.BG,
            text="0:00 / 0:00",
        )
        self.clock.pack(side="left", padx=16)
        self.btn_full = widgets.Button(
            bar, "Full screen", self.toggle_fullscreen, f
        )
        self.btn_full.pack(side="right")
        self.volume = widgets.Slider(
            bar,
            on_drag=self._set_volume,
            on_commit=lambda _fr: self._save_volume(),
            width=96,
        )
        self.volume.pack(side="right", padx=(0, 16))
        self.volume.set(self._volume_frac)
        tk.Label(
            bar, text="VOL", font=f.label, fg=widgets.MUTED, bg=widgets.BG
        ).pack(side="right", padx=(0, 8))
        self.note = tk.Label(
            bar, font=f.small, fg=widgets.WARN, bg=widgets.BG, anchor="e"
        )
        self.note.pack(side="right", padx=(0, 16))

        app.bind("<KeyPress>", self._key, add="+")

    # ── opening and closing ──────────────────────────────────────────────
    def start(
        self,
        anime: models.AnimeInfo,
        episodes: list[models.EpisodeInfo],
        index: int,
        audio: str,
    ) -> None:
        """Starts playing a list of episodes.

        Args:
            anime: The series.
            episodes: The episodes to play, in order.
            index: The position in ``episodes`` to start from.
            audio: The audio language to prefer, ``"jpn"`` or ``"eng"``.
        """
        self.stop_playback()
        if PIL_ERROR is not None:
            self._set_status(
                "The built-in player needs Pillow.\nInstall it with: "
                'pip install pillow',
                error=True,
            )
            return
        self.anime, self.base, self.audio = anime, list(episodes), audio
        self.note.configure(text="")
        self._build_playlist()
        self._load(index)
        self._schedule_tick()

    def stop_playback(self) -> None:
        """Stops everything and frees the stream, keeping the screen."""
        self._token += 1  # late results from the old episode are ignored
        for job in (self._seek_job, self._hide_job, self._resize_job):
            if job is not None:
                self.after_cancel(job)
        self._seek_job = self._hide_job = self._resize_job = None
        if self._tick_job is not None:
            self.after_cancel(self._tick_job)
            self._tick_job = None
        self._pending = None
        self._release_session()
        self._show_controls()

    def shutdown(self) -> None:
        """Stops playback and releases the audio device. Call on exit."""
        self.stop_playback()
        if self.eng is not None:
            self.eng.close()
            self.eng = None

    def back(self) -> None:
        """Leaves the player and returns to the episode list."""
        self.app.leave_player()

    def _release_session(self) -> None:
        if self._future is not None:
            self._future.cancel()
            self._future = None
        if self.eng is not None:
            self.eng.stop()
        session, self.session = self.session, None
        if session is not None:
            self.app.bridge.run(self.app.backend.close_stream(session))

    # ── playlist ─────────────────────────────────────────────────────────
    def _build_playlist(self) -> None:
        """Picks each episode's variant in the current audio language."""
        assert self.anime is not None
        out: list[models.EpisodeInfo] = []
        for ep in self.base:
            variant = self.anime.get_variant(ep.number, self.audio) or ep
            if variant not in out:
                out.append(variant)
        self.playlist = out

    def go(self, delta: int) -> None:
        """Moves to the next or previous episode.

        Args:
            delta: ``1`` for the next episode, ``-1`` for the previous.
        """
        target = self.index + delta
        if 0 <= target < len(self.playlist):
            self._load(target)

    def _load(self, index: int, start: float = 0.0) -> None:
        """Resolves an episode and plays it from ``start`` seconds."""
        self._token += 1
        token = self._token
        self.index = max(0, min(index, len(self.playlist) - 1))
        ep = self.playlist[self.index]
        self._pending = None
        self._failed = self._finished = False
        self._release_session()
        self._clear_picture()
        self._set_status(f"Resolving episode {ep.ep_str}…")
        self._update_titles(ep)
        self._update_buttons()
        self._future = self.app.bridge.run(
            self.app.backend.open_stream(ep, self.audio),
            ok=lambda s: self._resolved(token, s, start),
            err=lambda e: self._fail(token, e),
        )

    def _resolved(
        self, token: int, session: bridge.StreamSession, start: float
    ) -> None:
        if token != self._token:  # the user moved on: free the stream
            self.app.bridge.run(self.app.backend.close_stream(session))
            return
        self.session = session
        self._update_titles(session.episode)
        self._set_status("Opening stream…")
        ffmpeg = ffmpeg_setup.ffmpeg_exe()
        self.app.bridge.thread(
            lambda: engine.probe(ffmpeg, session.url),
            ok=lambda info: self._probed(token, session, info, start),
            err=lambda e: self._fail(token, e),
        )

    def _probed(
        self,
        token: int,
        session: bridge.StreamSession,
        info: engine.MediaInfo,
        start: float,
    ) -> None:
        if token != self._token:
            return
        if not info.duration and session.duration:
            info = dataclasses.replace(info, duration=session.duration)
        if self.eng is None:
            self.eng = engine.MediaEngine(ffmpeg_setup.ffmpeg_exe())
        self._apply_volume()
        self._info = info
        try:
            self.eng.open(session.url, info)
            self._start_engine(start)
        except engine.EngineError as exc:
            self._fail(token, exc)
            return
        self.eng.resume()
        if info.has_audio and not self.eng.audio_ok:
            self.note.configure(text="No audio device: playing without sound")
        self._set_status("")
        self._update_buttons()

    def _fail(self, token: int, exc: BaseException) -> None:
        if token != self._token:
            return
        self._failed = True
        self._release_session()
        self._set_status(f"✗ {exc}", error=True)
        self._update_buttons()

    # ── engine glue ──────────────────────────────────────────────────────
    def _duration(self) -> float:
        return self.eng.duration if self.eng is not None else 0.0

    def _decode_width(self) -> int:
        """The width to decode to: the size the picture is shown at."""
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw <= 1 or ch <= 1 or self._info is None:
            return DEFAULT_DECODE_WIDTH
        aspect = self._info.width / self._info.height
        return max(320, int(min(cw, ch * aspect)))

    def _start_engine(self, position: float) -> None:
        """Starts decoding at ``position`` at the current picture size."""
        assert self.eng is not None
        self._decoded_w = self._decode_width()
        self.eng.play_from(position, self._decoded_w)
        self._finished = False

    def _apply_volume(self) -> None:
        if self.eng is not None:
            self.eng.volume = self._volume_frac**2  # a gentler, ear-like curve

    # ── tick: draw the due frame, refresh labels ─────────────────────────
    def _schedule_tick(self) -> None:
        if self._tick_job is None:
            self._tick_job = self.after(TICK_MS, self._tick)

    def _tick(self) -> None:
        self._tick_job = None
        if not self.winfo_ismapped():
            return  # another screen is showing; start() restarts the tick
        eng = self.eng
        try:
            if eng is not None and self.session is not None:
                if (err := eng.error) is not None:
                    self._fail(self._token, RuntimeError(err))
                else:
                    frame = eng.take_frame()
                    if frame is not None:
                        self._draw(*frame)
                    now = time.monotonic()
                    if now - self._ui_at >= UI_REFRESH_S:
                        self._ui_at = now
                        self._refresh()
                    if eng.ended and not self._finished:
                        self._on_ended()
        finally:
            self._schedule_tick()

    def _draw(self, w: int, h: int, data: bytes) -> None:
        """Draws one RGB frame, letterboxed in the canvas."""
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        img = Image.frombuffer("RGB", (w, h), data, "raw", "RGB", 0, 1)
        scale = min(cw / w, ch / h)
        fw, fh = max(1, int(w * scale)), max(1, int(h * scale))
        if (fw, fh) != (w, h):
            img = img.resize((fw, fh), Image.Resampling.BILINEAR)
        photo = self._photo
        if photo is not None and (photo.width(), photo.height()) == (fw, fh):
            photo.paste(img)
        else:
            photo = self._photo = ImageTk.PhotoImage(img)
        self.canvas.coords(self._image_item, cw // 2, ch // 2)
        self.canvas.itemconfigure(self._image_item, image=photo)

    def _clear_picture(self) -> None:
        self.canvas.itemconfigure(self._image_item, image="")
        self._photo = None

    def _refresh(self) -> None:
        """Updates the clock, seek bar, play button and buffering notice."""
        eng = self.eng
        if eng is None:
            return
        dur = eng.duration
        pos = self._pending if self._pending is not None else eng.position()
        self.clock.configure(
            text=f"{fmt_time(pos)} / {fmt_time(dur)}" if dur else fmt_time(pos)
        )
        if dur:
            self.seek.set(pos / dur)
        self.btn_play.configure(text="Pause" if eng.playing else "Play")
        if (
            not self._failed
            and not self._finished
            and time.monotonic() >= self._flash_until
        ):
            self._set_status("Buffering…" if eng.buffering else "")

    def _on_ended(self) -> None:
        """Moves on to the next episode, or stops at the end of the list."""
        if self.index < len(self.playlist) - 1:
            self._load(self.index + 1)
            return
        self._finished = True
        if self.eng is not None:
            self.eng.pause()
        self.btn_play.configure(text="Replay")
        self._set_status("End of the list — press Replay to watch again")

    # ── status text over the picture ─────────────────────────────────────
    def _set_status(self, text: str, error: bool = False) -> None:
        if text == self._status and error == self._status_error:
            return
        self._status, self._status_error = text, error
        self.canvas.itemconfigure(
            self._text_item,
            text=text,
            fill=widgets.ERR if error else widgets.TEXT,
        )
        self._center_text()

    def _center_text(self) -> None:
        self.canvas.coords(
            self._text_item,
            self.canvas.winfo_width() // 2,
            self.canvas.winfo_height() // 2,
        )

    def _update_titles(self, ep: models.EpisodeInfo) -> None:
        assert self.anime is not None
        self.title.configure(text=self.anime.title)
        parts = [f"Episode {ep.ep_str}"]
        if name := _display_title(ep):
            parts.append(name)
        parts.append("DUB" if ep.audio == "eng" else "SUB")
        parts.append(f"{self.index + 1} of {len(self.playlist)}")
        self.subtitle.configure(text="  ·  ".join(parts))

    def _update_buttons(self) -> None:
        self.btn_prev.enable(self.index > 0)
        self.btn_next.enable(self.index < len(self.playlist) - 1)

    # ── user actions ─────────────────────────────────────────────────────
    def toggle_pause(self) -> None:
        """Pauses or resumes, retries a failed episode or replays."""
        eng = self.eng
        if self._failed:
            self._load(self.index)
        elif self._finished and eng is not None:
            self._start_engine(0.0)
            eng.resume()
            self._set_status("")
        elif eng is not None and self.session is not None:
            eng.toggle()
        self._refresh()

    def _preview_seek(self, frac: float) -> None:
        """Shows the time under the knob while it is dragged."""
        dur = self._duration()
        if dur:
            self._pending = frac * dur
            self._refresh()

    def seek_by(self, delta: float) -> None:
        """Seeks relative to the current position.

        Args:
            delta: Seconds to move, negative to go back.
        """
        if self.eng is None or self.session is None:
            return
        base = self._pending
        if base is None:
            base = self.eng.position()
        self._seek_to(base + delta)

    def _seek_to(self, position: float, instant: bool = False) -> None:
        """Seeks to a position.

        Rapid requests, such as held arrow keys, are merged into one seek.

        Args:
            position: Where to go, in seconds.
            instant: Whether to seek now instead of after a short pause.
        """
        if self.eng is None or self.session is None:
            return
        dur = self.eng.duration
        position = max(0.0, min(position, dur - 0.5 if dur else position))
        self._pending = position
        self._refresh()
        if self._seek_job is not None:
            self.after_cancel(self._seek_job)
        self._seek_job = self.after(
            0 if instant else SEEK_DELAY_MS, self._commit_seek
        )

    def _commit_seek(self) -> None:
        self._seek_job = None
        target, self._pending = self._pending, None
        if target is None or self.eng is None or self.session is None:
            return
        try:
            self._start_engine(target)
        except engine.EngineError as exc:
            self._fail(self._token, exc)
            return
        if self._finished:
            self._finished = False
            self.eng.resume()
        self._refresh()

    def _set_volume(self, frac: float) -> None:
        self._volume_frac = max(0.0, min(1.0, frac))
        self._apply_volume()

    def _nudge_volume(self, delta: float) -> None:
        self._set_volume(self._volume_frac + delta)
        self.volume.set(self._volume_frac)
        self._save_volume()

    def _wheel(self, e: tk.Event[Any]) -> None:
        self._nudge_volume(VOLUME_STEP if e.delta > 0 else -VOLUME_STEP)

    def _save_volume(self) -> None:
        self.app.cfg.set("volume", round(self._volume_frac * 100))
        bridge.save_settings(self.app.cfg)

    def toggle_mute(self) -> None:
        """Mutes, or restores the volume from before the mute."""
        if self._volume_frac > 0:
            self._pre_mute = self._volume_frac
            target = 0.0
        else:
            target = self._pre_mute or 1.0
        self._set_volume(target)
        self.volume.set(target)
        self._save_volume()

    def _flash(self, text: str, seconds: float = 2.5) -> None:
        """Shows a message over the picture for a short while.

        Args:
            text: The message.
            seconds: How long the periodic refresh leaves it alone.
        """
        self._flash_until = time.monotonic() + seconds
        self._set_status(text, error=True)

    # ── full screen, auto-hiding controls ────────────────────────────────
    def toggle_fullscreen(self) -> None:
        """Enters or leaves full screen."""
        self.app.set_fullscreen(not self.app.fullscreen)

    def on_fullscreen(self, on: bool) -> None:
        """Adapts the screen when full screen turns on or off.

        Args:
            on: True when full screen has just turned on.
        """
        self.btn_full.configure(
            text="Exit full screen" if on else "Full screen"
        )
        self.app.set_chrome(not on)
        self._show_controls()
        if on:
            self._activity()

    def _activity(self, _e: tk.Event[Any] | None = None) -> None:
        """Shows the controls and hides them again after a pause."""
        if self._controls_hidden:
            self._show_controls()
        if self._hide_job is not None:
            self.after_cancel(self._hide_job)
            self._hide_job = None
        if self.app.fullscreen:
            self._hide_job = self.after(HIDE_AFTER_MS, self._auto_hide)

    def _auto_hide(self) -> None:
        """Hides the controls, or tries again later if they are in use."""
        self._hide_job = None
        if not self.app.fullscreen:
            return
        playing = self.eng is not None and self.eng.playing
        if playing and not self.seek.dragging:
            self.top.pack_forget()
            self.controls.pack_forget()
            self.canvas.configure(cursor="none")
            self._controls_hidden = True
        else:  # paused, between episodes or dragging: keep them, look again
            self._hide_job = self.after(HIDE_AFTER_MS, self._auto_hide)

    def _show_controls(self) -> None:
        if self._controls_hidden:
            self.top.pack(fill="x", before=self.stage)
            self.controls.pack(fill="x", pady=(10, 0), after=self.stage)
            self.canvas.configure(cursor="")
            self._controls_hidden = False

    def _on_canvas_resize(self, _e: tk.Event[Any]) -> None:
        self._center_text()
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(500, self._maybe_redecode)

    def _maybe_redecode(self) -> None:
        """Decodes again at a new size after a big window size change.

        A small change is just scaled when drawn; a big one (such as full
        screen) would look soft or waste work, so FFmpeg restarts at the
        current position with the new size.
        """
        self._resize_job = None
        if (
            self.eng is None
            or self.session is None
            or self._pending is not None
            or not self._decoded_w
        ):
            return
        want = self._decode_width()
        if abs(want - self._decoded_w) / self._decoded_w < 0.3:
            return
        was_playing, pos = self.eng.playing, self.eng.position()
        try:
            self._start_engine(pos)
        except engine.EngineError as exc:
            self._fail(self._token, exc)
            return
        if was_playing:
            self.eng.resume()

    # ── keyboard ─────────────────────────────────────────────────────────
    def _key(self, e: tk.Event[Any]) -> None:
        if not self.winfo_ismapped():
            return
        keys: dict[str, Any] = {
            "space": self.toggle_pause,
            "k": self.toggle_pause,
            "Left": lambda: self.seek_by(-SEEK_STEP),
            "Right": lambda: self.seek_by(SEEK_STEP),
            "j": lambda: self.seek_by(-SEEK_STEP_LONG),
            "l": lambda: self.seek_by(SEEK_STEP_LONG),
            "Home": lambda: self._seek_to(0.0, True),
            "Up": lambda: self._nudge_volume(VOLUME_STEP),
            "Down": lambda: self._nudge_volume(-VOLUME_STEP),
            "m": self.toggle_mute,
            "f": self.toggle_fullscreen,
            "n": lambda: self.go(1),
            "p": lambda: self.go(-1),
        }
        action = keys.get(e.keysym)
        if action is not None:
            action()
            self._activity()
