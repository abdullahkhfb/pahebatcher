"""Main entry point: CLI arguments, service wiring and dispatch."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import logging
import os
import pathlib
import shutil
import sys
import time

from rich import box
import rich.panel
import rich.prompt
import rich.table

from pahebatcher import (
    config,
    config_manager,
    downloader,
    models,
    stream,
    utils,
    watchlist,
)
import pahebatcher.extract.scanner
import pahebatcher.http
import pahebatcher.sessions
import pahebatcher.solver
from pahebatcher.ui import console, prompts

log = logging.getLogger(__name__)


_MAIN_EPILOG = (
    "Examples:\n"
    "  %(prog)s https://animepahe.pw/anime/<uuid> "
    "                       # wizard\n"
    "  %(prog)s https://animepahe.pw/anime/<uuid> --all "
    "                 # all eps\n"
    "  %(prog)s https://animepahe.pw/anime/<uuid> --range 1-12 "
    "          # season 1\n"
    "  %(prog)s https://animepahe.pw/anime/<uuid> --latest 3 "
    "            # last 3\n"
    "  %(prog)s https://animepahe.pw/anime/<uuid> --list "
    "                # list only\n"
    "  %(prog)s https://animepahe.pw/anime/<uuid> --audio eng --all "
    "     # dubbed\n"
    "  %(prog)s https://animepahe.pw/anime/<uuid> -s "
    "                    # stream\n"
    "\n"
    "Configuration:\n"
    "  %(prog)s config show "
    "                                              # view settings\n"
    "  %(prog)s config set quality 720 "
    "                                   # save default quality\n"
    "  %(prog)s config reset "
    "                                             # reset to "
    "defaults\n"
    "\n"
    "Watchlist (auto-download ongoing series) — shorthand: wl, w, "
    "check:\n"
    "  %(prog)s watchlist add <URL> [-q 720] [--audio eng] [-o DIR] "
    "     # add\n"
    "  %(prog)s wl add <URL>                  # same as watchlist\n"
    "  %(prog)s check                         # same as watchlist "
    "check\n"
    "  %(prog)s wl list / wl show 1 / wl remove 1 / wl reset 1\n"
)


def _add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    """Adds the series URL, the episode-selection group and ``--list``.

    Args:
        parser: The parser to add the arguments to.
    """
    parser.add_argument(
        "url", metavar="URL", nargs="?", help="animepahe series URL"
    )
    sel = parser.add_mutually_exclusive_group()
    sel.add_argument(
        "--all", "-a", action="store_true", help="Download every episode"
    )
    sel.add_argument(
        "--range",
        "-r",
        metavar="RANGE",
        help='Episode range, e.g. "1-12" "1,4,7" "13-"',
    )
    sel.add_argument(
        "--latest",
        "-n",
        metavar="N",
        type=int,
        help="Download the latest N episodes",
    )
    sel.add_argument(
        "--stream", "-s", action="store_true", help="Stream episodes via MPV"
    )
    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        dest="list_only",
        help="List episodes and exit",
    )


def _add_download_arguments(parser: argparse.ArgumentParser) -> None:
    """Adds the output, quality, audio and performance options.

    Args:
        parser: The parser to add the arguments to.
    """
    parser.add_argument(
        "-o", "--output", default="./downloads", help="Output directory"
    )
    parser.add_argument(
        "-q", "--quality", metavar="Q", type=int, choices=[360, 720, 1080],
        default=None, help="Quality: 360, 720, or 1080",
    )
    parser.add_argument(
        "--audio", metavar="LANG", type=str, choices=["jpn", "eng"],
        default=None, dest="audio_lang", help="Audio: jpn=subbed, eng=dubbed",
    )
    parser.add_argument(
        "-j", "--parallel", metavar="N", type=int, default=None,
        help="Concurrent downloads (1-6)",
    )
    parser.add_argument(
        "-w", "--workers", metavar="N", type=int, default=None,
        help="HLS segment workers per episode (8-32)",
    )
    parser.add_argument(
        "--keep-temp", action="store_true", help="Keep raw segment files"
    )
    parser.add_argument(
        "--retry", type=int, choices=[0, 1, 2], default=None,
        help="Auto-retry failed episodes (0-2, default 2)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )


def build_parser() -> argparse.ArgumentParser:
    """Builds the main command-line parser.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="pahebatcher",
        description=f"pahe-batcher v{config.VERSION} \u2014 animepahe Batch "
        "Downloader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_MAIN_EPILOG,
    )

    _add_selection_arguments(parser)
    _add_download_arguments(parser)
    return parser


def build_config_parser() -> argparse.ArgumentParser:
    """Builds the parser for the ``config`` subcommand.

    Returns:
        The parser.
    """
    cfg = argparse.ArgumentParser(prog="pahebatcher config", add_help=True)
    cfg_sub = cfg.add_subparsers(dest="config_action")
    cfg_sub.add_parser("show", help="Show current configuration")
    set_p = cfg_sub.add_parser("set", help="Set a configuration value")
    set_p.add_argument(
        "key",
        choices=[
            "quality",
            "audio_lang",
            "max_parallel",
            "hls_workers",
            "output_dir",
            "keep_temp",
            "resolve_ahead",
            "cache_ttl",
            "cookie_string",
            "auto_retry",
            "clearance_ttl",
            "browser_mode",
        ],
    )
    set_p.add_argument("value")
    cfg_sub.add_parser("reset", help="Reset all settings to defaults")
    return cfg


_WATCHLIST_EPILOG = (
    "Examples:\n"
    "  pahebatcher watchlist add https://animepahe.pw/anime/<uuid> -q "
    "1080 --audio jpn -o ~/anime\n"
    "  pahebatcher wl add https://... -q 720  # wl/w = shorthand\n"
    "  pahebatcher watchlist list  |  pahebatcher wl list  |  "
    "pahebatcher wl ls\n"
    "  pahebatcher watchlist check |  pahebatcher check   |  "
    "pahebatcher wl c\n"
    "  pahebatcher watchlist remove https://animepahe.pw/anime/<uuid>\n"
    "  pahebatcher wl relink 1 https://animepahe.pw/anime/<new-uuid>  "
    "# fix dead link\n"
    "\n"
    "Shorthand: wl, w, watch = watchlist; check/sync = watchlist "
    "check\n"
    "Sub-aliases: a=add, ls/l=list, s=show, rm/r/del=remove,\n"
    "             rst=reset, c/sync=check, link/migrate=relink\n"
    "\n"
    "Cron example (run hourly):\n"
    "  0 * * * * cd /path/to/pahebatcher && venv/bin/python -m "
    "pahebatcher \\\n"
    "    watchlist check >> watchlist.log 2>&1\n"
    "  # or shorter:\n"
    "  0 * * * * cd /path/to/pahebatcher && make watchlist-check >> "
    "watchlist.log 2>&1\n"
)


def _add_add_command(
    sub: argparse._SubParsersAction[argparse.ArgumentParser]
) -> None:
    """Adds the ``add`` subcommand.

    Args:
        sub: The subcommand group to add it to.
    """
    p_add = sub.add_parser("add", aliases=["a"], help="Add anime to watchlist")
    p_add.add_argument(
        "url",
        nargs="?",
        default=None,
        help="animepahe series URL, search query, or omit to search "
        "interactively",
    )
    p_add.add_argument(
        "-q", "--quality", type=int, choices=[360, 720, 1080],
        default=None, help="Quality: 360, 720, or 1080",
    )
    p_add.add_argument(
        "--audio", dest="audio_lang", type=str, choices=["jpn", "eng"],
        default=None, help="Audio: jpn=subbed, eng=dubbed",
    )
    p_add.add_argument("-o", "--output", default=None, help="Output directory")
    p_add.add_argument(
        "-j",
        "--parallel",
        type=int,
        default=None,
        help="Concurrent downloads (1-6)",
    )
    p_add.add_argument(
        "-w", "--workers", type=int, default=None,
        help="HLS segment workers per episode (8-32)",
    )
    p_add.add_argument(
        "--keep-temp", action="store_true", help="Keep raw segment files"
    )
    p_add.add_argument(
        "--retry", type=int, choices=[0, 1, 2], default=None,
        help="Auto-retry failed episodes (0-2)",
    )
    p_add.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )


def _add_entry_commands(
    sub: argparse._SubParsersAction[argparse.ArgumentParser]
) -> None:
    """Adds the ``list``, ``show``, ``remove`` and ``reset`` subcommands.

    Args:
        sub: The subcommand group to add them to.
    """
    sub.add_parser("list", aliases=["ls", "l"], help="List watchlist entries")

    p_show = sub.add_parser(
        "show", aliases=["s", "info"], help="Show watchlist entry details"
    )
    p_show.add_argument(
        "identifier",
        help="URL, session UUID, title substring, or 1-based index",
    )

    p_remove = sub.add_parser(
        "remove",
        aliases=["rm", "r", "del", "delete"],
        help="Remove entry from watchlist",
    )
    p_remove.add_argument(
        "identifier",
        help="URL, session UUID, title substring, or 1-based index",
    )
    p_remove.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation"
    )

    p_reset = sub.add_parser(
        "reset",
        aliases=["rst", "clear"],
        help="Clear skip history for an entry (re-download deleted) — --all "
        "for all entries",
    )
    p_reset.add_argument(
        "identifier", nargs="?", default=None,
        help="URL, session UUID, title substring, 1-based index, or 'all'",
    )
    p_reset.add_argument(
        "--all", action="store_true", help="Reset skip history for all entries"
    )


def _add_sync_commands(
    sub: argparse._SubParsersAction[argparse.ArgumentParser]
) -> None:
    """Adds the ``relink`` and ``check`` subcommands.

    Args:
        sub: The subcommand group to add them to.
    """
    p_relink = sub.add_parser(
        "relink",
        aliases=["link", "migrate", "update-url"],
        help="Relink dead entry to new URL (preserves history) — no args = "
        "relink all",
    )
    p_relink.add_argument(
        "identifier", nargs="?", default=None,
        help="URL, session, title or # (omit to relink all dead entries)",
    )
    p_relink.add_argument(
        "new_url", nargs="?", default=None,
        help="New animepahe series URL (omit to auto-find by title)",
    )

    p_check = sub.add_parser(
        "check", aliases=["c", "sync", "update"],
        help="Check all watched anime for new episodes and download",
    )
    p_check.add_argument(
        "identifier",
        nargs="?",
        default=None,
        help="Optional: check only this URL/#",
    )
    p_check.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )


def build_watchlist_parser() -> argparse.ArgumentParser:
    """Builds the parser for the ``watchlist`` subcommand.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="pahebatcher watchlist",
        description="Manage watchlist for auto-downloading ongoing anime",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_WATCHLIST_EPILOG,
    )
    sub = parser.add_subparsers(dest="watchlist_action", required=True)

    _add_add_command(sub)
    _add_entry_commands(sub)
    _add_sync_commands(sub)

    return parser


@dataclasses.dataclass
class _Settings:
    """Download settings resolved from the command line and saved config.

    Attributes:
        quality: The preferred vertical resolution.
        audio_lang: The preferred audio language, ``"jpn"`` or ``"eng"``.
        parallel: How many episodes to download at once.
        workers: How many segments to download at once per episode.
        resolve_ahead: How many episodes to resolve ahead of the downloads.
        cache_ttl: Minutes a cached series scan stays valid.
        cookie_string: A user-supplied Cookie header value, or empty.
        auto_retry: How many times a failed episode is retried.
        cache_dir: The scan and segment cache directory.
    """

    quality: int
    audio_lang: str
    parallel: int
    workers: int
    resolve_ahead: int
    cache_ttl: int
    cookie_string: str
    auto_retry: int
    cache_dir: pathlib.Path


def _resolve_settings(
    args: argparse.Namespace, cm: config_manager.ConfigManager
) -> _Settings:
    """Combines command-line options with the saved configuration.

    Command-line options win. Concurrency and retry values are clamped to
    their supported ranges.

    Args:
        args: The parsed command-line arguments.
        cm: The loaded configuration manager.

    Returns:
        The resolved settings.
    """
    quality = (
        args.quality if args.quality is not None else int(cm.get("quality"))
    )
    audio_lang = (
        args.audio_lang
        if args.audio_lang is not None
        else str(cm.get("audio_lang"))
    )
    parallel = (
        args.parallel
        if args.parallel is not None
        else int(cm.get("max_parallel"))
    )
    workers = (
        args.workers if args.workers is not None else int(cm.get("hls_workers"))
    )
    parallel = max(1, min(6, parallel))
    workers = max(8, min(32, workers))
    resolve_ahead = int(cm.get("resolve_ahead"))
    cache_ttl = int(cm.get("cache_ttl"))
    cookie_string = str(cm.get("cookie_string"))
    auto_retry = (
        args.retry if args.retry is not None else int(cm.get("auto_retry"))
    )
    auto_retry = max(0, min(2, auto_retry))
    return _Settings(
        quality=quality,
        audio_lang=audio_lang,
        parallel=parallel,
        workers=workers,
        resolve_ahead=resolve_ahead,
        cache_ttl=cache_ttl,
        cookie_string=cookie_string,
        auto_retry=auto_retry,
        cache_dir=pathlib.Path("pahe_cache"),
    )


def _apply_watchlist_prefs(
    args: argparse.Namespace,
    wl_entry: watchlist.WatchlistEntry,
    settings: _Settings,
) -> None:
    """Applies a watchlist entry's preferences unless a flag was explicit.

    Args:
        args: The parsed command-line arguments.
        wl_entry: The watchlist entry the user jumped in from.
        settings: The settings to update in place.
    """
    if args.quality is None:
        settings.quality = wl_entry.quality
    if args.audio_lang is None:
        settings.audio_lang = wl_entry.audio_lang
    if args.parallel is None:
        settings.parallel = max(1, min(6, wl_entry.max_parallel))
    if args.workers is None:
        settings.workers = max(8, min(32, wl_entry.hls_workers))


async def _discover_url(
    args: argparse.Namespace, solver: pahebatcher.solver.Solver
) -> str | None:
    """Gets the series URL from the arguments or an interactive search.

    Args:
        args: The parsed command-line arguments.
        solver: The Cloudflare solver, used for the search.

    Returns:
        The series URL, or None if the user selected nothing.
    """
    url = args.url
    if not url:
        url, _ = await prompts.interactive_discovery(solver, "animepahe.pw")
        if not url:
            console.console.print(
                "\n  [yellow]No anime selected. Exiting.[/yellow]"
            )
            return None
    return str(url)


def _print_scan_banner(anime: models.AnimeInfo) -> None:
    """Prints the series title, episode count and episode ranges.

    Args:
        anime: The scanned series.
    """
    badge = (
        " [bold yellow][PARTIAL DOWNLOAD FOUND][/bold yellow]"
        if anime.has_session
        else ""
    )
    console.console.print(
        f"  [green]\u2713[/green] [bold]{anime.title}[/bold]{badge}\n"
        f"  \u2014 [brand]{len(anime.episodes)}[/brand] episodes  "
        f"({utils.compact_ep_range(anime.episodes)})"
    )


def _print_episode_table(anime: models.AnimeInfo) -> None:
    """Prints a table of the series' episodes.

    Args:
        anime: The scanned series.
    """
    t = rich.table.Table(
        box=box.SIMPLE, show_header=True, header_style="brand.bold"
    )
    t.add_column("Ep", width=6, justify="right")
    t.add_column("Title", style="white")
    t.add_column("Audio", width=5)
    for ep in anime.episodes:
        t.add_row(
            ep.ep_str,
            ep.title or "\u2014",
            utils.audio_badge(ep.audio),
        )
    console.console.print(t)


def _prompt_action(anime: models.AnimeInfo, cache_dir: pathlib.Path) -> str:
    """Shows the action menu and asks which action to take.

    Args:
        anime: The scanned series.
        cache_dir: The cache directory, whose size is shown as a hint.

    Returns:
        The chosen key: ``"1"`` to ``"5"``, or ``"w"`` / ``"W"``.
    """
    console.console.print()
    console.section("Action")
    sessions = pahebatcher.sessions.get_sessions(cache_dir)
    total_cache = sum(s["size"] for s in sessions)
    cache_hint = (
        f" [dim]({utils.fmt_bytes(total_cache)})[/dim]"
        if total_cache > 0
        else ""
    )
    wl_count = 0
    with contextlib.suppress(Exception):
        wl_count = len(watchlist.load_entries())
    wl_hint = f" [dim]({wl_count} tracked)[/dim]" if wl_count > 0 else ""

    console.console.print(
        rich.panel.Panel(
            "  [bold white]1[/bold white]  "
            "[brand]Download[/brand]"
            "  [dim]\u00b7 save .mp4 files[/dim]\n"
            "  [bold white]2[/bold white]  "
            "[brand]Stream[/brand]"
            "    [dim]\u00b7 play in MPV[/dim]\n"
            "  [bold white]3[/bold white]  [brand]Sessions & "
            f"Cache[/brand]{cache_hint}\n"
            "  [bold white]4[/bold white]  [brand]List[/brand]"
            "      [dim]\u00b7 show episode table[/dim]\n"
            "  [bold white]W[/bold white]  "
            f"[brand]Watchlist[/brand]{wl_hint}\n"
            "  [bold white]5[/bold white]  [red]Exit[/red]",
            title=f"[brand.bold]{anime.title}[/brand.bold]",
            border_style="brand",
            box=box.ROUNDED,
            padding=(0, 2),
        )
    )
    return rich.prompt.Prompt.ask(
        "  [brand]Select action[/brand]",
        choices=["1", "2", "3", "4", "5", "W", "w"],
        default="1",
        show_choices=False,
    )


def _choose_episodes(
    args: argparse.Namespace, anime: models.AnimeInfo, scripted: bool
) -> list[models.EpisodeInfo]:
    """Picks the episodes from the arguments or an interactive prompt.

    Args:
        args: The parsed command-line arguments.
        anime: The scanned series.
        scripted: Whether the arguments already say what to do.

    Returns:
        The chosen episodes. The list is empty if nothing matched.

    Raises:
        SystemExit: If ``--range`` matched nothing in scripted mode.
    """
    if args.all:
        return prompts.noninteractive_episodes(anime, "all")
    if args.range:
        chosen = prompts.noninteractive_episodes(
            anime, "range", range_str=args.range
        )
        if not chosen:
            console.console.print(
                f"  [red]\u2717 No episodes matched:[/red] {args.range}"
            )
            console.console.print(
                "    Available: "
                "[brand]"
                f"{utils.compact_ep_range(anime.episodes)}"
                "[/brand]"
            )
            if scripted:
                sys.exit(1)
        return chosen
    if args.latest:
        return prompts.noninteractive_episodes(
            anime, "latest", latest_n=args.latest
        )
    return prompts.select_episodes(anime)


def _make_context(
    settings: _Settings, output_dir: str, keep_temp: bool, auto_retry: int
) -> models.AppContext:
    """Builds the download settings for one run.

    Args:
        settings: The resolved settings.
        output_dir: The series output folder.
        keep_temp: Whether to keep temporary segment files.
        auto_retry: How many times a failed episode is retried.

    Returns:
        The download settings.
    """
    return models.AppContext(
        output_dir=output_dir,
        cache_dir=settings.cache_dir,
        quality=settings.quality,
        audio_lang=settings.audio_lang,
        max_parallel=settings.parallel,
        hls_workers=settings.workers,
        keep_temp=keep_temp,
        list_only=False,
        resolve_ahead=settings.resolve_ahead,
        cache_ttl=settings.cache_ttl,
        cookie_string=settings.cookie_string,
        auto_retry=auto_retry,
    )


def _audio_badge_text(audio_lang: str) -> str:
    """Returns the SUB or DUB markup for an audio language."""
    return "[cyan]SUB[/cyan]" if audio_lang == "jpn" else "[yellow]DUB[/yellow]"


def _build_context(
    args: argparse.Namespace,
    cm: config_manager.ConfigManager,
    settings: _Settings,
    wl_entry: watchlist.WatchlistEntry | None,
    scripted: bool,
    output_dir: str,
    mode: str,
) -> models.AppContext:
    """Decides the download settings for the chosen episodes.

    There are four sources, in order: the command-line arguments, a
    watchlist entry, saved settings, and finally the interactive wizard,
    whose answers are saved back to the configuration.

    Args:
        args: The parsed command-line arguments.
        cm: The loaded configuration manager.
        settings: The resolved settings.
        wl_entry: The watchlist entry the user jumped in from, or None.
        scripted: Whether the arguments already say what to do.
        output_dir: The series output folder.
        mode: ``"download"`` or ``"stream"``.

    Returns:
        The download settings.
    """
    if scripted:
        return _make_context(
            settings, output_dir, args.keep_temp, settings.auto_retry
        )
    badge = _audio_badge_text(settings.audio_lang)
    if wl_entry is not None:
        console.console.print(
            "\n  [dim]Using watchlist prefs for "
            f"'{wl_entry.title}':"
            f"  {badge}  {settings.quality}p  {settings.parallel} "
            f"concurrent  {settings.workers} workers[/dim]\n"
        )
        return _make_context(
            settings, output_dir, wl_entry.keep_temp, wl_entry.auto_retry
        )
    if cm.is_customized():
        console.console.print(
            f"\n  [dim]Using saved settings from pahebatcher.toml:"
            f"  {badge}  {settings.quality}p  {settings.parallel} "
            f"concurrent  {settings.workers} workers[/dim]\n"
        )
        return _make_context(
            settings, output_dir, False, settings.auto_retry
        )
    defaults = _make_context(settings, output_dir, False, settings.auto_retry)
    ctx = prompts.wizard_config(defaults, mode=mode)
    # Persist choices from wizard
    cm.set("quality", ctx.quality)
    cm.set("audio_lang", ctx.audio_lang)
    cm.set("max_parallel", ctx.max_parallel)
    cm.set("hls_workers", ctx.hls_workers)
    try:
        cm.save()
        console.console.print(
            "  [dim]Settings saved to [bold]pahebatcher.toml[/bold][/dim]\n"
        )
    except (OSError, ValueError):
        pass
    return ctx


def _record_download_history(
    anime: models.AnimeInfo,
    chosen: list[models.EpisodeInfo],
    results: dict[str, pathlib.Path | None],
) -> None:
    """Adds the downloaded episodes to the series' watchlist history.

    Args:
        anime: The downloaded series.
        chosen: The episodes that were requested.
        results: The download results, keyed by episode session.
    """
    done_nums = [
        ep.number for ep in chosen if results.get(ep.session) is not None
    ]
    updated = False
    with contextlib.suppress(Exception):
        updated = watchlist.record_watchlist_download(
            anime.session, anime.title, done_nums
        )
    if updated:
        console.console.print(
            f"  [dim]Watchlist history updated for '{anime.title}'[/dim]",
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


async def run(args: argparse.Namespace) -> None:
    """Runs the command described by the parsed arguments.

    Args:
        args: The parsed command-line arguments.
    """
    # ── Load persistent config as defaults (CLI args override) ──────────────
    cm = config_manager.ConfigManager()
    cm.load()

    console.print_banner()

    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    settings = _resolve_settings(args, cm)

    # Prerequisites check
    console.section("Checking Prerequisites")
    console.console.print("  [muted]Cloudflare solver[/muted]  ", end="")

    solver = pahebatcher.solver.Solver.from_config(cm)
    await solver.start()
    try:
        if not await solver.ping():
            console.console.print("[red]\u2717 none available[/red]")
            console.console.print(
                "\n  [red]No Cloudflare solver is available[/red]\n"
                f"  [dim]{pahebatcher.solver.no_solver_hint()}[/dim]"
            )
            sys.exit(1)
        console.console.print(f"[ok]\u2713 {solver.backend_name}[/ok]")

        http = pahebatcher.http.HttpClient(settings.workers)
        await http.start()

        try:
            # Discovery
            url = await _discover_url(args, solver)
            if not url:
                return

            host, session = pahebatcher.extract.scanner.parse_anime_url(url)

            # Watchlist jump (wizard browser): entry prefs win unless a CLI
            # flag was explicit.
            wl_entry = getattr(args, "_watchlist_entry", None)
            if wl_entry is not None and wl_entry.session != session:
                wl_entry = None
            if wl_entry is not None:
                _apply_watchlist_prefs(args, wl_entry, settings)

            # Scan
            scanner = pahebatcher.extract.scanner.AnimePaheScanner(
                solver, host, session
            )
            anime = await scanner.scan(
                settings.cache_dir,
                prefer_audio=settings.audio_lang,
                cache_ttl=settings.cache_ttl,
            )
            _print_scan_banner(anime)

            if args.list_only:
                _print_episode_table(anime)
                return

            scripted = bool(
                args.all or args.range or args.latest or args.stream
            )
            safe_title = utils.sanitize(anime.title)
            output_base = (
                wl_entry.output_dir if wl_entry is not None else args.output
            )
            default_output = os.path.join(output_base, safe_title)

            while True:
                if scripted:
                    mode = "stream" if args.stream else "download"
                else:
                    choice = _prompt_action(anime, settings.cache_dir)
                    if choice == "5":
                        break
                    if choice.upper() == "W":
                        new_url, wl_ent = (
                            await watchlist.run_watchlist_browser()
                        )
                        if new_url and new_url != args.url:
                            args.url = new_url
                            if wl_ent is not None:
                                args._watchlist_entry = wl_ent
                            return await run(args)
                        continue
                    if choice == "4":
                        _print_episode_table(anime)
                        continue
                    if choice == "3":
                        new_url = pahebatcher.sessions.run(settings.cache_dir)
                        if new_url and new_url != args.url:
                            args.url = new_url
                            return await run(args)
                        continue
                    mode = {"1": "download", "2": "stream"}[choice]

                chosen = _choose_episodes(args, anime, scripted)
                if not chosen:
                    if scripted:
                        break
                    continue

                ctx = _build_context(
                    args, cm, settings, wl_entry, scripted, default_output, mode
                )

                if mode == "stream":
                    await stream.run_stream(ctx, anime, chosen, solver)
                else:
                    if not scripted and not prompts.confirm_download(
                        anime, chosen, ctx
                    ):
                        continue
                    orch = downloader.BatchOrchestrator(
                        ctx, anime, http, solver
                    )
                    results = await orch.download(chosen)
                    if wl_entry is not None:
                        _record_download_history(anime, chosen, results)

                break

        finally:
            await http.close()
    finally:
        await solver.close()

    # Cleanup orphaned cache (>24h old)
    _cleanup_orphan_cache(settings.cache_dir)

    console.console.print("\n  [brand.bold]Session finished.[/brand.bold]")


WATCHLIST_ALIASES = {"watchlist", "wl", "w", "watch"}
CHECK_ALIASES = {"check", "sync", "update"}
RELINK_ALIASES = {"relink", "link", "migrate"}


def main() -> None:
        """Runs the command-line interface.

        Parses ``sys.argv``, dispatches to the requested command and exits the
        process on fatal errors.
        """
        # Config / watchlist subcommands use own parsers to avoid argparse
        # subparser greediness with URL positional arguments.
        if len(sys.argv) > 1 and sys.argv[1] == "config":
            cfg_parser = build_config_parser()
            cfg_args = cfg_parser.parse_args(sys.argv[2:])
            if cfg_args.config_action == "show":
                config_manager.cli_show()
            elif cfg_args.config_action == "set":
                config_manager.cli_set(
                    cfg_args.key, cfg_args.value
                )
            elif cfg_args.config_action == "reset":
                config_manager.cli_reset()
            return

        # Top-level shorthand: `pahebatcher check` → `pahebatcher watchlist
        # check` and `pahebatcher relink` → `pahebatcher watchlist relink` (no
        # id = relink all)
        if len(sys.argv) > 1 and sys.argv[1] in CHECK_ALIASES:
            # Parse check flags (identifier, --verbose) via watchlist parser
            wl_parser = build_watchlist_parser()
            # Reuse check subparser parsing: prepend "check"
            try:
                wl_args = wl_parser.parse_args(["check", *sys.argv[2:]])
            except SystemExit:
                raise
            try:
                asyncio.run(watchlist.run_watchlist_check(
                    verbose=bool(wl_args.verbose), filter_id=wl_args.identifier,
                ))
            except (KeyboardInterrupt, asyncio.CancelledError):
                console.console.print("\n  [yellow]Interrupted.[/yellow]")
                sys.exit(0)
            except SystemExit:
                raise
            except Exception as exc:
                console.console.print(
                    f"\n  [red]\u2717 Fatal Error:[/red] {exc}"
                )
                if getattr(wl_args, "verbose", False):
                    raise
                sys.exit(1)
            return

        if len(sys.argv) > 1 and sys.argv[1] in RELINK_ALIASES:
            # Top-level relink: `pahebatcher relink [id] [new-url]`
            wl_parser = build_watchlist_parser()
            try:
                # Normalize to `relink` subcommand
                wl_args = wl_parser.parse_args(["relink", *sys.argv[2:]])
            except SystemExit:
                raise
            try:
                watchlist.cli_relink(wl_args.identifier, wl_args.new_url)
            except (KeyboardInterrupt, asyncio.CancelledError):
                console.console.print("\n  [yellow]Interrupted.[/yellow]")
                sys.exit(0)
            except SystemExit:
                raise
            except Exception as exc:  # isolation point: keep going
                console.console.print(
                    f"\n  [red]\u2717 Fatal Error:[/red] {exc}"
                )
                sys.exit(1)
            return

        if len(sys.argv) > 1 and sys.argv[1] in WATCHLIST_ALIASES:
            wl_parser = build_watchlist_parser()
            wl_args = wl_parser.parse_args(sys.argv[2:])
            try:
                act = wl_args.watchlist_action
                if act in ("add", "a"):
                    watchlist.cli_add(
                        url=wl_args.url,
                        quality=wl_args.quality,
                        audio_lang=wl_args.audio_lang,
                        output=wl_args.output,
                        parallel=wl_args.parallel,
                        workers=wl_args.workers,
                        keep_temp=bool(wl_args.keep_temp),
                        retry=wl_args.retry,
                    )
                elif act in ("list", "ls", "l"):
                    watchlist.cli_list()
                elif act in ("show", "s", "info"):
                    watchlist.cli_show(wl_args.identifier)
                elif act in ("remove", "rm", "r", "del", "delete"):
                    if not wl_args.yes:
                        entries = watchlist.load_entries()
                        found = watchlist.find_entry(
                            entries, wl_args.identifier
                        )
                        if found:
                            _, ent = found
                            if not rich.prompt.Confirm.ask(
                                f"  [red]Remove '{ent.title}' from "
                                "watchlist?[/red]",
                                default=False,
                            ):
                                console.console.print("  [dim]Cancelled.[/dim]")
                                return

                    watchlist.cli_remove(wl_args.identifier)
                elif act in ("reset", "rst", "clear"):
                    watchlist.cli_reset(
                        wl_args.identifier, reset_all=bool(wl_args.all)
                    )
                elif act in ("relink", "link", "migrate", "update-url"):
                    watchlist.cli_relink(wl_args.identifier, wl_args.new_url)
                elif act in ("check", "c", "sync", "update"):
                    asyncio.run(
                        watchlist.run_watchlist_check(
                            verbose=bool(wl_args.verbose),
                            filter_id=wl_args.identifier,
                        )
                    )
            except (KeyboardInterrupt, asyncio.CancelledError):
                console.console.print("\n  [yellow]Interrupted.[/yellow]")
                sys.exit(0)
            except SystemExit:
                raise
            except Exception as exc:
                console.console.print(
                    f"\n  [red]\u2717 Fatal Error:[/red] {exc}"
                )
                if getattr(wl_args, "verbose", False):
                    raise
                sys.exit(1)
            return

        parser = build_parser()
        args = parser.parse_args()
        try:
            asyncio.run(run(args))
        except (KeyboardInterrupt, asyncio.CancelledError):
            console.console.print("\n  [yellow]Interrupted.[/yellow]")
            sys.exit(0)
        except Exception as exc:
            console.console.print(f"\n  [red]\u2717 Fatal Error:[/red] {exc}")
            if args.verbose:
                raise
            sys.exit(1)


if __name__ == "__main__":
    main()
