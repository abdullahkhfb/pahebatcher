"""Small, dependency-free helpers shared across pahebatcher."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pahebatcher import models


def sanitize(name: str) -> str:
    """Makes a string safe to use as a file or directory name.

    Removes every character that is not a word character, whitespace, hyphen
    or period, then replaces spaces with underscores and collapses repeats.

    Args:
        name: The raw name, such as an anime or episode title.

    Returns:
        The sanitized name. It may be empty if nothing usable remained.
    """
    safe = re.sub(r"[^\w\s\-.]", "", name).strip().replace(" ", "_")
    return re.sub(r"_+", "_", safe)


def ep_prefix(ep_num: str) -> str:
    """Formats an episode number for use in file names.

    Whole numbers are zero-padded to three digits (``"7"`` becomes ``"007"``)
    and fractional numbers to five characters (``"7.5"`` becomes ``"007.5"``).

    Args:
        ep_num: The episode number as text.

    Returns:
        The padded number, or ``ep_num`` unchanged if it is not numeric.
    """
    try:
        return (
            f"{float(ep_num):05.1f}" if "." in ep_num else f"{int(ep_num):03d}"
        )
    except (ValueError, TypeError):
        return ep_num


def fmt_bytes(n: int) -> str:
    """Formats a byte count for display.

    Args:
        n: The number of bytes.

    Returns:
        A string with one decimal place and a unit from B to TB, such as
        ``"1.5 MB"``.
    """
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def compact_ep_range(episodes: list[models.EpisodeInfo]) -> str:
    """Summarizes episode numbers as a compact list of ranges.

    Consecutive numbers are merged into ranges with an en dash between
    the ends, so episodes 1, 2, 3 and 7 become the range 1 to 3 followed by
    7, joined by a comma.

    Args:
        episodes: The episodes to summarize. Duplicate numbers are ignored.

    Returns:
        The summary, or ``"none"`` if ``episodes`` is empty.
    """
    if not episodes:
        return "none"
    nums = sorted({ep.number for ep in episodes})
    if len(nums) == 1:
        n = nums[0]
        return str(int(n) if n == int(n) else n)
    ranges: list[tuple[float, float]] = []
    s = p = nums[0]
    for n in nums[1:]:
        if n == p + 1:
            p = n
        else:
            ranges.append((s, p))
            s = p = n
    ranges.append((s, p))
    parts: list[str] = []
    for a, b in ranges:
        ai: int | float = int(a) if a == int(a) else a
        bi: int | float = int(b) if b == int(b) else b
        parts.append(str(ai) if a == b else f"{ai}\u2013{bi}")
    return ", ".join(parts)


def audio_badge(audio: str) -> str:
    """Returns the display badge for an audio language.

    Args:
        audio: The audio language code, ``"jpn"`` or ``"eng"``.

    Returns:
        ``"SUB"`` for Japanese audio and ``"DUB"`` for anything else.
    """
    return "SUB" if audio == "jpn" else "DUB"
