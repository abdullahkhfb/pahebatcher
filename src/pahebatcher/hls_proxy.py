"""A local HLS proxy that lets FFmpeg play a stream it cannot fetch itself.

animepahe's CDN rejects plain clients (HTTP 403) and needs a Referer and
cookies. Everything is therefore fetched through the shared ``HttpClient``,
which handles the 403 fallback, and FFmpeg only ever talks to
``127.0.0.1``.

Compared with the proxy MPV uses in ``stream.py`` this one also:

* follows a master playlist to its media playlist,
* rewrites every URI to a short relative name, so there is no port
  placeholder to patch,
* keeps recently fetched segments in memory and prefetches the next few, so
  seeking and replaying do not download the same data twice.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import re
from typing import TYPE_CHECKING
import urllib.parse

from aiohttp import web

from pahebatcher.extract import m3u8

if TYPE_CHECKING:
    from pahebatcher import models
    import pahebatcher.http

log = logging.getLogger(__name__)

_URI_RE = re.compile(r'URI="([^"]+)"')
_EXTINF_RE = re.compile(r"#EXTINF:\s*([\d.]+)")
_SEGMENT_EXT_RE = re.compile(r"\.(ts|m4s|mp4|m4v|aac|m4a|mp3)$", re.I)
_PLAYLIST_NAME = "index.m3u8"


def apply_range(
    header: str | None, data: bytes
) -> tuple[int, bytes, dict[str, str]]:
    """Applies an HTTP ``Range`` header to a body.

    Only a single ``bytes=start-end`` range is supported, which is all
    FFmpeg ever asks for. Anything else gets the whole body.

    Args:
        header: The ``Range`` header value, or None.
        data: The full body.

    Returns:
        A tuple ``(status, body, extra_headers)``. The status is 206 for a
        satisfiable range, 416 for an unsatisfiable one and 200 otherwise.
    """
    if not header:
        return 200, data, {}
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or not (m.group(1) or m.group(2)):
        return 200, data, {}
    total = len(data)
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else total - 1
    else:  # suffix range: the last N bytes
        start = max(0, total - int(m.group(2)))
        end = total - 1
    end = min(end, total - 1)
    if start > end or start >= total:
        return 416, b"", {"Content-Range": f"bytes */{total}"}
    return (
        206,
        data[start : end + 1],
        {"Content-Range": f"bytes {start}-{end}/{total}"},
    )


class HlsProxy:
    """Serves one stream on ``127.0.0.1`` and fetches it through ``HttpClient``.

    Call ``start`` once, play ``url`` and call ``close`` when done. All
    methods must run on the event loop that owns the ``HttpClient``.

    Attributes:
        url: The local playlist URL. Empty until ``start`` has run.
        duration: The stream's length in seconds, summed from the playlist.
            0.0 if the playlist has no durations.
    """

    PREFETCH = 3
    CACHE_BYTES = 96 * 1024 * 1024

    def __init__(
        self, http: pahebatcher.http.HttpClient, info: models.StreamInfo
    ) -> None:
        """Initializes the proxy without starting a server.

        Args:
            http: The started HTTP client used for every remote fetch.
            info: The resolved stream.
        """
        self._http = http
        self._info = info
        self._remote: dict[str, str] = {}
        self._segments: list[str] = []
        self._position: dict[str, int] = {}
        self._cache: collections.OrderedDict[str, bytes] = (
            collections.OrderedDict()
        )
        self._cache_size = 0
        self._inflight: dict[str, asyncio.Task[bytes]] = {}
        self._playlist = ""
        self._runner: web.AppRunner | None = None
        self.url = ""
        self.duration = 0.0

    async def start(self) -> str:
        """Fetches the playlist and starts serving.

        Returns:
            The local playlist URL.

        Raises:
            RuntimeError: If the playlist has no segments.
            aiohttp.ClientError: If the playlist cannot be fetched.
        """
        base = self._info.url
        text = await m3u8.fetch_m3u8(self._http, base, self._info.headers)
        if "#EXT-X-STREAM-INF" in text:
            lines = text.splitlines()
            variants = [
                urllib.parse.urljoin(base, lines[i + 1].strip())
                for i, line in enumerate(lines)
                if line.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines)
            ]
            if variants:
                base = variants[-1]
                text = await m3u8.fetch_m3u8(
                    self._http, base, self._info.headers
                )
        self._playlist = self._rewrite(text, base)
        if not self._segments:
            raise RuntimeError("The stream's playlist has no segments.")

        app = web.Application()
        app.router.add_get(f"/{_PLAYLIST_NAME}", self._handle_playlist)
        app.router.add_route("*", "/{name}", self._handle_media)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = self._runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}/{_PLAYLIST_NAME}"
        return self.url

    async def close(self) -> None:
        """Stops the server and cancels any downloads in progress."""
        tasks = list(self._inflight.values())
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._cache.clear()
        self._cache_size = 0
        if self._runner is not None:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
            self._runner = None

    # ── playlist ─────────────────────────────────────────────────────────
    def _register(self, remote: str, ext: str, prefix: str) -> str:
        """Gives a remote URL a short local name.

        Args:
            remote: The absolute remote URL.
            ext: The file extension for the local name, with a dot.
            prefix: One letter that tells keys, maps and segments apart.

        Returns:
            The local name, such as ``s12.ts``.
        """
        for name, url in self._remote.items():
            if url == remote and name.startswith(prefix):
                return name
        name = f"{prefix}{len(self._remote)}{ext}"
        self._remote[name] = remote
        return name

    def _rewrite(self, text: str, base: str) -> str:
        """Rewrites a media playlist to point at this proxy.

        Fills in the segment list and the duration as a side effect.

        Args:
            text: The remote media playlist.
            base: The URL its relative URIs are resolved against.

        Returns:
            The playlist text to serve.
        """
        out: list[str] = []
        total = 0.0

        def local_uri(match: re.Match[str], prefix: str, ext: str) -> str:
            remote = urllib.parse.urljoin(base, match.group(1))
            return f'URI="{self._register(remote, ext, prefix)}"'

        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#EXT-X-KEY") and "URI=" in line:
                out.append(
                    _URI_RE.sub(lambda m: local_uri(m, "k", ".key"), line)
                )
            elif line.startswith("#EXT-X-MAP") and "URI=" in line:
                out.append(
                    _URI_RE.sub(lambda m: local_uri(m, "m", ".mp4"), line)
                )
            elif line and not line.startswith("#"):
                remote = urllib.parse.urljoin(base, line)
                path = urllib.parse.urlparse(remote).path
                ext_m = _SEGMENT_EXT_RE.search(path)
                ext = ext_m.group(0).lower() if ext_m else ".ts"
                name = f"s{len(self._segments)}{ext}"
                self._remote[name] = remote
                self._position[name] = len(self._segments)
                self._segments.append(name)
                out.append(name)
            else:
                if m := _EXTINF_RE.match(line):
                    total += float(m.group(1))
                out.append(line)
        self.duration = total
        return "\n".join(out) + "\n"

    # ── fetching ─────────────────────────────────────────────────────────
    def _store(self, name: str, data: bytes) -> None:
        """Adds a body to the cache, evicting the oldest ones if needed."""
        if name in self._cache:
            self._cache_size -= len(self._cache.pop(name))
        self._cache[name] = data
        self._cache_size += len(data)
        while self._cache_size > self.CACHE_BYTES and len(self._cache) > 1:
            _, old = self._cache.popitem(last=False)
            self._cache_size -= len(old)

    async def _download(self, name: str) -> bytes:
        """Downloads and caches one resource."""
        try:
            data = await self._http.get(
                self._remote[name], headers=self._info.headers, timeout=60
            )
            self._store(name, data)
            return data
        finally:
            self._inflight.pop(name, None)

    def _start_download(self, name: str) -> asyncio.Task[bytes]:
        """Starts, or joins, the download of a resource."""
        task = self._inflight.get(name)
        if task is None:
            task = asyncio.get_running_loop().create_task(self._download(name))
            self._inflight[name] = task
            # a prefetch nobody awaits must not log "never retrieved"
            task.add_done_callback(
                lambda t: t.cancelled() or t.exception()
            )
        return task

    async def _fetch(self, name: str) -> bytes:
        """Returns a resource from the cache or the network."""
        if (data := self._cache.get(name)) is not None:
            self._cache.move_to_end(name)
            return data
        return await asyncio.shield(self._start_download(name))

    def _prefetch_after(self, name: str) -> None:
        """Starts downloading the segments that follow ``name``."""
        i = self._position.get(name)
        if i is None:
            return
        for nxt in self._segments[i + 1 : i + 1 + self.PREFETCH]:
            if nxt not in self._cache:
                self._start_download(nxt)

    # ── handlers ─────────────────────────────────────────────────────────
    async def _handle_playlist(self, request: web.Request) -> web.Response:
        return web.Response(
            text=self._playlist,
            content_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_media(self, request: web.Request) -> web.Response:
        """Serves a segment, key or init section.

        Args:
            request: The incoming request.

        Returns:
            The body, or an error response.
        """
        name = request.match_info["name"]
        if name not in self._remote:
            return web.Response(status=404, text="Not found")
        try:
            data = await self._fetch(name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # isolation point: report, keep serving
            log.warning("HLS proxy could not fetch %s: %s", name, exc)
            return web.Response(status=502, text=f"Fetch failed: {exc}")
        self._prefetch_after(name)

        if name.endswith(".key"):
            ctype = "application/octet-stream"
        elif name.endswith((".mp4", ".m4s", ".m4v")):
            ctype = "video/mp4"
        else:
            ctype = "video/MP2T"
        status, body, extra = apply_range(request.headers.get("Range"), data)
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
        headers.update(extra)
        if request.method == "HEAD":
            headers["Content-Length"] = str(len(body))
            return web.Response(status=status, headers=headers)
        return web.Response(
            status=status, body=body, content_type=ctype, headers=headers
        )
