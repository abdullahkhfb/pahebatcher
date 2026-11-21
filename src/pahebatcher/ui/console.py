"""Rich console singleton and the layout helpers shared by every screen.

Design language: black ground, one pink accent, muted secondary text and
no boxes. The selected row is marked by a pink ``▸`` and pink text,
never by a frame.
"""

from __future__ import annotations

from collections.abc import Iterable
import sys

import rich.console
import rich.table
import rich.text

from pahebatcher import config
from pahebatcher.ui import theme

# Windows consoles / redirected output default to cp1252, which can't print ✓ ✗
# ▸ ━ — never crash on that.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

console = rich.console.Console(theme=theme.THEME, highlight=False)

PAD = "  "

# The white anime mascot (braille dots, 2x4 per cell), generated from
# assets/logo/pahebatcher.svg.
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


def mascot() -> rich.text.Text:
    return rich.text.Text("\n".join(MASCOT), style="bold white", no_wrap=True)


def wordmark(size_style: str = "bold") -> rich.text.Text:
    """Builds the ``pahebatcher`` wordmark.

    ``pahe`` is in brand pink and ``batcher`` in white, mirroring the
    animepahe logo.

    Args:
        size_style: The Rich style applied to the whole mark.

    Returns:
        The styled text.
    """
    t = rich.text.Text()
    t.append("pahe", style="brand.bold" if size_style == "bold" else "brand")
    t.append("batcher", style="bold white" if size_style == "bold" else "white")
    return t


def print_banner(status: str | None = None) -> None:
    """Prints the mascot, wordmark, tagline and status.

    The text sits beside the mascot, or below it on narrow terminals.

    Args:
        status: A short status line to show quietly, or None.
    """
    info = rich.text.Text()
    info.append_text(wordmark())
    info.append("\n")
    info.append("batch-download & stream from animepahe\n", style="muted")
    if status:
        info.append("● ", style="ok")
        info.append(status + "  ", style="muted")
    info.append(f"v{config.VERSION}", style="muted")

    width = console.width
    console.print()
    if width >= MASCOT_WIDTH + 44:  # side by side
        grid = rich.table.Table.grid(padding=(0, 2))
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
    """Prints a small, spaced, uppercase, muted label.

    It replaces boxed headers and rules.

    Args:
        title: The label text.
    """
    console.print()
    console.print(rich.text.Text(PAD + title.upper(), style="muted"))


def heading(title: str, sub: str = "") -> None:
    """Prints a heading with an optional muted subtitle.

    Args:
        title: The heading text.
        sub: The subtitle, or empty for none.
    """
    console.print()
    console.print(rich.text.Text(PAD + title, style="bold white"))
    if sub:
        console.print(rich.text.Text(PAD + sub, style="muted"))


def hint_bar(*pairs: tuple[str, str]) -> None:
    """Prints a single quiet footer line of key hints.

    For example ``key action  key action``.

    Args:
        *pairs: ``(key, action)`` tuples.
    """
    line = rich.text.Text(PAD)
    for i, (key, action) in enumerate(pairs):
        if i:
            line.append("   ")
        line.append(key, style="white")
        line.append(" " + action, style="muted")
    console.print()
    console.print(line)


def menu(
    items: Iterable[tuple[str, str, str]],
    selected: str | None = None,
) -> None:
    """Prints a plain list of ``▸ key  label  description`` rows.

    The selected row is pink; the others use the default color.

    Args:
        items: ``(key, label, description)`` tuples.
        selected: The key of the selected row, or None.
    """
    for key, label, desc in items:
        on = key == selected
        row = rich.text.Text(PAD)
        row.append("▸ " if on else "  ", style="brand")
        row.append(f"{key}  ", style="brand" if on else "muted")
        row.append(f"{label:<14}", style="brand" if on else "white")
        if desc:
            row.append(desc, style="muted")
        console.print(row)


def prompt_label(text: str) -> str:
    """Builds the markup for a prompt: a pink chevron and the text.

    The caller adds any muted hint.

    Args:
        text: The prompt text.

    Returns:
        The Rich markup.
    """
    return f"{PAD}[brand]\u203a[/brand] {text}"
