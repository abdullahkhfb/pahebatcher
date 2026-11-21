"""Async HTTP client with a shared aiohttp session and retries."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import aiohttp

from pahebatcher import config, tls

log = logging.getLogger(__name__)

_curl_local = threading.local()


def _get_curl_session() -> Any:
    """Returns the calling thread's curl_cffi session.

    The session is created on first use and kept per thread.
    """
    sess: Any = getattr(_curl_local, "session", None)
    if sess is None:
        import curl_cffi.requests

        sess = curl_cffi.requests.Session()
        _curl_local.session = sess
    return sess


def _curl_fetch(
    url: str,
    headers: dict[str, str] | None,
    timeout: int,
) -> bytes:
    """Fetches a URL with curl_cffi, impersonating Chrome.

    This is a blocking fallback for when aiohttp is rejected with HTTP 403.
    It makes up to five attempts, one second apart.

    Args:
        url: The URL to fetch.
        headers: Request headers, or None.
        timeout: Request timeout in seconds.

    Returns:
        The response body.

    Raises:
        curl_cffi.requests.exceptions.Timeout: If the final attempt timed
            out.
        Exception: Whatever error the final attempt raised.
    """
    import time as _time

    import curl_cffi.requests

    sess: Any = _get_curl_session()
    for attempt in range(5):
        try:
            r: Any = sess.get(
                url, impersonate="chrome120", headers=headers, timeout=timeout
            )
            r.raise_for_status()
            return bytes(r.content)
        except curl_cffi.requests.exceptions.Timeout:
            if attempt < 4:
                log.debug(
                    "curl_cffi timeout on %s — retry %d/5", url, attempt + 1
                )
                _time.sleep(1)
                continue
            raise
        except Exception:
            if attempt < 4:
                _time.sleep(1)
                continue
            raise
    raise RuntimeError("curl fetch failed after retries")


class HttpClient:
    """Async HTTP client sharing one aiohttp session.

    Requests are retried with exponential backoff. An HTTP 403 response is
    retried once through curl_cffi, which impersonates a real browser.
    Call ``start`` before the first request and ``close`` when done.
    """

    def __init__(self, hls_workers: int = 24) -> None:
        """Initializes the client without opening any connections.

        Args:
            hls_workers: Maximum concurrent connections per host.
        """
        self._hls_workers = hls_workers
        self._session: aiohttp.ClientSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        """Opens the shared aiohttp session on the running event loop."""
        self._loop = asyncio.get_running_loop()
        connector = aiohttp.TCPConnector(
            limit=0,
            limit_per_host=self._hls_workers,
            ttl_dns_cache=300,
            keepalive_timeout=45,
            enable_cleanup_closed=True,
            ssl=tls.make_ssl_ctx(),
            use_dns_cache=True,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=45),
            connector_owner=True,
        )

    async def close(self) -> None:
        """Closes the shared session if it is open."""
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        """The shared aiohttp session.

        Raises:
            RuntimeError: If ``start`` has not been called.
        """
        if not self._session:
            raise RuntimeError("HttpClient not started — call start() first")
        return self._session

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> bytes:
        """Fetches a URL and returns its body.

        Network errors and timeouts are retried up to ``RETRY_ATTEMPTS`` times
        with exponential backoff. An HTTP 403 triggers a curl_cffi fallback
        instead of a retry.

        Args:
            url: The URL to fetch.
            headers: Request headers, or None.
            timeout: Total request timeout in seconds.

        Returns:
            The response body.

        Raises:
            aiohttp.ClientResponseError: On an HTTP error status after retries.
            aiohttp.ClientError: On a network error after retries.
            TimeoutError: If the final attempt timed out.
            OSError: On an operating system error after retries.
        """
        for attempt in range(config.RETRY_ATTEMPTS):
            try:
                async with self.session.get(
                    url, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    resp.raise_for_status()
                    return await resp.read()
            except aiohttp.ClientResponseError as exc:
                if exc.status == 403 and self._loop:
                    log.debug("aiohttp 403 on %s — curl_cffi fallback", url)
                    return await self._loop.run_in_executor(
                        None, _curl_fetch, url, headers, timeout
                    )
                if attempt == config.RETRY_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(config.RETRY_BASE_DELAY * (2**attempt))
            except (TimeoutError, aiohttp.ClientError, OSError):
                if attempt == config.RETRY_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(config.RETRY_BASE_DELAY * (2**attempt))
        raise AssertionError("unreachable")
