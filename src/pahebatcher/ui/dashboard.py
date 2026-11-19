"""Minimal live dashboard for download progress.

Layout (one pink accent, thin bars, no boxes)::

    pahebatcher                                   ● downloading
    Dandivine
    2 of 4 · 24.8 MB/s · eta 01:12
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  58%

    EP 1   ✓ done          ━━━━━━━━━━━━━━━━━━━━━━━━━━        142 MB
    EP 3   ↓ downloading   ━━━━━━━━━━━━━━━━━╺━━━━━━━  41/64 · 12.4 MB/s
    EP 4   resolving       ╸                                  5/64
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from rich.console import Group, RenderableType
from rich.live import Live
from rich.padding import Padding
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from pahebatcher.ui.console import wordmark
from pahebatcher.utils import fmt_bytes

_WINDOW = 4.0  # seconds of history used for speed / ETA

# state -> (glyph + label, style)
_STATES: dict[str, tuple[str, str]] = {
    "waiting": ("· waiting", "muted"),
    "queued": ("· queued", "muted"),
    "resolving": ("resolving", "warn"),
    "downloading": ("↓ downloading", "brand"),
    "remuxing": ("remuxing", "warn"),
    "retry": ("↻ retry", "warn"),
    "done": ("✓ done", "ok"),
    "fail": ("✗ failed", "red"),
}


@dataclass
class _Ep:
    label: str
    state: str = "waiting"
    total: int = 0
    done: int = 0
    nbytes: int = 0
    note: str = ""
    samples: deque[tuple[float, int]] = field(default_factory=deque)

    def speed(self, now: float) -> float:
        while self.samples and now - self.samples[0][0] > _WINDOW:
            self.samples.popleft()
        if len(self.samples) < 2:
            return 0.0
        dt = self.samples[-1][0] - self.samples[0][0]
        return (self.samples[-1][1] - self.samples[0][1]) / dt if dt > 0 else 0.0

    @property
    def fraction(self) -> float:
        if self.state == "done":
            return 1.0
        return min(1.0, self.done / self.total) if self.total else 0.0


def _fmt_eta(seconds: float | None) -> str:
    if seconds is None or seconds <= 0 or seconds > 99 * 3600:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Dashboard:
    def __init__(self, total_eps: int, console: Any | None = None, title: str = "") -> None:
        self._total_eps = total_eps
        self._title = title
        self._eps: dict[str, _Ep] = {}
        self._live: Live | None = None
        self._started = time.monotonic()

        if console is None:
            from pahebatcher.ui.console import console as default_console
            console = default_console
        self._console = console

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        self._console.clear()
        self._live = Live(
            get_renderable=self._render, console=self._console, refresh_per_second=8,
        )
        self._live.start()

    def stop(self) -> None:
        if self._live:
            self._live.stop()
        time.sleep(0.1)

    # ── rendering ────────────────────────────────────────────────────────
    @property
    def _done_eps(self) -> int:
        return sum(1 for e in self._eps.values() if e.state == "done")

    def _overall(self, now: float) -> tuple[float, float, float | None]:
        """(fraction, bytes/sec, eta seconds)."""
        n = max(self._total_eps, len(self._eps), 1)
        frac = sum(e.fraction for e in self._eps.values()) / n
        speed = sum(e.speed(now) for e in self._eps.values() if e.state == "downloading")
        remaining = 0.0
        for e in self._eps.values():
            if e.state == "downloading" and e.done and e.total:
                remaining += (e.total - e.done) * (e.nbytes / e.done)
        eta = remaining / speed if speed > 0 and remaining > 0 else None
        return frac, speed, eta

    def _render(self) -> RenderableType:
        now = time.monotonic()
        frac, speed, eta = self._overall(now)
        active = any(e.state in ("downloading", "remuxing", "resolving") for e in self._eps.values())
        finished = self._eps and all(e.state in ("done", "fail") for e in self._eps.values())

        status = Text()
        if finished:
            status.append("● finished", style="ok")
        elif active:
            status.append("● downloading", style="brand")
        else:
            status.append("● starting", style="muted")

        head = Table.grid(expand=True)
        head.add_column(ratio=1)
        head.add_column(justify="right")
        head.add_row(wordmark(), status)

        parts: list[RenderableType] = [head, Text("")]
        if self._title:
            parts.append(Text(self._title, style="bold white"))
        sub = Text(f"{self._done_eps} of {self._total_eps}", style="muted")
        if speed > 0:
            sub.append(f" · {fmt_bytes(int(speed))}/s · eta {_fmt_eta(eta)}", style="muted")
        parts.append(sub)
        parts.append(Text(""))

        overall = Table.grid(expand=True, padding=(0, 2))
        overall.add_column(ratio=1)
        overall.add_column(width=4, justify="right")
        overall.add_row(
            ProgressBar(total=100, completed=frac * 100, complete_style="bar",
                        finished_style="bar", style="track"),
            Text(f"{frac * 100:.0f}%", style="brand"),
        )
        parts.append(overall)
        parts.append(Text(""))

        rows = Table.grid(expand=True, padding=(0, 2))
        rows.add_column(width=5, style="muted")
        rows.add_column(width=14, no_wrap=True)
        rows.add_column(ratio=1)
        rows.add_column(width=22, justify="right", no_wrap=True)
        for key, ep in self._eps.items():
            glyph, style = _STATES.get(ep.state, (ep.state, "muted"))
            if ep.note:
                glyph = ep.note
            bar = ProgressBar(
                total=100, completed=ep.fraction * 100, complete_style="bar",
                finished_style="bar", style="track",
            )
            rows.add_row(f"EP {key}", Text(glyph, style=style), bar, self._detail(ep, now))
        parts.append(rows)
        parts.append(Text(""))
        return Padding(Group(*parts), (1, 2))

    @staticmethod
    def _detail(ep: _Ep, now: float) -> Text:
        t = Text(style="muted")
        if ep.state == "done":
            t.append(fmt_bytes(ep.nbytes) if ep.nbytes else "already exists")
        elif ep.state == "downloading":
            if ep.total:
                t.append(f"{ep.done}/{ep.total}")
            sp = ep.speed(now)
            if sp > 0:
                t.append(f" · {fmt_bytes(int(sp))}/s")
        elif ep.state in ("resolving", "retry", "remuxing") and ep.total:
            t.append(f"{ep.done}/{ep.total}")
        elif ep.state == "fail":
            t = Text(ep.label[:22], style="red")
        return t

    # ── state updates (public API used by the downloader) ────────────────
    def add_ep(self, key: str, label: str, total_segments: int = 0) -> None:
        ep = self._eps.get(key)
        if ep is None:
            self._eps[key] = _Ep(label=label, total=total_segments)
        else:
            ep.label = label
            if total_segments and not ep.total:
                ep.total = total_segments

    def set_total(self, key: str, n_segments: int) -> None:
        if ep := self._eps.get(key):
            ep.total = n_segments

    def seg_done(self, key: str, nbytes: int) -> None:
        if ep := self._eps.get(key):
            ep.done += 1
            ep.nbytes += nbytes
            ep.samples.append((time.monotonic(), ep.nbytes))

    def _set(self, key: str, state: str, label: str | None = None) -> None:
        if ep := self._eps.get(key):
            ep.state = state
            if label is not None:
                ep.label = label

    def mark_waiting(self, key: str, label: str) -> None:
        self._set(key, "waiting", label)

    def mark_queued(self, key: str, label: str) -> None:
        self._set(key, "queued", label)

    def mark_resolving(self, key: str, label: str) -> None:
        self._set(key, "resolving", label)

    def mark_downloading(self, key: str, label: str) -> None:
        self._set(key, "downloading", label)

    def mark_remuxing(self, key: str, label: str) -> None:
        self._set(key, "remuxing", label)

    def mark_retry(self, key: str, label: str) -> None:
        self._set(key, "retry", label)

    def mark_done(self, key: str, label: str) -> None:
        if ep := self._eps.get(key):
            ep.total = ep.total or ep.done or 1
            ep.done = ep.total
        self._set(key, "done", label)

    def mark_fail(self, key: str, reason: str) -> None:
        self._set(key, "fail", reason)
