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

import collections
import dataclasses
import time
from typing import Any

import rich.console
import rich.live
import rich.padding
import rich.progress_bar
import rich.table
import rich.text

from pahebatcher import utils
import pahebatcher.ui.console

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


@dataclasses.dataclass
class _Ep:
    """Display state of one episode row.

    Attributes:
        label: The text shown for the episode.
        state: The current state, such as ``"waiting"`` or ``"done"``.
        total: The number of segments.
        done: The number of finished segments.
        nbytes: The bytes downloaded so far.
        note: Extra text, such as a failure reason.
        samples: Recent ``(time, bytes)`` pairs used to compute speed.
    """

    label: str
    state: str = "waiting"
    total: int = 0
    done: int = 0
    nbytes: int = 0
    note: str = ""
    samples: collections.deque[tuple[float, int]] = dataclasses.field(
        default_factory=collections.deque
    )

    def speed(self, now: float) -> float:
        """Computes the recent download speed.

        Args:
            now: The current monotonic time.

        Returns:
            Bytes per second, or 0.0 if there are too few samples.
        """
        while self.samples and now - self.samples[0][0] > _WINDOW:
            self.samples.popleft()
        if len(self.samples) < 2:
            return 0.0
        dt = self.samples[-1][0] - self.samples[0][0]
        return (
            (self.samples[-1][1] - self.samples[0][1]) / dt if dt > 0 else 0.0
        )

    @property
    def fraction(self) -> float:
        """The fraction of segments finished, from 0.0 to 1.0."""
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
    """A live terminal display of per-episode download progress."""

    def __init__(
        self,
        total_eps: int,
        console: Any | None = None,
        title: str = "",
    ) -> None:
        """Initializes the dashboard without drawing it.

        Args:
            total_eps: The number of episodes in the batch.
            console: The Rich console to draw on. The shared console is used if
                None.
            title: The title shown above the progress.
        """
        self._total_eps = total_eps
        self._title = title
        self._eps: dict[str, _Ep] = {}
        self._live: rich.live.Live | None = None
        self._started = time.monotonic()

        if console is None:
            console = pahebatcher.ui.console.console
        self._console = console

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        """Starts the live display."""
        self._console.clear()
        self._live = rich.live.Live(
            get_renderable=self._render,
            console=self._console,
            refresh_per_second=8,
        )
        self._live.start()

    def stop(self) -> None:
        """Stops the live display."""
        if self._live:
            self._live.stop()
        time.sleep(0.1)

    # ── rendering ────────────────────────────────────────────────────────
    @property
    def _done_eps(self) -> int:
        return sum(1 for e in self._eps.values() if e.state == "done")

    def _overall(self, now: float) -> tuple[float, float, float | None]:
        """Computes the batch-wide progress.

        Args:
            now: The current monotonic time.

        Returns:
            A tuple ``(fraction, bytes_per_second, eta_seconds)``. The ETA is
            None while it cannot be estimated.
        """
        n = max(self._total_eps, len(self._eps), 1)
        frac = sum(e.fraction for e in self._eps.values()) / n
        speed = sum(
            e.speed(now) for e in self._eps.values() if e.state == "downloading"
        )
        remaining = 0.0
        for e in self._eps.values():
            if e.state == "downloading" and e.done and e.total:
                remaining += (e.total - e.done) * (e.nbytes / e.done)
        eta = remaining / speed if speed > 0 and remaining > 0 else None
        return frac, speed, eta

    def _render(self) -> rich.console.RenderableType:
        """Builds the full dashboard for the live display.

        Returns:
            The renderable.
        """
        now = time.monotonic()
        frac, speed, eta = self._overall(now)
        active = any(
            e.state in ("downloading", "remuxing", "resolving")
            for e in self._eps.values()
        )
        finished = (
            self._eps
            and all(e.state in ("done", "fail") for e in self._eps.values())
        )

        status = rich.text.Text()
        if finished:
            status.append("● finished", style="ok")
        elif active:
            status.append("● downloading", style="brand")
        else:
            status.append("● starting", style="muted")

        head = rich.table.Table.grid(expand=True)
        head.add_column(ratio=1)
        head.add_column(justify="right")
        head.add_row(pahebatcher.ui.console.wordmark(), status)

        parts: list[rich.console.RenderableType] = [head, rich.text.Text("")]
        if self._title:
            parts.append(rich.text.Text(self._title, style="bold white"))
        sub = rich.text.Text(
            f"{self._done_eps} of {self._total_eps}", style="muted"
        )
        if speed > 0:
            sub.append(
                f" · {utils.fmt_bytes(int(speed))}/s · eta {_fmt_eta(eta)}",
                style="muted",
            )
        parts.append(sub)
        parts.append(rich.text.Text(""))

        overall = rich.table.Table.grid(expand=True, padding=(0, 2))
        overall.add_column(ratio=1)
        overall.add_column(width=4, justify="right")
        overall.add_row(
            rich.progress_bar.ProgressBar(
                total=100,
                completed=frac * 100,
                complete_style="bar",
                finished_style="bar",
                style="track",
            ),
            rich.text.Text(f"{frac * 100:.0f}%", style="brand"),
        )
        parts.append(overall)
        parts.append(rich.text.Text(""))

        rows = rich.table.Table.grid(expand=True, padding=(0, 2))
        rows.add_column(width=5, style="muted")
        rows.add_column(width=14, no_wrap=True)
        rows.add_column(ratio=1)
        rows.add_column(width=22, justify="right", no_wrap=True)
        for key, ep in self._eps.items():
            glyph, style = _STATES.get(ep.state, (ep.state, "muted"))
            if ep.note:
                glyph = ep.note
            bar = rich.progress_bar.ProgressBar(
                total=100, completed=ep.fraction * 100, complete_style="bar",
                finished_style="bar", style="track",
            )
            rows.add_row(
                f"EP {key}",
                rich.text.Text(glyph, style=style),
                bar,
                detail_text(ep, now),
            )
        parts.append(rows)
        parts.append(rich.text.Text(""))
        return rich.padding.Padding(rich.console.Group(*parts), (1, 2))

    # ── state updates (public API used by the downloader) ────────────────
    def add_ep(self, key: str, label: str, total_segments: int = 0) -> None:
        """Adds an episode row.

        Args:
            key: The episode's key.
            label: The text to show.
            total_segments: The number of segments, if already known.
        """
        ep = self._eps.get(key)
        if ep is None:
            self._eps[key] = _Ep(label=label, total=total_segments)
        else:
            ep.label = label
            if total_segments and not ep.total:
                ep.total = total_segments

    def set_total(self, key: str, n_segments: int) -> None:
        """Sets an episode's segment count.

        Args:
            key: The episode's key.
            n_segments: The number of segments.
        """
        if ep := self._eps.get(key):
            ep.total = n_segments

    def seg_done(self, key: str, nbytes: int) -> None:
        """Records one finished segment.

        Args:
            key: The episode's key.
            nbytes: The size of the segment in bytes.
        """
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
        """Marks an episode as waiting.

        Args:
            key: The episode's key.
            label: The text to show.
        """
        self._set(key, "waiting", label)

    def mark_queued(self, key: str, label: str) -> None:
        """Marks an episode as queued for download.

        Args:
            key: The episode's key.
            label: The text to show.
        """
        self._set(key, "queued", label)

    def mark_resolving(self, key: str, label: str) -> None:
        """Marks an episode as having its stream resolved.

        Args:
            key: The episode's key.
            label: The text to show.
        """
        self._set(key, "resolving", label)

    def mark_downloading(self, key: str, label: str) -> None:
        """Marks an episode as downloading.

        Args:
            key: The episode's key.
            label: The text to show.
        """
        self._set(key, "downloading", label)

    def mark_remuxing(self, key: str, label: str) -> None:
        """Marks an episode as being assembled into its final file.

        Args:
            key: The episode's key.
            label: The text to show.
        """
        self._set(key, "remuxing", label)

    def mark_retry(self, key: str, label: str) -> None:
        """Marks an episode as being retried.

        Args:
            key: The episode's key.
            label: The text to show.
        """
        self._set(key, "retry", label)

    def mark_done(self, key: str, label: str) -> None:
        """Marks an episode as finished.

        Args:
            key: The episode's key.
            label: The text to show.
        """
        if ep := self._eps.get(key):
            ep.total = ep.total or ep.done or 1
            ep.done = ep.total
        self._set(key, "done", label)

    def mark_fail(self, key: str, reason: str) -> None:
        """Marks an episode as failed.

        Args:
            key: The episode's key.
            reason: The text to show.
        """
        self._set(key, "fail", reason)


def detail_text(ep: _Ep, now: float) -> rich.text.Text:
    """Builds the detail text shown beside an episode's bar.

    Args:
        ep: The episode row.
        now: The current monotonic time.

    Returns:
        The styled text.
    """
    t = rich.text.Text(style="muted")
    if ep.state == "done":
        t.append(
            utils.fmt_bytes(ep.nbytes) if ep.nbytes else "already exists"
        )
    elif ep.state == "downloading":
        if ep.total:
            t.append(f"{ep.done}/{ep.total}")
        sp = ep.speed(now)
        if sp > 0:
            t.append(f" · {utils.fmt_bytes(int(sp))}/s")
    elif ep.state in ("resolving", "retry", "remuxing") and ep.total:
        t.append(f"{ep.done}/{ep.total}")
    elif ep.state == "fail":
        t = rich.text.Text(ep.label[:22], style="red")
    return t
