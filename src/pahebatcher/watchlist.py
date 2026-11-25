"""Persistent watchlist: add, list, show, remove, relink and check."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import dataclasses
import json
import logging
import os
import pathlib
import re
import shutil
import sys
import time
from typing import Any

from rich import box
import rich.panel
import rich.prompt
import rich.table

from pahebatcher import config_manager, downloader, models, utils
import pahebatcher.extract.scanner
import pahebatcher.http
import pahebatcher.solver
from pahebatcher.ui import console, prompts

DEFAULT_WATCHLIST_PATH = pathlib.Path("watchlist.json")


def get_watchlist_path(path: pathlib.Path | None = None) -> pathlib.Path:
    """Returns the watchlist file's path.

    Args:
        path: An explicit path, which takes precedence.

    Returns:
        ``path`` if given, else the path from the environment, else the
        default location.
    """
    if path is not None:
        return path
    env = os.getenv("WATCHLIST_PATH")
    if env:
        return pathlib.Path(env)
    return DEFAULT_WATCHLIST_PATH


def _normalize_title(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", t.lower())).strip()


def _is_404_title(t: str) -> bool:
    low = t.lower()
    return "404" in low or "oops" in low or "not found" in low


def _recover_title_from_cache(
    session: str,
    cache_dir: pathlib.Path = pathlib.Path("pahe_cache"),
) -> str | None:
    """Recovers a series' real title from the scan cache.

    Used for watchlist entries whose stored title is corrupted.

    Args:
        session: The series' session identifier.
        cache_dir: The cache directory to search.

    Returns:
        The title, or None if no cache entry has one.
    """
    if not cache_dir.exists():
        return None
    # Find any cache ending with _<session>/_scan_cache.json
    for p in cache_dir.glob(f"*_{session}/_scan_cache.json"):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            t = str(raw.get("title", "")).strip()
            if t and t != "Unknown Anime" and not _is_404_title(t):
                return t
        except (AttributeError, OSError, ValueError):
            continue
    # Fallback: try downloads folder name inference (sanitized title)
    # Look for output dirs that might contain the series (best effort)
    return None


async def _find_single_new_session(
    solver: Any,
    host: str,
    title: str,
    exclude_session: str,
    cache_dir: pathlib.Path = pathlib.Path("pahe_cache"),
) -> tuple[str | None, str | None, str]:
    """Searches for the one new session a series has moved to.

    A shared helper for relink and check. It reuses
    ``AnimePaheScanner.search``.

    Args:
        solver: The Cloudflare solver.
        host: The animepahe host.
        title: The series title to search for.
        exclude_session: The old session, which is ignored in the results.
        cache_dir: The cache directory, used to recover titles.

    Returns:
        A tuple ``(new_host, new_session, status)``. The status is
        ``"ok"`` (exactly one match), ``"none"``, ``"ambiguous"`` or
        ``"unknown_title"``. The first two items are None unless the status
        is ``"ok"``.
    """
    search_title = title
    if _is_404_title(search_title):
        recovered = _recover_title_from_cache(exclude_session, cache_dir)
        if recovered:
            search_title = recovered
    if (
        search_title == exclude_session
        or search_title == "Unknown Anime"
        or not search_title.strip()
        or _is_404_title(search_title)
    ):
        return None, None, "unknown_title"
    candidates = await pahebatcher.extract.scanner.AnimePaheScanner.search(
        solver, host, search_title
    )
    norm_target = _normalize_title(search_title)
    new_sessions: set[str] = set()
    for res in candidates:
        sess = str(res.get("session", ""))
        t = str(res.get("title", ""))
        if (
            sess
            and sess != exclude_session
            and _normalize_title(t) == norm_target
        ):
            new_sessions.add(sess)
    if len(new_sessions) == 1:
        new_session = next(iter(new_sessions))
        new_host = (
            pahebatcher.extract.scanner.AnimePaheScanner._current_host or host
        )
        return new_host, new_session, "ok"
    if len(new_sessions) == 0:
        return None, None, "none"
    return None, None, "ambiguous"


async def _relink_entry_via_search(
    entry: WatchlistEntry,
    solver: Any,
    cache_dir: pathlib.Path = pathlib.Path("pahe_cache"),
) -> bool:
    """Tries to relink a dead entry by searching for its title.

    Args:
        entry: The entry to repair. Updated in place on success.
        solver: The Cloudflare solver.
        cache_dir: The cache directory, used to recover titles.

    Returns:
        True if the entry was relinked.
    """

    new_host, new_session, status = await _find_single_new_session(
        solver, entry.host, entry.title, entry.session, cache_dir
    )
    if status == "unknown_title":
        return False
    if status == "ok" and new_host and new_session:
        old = entry.session[:8]
        entry.session = new_session
        entry.host = new_host
        entry.url = f"https://{new_host}/anime/{new_session}"
        if _is_404_title(entry.title):
            # Fix corrupted title to recovered search title if possible
            recovered = _recover_title_from_cache(old, cache_dir)
            if recovered and not _is_404_title(recovered):
                entry.title = recovered
        return True
    return False


@dataclasses.dataclass
class WatchlistEntry:
    """One series being watched for new episodes.

    Attributes:
        url: The series URL.
        session: The series' session identifier.
        host: The animepahe host.
        title: The series title.
        quality: The preferred vertical resolution.
        audio_lang: The preferred audio language.
        output_dir: Where downloads are written.
        max_parallel: How many episodes to download at once.
        hls_workers: How many segments to download at once per episode.
        keep_temp: Whether to keep temporary segment files.
        auto_retry: How many times a failed episode is retried.
        added_at: When the entry was added, as a timestamp.
        last_checked: When the series was last checked, as a timestamp, or
            None if never.
        downloaded: The episode numbers already downloaded.
    """

    url: str
    session: str
    host: str
    title: str
    quality: int
    audio_lang: str
    output_dir: str
    max_parallel: int
    hls_workers: int
    keep_temp: bool
    auto_retry: int
    added_at: float
    last_checked: float | None = None
    downloaded: list[float] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Converts the entry to a JSON-serializable dict."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WatchlistEntry:
        """Creates an entry from a dict made by ``to_dict``.

        Args:
            data: The dict.

        Returns:
            The entry.
        """
        # downloaded history: list of episode numbers (float)
        raw_dl = data.get("downloaded", [])
        dl_list: list[float] = []
        if isinstance(raw_dl, list):
            for v in raw_dl:
                try:
                    dl_list.append(float(v))
                except (OverflowError, TypeError, ValueError):
                    continue
        return cls(
            url=data["url"],
            session=data["session"],
            host=data["host"],
            title=data.get("title", data["session"]),
            quality=int(data.get("quality", 1080)),
            audio_lang=str(data.get("audio_lang", "jpn")),
            output_dir=str(data.get("output_dir", "./downloads")),
            max_parallel=int(data.get("max_parallel", 2)),
            hls_workers=int(data.get("hls_workers", 24)),
            keep_temp=bool(data.get("keep_temp", False)),
            auto_retry=int(data.get("auto_retry", 2)),
            added_at=float(data.get("added_at", time.time())),
            last_checked=data.get("last_checked"),
            downloaded=dl_list,
        )


def load_entries(path: pathlib.Path | None = None) -> list[WatchlistEntry]:
    """Loads the watchlist.

    Args:
        path: The watchlist file, or None for the default.

    Returns:
        The entries. The list is empty if the file is missing or unreadable.
    """
    p = get_watchlist_path(path)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    # Support both {"entries": [...]} and plain list for backward compat
    if isinstance(raw, dict) and "entries" in raw:
        raw_list = raw["entries"]
    elif isinstance(raw, list):
        raw_list = raw
    else:
        return []
    entries: list[WatchlistEntry] = []
    for item in raw_list:
        try:
            entries.append(WatchlistEntry.from_dict(item))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return entries


def save_entries(
    entries: list[WatchlistEntry],
    path: pathlib.Path | None = None,
) -> None:
    """Writes the watchlist.

    Args:
        entries: The entries to store.
        path: The watchlist file, or None for the default.
    """
    p = get_watchlist_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = [e.to_dict() for e in entries]
    # Atomic write via tmp rename
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def find_entry(
    entries: list[WatchlistEntry], identifier: str
) -> tuple[int, WatchlistEntry] | None:
    """Finds an entry by index, session, URL or title.

    Args:
        entries: The entries to search.
        identifier: What to look for.

    Returns:
        A tuple ``(index, entry)``, or None if nothing matches.
    """
    ident = identifier.strip()
    # 1. numeric index (1-based)
    if ident.isdigit():
        idx = int(ident) - 1
        if 0 <= idx < len(entries):
            return idx, entries[idx]
    # 2. try parse as URL -> match session
    try:
        _, sess = pahebatcher.extract.scanner.parse_anime_url(ident)
        for i, e in enumerate(entries):
            if e.session == sess:
                return i, e
    except Exception:
        pass
    # 3. direct session uuid match
    for i, e in enumerate(entries):
        if e.session == ident or e.url == ident:
            return i, e
    # 4. substring title/URL match (case-insensitive)
    low = ident.lower()
    for i, e in enumerate(entries):
        if (
            low in e.title.lower()
            or low in e.url.lower()
            or low in e.session.lower()
        ):
            return i, e
    return None


def add_entry(
    entry: WatchlistEntry,
    path: pathlib.Path | None = None,
) -> tuple[bool, WatchlistEntry]:
    """Adds an entry, or updates the existing one for the same series.

    Args:
        entry: The entry to store.
        path: The watchlist file, or None for the default.

    Returns:
        A tuple ``(is_new, entry)``.
    """
    entries = load_entries(path)
    for i, e in enumerate(entries):
        if e.session == entry.session:
            # Update existing — keep added_at, downloaded history, and real
            # title
            old_added = e.added_at
            old_downloaded = list(e.downloaded)
            if (
                e.title
                and e.title != e.session
                and entry.title == entry.session
            ):
                entry.title = e.title
            entry.added_at = old_added
            # Preserve downloaded history unless caller explicitly set it
            if not entry.downloaded and old_downloaded:
                entry.downloaded = old_downloaded
            entries[i] = entry
            save_entries(entries, path)
            return False, entry
    entries.append(entry)
    save_entries(entries, path)
    return True, entry


def remove_entry(
    identifier: str,
    path: pathlib.Path | None = None,
) -> WatchlistEntry | None:
    """Removes an entry.

    Args:
        identifier: The entry's index, session, URL or title.
        path: The watchlist file, or None for the default.

    Returns:
        The removed entry, or None if nothing matched.
    """
    entries = load_entries(path)
    found = find_entry(entries, identifier)
    if not found:
        return None
    idx, ent = found
    entries.pop(idx)
    save_entries(entries, path)
    return ent


# ── CLI helpers ──────────────────────────────────────────────────────────


def cli_list(path: pathlib.Path | None = None) -> None:
    """Prints the watchlist as a table.

    Args:
        path: The watchlist file, or None for the default.
    """

    entries = load_entries(path)
    if not entries:
        console.console.print("\n  [dim]No watchlist entries.[/dim]")
        console.console.print(
            "  [dim]Add one with:[/dim] "
            "[brand]pahebatcher watchlist add <URL> [-q 720] [--audio eng] "
            "[-o DIR][/brand]",
        )
        return
    t = rich.table.Table(
        box=box.ROUNDED,
        header_style="brand.bold",
        title=f"watchlist ({len(entries)} series)",
    )
    t.add_column("#", justify="right", style="dim", width=4)
    t.add_column("Title", ratio=1)
    t.add_column("Quality", justify="center", width=8)
    t.add_column("Audio", justify="center", width=6)
    t.add_column("Output", style="dim", overflow="ellipsis", max_width=24)
    t.add_column("URL", style="dim", overflow="ellipsis", max_width=40)
    for i, e in enumerate(entries, 1):
        t.add_row(
            str(i),
            e.title,
            f"{e.quality}p",
            utils.audio_badge(e.audio_lang),
            e.output_dir,
            e.url,
        )
    console.console.print(t)


def cli_show(identifier: str, path: pathlib.Path | None = None) -> None:
    """Prints the details of one watchlist entry.

    Args:
        identifier: The entry's index, session, URL or title.
        path: The watchlist file, or None for the default.
    """

    entries = load_entries(path)
    if not entries:
        console.console.print("\n  [dim]No watchlist entries.[/dim]")
        return
    found = find_entry(entries, identifier)
    if not found:
        console.console.print(
            f"\n  [red]✗ No entry found for:[/red] {identifier}"
        )
        console.console.print(
            "  [dim]Use 'pahebatcher watchlist list' to see available "
            "entries.[/dim]"
        )
        sys.exit(1)
    _, e = found
    added = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(e.added_at))
        if e.added_at
        else "—"
    )
    checked = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(e.last_checked))
        if e.last_checked
        else "never"
    )
    audio_str = "SUB" if e.audio_lang == "jpn" else "DUB"
    dl_info = (
        f"{len(e.downloaded)} eps" if e.downloaded else "none"
    )
    # Show truncated list for brevity
    dl_detail = ""
    if e.downloaded:
        nums = sorted(e.downloaded)[:12]
        dl_detail = ", ".join(str(int(n) if n == int(n) else n) for n in nums)
        if len(e.downloaded) > 12:
            dl_detail += f" … +{len(e.downloaded)-12}"
        dl_detail = f" [dim]({dl_detail})[/dim]"
    console.console.print(
        rich.panel.Panel(
            f"  [dim]Title:[/dim]       [bold white]{e.title}[/bold white]\n"
            f"  [dim]URL:[/dim]         [brand]{e.url}[/brand]\n"
            f"  [dim]Session:[/dim]     {e.session}\n"
            f"  [dim]Host:[/dim]        {e.host}\n"
            f"  [dim]Quality:[/dim]     [brand]{e.quality}p[/brand]\n"
            f"  [dim]Audio:[/dim]       {audio_str} ({e.audio_lang})\n"
            f"  [dim]Output:[/dim]      {e.output_dir}\n"
            f"  [dim]Parallel:[/dim]    {e.max_parallel}  [dim]Workers:[/dim] "
            f"{e.hls_workers}\n"
            f"  [dim]Keep-temp:[/dim] {e.keep_temp}  [dim]Retry:[/dim] "
            f"{e.auto_retry}\n"
            f"  [dim]Downloaded:[/dim]  {dl_info}{dl_detail}\n"
            f"  [dim]Added:[/dim]       {added}\n"
            f"  [dim]Last checked:[/dim] {checked}",
            title=f"[brand.bold]{e.title}[/brand.bold]",
            border_style="brand",
        )
    )


def cli_remove(identifier: str, path: pathlib.Path | None = None) -> None:
    """Removes an entry and reports the result.

    Args:
        identifier: The entry's index, session, URL or title.
        path: The watchlist file, or None for the default.
    """

    removed = remove_entry(identifier, path)
    if not removed:
        console.console.print(
            f"\n  [red]✗ No entry found for:[/red] {identifier}"
        )
        sys.exit(1)
    remaining = len(load_entries(path))
    console.console.print(
        f"  [green]✓ Removed[/green] '{removed.title}' [dim]({remaining} "
        "remaining)[/dim]"
    )


def cli_reset(
    identifier: str | None = None,
    path: pathlib.Path | None = None,
    reset_all: bool = False,
) -> None:
    """Clears the downloaded-episode history of one or all entries.

    Args:
        identifier: The entry to reset. Ignored if ``reset_all`` is set.
        path: The watchlist file, or None for the default.
        reset_all: Whether to reset every entry.
    """

    entries = load_entries(path)
    if not entries:
        console.console.print("\n  [dim]No watchlist entries.[/dim]")
        return
    wants_all = (
        reset_all
        or (
            identifier is not None
            and identifier.strip().lower() in ("all", "*")
        )
    )
    if wants_all:
        total = sum(len(e.downloaded) for e in entries)
        for e in entries:
            e.downloaded = []
        save_entries(entries, path)
        console.console.print(
            f"  [green]✓ Reset[/green] {len(entries)} "
            f"entr{'y' if len(entries) == 1 else 'ies'}"
            f" [dim](cleared {total} history)[/dim]",
        )
        return
    if not identifier:
        console.console.print(
            "\n  [red]✗ Missing identifier.[/red]\n"
            "  [dim]Use: pahebatcher wl reset <id>  or  pahebatcher wl reset "
            "--all[/dim]",
        )
        sys.exit(2)
    found = find_entry(entries, identifier)
    if not found:
        console.console.print(
            f"\n  [red]✗ No entry found for:[/red] {identifier}"
        )
        sys.exit(1)
    _idx, entry = found
    cleared = len(entry.downloaded)
    entry.downloaded = []
    save_entries(entries, path)
    console.console.print(
        f"  [green]✓ Reset[/green] '{entry.title}' [dim](cleared {cleared} "
        "history)[/dim]"
    )


def record_watchlist_download(
    session: str,
    title: str,
    ep_numbers: list[float],
    path: pathlib.Path | None = None,
) -> bool:
    """Merges episodes downloaded in the wizard into watchlist history.

    Placeholder and 404 titles are skipped so a bad scan never overwrites a
    real one.

    Args:
        session: The series' session identifier.
        title: The series title.
        ep_numbers: The episode numbers that downloaded successfully.
        path: The watchlist file, or None for the default.

    Returns:
        True if a matching entry was found and updated.
    """
    entries = load_entries(path)
    for e in entries:
        if e.session != session:
            continue
        if title and title != "Unknown Anime" and not _is_404_title(title):
            e.title = title
        seen = set(e.downloaded)
        for n in ep_numbers:
            fn = float(n)
            if fn not in seen:
                seen.add(fn)
                e.downloaded.append(fn)
        e.downloaded = sorted(e.downloaded)
        e.last_checked = time.time()
        save_entries(entries, path)
        return True
    return False


async def run_watchlist_browser() -> tuple[str | None, WatchlistEntry | None]:
    """Runs the interactive watchlist browser for the wizard.

    Check actions run inline through the existing one-shot check path.

    Returns:
        A tuple ``(url, entry)`` to open for download, or ``(None, None)``
        to go back.
    """

    while True:
        entries = load_entries()
        if not entries:
            console.console.print("\n  [dim]No watchlist entries.[/dim]")
            console.console.print(
                "  [dim]Add one with:[/dim] "
                "[brand]pahebatcher watchlist add <URL> [-q 720] [--audio "
                "eng] [-o DIR][/brand]",
            )
            rich.prompt.Prompt.ask(
                "\n  [brand]Press Enter to return[/brand]", default=""
            )
            return None, None

        console.console.print()
        console.section("Watchlist")
        t = rich.table.Table(
            box=box.ROUNDED,
            header_style="brand.bold",
            title=f"watchlist ({len(entries)} series)",
        )
        t.add_column("#", justify="right", style="dim", width=4)
        t.add_column("Title", ratio=1)
        t.add_column("Quality", justify="center", width=8)
        t.add_column("Audio", justify="center", width=6)
        for i, e in enumerate(entries, 1):
            t.add_row(
                str(i),
                e.title,
                f"{e.quality}p",
                utils.audio_badge(e.audio_lang),
            )
        console.console.print(t)
        console.console.print(
            "  [dim]Saved quality/audio per entry. Full detail:[/dim]"
            " [brand]pahebatcher wl show <#>/[/brand]",
        )
        cmd = rich.prompt.Prompt.ask(
            "  [brand][#] open  [C]heck new  [B]ack[/brand]",
            default="B",
            show_choices=False,
        ).strip().lower()
        if cmd in ("b", "back", ""):
            return None, None
        if cmd in ("c", "check"):
            await run_watchlist_check()
            continue
        if not cmd.isdigit():
            console.console.print("  [red]⚠ Invalid selection.[/red]")
            continue
        idx = int(cmd) - 1
        if not 0 <= idx < len(entries):
            console.console.print("  [red]⚠ Invalid selection.[/red]")
            continue
        entry = entries[idx]
        console.console.print(
            rich.panel.Panel(
                f"  [dim]Quality:[/dim]     [brand]{entry.quality}p[/brand]\n"
                "  [dim]Audio:[/dim]       "
                f"{'SUB' if entry.audio_lang == 'jpn' else 'DUB'}"
                f" ({entry.audio_lang})\n"
                f"  [dim]Output:[/dim]      {entry.output_dir}\n"
                f"  [dim]Downloaded:[/dim]  {len(entry.downloaded)} eps\n"
                f"  [dim]URL:[/dim]         [brand]{entry.url}[/brand]",
                title=f"[brand.bold]{entry.title}[/brand.bold]",
                border_style="brand",
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )
        sub = rich.prompt.Prompt.ask(
            "  [brand][D]ownload  [C]heck new  [B]ack[/brand]",
            default="D",
            show_choices=False,
        ).strip().lower()
        if sub in ("b", "back"):
            continue
        if sub in ("c", "check"):
            await run_watchlist_check(filter_id=entry.session)
            continue
        if sub in ("d", "download", ""):
            return entry.url, entry
        console.console.print("  [red]⚠ Invalid selection.[/red]")


def cli_relink(
    identifier: str | None = None,
    new_url: str | None = None,
    path: pathlib.Path | None = None,
) -> None:
    """Points watchlist entries at a series' new URL.

    With an identifier and URL, relinks that one entry. With an identifier
    only, searches for the new session automatically. With neither, tries
    to relink every dead entry.

    Args:
        identifier: The entry's index, session, URL or title, or None.
        new_url: The new series URL, or None to search for it.
        path: The watchlist file, or None for the default.
    """

    entries = load_entries(path)
    # No identifier → auto-relink ALL dead entries in one command
    if not identifier:
        if not entries:
            console.console.print("\n  [dim]No watchlist entries.[/dim]")
            return
        # Reuse check's dead-detection: Unknown/0 + real title/history
        # But for relink we just try auto for every entry with real title

        async def _relink_all() -> int:
            """Tries to relink every entry that looks dead.

            Returns:
                The number of entries relinked.
            """
            cm = config_manager.ConfigManager()
            cm.load()
            solver = pahebatcher.solver.Solver.from_config(cm)
            await solver.start()
            try:
                if not await solver.ping():
                    console.console.print(
                        "[red]✗ no Cloudflare solver available[/red]"
                    )
                    return 0
                relinked = 0
                for ent in entries:
                    # Reuse shared search helper — same logic as check
                    # auto-migrate
                    console.console.print(
                        f"  [dim]Checking '{ent.title}'...[/dim]"
                    )
                    new_host, new_session, status = (
                        await _find_single_new_session(
                            solver, ent.host, ent.title, ent.session
                        )
                    )
                    if status == "ok" and new_host and new_session:
                        old = ent.session[:8]
                        old_title = ent.title
                        ent.session = new_session
                        ent.host = new_host
                        ent.url = f"https://{new_host}/anime/{new_session}"
                        if _is_404_title(old_title):
                            recovered = _recover_title_from_cache(
                                old, cache_dir=pathlib.Path("pahe_cache")
                            )
                            # _find_single_new_session already recovered, but
                            # ensure title fixed
                            if recovered and not _is_404_title(recovered):
                                ent.title = recovered
                        relinked += 1
                        console.console.print(
                            f"  [green]✓ Relinked '{old_title}'[/green] "
                            f"{old}… → {new_session[:8]}…",
                        )
                    elif status in ("none", "ambiguous", "unknown_title"):
                        # Live entries yield "none" (0 new) — silently skip;
                        # dead ambiguous/none will be reported in summary as not
                        # relinked
                        continue
                if relinked:
                    save_entries(entries, path)
                    console.console.print(
                        f"\n  [green]✓ Relinked {relinked} entry(s)[/green]"
                    )
                else:
                    # Distinguish healthy vs ambiguous dead
                    has_dead = any(
                        _is_404_title(e.title) or e.title == "Unknown Anime"
                        for e in entries
                    )
                    if has_dead:
                        console.console.print(
                            "\n  [dim]No entries relinked (ambiguous or title "
                            "unknown). "
                            "Use: pahebatcher wl relink <id> <new-url>[/dim]",
                        )
                    else:
                        console.console.print(
                            f"\n  [green]✓ All {len(entries)} entries are "
                            "live — no relinking needed[/green]",
                        )
                return relinked
            finally:
                await solver.close()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as pool:
                pool.submit(asyncio.run, _relink_all()).result()
        else:
            asyncio.run(_relink_all())
        return

    found = find_entry(entries, identifier)
    if not found:
        console.console.print(
            f"\n  [red]✗ No entry found for:[/red] {identifier}"
        )
        sys.exit(1)
    idx, entry = found

    # Auto mode: no new_url supplied → search by stored title (try cache
    # recovery for 404)
    if not new_url:
        search_title = entry.title
        if _is_404_title(search_title):
            recovered = _recover_title_from_cache(entry.session)
            if recovered:
                console.console.print(
                    f"  [dim]Recovered title '{recovered}' for "
                    f"{entry.session[:8]}…[/dim]"
                )
                search_title = recovered
        if (
            search_title == entry.session
            or search_title == "Unknown Anime"
            or not search_title.strip()
            or _is_404_title(search_title)
        ):
            console.console.print(
                f"\n  [red]✗ Cannot auto-relink '{entry.title}' — title "
                "unknown.[/red]\n"
                f"  [dim]Use: pahebatcher wl relink {entry.session[:8]} "
                "<new-url>[/dim]",
            )
            sys.exit(1)
        # Reuse shared search helper (same as check auto-migrate)

        async def _auto() -> tuple[str, str] | None:
            """Searches for the entry's new session.

            Returns:
                A tuple ``(host, session)``, or None if there is no single
                match.
            """
            cm = config_manager.ConfigManager()
            cm.load()
            solver = pahebatcher.solver.Solver.from_config(cm)
            await solver.start()
            try:
                if not await solver.ping():
                    console.console.print(
                        "[red]✗ no Cloudflare solver available[/red]"
                    )
                    return None
                console.console.print(
                    f"  [dim]Searching for '{search_title}'...[/dim]"
                )
                new_host, new_session, status = await _find_single_new_session(
                    solver, entry.host, search_title, entry.session
                )
                if status == "ok" and new_host and new_session:
                    return new_host, new_session
                if status == "none":
                    console.console.print(
                        "  [yellow]✗ No new session found for "
                        f"'{search_title}'.[/yellow]\n"
                        "  [dim]Use: pahebatcher wl relink "
                        f"{entry.session[:8]} <new-url>[/dim]",
                    )
                elif status == "ambiguous":
                    # Count candidates for message (re-search to get count, or
                    # just generic)
                    console.console.print(
                        f"  [yellow]✗ Multiple candidates for '{search_title}' "
                        "— ambiguous.[/yellow]\n"
                        "  [dim]Use: pahebatcher wl relink "
                        f"{entry.session[:8]} <new-url>[/dim]",
                    )
                elif status == "unknown_title":
                    console.console.print(
                        f"\n  [red]✗ Cannot auto-relink '{entry.title}' — "
                        "title unknown.[/red]\n"
                        "  [dim]Use: pahebatcher wl relink "
                        f"{entry.session[:8]} <new-url>[/dim]",
                    )
                return None
            finally:
                await solver.close()

        # Handle being called from already-running loop (e.g. tests)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(asyncio.run, _auto()).result()
        else:
            result = asyncio.run(_auto())
        if not result:
            sys.exit(1)
        new_host, new_session = result
        console.console.print(
            f"  [dim]Auto-found: {new_session} on {new_host}[/dim]"
        )
    else:
        try:
            new_host, new_session = pahebatcher.extract.scanner.parse_anime_url(
                new_url
            )
        except ValueError as exc:
            console.console.print(f"\n  [red]✗ Invalid new URL:[/red] {exc}")
            sys.exit(1)
    if new_session == entry.session:
        console.console.print(
            "\n  [yellow]Already linked to that session:[/yellow] "
            f"{new_session}"
        )
        return
    # Check if new session already exists as separate entry
    for i, e in enumerate(entries):
        if i != idx and e.session == new_session:
            console.console.print(
                f"\n  [red]✗ New session already in watchlist as #{i+1} "
                f"'{e.title}'[/red]\n"
                f"  [dim]Remove it first or merge manually.[/dim]",
            )
            sys.exit(1)
    old_session, old_url = entry.session, entry.url
    old_title = entry.title
    # Preserve history and prefs, update session/host/url, keep title if
    # placeholder new_host already set in auto case, else from parsed URL Ensure
    # new_host is defined (for manual case, it's from parsed) For auto case,
    # new_host/new_session already set
    entry.session = new_session
    entry.host = new_host
    entry.url = f"https://{new_host}/anime/{new_session}"
    # If old title was corrupted, fix it from recovered search_title or cache
    if _is_404_title(old_title):
        fixed = None
        search_val = locals().get("search_title")
        if (
            isinstance(search_val, str)
            and search_val
            and not _is_404_title(search_val)
            and search_val != old_session
        ):
            fixed = search_val
        else:
            fixed = _recover_title_from_cache(old_session)
            if fixed and _is_404_title(fixed):
                fixed = None
        if fixed:
            entry.title = fixed
            console.console.print(f"  [dim]Fixed title '{fixed}'[/dim]")
    # If old title was placeholder, keep new title placeholder to be refreshed
    # on next check Otherwise keep old title until next successful scan
    # refreshes it
    save_entries(entries, path)
    console.console.print(
        f"  [green]✓ Relinked '{old_title}'[/green] [dim]{old_session[:8]}… → "
        f"{new_session[:8]}…[/dim]\n"
        f"    {old_url} → [brand]{entry.url}[/brand]\n"
        f"  [dim]History ({len(entry.downloaded)} eps) preserved. Run check "
        "to verify.[/dim]",
    )


def _solver_from_config() -> Any:
    """Builds a Solver from the saved config and environment.

    The caller must start and close it.

    Returns:
        The new solver.
    """

    cm = config_manager.ConfigManager()
    cm.load()
    return pahebatcher.solver.Solver.from_config(cm)


def _discover_add_url(query: str | None) -> tuple[str | None, str | None]:
    """Resolves a watchlist-add target through interactive search.

    Args:
        query: The initial search text, or None to prompt from scratch.

    Returns:
        A tuple ``(series_url, result_title)``, or ``(None, None)`` if the
        user quit or the solver is unavailable.
    """

    async def _run() -> tuple[str | None, str | None]:
        """Runs the interactive search on the event loop."""
        solver = _solver_from_config()
        await solver.start()
        try:
            if not await solver.ping():
                console.console.print(
                    "[red]✗ no Cloudflare solver available[/red]"
                )
                return None, None

            return await prompts.interactive_discovery(
                solver, "animepahe.pw", initial=query
            )
        finally:
            await solver.close()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, _run()).result()
    return asyncio.run(_run())


def cli_add(
    url: str | None,
    quality: int | None,
    audio_lang: str | None,
    output: str | None,
    parallel: int | None,
    workers: int | None,
    keep_temp: bool,
    retry: int | None,
    path: pathlib.Path | None = None,
) -> None:
    """Adds a series to the watchlist.

    Unset options fall back to the saved configuration.

    Args:
        url: The series URL, or None to search interactively.
        quality: The preferred vertical resolution, or None.
        audio_lang: The preferred audio language, or None.
        output: The output directory, or None.
        parallel: How many episodes to download at once, or None.
        workers: How many segments to download at once, or None.
        keep_temp: Whether to keep temporary segment files.
        retry: How many times to retry failed episodes, or None.
        path: The watchlist file, or None for the default.
    """

    if url is None:
        # No target given — interactive search (same UI as bare `pahebatcher`).
        url, found_title = _discover_add_url(None)
        if url is None:
            console.console.print("\n  [dim]No anime selected.[/dim]")
            return
    else:
        found_title = None

    try:
        host, session = pahebatcher.extract.scanner.parse_anime_url(url)
    except ValueError:
        # Not a URL — treat it as a search query.
        url, found_title = _discover_add_url(url)
        if url is None:
            console.console.print("\n  [dim]No anime selected.[/dim]")
            return
        try:
            host, session = pahebatcher.extract.scanner.parse_anime_url(url)
        except ValueError as exc:
            console.console.print(f"\n  [red]✗ Invalid URL:[/red] {exc}")
            sys.exit(1)

    cm = config_manager.ConfigManager()
    cm.load()
    q = quality if quality is not None else int(cm.get("quality"))
    a = audio_lang if audio_lang is not None else str(cm.get("audio_lang"))
    o = output if output is not None else str(cm.get("output_dir"))
    par = parallel if parallel is not None else int(cm.get("max_parallel"))
    w = workers if workers is not None else int(cm.get("hls_workers"))
    par = max(1, min(6, par))
    w = max(8, min(32, w))
    auto_retry = retry if retry is not None else int(cm.get("auto_retry"))
    auto_retry = max(0, min(2, auto_retry))
    # Validate
    if q not in (360, 720, 1080):
        console.console.print(f"  [red]✗ Invalid quality:[/red] {q}")
        sys.exit(1)
    if a not in ("jpn", "eng"):
        console.console.print(f"  [red]✗ Invalid audio:[/red] {a}")
        sys.exit(1)
    # Persistent default guard: /tmp is volatile (tmpfs) and will be wiped on
    # reboot
    if o == "/tmp" or o.startswith("/tmp/"):
        console.console.print(
            f"  [yellow]⚠ Output is volatile tmpfs:[/yellow] {o}\n"
            f"  [dim]Use a persistent path like ./downloads or ~/anime. "
            f"With /tmp, folder-gone reset will redownload after reboot.[/dim]",
        )

    canonical_url = f"https://{host}/anime/{session}"
    # Search-discovered titles are real — keep them. Direct URL adds have no
    # title yet, so use the session as a placeholder refreshed on first check.
    # An existing real title always wins over either (see add_entry).
    title = (
        found_title.strip() if found_title and found_title.strip() else session
    )
    existing_entries = load_entries(path)
    for e in existing_entries:
        if e.session != session:
            continue
        # Keep a usable existing title; a fresh real title beats a
        # placeholder or a corrupted 404 one.
        if e.title and e.title != e.session and not _is_404_title(e.title):
            title = e.title
        break

    entry = WatchlistEntry(
        url=canonical_url,
        session=session,
        host=host,
        title=title,
        quality=q,
        audio_lang=a,
        output_dir=o,
        max_parallel=par,
        hls_workers=w,
        keep_temp=keep_temp,
        auto_retry=auto_retry,
        added_at=time.time(),
    )
    is_new, saved = add_entry(entry, path)
    verb = "Added" if is_new else "Updated"
    audio_str = "SUB" if a == "jpn" else "DUB"
    console.console.print(
        f"  [green]✓ {verb}[/green] [bold]{saved.title}[/bold] "
        f"[dim]({saved.session})[/dim]"
    )
    console.console.print(f"    [dim]URL:[/dim]     [brand]{saved.url}[/brand]")
    console.console.print(
        f"    [dim]Quality:[/dim] {saved.quality}p  [dim]Audio:[/dim] "
        f"{audio_str}"
        f"  [dim]Output:[/dim] {saved.output_dir}",
    )
    if is_new:
        console.console.print(
            f"  [dim]Saved to {get_watchlist_path(path)}"
            f" ({len(load_entries(path))} entries)[/dim]",
        )
        console.console.print(
            "  [dim]Run 'pahebatcher watchlist check' to download new "
            "episodes.[/dim]"
        )
    else:
        console.console.print("  [dim]Entry updated.[/dim]")


@dataclasses.dataclass
class _CheckRun:
    """Settings and services shared by every entry of one check run.

    Attributes:
        solver: The Cloudflare solver.
        http: The shared HTTP client.
        all_entries: Every watchlist entry. Saved after each change.
        path: The watchlist file, or None for the default.
        cache_dir: The scan and segment cache directory.
        resolve_ahead: How many episodes to resolve ahead of downloads.
        cache_ttl: Minutes a cached scan stays valid. 0 forces a fresh scan.
        cookie_string: A user-supplied Cookie header value, or empty.
        verbose: Whether download errors should be re-raised.
    """

    solver: pahebatcher.solver.Solver
    http: pahebatcher.http.HttpClient
    all_entries: list[WatchlistEntry]
    path: pathlib.Path | None
    cache_dir: pathlib.Path
    resolve_ahead: int
    cache_ttl: int
    cookie_string: str
    verbose: bool


@dataclasses.dataclass
class _EntryResult:
    """What checking one watchlist entry produced.

    Attributes:
        summaries: Rows for the final results table.
        new: How many new episodes were queued for download.
        done: How many of them downloaded.
        failed: How many episodes or checks failed.
    """

    summaries: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    new: int = 0
    done: int = 0
    failed: int = 0


def _select_entries(
    all_entries: list[WatchlistEntry], filter_id: str | None
) -> list[WatchlistEntry]:
    """Picks the entries to check.

    Args:
        all_entries: Every watchlist entry.
        filter_id: An identifier to check only one entry, or None for all.

    Returns:
        The matching entry, or every entry if ``filter_id`` is empty.

    Raises:
        SystemExit: If ``filter_id`` matches no entry.
    """
    if not filter_id:
        return all_entries
    found = find_entry(all_entries, filter_id)
    if not found:
        console.console.print(
            f"\n  [red]✗ No watchlist entry for:[/red] {filter_id}"
        )
        sys.exit(1)
    return [found[1]]


def _mark_checked(run: _CheckRun, entry: WatchlistEntry) -> None:
    """Stamps an entry as checked now and saves the watchlist.

    Args:
        run: The shared run settings.
        entry: The entry that was checked.
    """
    entry.last_checked = time.time()
    save_entries(run.all_entries, run.path)


def _give_up(
    run: _CheckRun, entry: WatchlistEntry, res: _EntryResult, error: str
) -> None:
    """Records that checking an entry failed.

    Args:
        run: The shared run settings.
        entry: The entry that failed.
        res: The entry's result, updated in place.
        error: The reason, kept in the summary row.
    """
    res.summaries.append(
        {
            "title": entry.title,
            "new": 0,
            "done": 0,
            "failed": 1,
            "error": error,
        },
    )
    _mark_checked(run, entry)
    res.failed += 1


def _unique_episodes(
    anime: models.AnimeInfo, audio_lang: str
) -> dict[float, models.EpisodeInfo]:
    """Reduces a scan to one episode per number, preferring an audio language.

    Args:
        anime: The scanned series.
        audio_lang: The preferred audio language.

    Returns:
        The chosen episode for each episode number.
    """
    unique: dict[float, models.EpisodeInfo] = {}
    for ep in anime.episodes:
        if ep.number not in unique:
            unique[ep.number] = ep
        else:
            existing = unique[ep.number]
            if existing.audio != audio_lang and ep.audio == audio_lang:
                unique[ep.number] = ep
    return unique


def _is_dead_candidate(
    entry: WatchlistEntry, anime: models.AnimeInfo, episode_count: int
) -> bool:
    """Whether a scan looks like a dead link worth trying to relink.

    The scan must look dead (no usable title or no episodes) and the entry
    must be worth rescuing: it has a real title or a download history.

    Args:
        entry: The watchlist entry.
        anime: The scan result.
        episode_count: The number of distinct episodes found.

    Returns:
        True if a relink should be attempted.
    """
    anime_is_dead = (
        anime.title == "Unknown Anime"
        or _is_404_title(anime.title)
        or episode_count == 0
    )
    return anime_is_dead and (
        (
            entry.title != entry.session
            and entry.title != "Unknown Anime"
            and not _is_404_title(entry.title)
        )
        or bool(entry.downloaded)
    )


def _print_dead_link(entry: WatchlistEntry, reason: str) -> None:
    """Tells the user a link is dead and how to relink it.

    Args:
        entry: The dead entry.
        reason: Why it could not be relinked automatically.
    """
    console.console.print(
        f"  [yellow]✗ Link dead for '{entry.title}' — {reason}[/yellow]\n"
        "  [dim]Use: pahebatcher wl relink "
        f"{entry.session[:8]} <new-url>[/dim]",
    )


def _search_title(entry: WatchlistEntry, cache_dir: pathlib.Path) -> str:
    """Picks the title to search for a dead entry's new session.

    A corrupted 404 title is replaced by one recovered from the cache.

    Args:
        entry: The dead entry.
        cache_dir: The cache directory.

    Returns:
        The title to search for. It may still be unusable.
    """
    search_title = entry.title
    if _is_404_title(search_title):
        recovered = _recover_title_from_cache(entry.session, cache_dir)
        if recovered:
            console.console.print(
                f"  [dim]Recovered title '{recovered}' for "
                f"{entry.session[:8]}…[/dim]",
            )
            search_title = recovered
    return search_title


async def _migrate_entry(
    run: _CheckRun, entry: WatchlistEntry, new_host: str, new_session: str
) -> tuple[models.AnimeInfo, dict[float, models.EpisodeInfo]]:
    """Points an entry at its new session and rescans it.

    Args:
        run: The shared run settings.
        entry: The entry to relink. Updated and saved in place.
        new_host: The host the series moved to.
        new_session: The series' new session identifier.

    Returns:
        The rescanned series and its de-duplicated episodes.

    Raises:
        RuntimeError: If the new session is still dead.
    """
    old_session = entry.session
    entry.session = new_session
    entry.host = new_host
    entry.url = f"https://{new_host}/anime/{new_session}"
    save_entries(run.all_entries, run.path)
    console.console.print(
        f"  [yellow]↻ Relinked '{entry.title}': "
        f"{old_session[:8]}… → "
        f"{new_session[:8]}…[/yellow]",
    )
    scanner = pahebatcher.extract.scanner.AnimePaheScanner(
        run.solver, entry.host, entry.session
    )
    anime = await scanner.scan(
        run.cache_dir,
        prefer_audio=entry.audio_lang,
        cache_ttl=run.cache_ttl,
    )
    if (
        anime.title == "Unknown Anime"
        or _is_404_title(anime.title)
        or not anime.episodes
    ):
        raise RuntimeError("migrated session still dead")
    if (
        anime.title
        and anime.title != "Unknown Anime"
        and not _is_404_title(anime.title)
    ):
        entry.title = anime.title
    return anime, _unique_episodes(anime, entry.audio_lang)


async def _recover_dead_entry(
    run: _CheckRun, entry: WatchlistEntry, res: _EntryResult
) -> tuple[models.AnimeInfo, dict[float, models.EpisodeInfo]] | None:
    """Tries to relink a dead entry by searching for its title.

    Args:
        run: The shared run settings.
        entry: The dead entry.
        res: The entry's result, updated in place when giving up.

    Returns:
        The rescanned series and its episodes if the entry was relinked, or
        None if it could not be, in which case the failure is recorded.
    """
    search_title = _search_title(entry, run.cache_dir)
    if (
        search_title == entry.session
        or search_title == "Unknown Anime"
        or _is_404_title(search_title)
    ):
        _print_dead_link(entry, "title unknown.")
        _give_up(run, entry, res, "dead link")
        return None
    try:
        new_host, new_session, status = await _find_single_new_session(
            run.solver, entry.host, search_title, entry.session
        )
        if status == "ok" and new_host and new_session:
            return await _migrate_entry(run, entry, new_host, new_session)
        if status == "none":
            _print_dead_link(entry, "no session found.")
            _give_up(run, entry, res, "dead link")
        elif status == "unknown_title":
            _print_dead_link(entry, "title unknown.")
            _give_up(run, entry, res, "dead link")
        else:  # ambiguous
            _print_dead_link(
                entry, "multiple candidates, not auto-migrating."
            )
            _give_up(run, entry, res, "ambiguous")
        return None
    except Exception as exc:  # isolation point: keep going
        if (
            "dead link" not in str(exc).lower()
            and "ambiguous" not in str(exc).lower()
        ):
            _print_dead_link(entry, f"auto-migrate failed: {exc}")
        _give_up(run, entry, res, str(exc))
        return None


def _build_context(
    run: _CheckRun, entry: WatchlistEntry, anime: models.AnimeInfo
) -> tuple[models.AppContext, str]:
    """Builds the download settings for one entry.

    The series folder is named after the entry when the scan has no usable
    title, which keeps the folder stable.

    Args:
        run: The shared run settings.
        entry: The watchlist entry.
        anime: The scan result.

    Returns:
        The download settings and the series output folder.
    """
    is_anime_dead = anime.title == "Unknown Anime" or _is_404_title(anime.title)
    safe_title = utils.sanitize(
        entry.title if is_anime_dead else anime.title,
    )
    full_output = os.path.join(entry.output_dir, safe_title)
    ctx = models.AppContext(
        output_dir=full_output,
        cache_dir=run.cache_dir,
        quality=entry.quality,
        audio_lang=entry.audio_lang,
        max_parallel=entry.max_parallel,
        hls_workers=entry.hls_workers,
        keep_temp=entry.keep_temp,
        list_only=False,
        resolve_ahead=run.resolve_ahead,
        cache_ttl=run.cache_ttl,
        cookie_string=run.cookie_string,
        auto_retry=entry.auto_retry,
    )
    return ctx, full_output


def _load_history(
    run: _CheckRun, entry: WatchlistEntry, full_output: str
) -> tuple[set[float], bool]:
    """Reads an entry's download history.

    If the series folder no longer exists, the history is forgotten so that
    deleted episodes are downloaded again.

    Args:
        run: The shared run settings.
        entry: The watchlist entry. Its history may be reset and saved.
        full_output: The series output folder.

    Returns:
        The downloaded episode numbers and whether the folder exists.
    """
    downloaded_set = set(float(v) for v in (entry.downloaded or []))
    folder_exists = pathlib.Path(full_output).exists()
    if not folder_exists and downloaded_set:
        console.console.print(
            f"  [dim]Folder not found ({full_output}) — resetting "
            "skip history[/dim]",
        )
        downloaded_set.clear()
        entry.downloaded = []
        save_entries(run.all_entries, run.path)
    return downloaded_set, folder_exists


def _find_pending(
    anime: models.AnimeInfo,
    unique_by_num: dict[float, models.EpisodeInfo],
    audio_lang: str,
    orch: downloader.BatchOrchestrator,
    folder_exists: bool,
    downloaded_set: set[float],
) -> tuple[list[models.EpisodeInfo], int]:
    """Works out which episodes still need downloading.

    Episodes already on disk are added to the history. Episodes in the
    history but missing from an existing folder were deleted on purpose and
    are skipped.

    Args:
        anime: The scanned series.
        unique_by_num: One episode per number.
        audio_lang: The preferred audio language.
        orch: An orchestrator, used only to look for existing files.
        folder_exists: Whether the series folder exists.
        downloaded_set: The downloaded episode numbers. Updated in place.

    Returns:
        The episodes to download and how many were skipped as deleted.
    """
    pending: list[models.EpisodeInfo] = []
    skipped_deleted = 0
    for num in sorted(unique_by_num.keys()):
        ep_variant = anime.get_variant(num, audio_lang)
        if not ep_variant:
            # Fall back to any variant of that number
            variants = anime.get_all_variants(num)
            if not variants:
                continue
            ep_variant = variants[0]
        existing = None
        try:
            existing = orch._find_existing(ep_variant)
        except Exception:  # isolation point: keep going
            existing = None
        if existing is not None:
            # A file on disk means it was downloaded
            if num not in downloaded_set:
                downloaded_set.add(num)
            continue
        if folder_exists and num in downloaded_set:
            skipped_deleted += 1
            continue
        pending.append(ep_variant)
    return pending, skipped_deleted


def _print_up_to_date(total: int, skipped_deleted: int) -> None:
    """Prints that an entry has nothing new.

    Args:
        total: The number of distinct episodes.
        skipped_deleted: How many were skipped because they were deleted.
    """
    if skipped_deleted:
        console.console.print(
            f"  [dim]Up to date — {total} "
            "episodes, "
            f"{skipped_deleted} skipped (deleted)[/dim]",
        )
    else:
        console.console.print(
            f"  [dim]Up to date — {total} episodes, 0 new[/dim]"
        )


async def _download_pending(
    run: _CheckRun,
    entry: WatchlistEntry,
    orch: downloader.BatchOrchestrator,
    pending: list[models.EpisodeInfo],
    downloaded_set: set[float],
    res: _EntryResult,
) -> None:
    """Downloads an entry's new episodes and records the outcome.

    The entry's history and check time are saved even if the download fails.

    Args:
        run: The shared run settings.
        entry: The watchlist entry.
        orch: The orchestrator that downloads the episodes.
        pending: The episodes to download.
        downloaded_set: The downloaded episode numbers. Updated in place.
        res: The entry's result, updated in place.

    Raises:
        Exception: Whatever the download raised, but only in verbose mode.
    """
    try:
        results = await orch.download(pending)
        # results is dict[session, Path|None]
        done = sum(1 for v in results.values() if v is not None)
        failed = len(results) - done
        res.new += len(pending)
        res.done += done
        res.failed += failed
        res.summaries.append(
            {
                "title": entry.title,
                "new": len(pending),
                "done": done,
                "failed": failed,
            },
        )
        for ep in pending:
            if results.get(ep.session) is not None:
                downloaded_set.add(float(ep.number))
        entry.downloaded = sorted(downloaded_set)
        if failed:
            console.console.print(
                f"  [yellow]⚠ {failed} failed, {done} done[/yellow]"
            )
        else:
            console.console.print(
                f"  [green]✓ {done} episode(s) completed[/green]"
            )
    except Exception as exc:  # isolation point: keep going
        console.console.print(f"  [red]✗ Download failed:[/red] {exc}")
        if run.verbose:
            raise
        res.summaries.append(
            {
                "title": entry.title,
                "new": len(pending),
                "done": 0,
                "failed": len(pending),
                "error": str(exc),
            },
        )
        res.failed += len(pending)
    finally:
        entry.last_checked = time.time()
        # Persist history even if the download failed partially
        entry.downloaded = sorted(downloaded_set)
        save_entries(run.all_entries, run.path)


async def _check_entry(run: _CheckRun, entry: WatchlistEntry) -> _EntryResult:
    """Checks one watched series and downloads its new episodes.

    Args:
        run: The shared run settings.
        entry: The watchlist entry to check.

    Returns:
        The outcome for the entry.
    """
    res = _EntryResult()
    try:
        scanner = pahebatcher.extract.scanner.AnimePaheScanner(
            run.solver, entry.host, entry.session
        )
        # Force a fresh scan (TTL 0) to detect new episodes promptly
        anime = await scanner.scan(
            run.cache_dir,
            prefer_audio=entry.audio_lang,
            cache_ttl=run.cache_ttl,
        )
    except Exception as exc:  # isolation point: keep going
        console.console.print(f"  [red]✗ Scan failed:[/red] {exc}")
        _give_up(run, entry, res, str(exc))
        return res

    unique_by_num = _unique_episodes(anime, entry.audio_lang)
    if _is_dead_candidate(entry, anime, len(unique_by_num)):
        recovered = await _recover_dead_entry(run, entry, res)
        if recovered is None:
            return res
        anime, unique_by_num = recovered
    # Keep the stored title in step with a valid (non-404) scanned title
    if (
        anime.title
        and anime.title != "Unknown Anime"
        and not _is_404_title(anime.title)
        and entry.title != anime.title
    ):
        entry.title = anime.title
        save_entries(run.all_entries, run.path)

    ctx, full_output = _build_context(run, entry, anime)
    downloaded_set, folder_exists = _load_history(run, entry, full_output)
    # A temporary orchestrator, only for its _find_existing helper (no network)
    tmp_orch = downloader.BatchOrchestrator(ctx, anime, run.http, run.solver)
    pending, skipped_deleted = _find_pending(
        anime,
        unique_by_num,
        entry.audio_lang,
        tmp_orch,
        folder_exists,
        downloaded_set,
    )
    # Persist backfilled history even when up to date
    if downloaded_set != set(float(v) for v in (entry.downloaded or [])):
        entry.downloaded = sorted(downloaded_set)
        save_entries(run.all_entries, run.path)

    if not pending:
        _print_up_to_date(len(unique_by_num), skipped_deleted)
        res.summaries.append(
            {"title": entry.title, "new": 0, "done": 0, "failed": 0}
        )
        _mark_checked(run, entry)
        return res

    console.console.print(
        f"  [yellow]{len(pending)} new episode(s)[/yellow]"
        f" [dim]({len(unique_by_num)} total)[/dim] → downloading "
        f"to [brand]{full_output}[/brand]",
    )
    # Download with an orchestrator (reuses the resolver and segment resume)
    orch = downloader.BatchOrchestrator(ctx, anime, run.http, run.solver)
    await _download_pending(run, entry, orch, pending, downloaded_set, res)
    return res


def _print_check_summary(
    rows: list[dict[str, Any]],
    total_new: int,
    total_done: int,
    total_failed: int,
) -> None:
    """Prints the results table and status panel of a check run.

    Args:
        rows: One summary row per entry.
        total_new: How many new episodes were found.
        total_done: How many were downloaded.
        total_failed: How many failed.
    """
    console.console.print()
    console.section("Watchlist check complete")
    table = rich.table.Table(
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="brand.bold",
        border_style="dim",
    )
    table.add_column("Series", style="bold white", ratio=1, overflow="ellipsis")
    table.add_column("New", justify="center", width=6)
    table.add_column("Done", justify="center", width=6, style="green")
    table.add_column("Failed", justify="center", width=8, style="red")
    for s in rows:
        table.add_row(
            s["title"], str(s["new"]), str(s["done"]), str(s["failed"])
        )
    console.console.print(table)
    status = f"[green]✓ {total_done} new episodes downloaded[/green]"
    if total_failed:
        status += f"  [red]✗ {total_failed} failed[/red]"
    if total_new == 0:
        status = "[dim]No new episodes[/dim]"
    console.console.print(
        rich.panel.Panel(
            status,
            border_style="green" if not total_failed else "yellow",
            box=box.ROUNDED,
        )
    )
    if total_failed:
        console.console.print(
            "  [dim]Tip: re-run 'pahebatcher watchlist check' to "
            "retry failed.[/dim]",
        )


def _cleanup_orphan_cache(cache_dir: pathlib.Path) -> None:
    """Deletes cached download folders that are over 24 hours old.

    Args:
        cache_dir: The cache directory.
    """
    with contextlib.suppress(Exception):
        now = time.time()
        for p in cache_dir.glob("*/*/*.ts"):
            if now - p.stat().st_mtime > 86400:
                with contextlib.suppress(Exception):
                    shutil.rmtree(p.parent.parent)


async def run_watchlist_check(
    path: pathlib.Path | None = None,
    verbose: bool = False,
    filter_id: str | None = None,
) -> None:
    """Checks every watched series for new episodes and downloads them.

    Args:
        path: The watchlist file, or None for the default.
        verbose: Whether to enable debug logging.
        filter_id: Only check the entry matching this identifier, or None
            to check all.

    Raises:
        RuntimeError: If the solver cannot be started.
    """

    cm = config_manager.ConfigManager()
    cm.load()

    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    console.print_banner()

    all_entries = load_entries(path)
    if not all_entries:
        console.console.print(
            "\n  [dim]No watchlist entries. Add one with:[/dim]"
        )
        console.console.print(
            "  [brand]pahebatcher watchlist add <URL>[/brand]"
        )
        return

    to_check = _select_entries(all_entries, filter_id)

    console.section(f"Watchlist check — {len(to_check)} series")

    cache_dir = pathlib.Path("pahe_cache")
    resolve_ahead = int(cm.get("resolve_ahead"))
    cache_ttl_watch = 0  # force fresh for watchlist check
    cookie_string = str(cm.get("cookie_string"))

    console.section("Checking Prerequisites")
    console.console.print("  [muted]Cloudflare solver[/muted]  ", end="")

    solver = pahebatcher.solver.Solver.from_config(cm)
    await solver.start()
    try:
        if not await solver.ping():
            console.console.print("[red]✗ none available[/red]")
            console.console.print(
                "\n  [red]No Cloudflare solver is available[/red]\n"
                f"  [dim]{pahebatcher.solver.no_solver_hint()}[/dim]"
            )
            sys.exit(1)
        console.console.print(f"[ok]✓ {solver.backend_name}[/ok]")

        # Use max workers for the shared client, but per-entry ctx controls
        # batch parallelism
        max_workers = max((e.hls_workers for e in to_check), default=24)
        http = pahebatcher.http.HttpClient(max_workers)
        await http.start()
        try:
            run = _CheckRun(
                solver=solver,
                http=http,
                all_entries=all_entries,
                path=path,
                cache_dir=cache_dir,
                resolve_ahead=resolve_ahead,
                cache_ttl=cache_ttl_watch,
                cookie_string=cookie_string,
                verbose=verbose,
            )
            total_new = 0
            total_done = 0
            total_failed = 0
            per_entry_summary: list[dict[str, Any]] = []

            for idx, entry in enumerate(to_check, 1):
                console.console.print(
                    f"\n  [brand][{idx}/{len(to_check)}][/brand] "
                    f"[bold]{entry.title}[/bold]"
                    f" [dim]{entry.url}[/dim]",
                )
                res = await _check_entry(run, entry)
                per_entry_summary.extend(res.summaries)
                total_new += res.new
                total_done += res.done
                total_failed += res.failed

            _print_check_summary(
                per_entry_summary, total_new, total_done, total_failed
            )

        finally:
            await http.close()
    finally:
        await solver.close()

    # Clean up orphaned cache folders (over 24h old)
    _cleanup_orphan_cache(cache_dir)
