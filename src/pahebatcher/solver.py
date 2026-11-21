"""Cloudflare solver: built-in browser, direct fast path and caching."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any
import urllib.parse

from pahebatcher import browser_solver
import pahebatcher.cache

if TYPE_CHECKING:
    import pathlib

    from pahebatcher import config_manager

log = logging.getLogger(__name__)

_CHALLENGE_MARKERS = (
    "just a moment",
    "__cf_chl",
    "cf-chl",
    "checking your browser",
    "attention required",
    "verify you are human",
)

_DIRECT_TIMEOUT = 15
# one resolve attempt gets 120 s upstream; leave headroom
_BROWSER_BUDGET_MS = 100_000
_DIRECT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _host_of(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def _is_challenge_body(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


def _cookie_applies(cookie_domain: str, host: str) -> bool:
    d = cookie_domain.lstrip(".").lower()
    # netloc may carry a port
    h = host.rsplit(":", 1)[0] if ":" in host else host
    return not d or h == d or h.endswith("." + d)


def _cookie_header(cookies: list[dict[str, str]]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


_DEFAULT_IMPERSONATE = "chrome120"


def _impersonate_for(user_agent: str) -> str:
    """Picks the curl_cffi Chrome profile matching a user agent.

    cf_clearance is bound to the user agent that earned it, and Cloudflare
    also compares the TLS fingerprint, so the direct fast path should look
    like that same browser.

    Args:
        user_agent: The user agent string of the browser that solved the
            challenge.

    Returns:
        The newest curl_cffi Chrome profile that is not newer than the user
        agent's major version, or the default profile if none matches.
    """
    m = re.search(r"Chrome/(\d+)", user_agent)
    if not m:
        return _DEFAULT_IMPERSONATE
    major = int(m.group(1))
    try:
        import curl_cffi.requests

        versions = sorted(
            int(b.value[6:])
            for b in curl_cffi.requests.BrowserType
            if re.fullmatch(r"chrome\d+", b.value)
        )
    except Exception:
        return _DEFAULT_IMPERSONATE
    usable = [v for v in versions if v <= major]
    return f"chrome{usable[-1]}" if usable else _DEFAULT_IMPERSONATE


def _direct_fetch_sync(
    url: str, cookie_header: str, timeout: int,
    user_agent: str = _DIRECT_UA, impersonate: str = _DEFAULT_IMPERSONATE,
) -> tuple[int, str, list[dict[str, str]]] | None:
    """Fetches a URL directly, sending clearance cookies.

    Blocking, so call it from an executor. Never raises. Challenge detection
    is left to the caller, which owns the store.

    Args:
        url: The URL to fetch.
        cookie_header: The Cookie header value to send.
        timeout: Request timeout in seconds.
        user_agent: The User-Agent header to send.
        impersonate: The curl_cffi browser profile to impersonate.

    Returns:
        A tuple ``(status, body, set_cookies)``, or None on a transport
        error so the caller can fall back to the browser path.
    """
    try:
        import curl_cffi.requests
    except ImportError:
        return None
    try:
        headers = {"User-Agent": user_agent}
        if cookie_header:
            headers["Cookie"] = cookie_header
        r: Any = curl_cffi.requests.get(
            url, impersonate=impersonate, headers=headers, timeout=timeout
        )
        text = bytes(r.content).decode("utf-8", errors="replace")
        jar: Any = r.cookies
        set_cookies = [
            {"name": str(k), "value": str(v)} for k, v in jar.items()
        ]
        return int(r.status_code), text, set_cookies
    except Exception:
        return None


def no_solver_hint() -> str:
    """Returns advice for when the built-in browser solver cannot start."""
    return browser_solver.browser_hint()


class SolverError(Exception):
    """Raised when solving a Cloudflare challenge fails."""


class Solver:
    """Fetches Cloudflare-protected pages.

    Uses a fast direct request when clearance cookies are available and
    falls back to the built-in browser otherwise. Results are cached.
    """

    def __init__(
        self,
        proxy: str | None = None,
        user_cookies: str = "",
        clearance_ttl: float = 1200.0,
        browser_mode: str = "auto",
        profile_dir: pathlib.Path | None = None,
    ) -> None:
        """Initializes the solver. The browser starts lazily on first use.

        Args:
            proxy: A proxy URL to route traffic through, or None.
            user_cookies: Cookies supplied by the user, as a
                ``name=value; ...`` string.
            clearance_ttl: Seconds that harvested clearance stays usable. 0
                disables the direct fast path.
            browser_mode: ``"auto"``, ``"headless"`` or ``"headed"``.
            profile_dir: The browser profile directory, or None for the
                default.
        """
        self._browser_mode = browser_mode
        self._profile_dir = profile_dir
        self._engine_obj: browser_solver.BrowserEngine | None = None
        self._proxy = proxy
        self._sem = asyncio.Semaphore(1)
        self._lock = asyncio.Lock()
        self._solver_cache = pahebatcher.cache.TTLCache(ttl=120.0, max_size=256)
        self._user_cookies = self._parse_cookie_string(user_cookies)
        # Clearance store: live cf_clearance per domain, harvested from
        # successful responses. clearance_ttl <= 0 disables the store and
        # the direct fast path (legacy behavior: transplant always sent).
        self._clearance_ttl = clearance_ttl
        self._clearance = pahebatcher.cache.TTLCache(
            ttl=clearance_ttl, max_size=64
        )
        self._clearance_dead: set[str] = set()
        # UA that earned each host's harvested clearance (cf_clearance is
        # UA-bound).
        self._clearance_ua: dict[str, str] = {}

    @classmethod
    def from_config(cls, cm: config_manager.ConfigManager) -> Solver:
        """Builds a solver from the saved settings.

        The ``PAHEBATCHER_PROXY`` environment variable, if set, supplies the
        proxy.

        Args:
            cm: The loaded configuration manager.

        Returns:
            The new solver.
        """
        return cls(
            proxy=os.getenv("PAHEBATCHER_PROXY") or None,
            user_cookies=str(cm.get("cookie_string")),
            clearance_ttl=int(cm.get("clearance_ttl")) * 60,
            browser_mode=str(cm.get("browser_mode")),
        )

    @property
    def backend_name(self) -> str:
        return "built-in browser"

    def _engine(self) -> browser_solver.BrowserEngine:
        """Returns the browser engine, creating it on first use."""
        if self._engine_obj is None:
            profile = self._profile_dir
            if profile is None:
                from pahebatcher import ffmpeg_setup

                profile = ffmpeg_setup.app_dir() / "browser-profile"
            self._engine_obj = browser_solver.BrowserEngine(
                profile, self._browser_mode, self._proxy
            )
        return self._engine_obj

    @staticmethod
    def _parse_cookie_string(s: str) -> list[dict[str, str]]:
        """Parses a ``name=value; ...`` string into cookie dicts.

        Args:
            s: The cookie string.

        Returns:
            One dict with ``name`` and ``value`` per cookie.
        """
        if not s:
            return []
        cookies: list[dict[str, str]] = []
        for part in s.split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                cookies.append({"name": name.strip(), "value": value.strip()})
        return cookies

    async def _clearance_for(
        self,
        host: str,
    ) -> tuple[list[dict[str, str]], str]:
        """Picks clearance cookies for a host.

        User-supplied cookies are used unless marked dead; otherwise cookies
        harvested from earlier responses are used. Memory only: harvested
        values are never persisted to disk.

        Args:
            host: The host the request is for.

        Returns:
            A tuple ``(cookies, source)`` where source is ``"user"``,
            ``"harvested"`` or ``"none"``.
        """
        if self._user_cookies:
            async with self._lock:
                dead = host in self._clearance_dead
            if not dead:
                return list(self._user_cookies), "user"
        if self._clearance_ttl > 0:
            hit = await self._clearance.get(host)
            if isinstance(hit, list) and hit:
                return (
                    [dict(c) for c in hit if isinstance(c, dict)], "harvested"
                )
        return [], "none"

    async def _note_clearance_success(
        self, host: str, cookies: list[Any], user_agent: str | None = None
    ) -> None:
        """Harvests cf_clearance from a successful response for later reuse.

        Args:
            host: The host that answered.
            cookies: The response's cookies.
            user_agent: The user agent that was used, if known.
        """
        if self._clearance_ttl <= 0:
            return
        found: list[dict[str, str]] = []
        for c in cookies:
            if not isinstance(c, dict):
                continue
            name, value = str(c.get("name", "")), c.get("value", "")
            if name == "cf_clearance" and value:
                found.append({"name": name, "value": str(value)})
        if found:
            await self._clearance.set(host, found)
            if user_agent:
                self._clearance_ua[host] = user_agent

    async def _note_clearance_dead(self, host: str, source: str) -> None:
        """Drops clearance that just failed a challenge.

        Args:
            host: The host that rejected the clearance.
            source: Where the clearance came from, ``"user"`` or
                ``"harvested"``.
        """
        if self._clearance_ttl <= 0:
            return
        await self._clearance.discard(host)
        self._clearance_ua.pop(host, None)
        if source == "user":
            async with self._lock:
                self._clearance_dead.add(host)

    async def _direct_request(
        self, url: str, host: str, cookies: list[dict[str, str]], source: str
    ) -> dict[str, Any] | None:
        """Fetches a URL directly, shaped like a browser solution.

        Args:
            url: The URL to fetch.
            host: The URL's host.
            cookies: The clearance cookies to send.
            source: Where the cookies came from.

        Returns:
            A dict with ``response``, ``cookies`` and ``userAgent``, or None to
            fall back to the browser.
        """
        loop = asyncio.get_running_loop()
        ua = (
            self._clearance_ua.get(host, _DIRECT_UA)
            if source == "harvested"
            else _DIRECT_UA
        )
        fetched = await loop.run_in_executor(
            None,
            lambda: _direct_fetch_sync(
                url, _cookie_header(cookies), _DIRECT_TIMEOUT,
                user_agent=ua, impersonate=_impersonate_for(ua),
            ),
        )
        if fetched is None:
            return None  # transport error or no curl — transient, keep store
        status, text, set_cookies = fetched
        if _is_challenge_body(text):
            await self._note_clearance_dead(host, source)
            return None
        if status != 200:
            return None  # e.g. rate limit — transient, keep store
        await self._note_clearance_success(host, set_cookies)
        return {"response": text, "cookies": set_cookies, "userAgent": ua}

    async def start(self) -> None:
        """Does nothing; kept for symmetry with ``close``.

        The browser itself starts lazily in ``ping``.
        """

    async def close(self) -> None:
        """Closes the browser if it was started."""
        if self._engine_obj is not None:
            await self._engine_obj.close()
            self._engine_obj = None

    async def ping(self) -> bool:
        """Checks whether the built-in browser solver can start.

        Returns:
            True if it can.
        """
        if not browser_solver.available():
            return False
        try:
            await self._engine().start()
        except browser_solver.BrowserSolverError as exc:
            log.warning("Built-in browser unavailable: %s", exc)
            return False
        return True

    async def request(
        self, url: str, cache: bool = True,
        max_timeout: int = 120000, wait: int = 2000,
    ) -> dict[str, Any] | None:
        """Fetches a page, clearing Cloudflare if needed.

        Tries a direct request with live clearance first and falls back to the
        browser.

        Args:
            url: The URL to fetch.
            cache: Whether to use and fill the response cache.
            max_timeout: Maximum time to wait for the browser, in milliseconds.
            wait: Time to let the page settle, in milliseconds.

        Returns:
            A dict with ``response``, ``cookies`` and ``userAgent``, or None if
            the page could not be fetched.
        """
        if cache and (hit := await self._solver_cache.get(url)):
            return hit  # type: ignore[no-any-return]

        host = _host_of(url)
        # Fast path: direct fetch with live clearance (transplant or harvested).
        # Falls back to the browser transparently.
        if self._clearance_ttl > 0:
            fast_cookies, fast_source = await self._clearance_for(host)
            if fast_cookies:
                sol = await self._direct_request(
                    url, host, fast_cookies, fast_source
                )
                if sol is not None:
                    if cache:
                        await self._solver_cache.set(url, sol)
                    return sol

        async with self._sem:
            return await self._browser_request(
                url, host, cache, max_timeout, wait
            )

    async def _browser_request(
        self, url: str, host: str, cache: bool, max_timeout: int, wait: int
    ) -> dict[str, Any] | None:
        """Fetches a page with the browser.

        Args:
            url: The URL to fetch.
            host: The URL's host.
            cache: Whether to cache the solution.
            max_timeout: Maximum time to wait, in milliseconds.
            wait: Time to let the page settle, in milliseconds.

        Returns:
            The solution dict, or None on failure.
        """
        sol = await self._engine().solve(
            url, timeout_ms=min(max_timeout, _BROWSER_BUDGET_MS), settle_ms=wait
        )
        if not sol:
            log.warning(
                "Built-in browser could not clear Cloudflare for %s.\n"
                "  Try browser_mode=headed (you can tick the checkbox "
                "yourself), or set "
                "PAHEBATCHER_PROXY to route through a proxy/VPN.",
                host,
            )
            return None
        # Persistent profile holds every domain's cookies; harvest only this
        # host's.
        mine = [
            c
            for c in sol.get("cookies", [])
            if isinstance(c, dict) and _cookie_applies(
                str(c.get("domain", "")), host
            )
        ]
        await self._note_clearance_success(
            host, mine, str(sol.get("userAgent", "")) or None
        )
        if cache:
            await self._solver_cache.set(url, sol)
        return sol

    async def fetch_json(self, url: str) -> dict[str, Any] | None:
        """Fetches a URL and decodes its JSON body.

        Browsers render JSON inside a ``<pre>`` element, so the text is taken
        from there and HTML-unescaped.

        Args:
            url: The URL to fetch.

        Returns:
            The decoded JSON object, or None if the fetch failed or the body is
            not a JSON object.
        """
        sol = await self.request(url)
        body = (sol or {}).get("response", "")
        if not body:
            return None
        for m in re.finditer(r"<pre[^>]*>([\s\S]*?)</pre>", body, re.I):
            txt = (
                m.group(1)
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&#39;", "'")
                .strip()
            )
            with contextlib.suppress(json.JSONDecodeError):
                parsed: Any = json.loads(txt)
                return parsed if isinstance(parsed, dict) else None
        with contextlib.suppress(json.JSONDecodeError):
            parsed2: Any = json.loads(re.sub(r"<[^>]+>", "", body).strip())
            return parsed2 if isinstance(parsed2, dict) else None
        start = body.find("{")
        if start >= 0:
            depth = in_str = escape = 0
            for i, ch in enumerate(body[start:], start):
                if escape:
                    escape = 0
                    continue
                if ch == "\\" and in_str:
                    escape = 1
                    continue
                if ch == '"':
                    in_str ^= 1
                    continue
                if in_str:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        with contextlib.suppress(json.JSONDecodeError):
                            parsed3: Any = json.loads(body[start : i + 1])
                            if isinstance(parsed3, dict):
                                return parsed3
                        break
        return None

    async def fetch_html(
        self,
        url: str,
    ) -> tuple[str, list[dict[str, Any]]] | None:
        """Fetches a page's HTML.

        Args:
            url: The URL to fetch.

        Returns:
            A tuple ``(html, cookies)``, or None if the fetch failed.
        """
        sol = await self.request(url)
        if not sol:
            return None
        resp = sol.get("response", "")
        cookies_val = sol.get("cookies", [])
        cookies_list: list[dict[str, Any]] = (
            list(cookies_val) if isinstance(cookies_val, list) else []
        )
        return str(resp), cookies_list
