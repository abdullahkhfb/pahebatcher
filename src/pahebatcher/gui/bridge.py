"""Runs the asyncio backend on a worker thread and hands results to Tk safely."""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import os
import queue
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pahebatcher.config_manager import ConfigManager
from pahebatcher.ffmpeg_setup import app_dir
from pahebatcher.models import AnimeInfo, AppContext, EpisodeInfo

Callback = Callable[[Any], None]


class Bridge:
    """Backend coroutines run on a private event loop; callbacks land on the Tk thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._q: queue.Queue[Callable[[], None]] = queue.Queue()
        self._pool = cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="pb-work")
        threading.Thread(target=self.loop.run_forever, daemon=True, name="pb-loop").start()

    # ── thread-safe hand-off to Tk ───────────────────────────────────────
    def post(self, fn: Callable[[], None]) -> None:
        self._q.put(fn)

    def drain(self) -> None:
        """Call from Tk's ``after`` loop."""
        while True:
            try:
                fn = self._q.get_nowait()
            except queue.Empty:
                return
            fn()

    def _finish(self, fut: cf.Future[Any], ok: Callback | None, err: Callback | None) -> None:
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
        self, coro: Awaitable[Any], ok: Callback | None = None, err: Callback | None = None,
    ) -> cf.Future[Any]:
        fut: cf.Future[Any] = asyncio.run_coroutine_threadsafe(coro, self.loop)  # type: ignore[arg-type]
        fut.add_done_callback(lambda f: self._finish(f, ok, err))
        return fut

    def thread(
        self, fn: Callable[[], Any], ok: Callback | None = None, err: Callback | None = None,
    ) -> cf.Future[Any]:
        fut = self._pool.submit(fn)
        fut.add_done_callback(lambda f: self._finish(f, ok, err))
        return fut

    def shutdown(self) -> None:
        """Cancel leftover tasks (e.g. the browser solver's driver) before stopping the loop."""

        async def _cancel_all() -> None:
            current = asyncio.current_task()
            tasks = [t for t in asyncio.all_tasks() if t is not current]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_cancel_all(), self.loop).result(timeout=3)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._pool.shutdown(wait=False, cancel_futures=True)


def load_settings() -> ConfigManager:
    path = app_dir() / "gui.toml"
    cfg = ConfigManager(path)
    first_run = not path.exists()
    cfg.load()
    if first_run:
        cfg.data["output_dir"] = str(Path.home() / "Downloads" / "pahebatcher")
        cfg.data["max_parallel"] = 2
    return cfg


def save_settings(cfg: ConfigManager) -> None:
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    cfg.save()


class Backend:
    """Owns the Cloudflare solver (built-in browser) and the HTTP client."""

    def __init__(self, cfg: ConfigManager) -> None:
        self.cfg = cfg
        self.solver: Any = None
        self.http: Any = None
        self.host = "animepahe.pw"
        self.current_dash: Any = None

    async def start(self) -> None:
        from pahebatcher.http import HttpClient
        from pahebatcher.solver import Solver

        self.solver = Solver.from_config(self.cfg)
        await self.solver.start()
        self.http = HttpClient(int(self.cfg.get("hls_workers")))
        await self.http.start()

    async def ping(self) -> bool:
        return bool(await self.solver.ping())

    async def search(self, query: str) -> list[dict[str, Any]]:
        from pahebatcher.extract.scanner import AnimePaheScanner

        res = await AnimePaheScanner.search(self.solver, self.host, query)
        self.host = getattr(AnimePaheScanner, "_current_host", self.host) or self.host
        return res

    async def scan(self, session: str) -> AnimeInfo:
        from pahebatcher.extract.scanner import AnimePaheScanner

        scanner = AnimePaheScanner(self.solver, self.host, session)
        return await scanner.scan(
            app_dir() / "cache", prefer_audio=str(self.cfg.get("audio_lang")),
            cache_ttl=int(self.cfg.get("cache_ttl")),
        )

    def context(self, title: str) -> AppContext:
        from pahebatcher.utils import sanitize

        c = self.cfg
        return AppContext(
            output_dir=os.path.join(str(c.get("output_dir")), sanitize(title)),
            cache_dir=app_dir() / "cache",
            quality=int(c.get("quality")), audio_lang=str(c.get("audio_lang")),
            max_parallel=int(c.get("max_parallel")), hls_workers=int(c.get("hls_workers")),
            keep_temp=bool(c.get("keep_temp")), list_only=False,
            resolve_ahead=int(c.get("resolve_ahead")), cache_ttl=int(c.get("cache_ttl")),
            cookie_string=str(c.get("cookie_string")), auto_retry=int(c.get("auto_retry")),
        )

    async def download(self, anime: AnimeInfo, episodes: list[EpisodeInfo]) -> dict[str, Path | None]:
        from pahebatcher.downloader import BatchOrchestrator

        ctx = self.context(anime.title)
        Path(ctx.output_dir).mkdir(parents=True, exist_ok=True)
        return await BatchOrchestrator(ctx, anime, self.http, self.solver).download(episodes)

    async def close(self) -> None:
        for obj in (self.http, self.solver):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
