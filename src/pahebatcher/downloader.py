"""Episode and batch download orchestrator with prefetching."""

from __future__ import annotations

import asyncio
import logging
import pathlib
import re
import time
from typing import TYPE_CHECKING, Any

from Cryptodome.Cipher import AES
from rich import box
import rich.panel
import rich.table

from pahebatcher import cache, models, utils
from pahebatcher.extract import kwik, m3u8
import pahebatcher.store
from pahebatcher.ui import console, dashboard

if TYPE_CHECKING:
    import pahebatcher.http
    import pahebatcher.solver

log = logging.getLogger(__name__)


class EpisodeDownloader:
    """Downloads, decrypts and assembles a single episode.

    Segments are fetched concurrently into a ``SegmentStore`` so that an
    interrupted download can resume, then assembled into an MP4.

    Attributes:
        ctx: The settings for this run.
        anime: The series being downloaded.
        dash: The dashboard to report progress to.
        http: The HTTP client.
        solver: The Cloudflare solver.
    """

    def __init__(
        self,
        ctx: models.AppContext,
        anime: models.AnimeInfo,
        dash: dashboard.Dashboard,
        http: pahebatcher.http.HttpClient,
        solver: pahebatcher.solver.Solver,
        aes_key_cache: cache.TTLCache | None = None,
    ) -> None:
        """Initializes the downloader.

        Args:
            ctx: The settings for this run.
            anime: The series being downloaded.
            dash: The dashboard to report progress to.
            http: The HTTP client.
            solver: The Cloudflare solver.
            aes_key_cache: A cache for AES keys shared between episodes. A
                private cache is created if None.
        """
        self.ctx = ctx
        self.anime = anime
        self.dash = dash
        self.http = http
        self.solver = solver
        self._aes_key_cache = (
            aes_key_cache or cache.TTLCache(ttl=3600.0, max_size=128)
        )

    async def run(
        self,
        ep: models.EpisodeInfo,
        info: models.StreamInfo,
    ) -> pathlib.Path | None:
        """Downloads one episode, retrying on failure.

        Skips the download if the output file already exists.

        Args:
            ep: The episode to download.
            info: The episode's resolved stream.

        Returns:
            The path of the finished MP4, or None if every attempt failed.
        """
        key = ep.ep_str
        title = ep.title or info.title or f"Episode {ep.ep_str}"
        label = f"Ep {ep.ep_str} \u2014 {title}"
        store = pahebatcher.store.SegmentStore(
            self.ctx.cache_dir,
            self.anime.title,
            self.anime.session,
            ep.ep_str,
            ep.audio,
        )
        loop = asyncio.get_running_loop()

        await loop.run_in_executor(
            None, store.save_metadata, self.anime.title, self.anime.session
        )

        outdir = pathlib.Path(self.ctx.output_dir)
        outdir.mkdir(parents=True, exist_ok=True)
        prefix = utils.ep_prefix(ep.ep_str)
        fname = utils.sanitize(f"Ep {prefix} - {title}") or f"ep_{ep.ep_str}"
        out = outdir / f"{fname}.mp4"

        if out.exists() and out.stat().st_size > 0:
            self.dash.mark_done(key, f"Ep {ep.ep_str} (already exists)")
            return out

        self.dash.add_ep(key, label)
        max_attempts = self.ctx.auto_retry + 1
        for attempt in range(max_attempts):
            try:
                hdrs = info.headers

                m3u8_txt = await m3u8.fetch_m3u8(self.http, info.url, hdrs)
                segments = await m3u8.resolve_m3u8(
                    self.http, m3u8_txt, info.url, hdrs
                )
                if not segments:
                    raise RuntimeError("No segments found in M3U8")

                n = len(segments)
                done_set = await loop.run_in_executor(None, store.done_indices)
                pending = [
                    (i, s) for i, s in enumerate(segments) if i not in done_set
                ]

                self.dash.set_total(key, n)
                self.dash.mark_downloading(key, label)
                for _ in done_set:
                    self.dash.seg_done(key, 0)

                key_map: dict[str, bytes] = {}
                unique_keys: set[str] = {
                    str(s["key_url"]) for s in segments if s["key_url"]
                }
                for kurl in unique_keys:
                    cached_any: Any = await self._aes_key_cache.get(kurl)
                    if isinstance(cached_any, bytes):
                        key_map[kurl] = cached_any
                    else:
                        key_data = await self.http.get(kurl, hdrs)
                        key_map[kurl] = key_data
                        await self._aes_key_cache.set(kurl, key_data)

                seg_sem = asyncio.Semaphore(self.ctx.hls_workers)

                async def fetch_one(idx: int, seg: dict[str, Any]) -> None:
                    """Downloads, decrypts and stores one segment.

                    Args:
                        idx: The segment's index.
                        seg: The segment description from the playlist.
                    """
                    async with seg_sem:
                        raw = await self.http.get(seg["url"], hdrs)
                        if seg["key_url"]:
                            cipher = AES.new(
                                key_map[seg["key_url"]],
                                AES.MODE_CBC,
                                iv=seg["iv"],
                            )
                            raw = cipher.decrypt(raw)
                        await loop.run_in_executor(
                            None, store.write_seg, idx, raw
                        )
                        self.dash.seg_done(key, len(raw))

                # Use return_exceptions to avoid cancelling siblings on first
                # failure
                results = await asyncio.gather(
                    *(asyncio.create_task(fetch_one(i, s)) for i, s in pending),
                    return_exceptions=True,
                )
                failed_segs = [
                    r for r in results if isinstance(r, BaseException)
                ]
                if failed_segs:
                    raise RuntimeError(
                        f"{len(failed_segs)}/{len(pending)} segments failed"
                    )

                self.dash.mark_remuxing(key, f"Ep {ep.ep_str}")
                ok = await loop.run_in_executor(None, store.assemble, n, out)
                if not ok:
                    raise RuntimeError("Assembly failed")

                if not self.ctx.keep_temp:
                    await loop.run_in_executor(None, store.cleanup)

                self.dash.mark_done(key, f"Ep {ep.ep_str} \u2014 {title[:34]}")
                return out
            except Exception as exc:  # isolation point: keep going
                is_last = attempt == max_attempts - 1
                # Don't retry permanent parse errors
                msg = str(exc)
                is_permanent = "No Kwik link" in msg or "No segments" in msg
                if is_permanent:
                    is_last = True
                if is_last:
                    log.exception(
                        "Episode %s failed after %d attempts",
                        ep.ep_str,
                        max_attempts,
                    )
                    self.dash.mark_fail(
                        key, f"Ep {ep.ep_str} \u2014 {exc!s:.40}"
                    )
                    return None
                log.warning(
                    "Episode %s failed (attempt %d/%d): %s — retrying",
                    ep.ep_str, attempt + 1, max_attempts, exc,
                )
                self.dash.mark_retry(
                    key, f"Ep {ep.ep_str} retry {attempt+1}/{max_attempts-1}"
                )
                await asyncio.sleep(1.5 * (2**attempt))
        return None


class BatchOrchestrator:
    """Downloads many episodes with a two-stage prefetch pipeline.

    One stage resolves episode streams ahead of time while worker tasks
    download up to ``ctx.max_parallel`` episodes at once.

    Attributes:
        ctx: The settings for this run.
        anime: The series being downloaded.
        http: The HTTP client.
        solver: The Cloudflare solver.
    """

    def __init__(
        self,
        ctx: models.AppContext,
        anime: models.AnimeInfo,
        http: pahebatcher.http.HttpClient,
        solver: pahebatcher.solver.Solver,
    ) -> None:
        """Initializes the orchestrator.

        Args:
            ctx: The settings for this run.
            anime: The series being downloaded.
            http: The HTTP client.
            solver: The Cloudflare solver.
        """
        self.ctx = ctx
        self.anime = anime
        self.http = http
        self.solver = solver
        self._results: dict[str, pathlib.Path | None] = {}

    def _find_existing(self, ep: models.EpisodeInfo) -> pathlib.Path | None:
        """Looks for an already downloaded file for an episode.

        Args:
            ep: The episode.

        Returns:
            The file's path, or None if there is none.
        """
        outdir = pathlib.Path(self.ctx.output_dir)
        if not outdir.exists():
            return None
        prefix = utils.ep_prefix(ep.ep_str)
        for p in outdir.iterdir():
            if not p.is_file() or p.suffix not in (".mp4",):
                continue
            is_target = (
                p.name.startswith(f"Ep {prefix}")
                or p.name.startswith(f"Ep_{prefix}")
            )
            if is_target and p.stat().st_size > 0:
                return p
        return None

    async def download(
        self,
        episodes: list[models.EpisodeInfo],
    ) -> dict[str, pathlib.Path | None]:
        """Downloads the given episodes.

        Episodes already on disk are skipped. A dashboard shows progress and a
        summary is printed at the end.

        Args:
            episodes: The episodes to download.

        Returns:
            A dict mapping each episode's session to the downloaded file, or to
            None if it failed.
        """
        loop = asyncio.get_running_loop()
        start = time.time()
        max_batch_attempts = self.ctx.auto_retry + 1
        remaining: list[models.EpisodeInfo] = list(episodes)
        shared_key_cache = cache.TTLCache(ttl=3600.0, max_size=128)

        for batch_attempt in range(max_batch_attempts):
            if not remaining:
                break
            is_retry = batch_attempt > 0
            if is_retry:
                console.console.print(
                    f"\n  [yellow]Retrying {len(remaining)} failed episode(s) "
                    "(attempt "
                    f"{batch_attempt+1}/{max_batch_attempts})...[/yellow]"
                )
                await asyncio.sleep(2 * batch_attempt)

            # Dashboard for this batch attempt
            dash_episodes = remaining if is_retry else episodes
            dash = dashboard.Dashboard(
                len(dash_episodes), title=self.anime.title
            )
            for ep in dash_episodes:
                label = f"Ep {ep.ep_str} \u2014 {ep.title or 'Pending...'}"
                dash.add_ep(ep.ep_str, label)
                dash.mark_waiting(ep.ep_str, label)
            # For retry, also show already-done eps as done
            if is_retry:
                for ep in episodes:
                    if (
                        ep.session in self._results
                        and self._results[ep.session] is not None
                    ):
                        dash.add_ep(
                            ep.ep_str, f"Ep {ep.ep_str} \u2014 {ep.title or ''}"
                        )
                        dash.mark_done(ep.ep_str, f"Ep {ep.ep_str} (done)")

            resolve_queue: asyncio.Queue[models.EpisodeInfo] = asyncio.Queue()
            for ep in remaining:
                await resolve_queue.put(ep)

            resolve_ahead = self.ctx.resolve_ahead
            download_queue: asyncio.Queue[
                tuple[models.EpisodeInfo, models.StreamInfo] | None
            ] = asyncio.Queue(maxsize=resolve_ahead + self.ctx.max_parallel)

            async def resolver() -> None:
                """Resolves streams ahead of the downloads.

                Looks up each episode's stream, retrying failures and timeouts,
                and queues the result for the download workers.
                """
                while not resolve_queue.empty():
                    raw_ep = await resolve_queue.get()
                    key = raw_ep.ep_str
                    variants = self.anime.get_all_variants(raw_ep.number)
                    ep = self.anime.get_variant(
                        raw_ep.number, self.ctx.audio_lang
                    )
                    if not ep:
                        ep = variants[0]

                    display_title = re.sub(
                        r"\s+\(?(?:dub|sub)\)?$", "",
                        ep.title or "Episode " + ep.ep_str, flags=re.I,
                    ).strip()

                    existing = await loop.run_in_executor(
                        None, self._find_existing, ep
                    )
                    if existing:
                        self._results[ep.session] = existing
                        dash.add_ep(
                            key, f"Ep {ep.ep_str} \u2014 {display_title}"
                        )
                        dash.mark_done(key, f"Ep {ep.ep_str} (already exists)")
                        resolve_queue.task_done()
                        continue

                    dash.mark_resolving(
                        key, f"Ep {ep.ep_str} \u2014 Resolving..."
                    )
                    max_attempts = self.ctx.auto_retry + 1
                    info: models.StreamInfo | None = None
                    for r_attempt in range(max_attempts):
                        try:
                            info = await asyncio.wait_for(
                                kwik.extract_stream(
                                    self.solver, ep.play_url, self.ctx.quality,
                                    self.ctx.audio_lang, self.ctx.cookie_string,
                                ),
                                timeout=120.0,
                            )
                            break
                        except TimeoutError:
                            is_last = r_attempt == max_attempts - 1
                            if is_last:
                                log.error(
                                    "Resolution timed out for Ep %s after %d "
                                    "attempts",
                                    ep.ep_str,
                                    max_attempts,
                                )
                                dash.mark_fail(
                                    key, f"Ep {ep.ep_str}: Resolution Timeout"
                                )
                                self._results[ep.session] = None
                            else:
                                log.warning(
                                    "Resolution timeout Ep %s (attempt %d/%d)",
                                    ep.ep_str, r_attempt + 1, max_attempts,
                                )
                                dash.mark_retry(
                                    key,
                                    f"Ep {ep.ep_str} retry "
                                    f"{r_attempt+1}/{max_attempts-1}",
                                )
                                await asyncio.sleep(2 * (2**r_attempt))
                        except Exception as exc:  # isolation point: keep going
                            msg = str(exc)
                            is_permanent = (
                                "No Kwik link" in msg or "No anime UUID" in msg
                            )
                            is_last = (
                                r_attempt == max_attempts - 1 or is_permanent
                            )
                            if is_last:
                                log.error(
                                    "Failed to resolve Ep %s: %s",
                                    ep.ep_str,
                                    exc,
                                )
                                dash.mark_fail(
                                    key, f"Ep {ep.ep_str}: {exc!s:.35}"
                                )
                                self._results[ep.session] = None
                                break
                            log.warning(
                                "Failed to resolve Ep %s (attempt %d/%d): %s "
                                "— retrying",
                                ep.ep_str,
                                r_attempt + 1,
                                max_attempts,
                                exc,
                            )
                            dash.mark_retry(
                                key,
                                f"Ep {ep.ep_str} retry "
                                f"{r_attempt+1}/{max_attempts-1}",
                            )
                            await asyncio.sleep(1.5 * (2**r_attempt))
                    if info is not None:
                        ep.audio = info.audio
                        if info.title and (not ep.title or ep.title == "?"):
                            ep.title = info.title
                        clean_label = re.sub(
                            r"\s+\(?(?:dub|sub)\)?$", "",
                            ep.title or f"Episode {ep.ep_str}", flags=re.I,
                        ).strip()
                        real_label = f"Ep {ep.ep_str} \u2014 {clean_label}"
                        dash.add_ep(key, real_label)
                        dash.mark_queued(key, real_label)
                        await download_queue.put((ep, info))
                    resolve_queue.task_done()

                for _ in range(self.ctx.max_parallel):
                    await download_queue.put(None)

            async def download_worker() -> None:
                """Downloads queued episodes until the queue is exhausted."""
                ep_dl = EpisodeDownloader(
                    self.ctx,
                    self.anime,
                    dash,
                    self.http,
                    self.solver,
                    aes_key_cache=shared_key_cache,
                )
                while True:
                    item = await download_queue.get()
                    if item is None:
                        break
                    ep, info = item
                    path = await ep_dl.run(ep, info)
                    self._results[ep.session] = path
                    download_queue.task_done()

            dash.start()
            try:
                tasks = [asyncio.create_task(resolver())]
                tasks.extend(
                    asyncio.create_task(download_worker())
                    for _ in range(self.ctx.max_parallel)
                )
                await asyncio.gather(*tasks)
                await asyncio.sleep(0.4)
            finally:
                dash.stop()

            # Check for still-failed after this batch attempt
            failed = [
                ep for ep in remaining if self._results.get(ep.session) is None
            ]
            if not failed:
                remaining = []
                break
            remaining = failed
            if batch_attempt == max_batch_attempts - 1:
                break

        self._print_summary(episodes, time.time() - start)
        return self._results

    def _print_summary(
        self,
        episodes: list[models.EpisodeInfo],
        elapsed: float,
    ) -> None:
        """Prints the results table after a batch.

        Args:
            episodes: The episodes that were requested.
            elapsed: The total time taken, in seconds.
        """

        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        time_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"
        ok = sum(1 for v in self._results.values() if v is not None)
        fail = len(self._results) - ok

        console.console.print()
        console.section("Download Complete")

        table = rich.table.Table(
            box=box.SIMPLE_HEAVY, show_header=True,
            header_style="brand.bold", border_style="dim",
        )
        table.add_column("Ep", style="brand", width=6, justify="right")
        table.add_column(
            "Title", style="bold white", ratio=1, overflow="ellipsis"
        )
        table.add_column("Audio", width=5)
        table.add_column("Status", justify="center", width=10)
        table.add_column("Size", justify="right", width=10, style="brand")
        table.add_column("File", style="dim", ratio=1, overflow="ellipsis")

        for ep in episodes:
            path = self._results.get(ep.session)
            badge = (
                "[bold green]\u2713  done[/bold green]"
                if path
                else "[red]\u2717 failed[/red]"
            )
            size = (
                utils.fmt_bytes(path.stat().st_size)
                if path and path.exists()
                else "\u2014"
            )
            fname = path.name if path else "\u2014"
            table.add_row(
                ep.ep_str,
                ep.title or "\u2014",
                utils.audio_badge(ep.audio),
                badge,
                size,
                fname,
            )

        console.console.print(table)
        status_line = f"[bold green]\u2713 {ok} completed[/bold green]"
        if fail:
            status_line += f"  [bold red]\u2717 {fail} failed[/bold red]"
        console.console.print(rich.panel.Panel(
            f"  {status_line}\n"
            f"  [dim]Time:[/dim]      [brand]{time_str}[/brand]\n"
            f"  [dim]Saved to:[/dim]  {self.ctx.output_dir}",
            border_style="green" if not fail else "yellow", box=box.ROUNDED,
        ))
