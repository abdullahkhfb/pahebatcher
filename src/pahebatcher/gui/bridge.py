"""Runs the asyncio backend on a worker thread and feeds Tk safely."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import concurrent.futures as cf
import os
import pathlib
import queue
import threading
from typing import Any

from pahebatcher import config_manager, ffmpeg_setup, models

Callback = Callable[[Any], None]


class Bridge:
    """Runs coroutines on a private event loop for the Tk interface.

    Backend coroutines run on a worker thread. Their callbacks are queued
    and run on the Tk thread when ``drain`` is called.
    """

    def __init__(self) -> None:
        """Starts the worker thread and its event loop."""
        self.loop = asyncio.new_event_loop()
        self._q: queue.Queue[Callable[[], None]] = queue.Queue()
        self._pool = cf.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="pb-work"
        )
        threading.Thread(
            target=self.loop.run_forever, daemon=True, name="pb-loop"
        ).start()

    # ── thread-safe hand-off to Tk ───────────────────────────────────────
    def post(self, fn: Callable[[], None]) -> None:
        self._q.put(fn)

    def drain(self) -> None:
        """Runs the queued callbacks.

        Call this periodically from Tk's ``after`` loop.
        """
        while True:
            try:
                fn = self._q.get_nowait()
            except queue.Empty:
                return
            fn()

    def _finish(
        self,
        fut: cf.Future[Any],
        ok: Callback | None,
        err: Callback | None,
    ) -> None:
        """Queues the right callback once a future completes.

        Args:
            fut: The completed future.
            ok: Called with the result on success, or None.
            err: Called with the exception on failure, or None.
        """
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            if err:
                self.post(lambda: err(exc))
        elif ok:
            res = fut.result()
            self.post(lambda: ok(res))

    def run(
        self,
        coro: Awaitable[Any],
        ok: Callback | None = None,
        err: Callback | None = None,
    ) -> cf.Future[Any]:
        """Runs a coroutine on the backend loop.

        Args:
            coro: The coroutine to run.
            ok: Called on the Tk thread with the result, or None.
            err: Called on the Tk thread with the exception, or None.

        Returns:
            A future for the result.
        """
        fut: cf.Future[Any] = asyncio.run_coroutine_threadsafe(coro, self.loop)  # type: ignore[arg-type]
        fut.add_done_callback(lambda f: self._finish(f, ok, err))
        return fut

    def thread(
        self,
        fn: Callable[[], Any],
        ok: Callback | None = None,
        err: Callback | None = None,
    ) -> cf.Future[Any]:
        """Runs a blocking function on a worker thread.

        Args:
            fn: The function to run.
            ok: Called on the Tk thread with the result, or None.
            err: Called on the Tk thread with the exception, or None.

        Returns:
            A future for the result.
        """
        fut = self._pool.submit(fn)
        fut.add_done_callback(lambda f: self._finish(f, ok, err))
        return fut

    def shutdown(self) -> None:
        """Cancels leftover tasks and stops the loop.

        Leftover tasks include, for example, the browser solver's driver.
        """

        async def _cancel_all() -> None:
            current = asyncio.current_task()
            tasks = [t for t in asyncio.all_tasks() if t is not current]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_cancel_all(), self.loop).result(
                timeout=3
            )
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._pool.shutdown(wait=False, cancel_futures=True)


def load_settings() -> config_manager.ConfigManager:
    """Loads the saved settings.

    Returns:
        The loaded configuration manager.
    """
    path = ffmpeg_setup.app_dir() / "gui.toml"
    cfg = config_manager.ConfigManager(path)
    first_run = not path.exists()
    cfg.load()
    if first_run:
        cfg.data["output_dir"] = str(
            pathlib.Path.home() / "Downloads" / "pahebatcher"
        )
        cfg.data["max_parallel"] = 2
    return cfg


def save_settings(cfg: config_manager.ConfigManager) -> None:
    """Saves the settings.

    Args:
        cfg: The configuration manager to save.
    """
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    cfg.save()


class Backend:
    """Owns the Cloudflare solver (built-in browser) and the HTTP client."""

    def __init__(self, cfg: config_manager.ConfigManager) -> None:
        """Initializes the backend without starting anything.

        Args:
            cfg: The settings to use.
        """
        self.cfg = cfg
        self.solver: Any = None
        self.http: Any = None
        self.host = "animepahe.pw"
        self.current_dash: Any = None

    async def start(self) -> None:
        """Starts the HTTP client and the solver."""
        from pahebatcher import http, solver

        self.solver = solver.Solver.from_config(self.cfg)
        await self.solver.start()
        self.http = http.HttpClient(int(self.cfg.get("hls_workers")))
        await self.http.start()

    async def ping(self) -> bool:
        return bool(await self.solver.ping())

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Searches animepahe.

        Args:
            query: The search text.

        Returns:
            The raw search results.
        """
        import pahebatcher.extract.scanner

        res = await pahebatcher.extract.scanner.AnimePaheScanner.search(
            self.solver, self.host, query
        )
        self.host = (
            getattr(
                pahebatcher.extract.scanner.AnimePaheScanner,
                "_current_host",
                self.host,
            )
            or self.host
        )
        return res

    async def scan(self, session: str) -> models.AnimeInfo:
        """Scans a series for its episodes.

        Args:
            session: The series' session identifier.

        Returns:
            The series with its episodes.
        """
        import pahebatcher.extract.scanner

        scanner = pahebatcher.extract.scanner.AnimePaheScanner(
            self.solver, self.host, session
        )
        return await scanner.scan(
            ffmpeg_setup.app_dir() / "cache",
            prefer_audio=str(self.cfg.get("audio_lang")),
            cache_ttl=int(self.cfg.get("cache_ttl")),
        )

    def context(self, title: str) -> models.AppContext:
        """Builds download settings from the saved configuration.

        Args:
            title: The series title, used to name the output directory.

        Returns:
            The settings for one download run.
        """
        from pahebatcher import utils

        c = self.cfg
        return models.AppContext(
            output_dir=os.path.join(
                str(c.get("output_dir")), utils.sanitize(title)
            ),
            cache_dir=ffmpeg_setup.app_dir() / "cache",
            quality=int(c.get("quality")),
            audio_lang=str(c.get("audio_lang")),
            max_parallel=int(c.get("max_parallel")),
            hls_workers=int(c.get("hls_workers")),
            keep_temp=bool(c.get("keep_temp")),
            list_only=False,
            resolve_ahead=int(c.get("resolve_ahead")),
            cache_ttl=int(c.get("cache_ttl")),
            cookie_string=str(c.get("cookie_string")),
            auto_retry=int(c.get("auto_retry")),
        )

    async def download(
        self,
        anime: models.AnimeInfo,
        episodes: list[models.EpisodeInfo],
    ) -> dict[str, pathlib.Path | None]:
        """Downloads episodes.

        Args:
            anime: The series.
            episodes: The episodes to download.

        Returns:
            A dict mapping each episode's session to the downloaded file, or to
            None if it failed.
        """
        from pahebatcher import downloader

        ctx = self.context(anime.title)
        pathlib.Path(ctx.output_dir).mkdir(parents=True, exist_ok=True)
        return await downloader.BatchOrchestrator(
            ctx, anime, self.http, self.solver
        ).download(episodes)

    async def close(self) -> None:
        """Closes the solver and the HTTP client."""
        for obj in (self.http, self.solver):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
