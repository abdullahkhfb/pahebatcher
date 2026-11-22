"""Interactive prompts: episode selection, confirmation and wizard."""

from __future__ import annotations

import contextlib
import dataclasses
import pathlib
import re
from typing import TYPE_CHECKING

import rich.progress
import rich.prompt
import rich.table
import rich.text

from pahebatcher import utils
from pahebatcher.extract import scanner
import pahebatcher.store
from pahebatcher.ui import console, tables

if TYPE_CHECKING:
    from pahebatcher import models
    import pahebatcher.solver


def _parse_ep_range(raw: str, all_eps: list[models.EpisodeInfo]) -> list[float]:
    """Parses an episode range expression.

    Args:
        raw: The expression, such as ``"1-12"``, ``"1,4,7"`` or ``"13-"``.
        all_eps: The available episodes, used to resolve open ranges.

    Returns:
        The matching episode numbers in ascending order.
    """
    numbers = sorted({ep.number for ep in all_eps})
    result: set[float] = set()
    for token in re.split(r"[,\s]+", raw):
        token = token.strip()
        if not token:
            continue
        if m := re.match(r"^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$", token):
            lo, hi = float(m.group(1)), float(m.group(2))
            result.update(n for n in numbers if lo <= n <= hi)
        elif m := re.match(r"^(\d+(?:\.\d+)?)-$", token):
            lo = float(m.group(1))
            result.update(n for n in numbers if n >= lo)
        elif m := re.match(r"^(\d+(?:\.\d+)?)$", token):
            result.add(float(m.group(1)))
    return sorted(result)


def _print_ep_table(
    anime: models.AnimeInfo,
    episodes: list[models.EpisodeInfo],
    selected: set[str],
) -> int:
    """Prints the episodes as a minimal checklist.

    Each line looks like ``[x] Episode 3   title``.

    Args:
        anime: The series.
        episodes: The episodes to list.
        selected: The keys of the selected episodes.

    Returns:
        The number of selected episodes.
    """
    t = rich.table.Table.grid(padding=(0, 2))
    t.add_column(width=5)
    t.add_column(width=12)
    t.add_column()

    seen_nums = set()
    count = 0
    for ep in episodes:
        if ep.number in seen_nums:
            continue
        seen_nums.add(ep.number)
        variants = anime.get_all_variants(ep.number)
        on = (
            ep.session in selected
            or any(v.session in selected for v in variants)
        )
        count += on
        t.add_row(
            rich.text.Text(
                f"{console.PAD}[x]" if on else f"{console.PAD}[ ]",
                style="brand" if on else "muted",
            ),
            rich.text.Text(
                f"Episode {ep.ep_str}", style="white" if on else "muted"
            ),
            rich.text.Text(ep.title or "\u2014", style="muted"),
        )
    console.console.print(t)
    return count


def select_episodes(anime: models.AnimeInfo) -> list[models.EpisodeInfo]:
    """Lets the user pick episodes interactively.

    Args:
        anime: The series to choose from.

    Returns:
        The chosen episodes. The list is empty if the user cancels.
    """
    unique_eps: list[models.EpisodeInfo] = list(
        {ep.number: ep for ep in anime.episodes}.values()
    )
    unique_eps.sort(key=lambda e: e.number)

    available = utils.compact_ep_range(unique_eps)
    console.heading(anime.title, f"{anime.total} episodes \u00b7 {available}")
    console.console.print()
    console.menu(
        [
            ("a", "All", "every episode"),
            ("r", "Range", "e.g. 1-12  or  1,4,7  or  13-"),
            ("l", "Pick", "interactive checklist"),
            ("n", "Latest N", "most recently aired"),
            ("s", "Skip", ""),
        ],
        selected="a",
    )
    console.console.print()

    mode = rich.prompt.Prompt.ask(
        console.prompt_label("select"),
        choices=["A", "a", "R", "r", "L", "l", "N", "n", "S", "s"],
        default="A", show_choices=False,
    ).upper()

    eps_by_num = {ep.number: ep for ep in anime.episodes}

    if mode == "S":
        return []
    if mode == "A":
        console.console.print(
            f"{console.PAD}[ok]\u2713[/ok] [muted]all[/muted] {anime.total} "
            "[muted]selected[/muted]"
        )
        return unique_eps
    if mode == "N":
        n = rich.prompt.IntPrompt.ask(
            console.prompt_label("latest how many"), default=1
        )
        chosen = unique_eps[-max(1, min(n, len(unique_eps))):]
        console.console.print(
            f"{console.PAD}[ok]\u2713[/ok] {len(chosen)} [muted]latest "
            "selected[/muted]"
        )
        return chosen
    if mode == "R":
        hint = "[muted]1-12 \u00b7 1,4,7 \u00b7 5- \u00b7 1-6,10[/muted]"
        raw = rich.prompt.Prompt.ask(
            console.prompt_label(f"episodes {hint}")
        ).strip()
        nums = _parse_ep_range(raw, unique_eps)
        chosen = [eps_by_num[n] for n in nums if n in eps_by_num]
        if not chosen:
            console.console.print(
                f"{console.PAD}[warn]nothing matched[/warn] {raw} "
                "[muted]\u00b7 "
                f"available: {available}[/muted]",
            )
        else:
            console.console.print(
                f"{console.PAD}[ok]\u2713[/ok] {len(chosen)} [muted]episodes "
                "selected[/muted]"
            )
        return chosen

    # Pick mode
    selected: set[str] = set()
    while True:
        console.console.clear()
        console.heading(anime.title, "pick episodes")
        console.console.print()
        count = _print_ep_table(anime, unique_eps, selected)
        console.console.print()
        if count:
            console.console.print(
                f"{console.PAD}[brand]\u25b8 Download {count} "
                f"episode{'s' if count != 1 else ''}[/brand]"
            )
        else:
            console.console.print(
                f"{console.PAD}[muted]nothing selected[/muted]"
            )
        console.hint_bar(
            ("<num>", "toggle"), ("a", "all"), ("n", "none"), ("enter", "done")
        )
        cmd = rich.prompt.Prompt.ask(console.prompt_label("")).strip().lower()
        if cmd in ("done", "d", ""):
            break
        elif cmd == "a":
            selected = {ep.session for ep in unique_eps}
        elif cmd == "n":
            selected.clear()
        else:
            for num in _parse_ep_range(cmd, unique_eps):
                variants = anime.get_all_variants(num)
                if not variants:
                    continue
                if any(v.session in selected for v in variants):
                    for v in variants:
                        selected.discard(v.session)
                else:
                    selected.add(variants[0].session)

    chosen = [ep for ep in unique_eps if ep.session in selected]
    console.console.print(
        f"{console.PAD}[ok]\u2713[/ok] {len(chosen)} [muted]episodes "
        "selected[/muted]"
    )
    return chosen


def noninteractive_episodes(
    anime: models.AnimeInfo, mode: str, range_str: str = "", latest_n: int = 1,
) -> list[models.EpisodeInfo]:
    """Selects episodes without prompting, for command-line use.

    Args:
        anime: The series to choose from.
        mode: The selection mode, such as ``"all"``, ``"range"`` or
            ``"latest"``.
        range_str: The range expression when the mode is ``"range"``.
        latest_n: How many episodes to take when the mode is ``"latest"``.

    Returns:
        The selected episodes.
    """
    unique_eps = sorted(
        {ep.number: ep for ep in anime.episodes}.values(),
        key=lambda e: e.number,
    )
    if mode == "all":
        return unique_eps
    if mode == "latest":
        return unique_eps[-max(1, min(latest_n, len(unique_eps))):]
    if mode == "range":
        eps_by_num = {ep.number: ep for ep in unique_eps}
        return [
            eps_by_num[n]
            for n in _parse_ep_range(range_str, unique_eps)
            if n in eps_by_num
        ]
    return []


def confirm_download(
    anime: models.AnimeInfo,
    episodes: list[models.EpisodeInfo],
    ctx: models.AppContext,
) -> bool:
    """Shows what is about to be downloaded and asks for confirmation.

    Args:
        anime: The series.
        episodes: The episodes to download.
        ctx: The settings for this run.

    Returns:
        True if the user confirmed.
    """
    n = len(episodes)
    ep_range_str = utils.compact_ep_range(episodes)

    reused_count = 0
    if anime.has_session:
        for ep in episodes:
            target_ep = anime.get_variant(ep.number, ctx.audio_lang) or ep
            store = pahebatcher.store.SegmentStore(
                ctx.cache_dir,
                anime.title,
                anime.session,
                target_ep.ep_str,
                target_ep.audio,
            )
            reused_count += len(store.done_indices())

    est_mb_per = {360: 50, 720: 90, 1080: 150}.get(ctx.quality, 120)
    est_total = n * est_mb_per
    audio_str = "sub" if ctx.audio_lang == "jpn" else "dub"

    console.heading(
        anime.title, f"{n} episode{'s' if n != 1 else ''} \u00b7 {ep_range_str}"
    )
    console.console.print()
    kv = rich.table.Table.grid(padding=(0, 3))
    kv.add_column(style="muted")
    kv.add_column()
    kv.add_row(f"{console.PAD}quality", f"[brand]{ctx.quality}p[/brand]")
    kv.add_row(f"{console.PAD}audio", audio_str)
    kv.add_row(f"{console.PAD}output", str(ctx.output_dir))
    kv.add_row(
        f"{console.PAD}size",
        f"\u2248 {est_total} MB [muted](~{est_mb_per} MB/ep)[/muted]",
    )
    if reused_count > 0:
        kv.add_row(
            f"{console.PAD}reusing",
            f"[ok]{reused_count}[/ok] [muted]segments from previous "
            "session[/muted]",
        )
    console.console.print(kv)
    console.console.print()
    return rich.prompt.Confirm.ask(
        console.prompt_label("[brand]Start download?[/brand]"), default=True
    )


def wizard_config(
    defaults: models.AppContext,
    mode: str = "download",
) -> models.AppContext:
    """Walks the user through the main settings.

    Args:
        defaults: The settings to start from.
        mode: The kind of run, such as ``"download"``. It decides which
            questions are asked.

    Returns:
        A copy of ``defaults`` with the user's answers applied.
    """

    console.heading(
        "Settings",
        "saved to pahebatcher.toml \u00b7 change later: pahebatcher config "
        "set KEY VALUE",
    )

    q_default = {360: "1", 720: "2", 1080: "3"}.get(defaults.quality, "3")
    console.section("Quality")
    console.menu(
        [
            ("1", "360p", "~50 MB/ep"),
            ("2", "720p", "~90 MB/ep \u00b7 recommended"),
            ("3", "1080p", "~150 MB/ep \u00b7 best"),
        ],
        selected=q_default,
    )
    console.console.print()
    quality = {1: 360, 2: 720, 3: 1080}[
        int(
            rich.prompt.Prompt.ask(
                console.prompt_label("quality"),
                choices=["1", "2", "3"],
                default=q_default,
                show_choices=False,
            ),
        )
    ]

    audio_default = "1" if defaults.audio_lang == "jpn" else "2"
    console.section("Audio")
    console.menu(
        [("1", "Sub", "Japanese audio"), ("2", "Dub", "English audio")],
        selected=audio_default,
    )
    console.console.print()
    audio_lang = (
        "jpn"
        if rich.prompt.Prompt.ask(
            console.prompt_label("audio"),
            choices=["1", "2"],
            default=audio_default,
            show_choices=False,
        ) == "1"
        else "eng"
    )

    if mode == "download":
        console.section("Output")
        output_dir = rich.prompt.Prompt.ask(
            console.prompt_label("directory"), default=defaults.output_dir
        ).strip()
        pathlib.Path(output_dir).mkdir(parents=True, exist_ok=True)

        console.section("Parallel downloads")
        console.menu(
            [
                ("1", "1 at a time", "safest"),
                ("2", "2 at a time", "recommended"),
                ("4", "4 at a time", "faster, more RAM"),
                ("6", "6 at a time", "may trigger rate limits"),
            ],
            selected="2",
        )
        console.console.print()
        max_parallel = max(
            1,
            min(
                6,
                rich.prompt.IntPrompt.ask(
                    console.prompt_label("parallel"),
                    default=defaults.max_parallel,
                ),
            ),
        )
        hls_workers_val = max(
            8,
            min(
                32,
                rich.prompt.IntPrompt.ask(
                    console.prompt_label(
                        "workers per episode [muted](8-32)[/muted]"
                    ),
                    default=defaults.hls_workers,
                ),
            ),
        )
        return dataclasses.replace(
            defaults,
            output_dir=output_dir,
            quality=quality,
            audio_lang=audio_lang,
            max_parallel=max_parallel,
            hls_workers=hls_workers_val,
        )
    return dataclasses.replace(defaults, quality=quality, audio_lang=audio_lang)


async def interactive_discovery(
    solver: pahebatcher.solver.Solver, host: str, initial: str | None = None,
) -> tuple[str | None, str | None]:
    """Searches animepahe interactively.

    Args:
        solver: The Cloudflare solver.
        host: The animepahe host.
        initial: A search text to start with, or None to ask.

    Returns:
        A tuple ``(series_url, result_title)``, or ``(None, None)`` if the
        user cancels or nothing is found.
    """

    pending = initial.strip() if initial and initial.strip() else None
    while True:
        console.console.print()
        if pending is None:
            hint = "[muted]or paste a url \u00b7 q to quit[/muted]"
            query = rich.prompt.Prompt.ask(
                console.prompt_label(f"search anime {hint}")
            ).strip()
        else:
            query, pending = pending, None
            console.console.print(
                f"{console.PAD}[brand]\u203a[/brand] [muted]{query}[/muted]"
            )
        if not query or query.lower() == "q":
            return None, None

        with rich.progress.Progress(
            rich.progress.SpinnerColumn(style="brand"),
            rich.progress.TextColumn(f"[muted]searching {query}\u2026[/muted]"),
            console=console.console,
            transient=True,
        ) as prog:
            prog.add_task("", total=None)
            results = await scanner.AnimePaheScanner.search(solver, host, query)

        if not results:
            console.console.print(
                f"{console.PAD}[warn]no results[/warn] [muted]for[/muted] "
                f"{query}"
            )
            continue

        console.console.print(tables.search_results_table(results, query))
        choice = rich.prompt.Prompt.ask(
            console.prompt_label(
                f"select 1-{len(results)} [muted]s search again \u00b7 q "
                "quit[/muted]"
            ),
            default="1",
        ).lower()
        if choice == "q":
            return None, None
        if choice == "s":
            continue
        with contextlib.suppress(ValueError):
            idx = int(choice)
            if 1 <= idx <= len(results):
                target = results[idx - 1]
                session = target.get("session")
                if session:
                    title = str(target.get("title", "") or "").strip()
                    return (
                        f"https://{scanner.AnimePaheScanner._current_host}"
                        f"/anime/{session}",
                        title or None,
                    )
        console.console.print(f"{console.PAD}[red]invalid selection[/red]")
