"""Persistent watchlist: add, list, show, remove, relink and check."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import sys
import time
from typing import Any

from pahebatcher import config_manager, utils
import pahebatcher.extract.scanner

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
            import json as _json

            raw = _json.loads(p.read_text(encoding="utf-8"))
            t = str(raw.get("title", "")).strip()
            if t and t != "Unknown Anime" and not _is_404_title(t):
                return t
        except Exception:
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
                except Exception:
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


class WatchlistManager:
    """Loads, saves and edits the stored list of watchlist entries."""

    @staticmethod
    def load(path: pathlib.Path | None = None) -> list[WatchlistEntry]:
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
        except Exception:
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
            except Exception:
                continue
        return entries

    @staticmethod
    def save(
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

    @staticmethod
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

    @staticmethod
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
        entries = WatchlistManager.load(path)
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
                WatchlistManager.save(entries, path)
                return False, entry
        entries.append(entry)
        WatchlistManager.save(entries, path)
        return True, entry

    @staticmethod
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
        entries = WatchlistManager.load(path)
        found = WatchlistManager.find_entry(entries, identifier)
        if not found:
            return None
        idx, ent = found
        entries.pop(idx)
        WatchlistManager.save(entries, path)
        return ent


# ── CLI helpers ──────────────────────────────────────────────────────────


def cli_list(path: pathlib.Path | None = None) -> None:
    """Prints the watchlist as a table.

    Args:
        path: The watchlist file, or None for the default.
    """
    from rich import box
    import rich.table

    from pahebatcher.ui import console

    entries = WatchlistManager.load(path)
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
    import rich.panel

    from pahebatcher.ui import console

    entries = WatchlistManager.load(path)
    if not entries:
        console.console.print("\n  [dim]No watchlist entries.[/dim]")
        return
    found = WatchlistManager.find_entry(entries, identifier)
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
    from pahebatcher.ui import console

    removed = WatchlistManager.remove_entry(identifier, path)
    if not removed:
        console.console.print(
            f"\n  [red]✗ No entry found for:[/red] {identifier}"
        )
        sys.exit(1)
    remaining = len(WatchlistManager.load(path))
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
    from pahebatcher.ui import console

    entries = WatchlistManager.load(path)
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
        WatchlistManager.save(entries, path)
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
    found = WatchlistManager.find_entry(entries, identifier)
    if not found:
        console.console.print(
            f"\n  [red]✗ No entry found for:[/red] {identifier}"
        )
        sys.exit(1)
    _idx, entry = found
    cleared = len(entry.downloaded)
    entry.downloaded = []
    WatchlistManager.save(entries, path)
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
    entries = WatchlistManager.load(path)
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
        WatchlistManager.save(entries, path)
        return True
    return False


async def run_watchlist_browser() -> tuple[str | None, WatchlistEntry | None]:
    """Runs the interactive watchlist browser for the wizard.

    Check actions run inline through the existing one-shot check path.

    Returns:
        A tuple ``(url, entry)`` to open for download, or ``(None, None)``
        to go back.
    """
    from rich import box
    import rich.panel
    import rich.prompt
    import rich.table

    from pahebatcher.ui import console

    while True:
        entries = WatchlistManager.load()
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
    from pahebatcher.ui import console

    entries = WatchlistManager.load(path)
    # No identifier → auto-relink ALL dead entries in one command
    if not identifier:
        if not entries:
            console.console.print("\n  [dim]No watchlist entries.[/dim]")
            return
        # Reuse check's dead-detection: Unknown/0 + real title/history
        # But for relink we just try auto for every entry with real title
        import asyncio

        import pahebatcher.solver

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
                    WatchlistManager.save(entries, path)
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
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                pool.submit(asyncio.run, _relink_all()).result()
        else:
            asyncio.run(_relink_all())
        return

    found = WatchlistManager.find_entry(entries, identifier)
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
        import asyncio

        import pahebatcher.solver

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
            import concurrent.futures

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
    WatchlistManager.save(entries, path)
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
    import pahebatcher.solver

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
    import asyncio

    from pahebatcher.ui import console

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
            from pahebatcher.ui import prompts

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
        import concurrent.futures

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
    from pahebatcher.ui import console

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
    existing_entries = WatchlistManager.load(path)
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
    is_new, saved = WatchlistManager.add_entry(entry, path)
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
            f" ({len(WatchlistManager.load(path))} entries)[/dim]",
        )
        console.console.print(
            "  [dim]Run 'pahebatcher watchlist check' to download new "
            "episodes.[/dim]"
        )
    else:
        console.console.print("  [dim]Entry updated.[/dim]")


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
    import logging

    from rich import box
    import rich.panel
    import rich.table

    from pahebatcher import downloader, models
    import pahebatcher.http
    import pahebatcher.solver
    from pahebatcher.ui import console

    cm = config_manager.ConfigManager()
    cm.load()

    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    console.print_banner()

    all_entries = WatchlistManager.load(path)
    if not all_entries:
        console.console.print(
            "\n  [dim]No watchlist entries. Add one with:[/dim]"
        )
        console.console.print(
            "  [brand]pahebatcher watchlist add <URL>[/brand]"
        )
        return

    if filter_id:
        found = WatchlistManager.find_entry(all_entries, filter_id)
        if not found:
            console.console.print(
                f"\n  [red]✗ No watchlist entry for:[/red] {filter_id}"
            )
            sys.exit(1)
        to_check = [found[1]]
    else:
        to_check = all_entries

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

        # Use max workers for shared client, but per-entry ctx controls batch
        # parallelism
        max_workers = max((e.hls_workers for e in to_check), default=24)
        http = pahebatcher.http.HttpClient(max_workers)
        await http.start()
        try:
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
                # Need to update last_checked after each entry
                try:
                    scanner = pahebatcher.extract.scanner.AnimePaheScanner(
                        solver, entry.host, entry.session
                    )
                    # Force fresh scan (TTL 0) to detect new episodes promptly
                    anime = await scanner.scan(
                        cache_dir,
                        prefer_audio=entry.audio_lang,
                        cache_ttl=cache_ttl_watch,
                    )
                except Exception as exc:
                    console.console.print(f"  [red]✗ Scan failed:[/red] {exc}")
                    per_entry_summary.append(
                        {
                            "title": entry.title,
                            "new": 0,
                            "done": 0,
                            "failed": 1,
                            "error": str(exc),
                        },
                    )
                    # update last_checked even on failure
                    entry.last_checked = time.time()
                    WatchlistManager.save(all_entries, path)
                    total_failed += 1
                    continue

                # Deduplicate to unique episode numbers, prefer requested audio
                # Mirrors prompts.noninteractive_episodes and downloader
                # resolver logic
                unique_by_num: dict[float, Any] = {}
                for ep in anime.episodes:
                    if ep.number not in unique_by_num:
                        unique_by_num[ep.number] = ep
                    else:
                        # Prefer audio_lang variant
                        existing = unique_by_num[ep.number]
                        if (
                            existing.audio != entry.audio_lang
                            and ep.audio == entry.audio_lang
                        ):
                            unique_by_num[ep.number] = ep
                # Dead-link auto-migrate: same title now at new UUID (e.g. site
                # re-upload) Use entry.title before overwriting, and treat 404
                # titles as dead
                anime_is_dead = (
                    anime.title == "Unknown Anime"
                    or _is_404_title(anime.title)
                    or len(unique_by_num) == 0
                )
                is_dead_candidate = (
                    anime_is_dead
                    and (
                        (
                            entry.title != entry.session
                            and entry.title != "Unknown Anime"
                            and not _is_404_title(entry.title)
                        )
                        or bool(entry.downloaded)
                    )
                )
                if is_dead_candidate:
                    # Try to recover corrupted title from cache
                    search_title = entry.title
                    if _is_404_title(search_title):
                        recovered = _recover_title_from_cache(
                            entry.session, cache_dir
                        )
                        if recovered:
                            console.console.print(
                                f"  [dim]Recovered title '{recovered}' for "
                                f"{entry.session[:8]}…[/dim]",
                            )
                            search_title = recovered
                    # Placeholder or still corrupted can't be searched
                    if (
                        search_title == entry.session
                        or search_title == "Unknown Anime"
                        or _is_404_title(search_title)
                    ):
                        console.console.print(
                            f"  [yellow]✗ Link dead for '{entry.title}' — "
                            "title unknown.[/yellow]\n"
                            "  [dim]Use: pahebatcher wl relink "
                            f"{entry.session[:8]} <new-url>[/dim]",
                        )
                        per_entry_summary.append(
                            {
                                "title": entry.title,
                                "new": 0,
                                "done": 0,
                                "failed": 1,
                                "error": "dead link",
                            },
                        )
                        entry.last_checked = time.time()
                        WatchlistManager.save(all_entries, path)
                        total_failed += 1
                        continue
                    try:
                        new_host, new_session, status = (
                            await _find_single_new_session(
                                solver, entry.host, search_title, entry.session
                            )
                        )
                        if status == "ok" and new_host and new_session:
                            old_session = entry.session
                            entry.session = new_session
                            entry.host = new_host
                            entry.url = (
                                f"https://{new_host}/anime/{new_session}"
                            )
                            WatchlistManager.save(all_entries, path)
                            console.console.print(
                                f"  [yellow]↻ Relinked '{entry.title}': "
                                f"{old_session[:8]}… → "
                                f"{new_session[:8]}…[/yellow]",
                            )
                            # Re-scan with new session
                            scanner2 = (
                                pahebatcher.extract.scanner.AnimePaheScanner(
                                    solver, entry.host, entry.session
                                )
                            )
                            anime2 = await scanner2.scan(
                                cache_dir,
                                prefer_audio=entry.audio_lang,
                                cache_ttl=cache_ttl_watch,
                            )
                            if (
                                anime2.title != "Unknown Anime"
                                and not _is_404_title(anime2.title)
                                and anime2.episodes
                            ):
                                anime = anime2
                                if (
                                    anime.title
                                    and anime.title != "Unknown Anime"
                                    and not _is_404_title(anime.title)
                                ):
                                    entry.title = anime.title
                                # Recompute unique_by_num from migrated anime
                                # (prefer requested audio)
                                unique_by_num = {}
                                for ep in anime.episodes:
                                    if ep.number not in unique_by_num:
                                        unique_by_num[ep.number] = ep
                                    else:
                                        ex = unique_by_num[ep.number]
                                        if (
                                            ex.audio != entry.audio_lang
                                            and ep.audio == entry.audio_lang
                                        ):
                                            unique_by_num[ep.number] = ep
                            else:
                                raise RuntimeError(
                                    "migrated session still dead"
                                )
                        elif status == "none":
                            console.console.print(
                                f"  [yellow]✗ Link dead for '{entry.title}' — "
                                "no session found.[/yellow]\n"
                                "  [dim]Use: pahebatcher wl relink "
                                f"{entry.session[:8]} <new-url>[/dim]",
                            )
                            per_entry_summary.append(
                                {
                                    "title": entry.title,
                                    "new": 0,
                                    "done": 0,
                                    "failed": 1,
                                    "error": "dead link",
                                },
                            )
                            entry.last_checked = time.time()
                            WatchlistManager.save(all_entries, path)
                            total_failed += 1
                            continue
                        elif status == "unknown_title":
                            console.console.print(
                                f"  [yellow]✗ Link dead for '{entry.title}' — "
                                "title unknown.[/yellow]\n"
                                "  [dim]Use: pahebatcher wl relink "
                                f"{entry.session[:8]} <new-url>[/dim]",
                            )
                            per_entry_summary.append(
                                {
                                    "title": entry.title,
                                    "new": 0,
                                    "done": 0,
                                    "failed": 1,
                                    "error": "dead link",
                                },
                            )
                            entry.last_checked = time.time()
                            WatchlistManager.save(all_entries, path)
                            total_failed += 1
                            continue
                        else:  # ambiguous
                            # Count for message (helper doesn't return count, so
                            # generic)
                            console.console.print(
                                f"  [yellow]✗ Link dead for '{entry.title}' — "
                                "multiple candidates, not "
                                "auto-migrating.[/yellow]\n"
                                "  [dim]Use: pahebatcher wl relink "
                                f"{entry.session[:8]} <new-url>[/dim]",
                            )
                            per_entry_summary.append(
                                {
                                    "title": entry.title,
                                    "new": 0,
                                    "done": 0,
                                    "failed": 1,
                                    "error": "ambiguous",
                                },
                            )
                            entry.last_checked = time.time()
                            WatchlistManager.save(all_entries, path)
                            total_failed += 1
                            continue
                    except Exception as exc:
                        # If we already handled and continued, not here;
                        # otherwise treat as dead
                        if (
                            "dead link" not in str(exc).lower()
                            and "ambiguous" not in str(exc).lower()
                        ):
                            console.console.print(
                                f"  [yellow]✗ Link dead for '{entry.title}' — "
                                f"auto-migrate failed: {exc}[/yellow]\n"
                                "  [dim]Use: pahebatcher wl relink "
                                f"{entry.session[:8]} <new-url>[/dim]",
                            )
                        per_entry_summary.append(
                            {
                                "title": entry.title,
                                "new": 0,
                                "done": 0,
                                "failed": 1,
                                "error": str(exc),
                            },
                        )
                        entry.last_checked = time.time()
                        WatchlistManager.save(all_entries, path)
                        total_failed += 1
                        continue
                # Update stored title if scan succeeded with valid (non-404)
                # title
                if (
                    anime.title
                    and anime.title != "Unknown Anime"
                    and not _is_404_title(anime.title)
                    and entry.title != anime.title
                ):
                    entry.title = anime.title
                    WatchlistManager.save(all_entries, path)
                # Actually better to use get_variant logic per number
                pending: list[Any] = []
                skipped_deleted = 0
                # For _find_existing we need ctx output_dir with sanitized title
                # Use entry.title when anime is dead to keep folder stable
                is_anime_dead = (
                    anime.title == "Unknown Anime" or _is_404_title(anime.title)
                )
                safe_title = utils.sanitize(
                    entry.title if is_anime_dead else anime.title,
                )
                full_output = os.path.join(entry.output_dir, safe_title)
                ctx_for_check = models.AppContext(
                    output_dir=full_output,
                    cache_dir=cache_dir,
                    quality=entry.quality,
                    audio_lang=entry.audio_lang,
                    max_parallel=entry.max_parallel,
                    hls_workers=entry.hls_workers,
                    keep_temp=entry.keep_temp,
                    list_only=False,
                    resolve_ahead=resolve_ahead,
                    cache_ttl=cache_ttl_watch,
                    cookie_string=cookie_string,
                    auto_retry=entry.auto_retry,
                )
                # Folder-reset: if series folder no longer exists, forget
                # deleted skip history
                downloaded_set = set(float(v) for v in (entry.downloaded or []))
                folder_exists = pathlib.Path(full_output).exists()
                if not folder_exists and downloaded_set:
                    console.console.print(
                        f"  [dim]Folder not found ({full_output}) — resetting "
                        "skip history[/dim]",
                    )
                    downloaded_set.clear()
                    entry.downloaded = []
                    WatchlistManager.save(all_entries, path)
                # Temporary orchestrator just for _find_existing helper (no
                # network)
                tmp_orch = downloader.BatchOrchestrator(
                    ctx_for_check, anime, http, solver
                )

                for num in sorted(unique_by_num.keys()):
                    # Resolve to desired audio variant if available
                    ep_variant = anime.get_variant(num, entry.audio_lang)
                    if not ep_variant:
                        # fallback to any variant for that number
                        variants = anime.get_all_variants(num)
                        if not variants:
                            continue
                        ep_variant = variants[0]
                    # Use existing file check (idempotency) + deleted skip
                    existing = None
                    try:
                        existing = tmp_orch._find_existing(ep_variant)
                    except Exception:
                        existing = None
                    if existing is not None:
                        # Backfill history: file present means it was downloaded
                        if num not in downloaded_set:
                            downloaded_set.add(num)
                        continue
                    # Not on disk — if folder exists and we have history, skip
                    # deleted
                    if folder_exists and num in downloaded_set:
                        skipped_deleted += 1
                        continue
                    pending.append(ep_variant)

                # Persist backfilled history even when up-to-date
                if (
                    downloaded_set
                    != set(float(v) for v in (entry.downloaded or []))
                ):
                    entry.downloaded = sorted(downloaded_set)
                    WatchlistManager.save(all_entries, path)

                if not pending:
                    if skipped_deleted:
                        console.console.print(
                            f"  [dim]Up to date — {len(unique_by_num)} "
                            "episodes, "
                            f"{skipped_deleted} skipped (deleted)[/dim]",
                        )
                    else:
                        console.console.print(
                            f"  [dim]Up to date — {len(unique_by_num)} "
                            "episodes, 0 new[/dim]"
                        )
                    per_entry_summary.append(
                        {"title": entry.title, "new": 0, "done": 0, "failed": 0}
                    )
                    entry.last_checked = time.time()
                    WatchlistManager.save(all_entries, path)
                    continue

                console.console.print(
                    f"  [yellow]{len(pending)} new episode(s)[/yellow]"
                    f" [dim]({len(unique_by_num)} total)[/dim] → downloading "
                    f"to [brand]{full_output}[/brand]",
                )

                # Now actually download using orchestrator (reuses resolver +
                # segment resume)
                orch = downloader.BatchOrchestrator(
                    ctx_for_check, anime, http, solver
                )
                try:
                    results = await orch.download(pending)
                    # results is dict[session, Path|None]
                    done = sum(1 for v in results.values() if v is not None)
                    failed = len(results) - done
                    total_new += len(pending)
                    total_done += done
                    total_failed += failed
                    per_entry_summary.append(
                        {
                            "title": entry.title,
                            "new": len(pending),
                            "done": done,
                            "failed": failed,
                        },
                    )
                    # Update history for successfully downloaded episodes
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
                except Exception as exc:
                    console.console.print(
                        f"  [red]✗ Download failed:[/red] {exc}"
                    )
                    if verbose:
                        raise
                    per_entry_summary.append(
                        {"title": entry.title, "new": len(pending), "done": 0,
                         "failed": len(pending), "error": str(exc)},
                    )
                    total_failed += len(pending)
                finally:
                    entry.last_checked = time.time()
                    # Persist history even if download failed partially (only
                    # successes counted)
                    if 'downloaded_set' in locals():
                        entry.downloaded = sorted(downloaded_set)
                    WatchlistManager.save(all_entries, path)
                    # Orphan cleanup per entry not needed here

            # Final summary
            console.console.print()
            console.section("Watchlist check complete")
            table = rich.table.Table(
                box=box.SIMPLE_HEAVY, show_header=True,
                header_style="brand.bold", border_style="dim",
            )
            table.add_column(
                "Series", style="bold white", ratio=1, overflow="ellipsis"
            )
            table.add_column("New", justify="center", width=6)
            table.add_column("Done", justify="center", width=6, style="green")
            table.add_column("Failed", justify="center", width=8, style="red")
            for s in per_entry_summary:
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

        finally:
            await http.close()
    finally:
        await solver.close()

    # Cleanup orphaned cache (>24h) — reuse main.py logic
    import contextlib as _ctxlib
    import shutil as _shutil

    with _ctxlib.suppress(Exception):
        now = time.time()
        for p in cache_dir.glob("*/*/*.ts"):
            if now - p.stat().st_mtime > 86400:
                with _ctxlib.suppress(Exception):
                    _shutil.rmtree(p.parent.parent)
