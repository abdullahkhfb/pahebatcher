"""Interactive prompts — episode selection, download confirmation, wizard."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from pahebatcher.ui.console import PAD, heading, hint_bar, menu, prompt_label, section
from pahebatcher.utils import compact_ep_range

if TYPE_CHECKING:
    from pahebatcher.models import AnimeInfo, AppContext, EpisodeInfo
    from pahebatcher.solver import Solver


def _parse_ep_range(raw: str, all_eps: list[EpisodeInfo]) -> list[float]:
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


def _print_ep_table(anime: AnimeInfo, episodes: list[EpisodeInfo], selected: set[str]) -> int:
    """Minimal checklist: ``[x] Episode 3   title``. Returns the number selected."""
    from pahebatcher.ui.console import console

    t = Table.grid(padding=(0, 2))
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
        on = ep.session in selected or any(v.session in selected for v in variants)
        count += on
        t.add_row(
            Text(f"{PAD}[x]" if on else f"{PAD}[ ]", style="brand" if on else "muted"),
            Text(f"Episode {ep.ep_str}", style="white" if on else "muted"),
            Text(ep.title or "\u2014", style="muted"),
        )
    console.print(t)
    return count


def select_episodes(anime: AnimeInfo) -> list[EpisodeInfo]:
    from pahebatcher.ui.console import console

    unique_eps: list[EpisodeInfo] = list({ep.number: ep for ep in anime.episodes}.values())
    unique_eps.sort(key=lambda e: e.number)

    available = compact_ep_range(unique_eps)
    heading(anime.title, f"{anime.total} episodes \u00b7 {available}")
    console.print()
    menu(
        [
            ("a", "All", "every episode"),
            ("r", "Range", "e.g. 1-12  or  1,4,7  or  13-"),
            ("l", "Pick", "interactive checklist"),
            ("n", "Latest N", "most recently aired"),
            ("s", "Skip", ""),
        ],
        selected="a",
    )
    console.print()

    mode = Prompt.ask(
        prompt_label("select"),
        choices=["A", "a", "R", "r", "L", "l", "N", "n", "S", "s"],
        default="A", show_choices=False,
    ).upper()

    eps_by_num = {ep.number: ep for ep in anime.episodes}

    if mode == "S":
        return []
    if mode == "A":
        console.print(f"{PAD}[ok]\u2713[/ok] [muted]all[/muted] {anime.total} [muted]selected[/muted]")
        return unique_eps
    if mode == "N":
        n = IntPrompt.ask(prompt_label("latest how many"), default=1)
        chosen = unique_eps[-max(1, min(n, len(unique_eps))):]
        console.print(f"{PAD}[ok]\u2713[/ok] {len(chosen)} [muted]latest selected[/muted]")
        return chosen
    if mode == "R":
        hint = "[muted]1-12 \u00b7 1,4,7 \u00b7 5- \u00b7 1-6,10[/muted]"
        raw = Prompt.ask(prompt_label(f"episodes {hint}")).strip()
        nums = _parse_ep_range(raw, unique_eps)
        chosen = [eps_by_num[n] for n in nums if n in eps_by_num]
        if not chosen:
            console.print(
                f"{PAD}[warn]nothing matched[/warn] {raw} [muted]\u00b7 available: {available}[/muted]",
            )
        else:
            console.print(f"{PAD}[ok]\u2713[/ok] {len(chosen)} [muted]episodes selected[/muted]")
        return chosen

    # Pick mode
    selected: set[str] = set()
    while True:
        console.clear()
        heading(anime.title, "pick episodes")
        console.print()
        count = _print_ep_table(anime, unique_eps, selected)
        console.print()
        if count:
            console.print(f"{PAD}[brand]\u25b8 Download {count} episode{'s' if count != 1 else ''}[/brand]")
        else:
            console.print(f"{PAD}[muted]nothing selected[/muted]")
        hint_bar(("<num>", "toggle"), ("a", "all"), ("n", "none"), ("enter", "done"))
        cmd = Prompt.ask(prompt_label("")).strip().lower()
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
    console.print(f"{PAD}[ok]\u2713[/ok] {len(chosen)} [muted]episodes selected[/muted]")
    return chosen


def noninteractive_episodes(
    anime: AnimeInfo, mode: str, range_str: str = "", latest_n: int = 1,
) -> list[EpisodeInfo]:
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
        return [eps_by_num[n] for n in _parse_ep_range(range_str, unique_eps) if n in eps_by_num]
    return []


def confirm_download(anime: AnimeInfo, episodes: list[EpisodeInfo], ctx: AppContext) -> bool:
    from pahebatcher.store import SegmentStore
    from pahebatcher.ui.console import console

    n = len(episodes)
    ep_range_str = compact_ep_range(episodes)

    reused_count = 0
    if anime.has_session:
        for ep in episodes:
            target_ep = anime.get_variant(ep.number, ctx.audio_lang) or ep
            store = SegmentStore(ctx.cache_dir, anime.title, anime.session, target_ep.ep_str, target_ep.audio)
            reused_count += len(store.done_indices())

    est_mb_per = {360: 50, 720: 90, 1080: 150}.get(ctx.quality, 120)
    est_total = n * est_mb_per
    audio_str = "sub" if ctx.audio_lang == "jpn" else "dub"

    heading(anime.title, f"{n} episode{'s' if n != 1 else ''} \u00b7 {ep_range_str}")
    console.print()
    kv = Table.grid(padding=(0, 3))
    kv.add_column(style="muted")
    kv.add_column()
    kv.add_row(f"{PAD}quality", f"[brand]{ctx.quality}p[/brand]")
    kv.add_row(f"{PAD}audio", audio_str)
    kv.add_row(f"{PAD}output", str(ctx.output_dir))
    kv.add_row(f"{PAD}size", f"\u2248 {est_total} MB [muted](~{est_mb_per} MB/ep)[/muted]")
    if reused_count > 0:
        kv.add_row(f"{PAD}reusing", f"[ok]{reused_count}[/ok] [muted]segments from previous session[/muted]")
    console.print(kv)
    console.print()
    return Confirm.ask(prompt_label("[brand]Start download?[/brand]"), default=True)


def wizard_config(defaults: AppContext, mode: str = "download") -> AppContext:
    from dataclasses import replace

    from pahebatcher.ui.console import console

    heading("Settings", "saved to pahebatcher.toml \u00b7 change later: pahebatcher config set KEY VALUE")

    q_default = {360: "1", 720: "2", 1080: "3"}.get(defaults.quality, "3")
    section("Quality")
    menu([("1", "360p", "~50 MB/ep"), ("2", "720p", "~90 MB/ep \u00b7 recommended"),
          ("3", "1080p", "~150 MB/ep \u00b7 best")], selected=q_default)
    console.print()
    quality = {1: 360, 2: 720, 3: 1080}[int(
        Prompt.ask(prompt_label("quality"), choices=["1", "2", "3"], default=q_default, show_choices=False),
    )]

    audio_default = "1" if defaults.audio_lang == "jpn" else "2"
    section("Audio")
    menu([("1", "Sub", "Japanese audio"), ("2", "Dub", "English audio")], selected=audio_default)
    console.print()
    audio_lang = "jpn" if Prompt.ask(
        prompt_label("audio"), choices=["1", "2"], default=audio_default, show_choices=False,
    ) == "1" else "eng"

    if mode == "download":
        section("Output")
        output_dir = Prompt.ask(prompt_label("directory"), default=defaults.output_dir).strip()
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        section("Parallel downloads")
        menu([("1", "1 at a time", "safest"), ("2", "2 at a time", "recommended"),
              ("4", "4 at a time", "faster, more RAM"), ("6", "6 at a time", "may trigger rate limits")],
             selected="2")
        console.print()
        max_parallel = max(1, min(6, IntPrompt.ask(prompt_label("parallel"), default=defaults.max_parallel)))
        hls_workers_val = max(8, min(32, IntPrompt.ask(
            prompt_label("workers per episode [muted](8-32)[/muted]"), default=defaults.hls_workers,
        )))
        return replace(defaults, output_dir=output_dir, quality=quality, audio_lang=audio_lang,
                       max_parallel=max_parallel, hls_workers=hls_workers_val)
    return replace(defaults, quality=quality, audio_lang=audio_lang)


async def interactive_discovery(
    solver: Solver, host: str, initial: str | None = None,
) -> tuple[str | None, str | None]:
    """Interactive animepahe search. Returns (series URL, result title) or (None, None)."""
    from pahebatcher.extract.scanner import AnimePaheScanner
    from pahebatcher.ui.console import console
    from pahebatcher.ui.tables import search_results_table

    pending = initial.strip() if initial and initial.strip() else None
    while True:
        console.print()
        if pending is None:
            hint = "[muted]or paste a url \u00b7 q to quit[/muted]"
            query = Prompt.ask(prompt_label(f"search anime {hint}")).strip()
        else:
            query, pending = pending, None
            console.print(f"{PAD}[brand]\u203a[/brand] [muted]{query}[/muted]")
        if not query or query.lower() == "q":
            return None, None

        with Progress(
            SpinnerColumn(style="brand"), TextColumn(f"[muted]searching {query}\u2026[/muted]"),
            console=console, transient=True,
        ) as prog:
            prog.add_task("", total=None)
            results = await AnimePaheScanner.search(solver, host, query)

        if not results:
            console.print(f"{PAD}[warn]no results[/warn] [muted]for[/muted] {query}")
            continue

        console.print(search_results_table(results, query))
        choice = Prompt.ask(
            prompt_label(f"select 1-{len(results)} [muted]s search again \u00b7 q quit[/muted]"),
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
                    return f"https://{AnimePaheScanner._current_host}/anime/{session}", title or None
        console.print(f"{PAD}[red]invalid selection[/red]")
