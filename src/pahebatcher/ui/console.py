"""Rich console singleton, wordmark and the small layout helpers shared by every screen.

Design language: black ground, one pink accent, muted secondary text, no boxes.
The selected row is marked by a pink ``▸`` and pink text — never by a frame.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

from rich.console import Console
from rich.table import Table
from rich.text import Text

from pahebatcher.config import VERSION
from pahebatcher.ui.theme import THEME

# Windows consoles / redirected output default to cp1252, which can't print ✓ ✗ ▸ ━ — never crash on that.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

console = Console(theme=THEME, highlight=False)

PAD = "  "

# The white anime mascot (braille dots, 2x4 per cell), generated from assets/logo/pahebatcher.svg.
MASCOT = (
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣠⣴⠶⠿⠻⢷⣄",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣠⣴⠟⠋⣀⣤⡶⣦⠈⢿⡆",
    "⠀⠀⠀⠀⠀⠀⠀⠀⢀⣠⣴⠾⠟⠛⠋⠁⢠⣾⡿⠋⢰⠸⡆⠘⣿⠀⠀⢠⡾⠛⢷⡄",
    "⠀⢀⣤⣶⠶⠶⠿⠿⠟⠋⣀⣤⣶⣾⣿⣿⣿⣿⣷⣶⣬⣀⣷⠀⣿⡆⢠⣿⠁⢠⣾⣃⣤⣤⡀",
    "⢰⡿⠁⣠⣤⣴⣶⣶⣦⣾⣿⣿⣿⡿⠛⠉⠀⠀⠀⠈⠉⠛⢷⣄⠈⠻⣿⠃⢠⣿⡿⠟⠁⢈⣿",
    "⢸⣷⠀⢿⡄⡆⠀⢉⣿⣿⣿⡿⠁⠀⠀⠀⠀⢀⡠⠄⠀⠒⠀⢈⢳⣄⠘⢷⣿⠋⢀⣤⡾⠟⠁",
    "⠀⢿⡆⠘⣷⡜⢀⣾⣿⣿⠏⠐⢀⠀⠀⢀⠀⠈⠀⠀⣿⣦⠀⠀⢣⠹⣆⠈⣿⣿⡿⠿⠿⢷⣆",
    "⠀⠈⢿⡄⠈⢿⣿⣿⣿⡏⠀⠆⠉⠀⢠⣿⡇⢀⣤⣼⠟⣉⣹⠀⠀⠁⠟⠀⣿⣤⣤⣤⣤⣾⠏",
    "⠀⠀⢨⣿⠀⣼⣿⣿⣿⠀⠀⠀⠠⡴⠛⣉⣙⣿⣿⡿⢿⣿⣿⡇⠀⠘⠀⣼⣟⢉⣉⣀⣀⣀⣀⣀",
    "⠀⠀⣸⡇⢀⣿⣿⣿⣿⠀⠀⠀⠀⠙⢾⣿⣿⣿⣿⣿⣾⣿⠟⣠⣤⣤⣠⡈⠻⠟⠉⠉⠉⠉⠉⠛⣷⡄",
    "⠀⠀⣿⡇⠸⣿⣿⣿⣿⣧⡀⠀⠀⠈⣫⣉⣛⠛⢛⣉⣩⣤⡀⠾⢿⣿⣷⣿⠀⠀⢸⣿⣿⣿⣷⠀⣿⡇",
    "⠀⠀⠘⣷⣄⠉⠻⢿⣿⣿⡿⣣⣾⢸⡿⣿⠟⢁⡉⠁⠀⠀⠀⠀⠀⠀⠀⠛⠀⠀⢸⣿⣿⣿⡇⠀⣿⣷⣶⣶⣤⣄⡀",
    "⠀⠀⠀⣨⡿⠃⢀⡀⠀⠀⣰⣿⣿⡎⠓⡀⣴⠟⠀⠀⠀⠀⠀⠀⠀⠀⠀⠑⠄⠀⣼⣿⣿⣿⡇⠀⣀⣀⣤⣄⣀⠉⠻⣷⣄",
    "⠀⠀⢰⡿⠀⣼⣿⡟⠀⠀⢿⣿⣿⡖⢰⣥⠁⠀⠀⢠⣶⣦⣀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣷⣾⣿⣿⣿⣿⣿⣿⣦⡈⢻⣧",
    "⠀⠀⢸⣇⠀⣿⣿⡇⠀⠸⡤⣭⣉⠀⣠⠁⠀⠀⠀⢸⣿⣿⣿⣷⡆⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⡿⠟⠛⠿⣿⣿⣿⣿⣷⡀⢻⡇",
    "⠀⠀⠘⣿⡄⠙⢿⣿⣶⣤⣝⠮⡝⢠⠏⠀⠀⠀⠀⢸⣿⠿⠋⠁⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⡟⢠⣾⣿⣷⡌⢿⣿⣿⣿⣇⢸⣿",
    "⠀⠀⠀⠈⠻⣦⣄⡉⠉⠉⠉⠀⠀⠌⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣼⠃⣿⣿⣿⣿⡇⣾⣿⣿⣿⡇⢸⣿⣿⣿⡟⢸⣿",
    "⠀⠀⠀⠀⠀⠈⠉⠛⠛⠛⣿⠃⣿⠀⠀⠀⠀⠀⢠⣀⠀⠀⠀⠀⢀⣀⡴⠋⣩⣤⣿⣿⣿⣿⣧⣬⠙⠿⠋⣠⣿⣿⣿⣿⠇⣸⡏",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⢰⡿⢸⡇⠀⠀⠀⠀⠀⡿⢩⣉⣛⣛⣋⣩⣥⣦⡀⠙⣿⣿⣿⣿⣿⡿⢋⣴⣾⣿⣿⣿⣿⣿⠏⣰⡿",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⣾⡇⣾⠁⠀⠀⠀⠀⢸⡇⣾⡏⠉⠉⠉⠉⠁⠙⢷⣄⠈⢿⣿⣿⠟⠁⢾⣿⣿⣿⣿⠿⠛⢁⣴⠟⠁",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⢻⣧⣙⣛⣛⣛⣛⣛⣋⣴⡟⠀⠀⠀⠀⠀⠀⠀⠈⢻⣦⠀⠙⠋⢀⣤⣄⣀⣀⣀⣀⣤⣶⠟⠁",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠙⠛⠛⠛⠛⠛⠋⠉⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⢷⣦⣶⠿⠋⠉⠙⠛⠛⠉⠉",
)
MASCOT_WIDTH = max(len(line) for line in MASCOT)


def mascot() -> Text:
    return Text("\n".join(MASCOT), style="bold white", no_wrap=True)


def wordmark(size_style: str = "bold") -> Text:
    """``pahe`` in brand pink, ``batcher`` in white — mirrors the animepahe logo."""
    t = Text()
    t.append("pahe", style="brand.bold" if size_style == "bold" else "brand")
    t.append("batcher", style="bold white" if size_style == "bold" else "white")
    return t


def print_banner(status: str | None = None) -> None:
    """White anime mascot, with wordmark, tagline and quiet status beside it (below on narrow terminals)."""
    info = Text()
    info.append_text(wordmark())
    info.append("\n")
    info.append("batch-download & stream from animepahe\n", style="muted")
    if status:
        info.append("● ", style="ok")
        info.append(status + "  ", style="muted")
    info.append(f"v{VERSION}", style="muted")

    width = console.width
    console.print()
    if width >= MASCOT_WIDTH + 44:  # side by side
        grid = Table.grid(padding=(0, 2))
        grid.add_column(no_wrap=True)
        grid.add_column(vertical="middle")
        grid.add_row(mascot(), info)
        console.print(grid)
    elif width >= MASCOT_WIDTH + 2:  # mascot on top
        console.print(mascot())
        console.print()
        console.print(info)
    else:  # too narrow for the art
        console.print(info)
    console.print()


def section(title: str) -> None:
    """Small spaced, uppercase, muted label — replaces boxed headers and rules."""
    console.print()
    console.print(Text(PAD + title.upper(), style="muted"))


def heading(title: str, sub: str = "") -> None:
    console.print()
    console.print(Text(PAD + title, style="bold white"))
    if sub:
        console.print(Text(PAD + sub, style="muted"))


def hint_bar(*pairs: tuple[str, str]) -> None:
    """Single quiet footer line: ``key action  key action``."""
    line = Text(PAD)
    for i, (key, action) in enumerate(pairs):
        if i:
            line.append("   ")
        line.append(key, style="white")
        line.append(" " + action, style="muted")
    console.print()
    console.print(line)


def menu(items: Iterable[tuple[str, str, str]], selected: str | None = None) -> None:
    """Plain list: ``▸ key  label  description``. Selected row is pink, others default."""
    for key, label, desc in items:
        on = key == selected
        row = Text(PAD)
        row.append("▸ " if on else "  ", style="brand")
        row.append(f"{key}  ", style="brand" if on else "muted")
        row.append(f"{label:<14}", style="brand" if on else "white")
        if desc:
            row.append(desc, style="muted")
        console.print(row)


def prompt_label(text: str) -> str:
    """Markup for a prompt: pink chevron, muted hint is added by the caller."""
    return f"{PAD}[brand]\u203a[/brand] {text}"
