"""Cloudflare solver: the built-in browser, a fast direct path and TTL caching."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pahebatcher import browser_solver
from pahebatcher.cache import TTLCache

if TYPE_CHECKING:
    from pathlib import Path

    from pahebatcher.config_manager import ConfigManager

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
_BROWSER_BUDGET_MS = 100_000  # one resolve attempt gets 120 s upstream; leave headroom
_DIRECT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _host_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def _is_challenge_body(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


def _cookie_applies(cookie_domain: str, host: str) -> bool:
    d = cookie_domain.lstrip(".").lower()
    h = host.rsplit(":", 1)[0] if ":" in host else host  # netloc may carry a port
    return not d or h == d or h.endswith("." + d)


def _cookie_header(cookies: list[dict[str, str]]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


_DEFAULT_IMPERSONATE = "chrome120"


def _impersonate_for(user_agent: str) -> str:
    """Newest curl_cffi Chrome profile not newer than the UA's major version.

    cf_clearance is bound to the UA that earned it, and Cloudflare also compares the
    TLS fingerprint — so the direct fast path should look like that same browser.
    """
    m = re.search(r"Chrome/(\d+)", user_agent)
    if not m:
        return _DEFAULT_IMPERSONATE
    major = int(m.group(1))
    try:
        from curl_cffi.requests import BrowserType

        versions = sorted(
            int(b.value[6:]) for b in BrowserType if re.fullmatch(r"chrome\d+", b.value)
        )
    except Exception:
        return _DEFAULT_IMPERSONATE
    usable = [v for v in versions if v <= major]
    return f"chrome{usable[-1]}" if usable else _DEFAULT_IMPERSONATE


def _direct_fetch_sync(
    url: str, cookie_header: str, timeout: int,
    user_agent: str = _DIRECT_UA, impersonate: str = _DEFAULT_IMPERSONATE,
) -> tuple[int, str, list[dict[str, str]]] | None:
    """Direct fetch with clearance cookies. Returns (status, body, set-cookies).

    Returns None on transport errors (caller falls back to the browser path).
    Challenge detection is left to the caller, which owns the store.
    Never raises.
    """
    try:
        import curl_cffi.requests
    except ImportError:
        return None
    try:
        headers = {"User-Agent": user_agent}
        if cookie_header:
            headers["Cookie"] = cookie_header
        r: Any = curl_cffi.requests.get(url, impersonate=impersonate, headers=headers, timeout=timeout)
        text = bytes(r.content).decode("utf-8", errors="replace")
        jar: Any = r.cookies
        set_cookies = [{"name": str(k), "value": str(v)} for k, v in jar.items()]
        return int(r.status_code), text, set_cookies
    except Exception:
        return None


def no_solver_hint() -> str:
    """What to do when the built-in browser solver cannot start."""
    return browser_solver.browser_hint()


class SolverError(Exception):
    """Cloudflare solving failure."""


class Solver:
    def __init__(
        self,
        proxy: str | None = None,
        user_cookies: str = "",
        clearance_ttl: float = 1200.0,
        browser_mode: str = "auto",
        profile_dir: Path | None = None,
    ) -> None:
        self._browser_mode = browser_mode
        self._profile_dir = profile_dir
        self._engine_obj: browser_solver.BrowserEngine | None = None
        self._proxy = proxy
        self._sem = asyncio.Semaphore(1)
        self._lock = asyncio.Lock()
        self._solver_cache = TTLCache(ttl=120.0, max_size=256)
        self._user_cookies = self._parse_cookie_string(user_cookies)
        # Clearance store: live cf_clearance per domain, harvested from
        # successful responses. clearance_ttl <= 0 disables the store and
        # the direct fast path (legacy behavior: transplant always sent).
        self._clearance_ttl = clearance_ttl
        self._clearance = TTLCache(ttl=clearance_ttl, max_size=64)
        self._clearance_dead: set[str] = set()
        # UA that earned each host's harvested clearance (cf_clearance is UA-bound).
        self._clearance_ua: dict[str, str] = {}

    @classmethod
    def from_config(cls, cm: ConfigManager) -> Solver:
        """Build a solver from persisted settings plus the optional PAHEBATCHER_PROXY env var."""
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
        if self._engine_obj is None:
            profile = self._profile_dir
            if profile is None:
                from pahebatcher.ffmpeg_setup import app_dir

                profile = app_dir() / "browser-profile"
            self._engine_obj = browser_solver.BrowserEngine(profile, self._browser_mode, self._proxy)
        return self._engine_obj

    @staticmethod
    def _parse_cookie_string(s: str) -> list[dict[str, str]]:
        if not s:
            return []
        cookies: list[dict[str, str]] = []
        for part in s.split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                cookies.append({"name": name.strip(), "value": value.strip()})
        return cookies

    async def _clearance_for(self, host: str) -> tuple[list[dict[str, str]], str]:
        """Pick clearance cookies for host: transplant unless marked dead, else harvested.

        Returns (cookies, source) with source in {"user", "harvested", "none"}.
        Memory only — harvested values are never persisted to disk.
        """
        if self._user_cookies:
            async with self._lock:
                dead = host in self._clearance_dead
            if not dead:
                return list(self._user_cookies), "user"
        if self._clearance_ttl > 0:
            hit = await self._clearance.get(host)
            if isinstance(hit, list) and hit:
                return [dict(c) for c in hit if isinstance(c, dict)], "harvested"
        return [], "none"

    async def _note_clearance_success(
        self, host: str, cookies: list[Any], user_agent: str | None = None
    ) -> None:
        """Harvest cf_clearance from a successful response for later reuse."""
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
        """Drop clearance that just failed a challenge for host."""
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
        """Direct fetch shaped like a browser solution, or None to fall back."""
        loop = asyncio.get_running_loop()
        ua = self._clearance_ua.get(host, _DIRECT_UA) if source == "harvested" else _DIRECT_UA
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
        """Kept for symmetry with close(); the browser itself starts lazily in ping()."""

    async def close(self) -> None:
        if self._engine_obj is not None:
            await self._engine_obj.close()
            self._engine_obj = None

    async def ping(self) -> bool:
        """True when the built-in browser solver can start."""
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
        """Fetch a page, clearing Cloudflare if needed. ``max_timeout`` and ``wait`` are milliseconds."""
        if cache and (hit := await self._solver_cache.get(url)):
            return hit  # type: ignore[no-any-return]

        host = _host_of(url)
        # Fast path: direct fetch with live clearance (transplant or harvested).
        # Falls back to the browser transparently.
        if self._clearance_ttl > 0:
            fast_cookies, fast_source = await self._clearance_for(host)
            if fast_cookies:
                sol = await self._direct_request(url, host, fast_cookies, fast_source)
                if sol is not None:
                    if cache:
                        await self._solver_cache.set(url, sol)
                    return sol

        async with self._sem:
            return await self._browser_request(url, host, cache, max_timeout, wait)

    async def _browser_request(
        self, url: str, host: str, cache: bool, max_timeout: int, wait: int
    ) -> dict[str, Any] | None:
        sol = await self._engine().solve(url, timeout_ms=min(max_timeout, _BROWSER_BUDGET_MS), settle_ms=wait)
        if not sol:
            log.warning(
                "Built-in browser could not clear Cloudflare for %s.\n"
                "  Try browser_mode=headed (you can tick the checkbox yourself), or set "
                "PAHEBATCHER_PROXY to route through a proxy/VPN.", host,
            )
            return None
        # Persistent profile holds every domain's cookies; harvest only this host's.
        mine = [
            c for c in sol.get("cookies", [])
            if isinstance(c, dict) and _cookie_applies(str(c.get("domain", "")), host)
        ]
        await self._note_clearance_success(host, mine, str(sol.get("userAgent", "")) or None)
        if cache:
            await self._solver_cache.set(url, sol)
        return sol

    async def fetch_json(self, url: str) -> dict[str, Any] | None:
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

    async def fetch_html(self, url: str) -> tuple[str, list[dict[str, Any]]] | None:
        sol = await self.request(url)
        if not sol:
            return None
        resp = sol.get("response", "")
        cookies_val = sol.get("cookies", [])
        cookies_list: list[dict[str, Any]] = list(cookies_val) if isinstance(cookies_val, list) else []
        return str(resp), cookies_list
