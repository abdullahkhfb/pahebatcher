"""Rich table rendering helpers — borderless, muted headers, one pink accent."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich import box
from rich.table import Table

from pahebatcher.utils import fmt_bytes

if TYPE_CHECKING:
    from pahebatcher.models import AnimeInfo, EpisodeInfo


def _table(**kw: Any) -> Table:
    return Table(
        box=box.SIMPLE_HEAD, show_edge=False, pad_edge=True, padding=(0, 2),
        header_style="muted", border_style="track", **kw,
    )


def episode_table(
    anime: AnimeInfo,
    episodes: list[EpisodeInfo],
    selected_set: set[str] | None = None,
    show_audio: bool = True,
) -> Table:
    """Render episode list as Rich Table."""
    t = _table()
    t.add_column("", width=2, justify="center", style="brand")
    t.add_column("Ep", width=6, justify="right", style="muted")
    t.add_column("Title", style="white")
    if show_audio:
        t.add_column("Audio", width=5)

    seen_nums = set()
    for ep in episodes:
        if ep.number in seen_nums:
            continue
        seen_nums.add(ep.number)
        check = "\u2713" if selected_set and ep.session in selected_set else " "
        row = [check, ep.ep_str, ep.title or "\u2014"]
        if show_audio:
            from pahebatcher.utils import audio_badge
            row.append(audio_badge(ep.audio))
        t.add_row(*row)
    return t


def summary_table(
    results: dict[str, Path | None],
    episodes: list[EpisodeInfo],
    output_dir: str,
) -> Table:
    """Render download completion summary."""
    from pahebatcher.utils import audio_badge

    table = _table()
    table.add_column("Ep", style="muted", width=6, justify="right")
    table.add_column("Title", style="white", ratio=1, overflow="ellipsis")
    table.add_column("Audio", width=5)
    table.add_column("Status", justify="left", width=9)
    table.add_column("Size", justify="right", width=10, style="muted")
    table.add_column("File", style="muted", ratio=1, overflow="ellipsis")

    for ep in episodes:
        path = results.get(ep.session)
        badge = "[ok]\u2713 done[/ok]" if path else "[red]\u2717 failed[/red]"
        size = fmt_bytes(path.stat().st_size) if path and path.exists() else "\u2014"
        fname = path.name if path else "\u2014"
        table.add_row(ep.ep_str, ep.title or "\u2014", audio_badge(ep.audio), badge, size, fname)
    return table


def search_results_table(results: list[dict[str, Any]], query: str) -> Table:
    """Render interactive search results."""
    table = _table(title=f"[muted]results for[/muted] {query}", title_justify="left")
    table.add_column("#", justify="right", style="brand", width=3)
    table.add_column("Title", ratio=1)
    table.add_column("Type", justify="left", width=7, style="muted")
    table.add_column("Year", justify="left", width=5, style="muted")
    table.add_column("Eps", justify="right", width=4, style="muted")
    table.add_column("Score", justify="right", width=5, style="muted")
    for i, res in enumerate(results, 1):
        table.add_row(
            str(i), res.get("title", "Unknown"), res.get("type", "-"),
            str(res.get("year", "-")), str(res.get("episodes", "-")),
            str(res.get("score", "-")),
        )
    return table
