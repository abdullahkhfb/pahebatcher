"""Built-in browser solver — a real local browser (Chrome or Edge) that clears Cloudflare.

Returns solutions shaped as ``response``, ``cookies``, ``userAgent`` so
:class:`pahebatcher.solver.Solver` can use either backend interchangeably.

Optional dependency (the control library only — no browser is downloaded or bundled)::

    pip install "pahebatcher[browser]"

Requires Google Chrome or Microsoft Edge on the machine (Edge ships with Windows 10/11).
Browser choice, best first:

1. Your installed Chrome or Edge, started as a normal process (no automation flags) with a
   dedicated profile and controlled over the DevTools port. Looks like an ordinary browser.
   Set ``PAHEBATCHER_BROWSER`` to an executable path to pick a specific one.
2. The same Chrome/Edge launched through Playwright.

``mode`` controls window behaviour:

* ``auto``     — headless first; if the challenge doesn't clear, retry once with a visible
                 window (where an interactive checkbox can also be clicked by hand).
* ``headless`` — never show a window.
* ``headed``   — always show a window.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Same markers as solver.py (duplicated to avoid a circular import).
_CHALLENGE_MARKERS = (
    "just a moment",
    "__cf_chl",
    "cf-chl",
    "checking your browser",
    "attention required",
    "verify you are human",
)

MODES = ("auto", "headless", "headed")
_HEADLESS_TRY_MS = 45_000  # in auto mode, how long headless gets before the headed retry
_CHANNELS: tuple[str, ...] = ("chrome", "msedge")


class BrowserSolverError(Exception):
    """Browser could not be started."""


def _is_challenge(html: str) -> bool:
    low = html.lower()
    return any(m in low for m in _CHALLENGE_MARKERS)


def _import_playwright() -> Any:
    """patchright (stealth-patched Playwright) if present, else plain playwright."""
    for module in ("patchright.async_api", "playwright.async_api"):
        try:
            return importlib.import_module(module).async_playwright
        except ImportError:
            continue
    return None


def available() -> bool:
    """True if a Playwright-compatible package is importable (says nothing about browsers)."""
    return _import_playwright() is not None


def browser_hint() -> str:
    return (
        "Install the built-in browser solver:\n"
        '    pip install "pahebatcher[browser]"\n'
        "  (needs Google Chrome or Microsoft Edge installed)"
    )


_BROWSER_RANK = {"custom": 0, "chrome": 1, "edge": 2, "chromium": 3}


def find_system_browser() -> tuple[str, str] | None:
    """(kind, executable) of an installed Chromium-family browser, or None."""
    override = os.environ.get("PAHEBATCHER_BROWSER")
    if override and Path(override).is_file():
        return "custom", override
    found: list[tuple[str, str]] = []
    if sys.platform.startswith("win"):
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if root := os.environ.get(key):
                found.append(("chrome", rf"{root}\Google\Chrome\Application\chrome.exe"))
                found.append(("edge", rf"{root}\Microsoft\Edge\Application\msedge.exe"))
    elif sys.platform == "darwin":
        found += [
            ("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ("edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    else:
        for kind, exe in (
            ("chrome", "google-chrome"), ("chrome", "google-chrome-stable"),
            ("edge", "microsoft-edge"), ("edge", "microsoft-edge-stable"),
            ("chromium", "chromium"), ("chromium", "chromium-browser"),
        ):
            if path := shutil.which(exe):
                found.append((kind, path))
    for kind, path in sorted(found, key=lambda c: _BROWSER_RANK[c[0]]):
        if os.path.isfile(path):
            return kind, path
    return None


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return int(sk.getsockname()[1])


def _devtools_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
            return bool(r.status == 200)
    except Exception:
        return False


def _platform_info() -> tuple[str, str]:
    if sys.platform.startswith("win"):
        return "Windows", "10.0.0"
    if sys.platform == "darwin":
        return "macOS", "14.0.0"
    return "Linux", "6.0.0"


def _proxy_dict(proxy: str | None) -> dict[str, str] | None:
    """Playwright wants credentials split out of the proxy URL."""
    if not proxy:
        return None
    u = urlparse(proxy)
    if not u.hostname:
        return {"server": proxy}
    out = {"server": f"{u.scheme or 'http'}://{u.hostname}" + (f":{u.port}" if u.port else "")}
    if u.username:
        out["username"] = u.username
        out["password"] = u.password or ""
    return out


class BrowserEngine:
    def __init__(self, profile_dir: Path, mode: str = "auto", proxy: str | None = None) -> None:
        self._profile = Path(profile_dir)
        self._mode = mode if mode in MODES else "auto"
        self._proxy = proxy
        self._pw_cm: Any = None
        self._pw: Any = None
        self._ctx: Any = None
        self._ctx_headless: bool | None = None
        self._proc: subprocess.Popen[bytes] | None = None  # system browser we spawned (CDP mode)
        self._cdp_browser: Any = None
        self.launch_kind = ""  # what actually started, for logs/UI
        self._headed_sticky = self._mode == "headed"
        self._lock = asyncio.Lock()
        self.last_error = ""

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self, headless: bool | None = None) -> None:
        """Launch (or re-launch with a different window mode). Raises BrowserSolverError."""
        want = headless if headless is not None else not self._headed_sticky
        if self._mode == "headless":
            want = True
        async with self._lock:
            if self._ctx is not None and self._ctx_headless == want:
                return
            await self._close_ctx()
            factory = _import_playwright()
            if factory is None:
                self.last_error = "playwright/patchright is not installed"
                raise BrowserSolverError(self.last_error)
            if self._pw is None:
                self._pw_cm = factory()
                self._pw = await self._pw_cm.start()
            self._profile.mkdir(parents=True, exist_ok=True)
            errors: list[str] = []
            try:
                if await self._start_system_browser(want):
                    return
            except Exception as exc:
                errors.append(f"system browser: {str(exc).splitlines()[0][:120]}")
                await self._close_ctx()
            for channel in _CHANNELS:
                kwargs: dict[str, Any] = {
                    "user_data_dir": str(self._profile),
                    "channel": channel,
                    "headless": want,
                    "no_viewport": True,
                    "args": ["--disable-blink-features=AutomationControlled"],
                    "ignore_default_args": ["--enable-automation"],
                }
                if px := _proxy_dict(self._proxy):
                    kwargs["proxy"] = px
                try:
                    self._ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
                    self._ctx_headless = want
                    self.launch_kind = channel
                    log.info("Browser solver started (%s, headless=%s)", self.launch_kind, want)
                    self.last_error = ""
                    return
                except Exception as exc:
                    errors.append(f"{channel}: {str(exc).splitlines()[0][:120]}")
            self.last_error = "; ".join(errors)
            raise BrowserSolverError(
                "no Chrome or Edge found — install one (or set PAHEBATCHER_BROWSER to a browser executable)"
            )

    async def _start_system_browser(self, headless: bool) -> bool:
        """Spawn the user's Chrome/Edge as a plain process and attach over CDP.

        Returns False when no such browser exists (caller falls back to Playwright's launcher).
        Raises if one exists but could not be driven.
        """
        found = find_system_browser()
        if found is None:
            return False
        px = _proxy_dict(self._proxy)
        if px and "username" in px:
            return False  # --proxy-server can't carry credentials; use Playwright's launcher
        kind, exe = found
        port = _free_port()
        args = [
            exe,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self._profile / ('cdp-' + kind)}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if headless:
            args.append("--headless=new")
        if px:
            args.append(f"--proxy-server={px['server']}")
        if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
            args.append("--no-sandbox")  # Chromium refuses to run as root otherwise
        args.append("about:blank")
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._proc = proc
        deadline = time.monotonic() + 20
        while not await asyncio.to_thread(_devtools_up, port):
            if proc.poll() is not None or time.monotonic() > deadline:
                # Typically: the same profile is already open in a leftover window.
                raise BrowserSolverError(
                    f"{kind} did not open its debugging port — close any leftover pahebatcher browser window"
                )
            await asyncio.sleep(0.25)
        self._cdp_browser = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        contexts = self._cdp_browser.contexts
        self._ctx = contexts[0] if contexts else await self._cdp_browser.new_context()
        self._ctx_headless = headless
        self.launch_kind = f"{kind} (own process)"
        self.last_error = ""
        log.info("Browser solver started (%s at %s, headless=%s)", self.launch_kind, exe, headless)
        return True

    async def _close_ctx(self) -> None:
        if self._cdp_browser is not None:
            try:
                await self._cdp_browser.close()
            except Exception:
                pass
            self._cdp_browser = None
            self._ctx = None
        if self._proc is not None:
            proc, self._proc = self._proc, None
            if proc.poll() is None:
                proc.terminate()
                try:
                    await asyncio.to_thread(proc.wait, 5)
                except Exception:
                    proc.kill()
        if self._ctx is not None:
            try:
                await self._ctx.close()
            except Exception:
                pass
            self._ctx = None
        self._ctx_headless = None

    async def close(self) -> None:
        async with self._lock:
            await self._close_ctx()
            if self._pw_cm is not None:
                try:
                    await self._pw_cm.stop()
                except Exception:
                    pass
                self._pw_cm = self._pw = None

    # ── solving ────────────────────────────────────────────────────────────
    async def solve(
        self, url: str, timeout_ms: int = 120_000, settle_ms: int = 2_000
    ) -> dict[str, Any] | None:
        """Load ``url`` until Cloudflare lets us through. Returns a solution dict, or None."""
        if self._mode == "auto" and not self._headed_sticky:
            first = min(_HEADLESS_TRY_MS, int(timeout_ms * 0.4))
            plan = [(True, first), (False, timeout_ms - first)]  # total stays within timeout_ms
        elif self._headed_sticky:
            plan = [(False, timeout_ms)]
        else:
            plan = [(True, timeout_ms)]

        for headless, budget in plan:
            try:
                await self.start(headless)
            except BrowserSolverError as exc:
                log.warning("Browser solver: %s", exc)
                return None
            sol = await self._solve_in_page(url, budget, settle_ms)
            if sol is not None:
                if not headless and self._mode == "auto":
                    self._headed_sticky = True  # headless got blocked; don't waste time next round
                return sol
            if headless:
                log.warning("Headless browser didn't clear the challenge — retrying with a visible window.")
        return None

    async def _solve_in_page(self, url: str, timeout_ms: int, settle_ms: int) -> dict[str, Any] | None:
        ctx = self._ctx
        if ctx is None:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        page = await ctx.new_page()
        try:
            ua = await self._mask_headless(ctx, page)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=min(timeout_ms, 60_000))
            except Exception as exc:  # slow/blocked navigation — the poll loop decides
                log.debug("goto %s: %s", url, exc)

            html = ""
            while True:
                try:
                    html = await page.content()
                except Exception:
                    html = ""  # mid-navigation (challenge redirect) — try again
                if html and not _is_challenge(html):
                    break
                if loop.time() > deadline:
                    return None
                await asyncio.sleep(0.75)

            await asyncio.sleep(max(0, settle_ms) / 1000)  # let post-clearance redirects/XHR land
            try:
                html = await page.content()
            except Exception:
                pass
            if _is_challenge(html):
                return None
            cookies = await ctx.cookies()
            return {
                "url": page.url,
                "status": 200,
                "response": html,
                "cookies": [dict(c) for c in cookies],
                "userAgent": ua,
                "startTimestamp": int(time.time() * 1000),
            }
        except Exception as exc:
            log.warning("Browser solve failed for %s: %s", url, exc)
            return None
        finally:
            try:
                await page.close()
            except Exception:
                pass

    @staticmethod
    async def _mask_headless(ctx: Any, page: Any) -> str:
        """Return the page's UA; if headless, rewrite UA + client hints to a normal Chrome's."""
        ua = str(await page.evaluate("navigator.userAgent"))
        if "Headless" not in ua:
            return ua
        ua = ua.replace("HeadlessChrome", "Chrome")
        try:
            cdp = await ctx.new_cdp_session(page)
            product = str((await cdp.send("Browser.getVersion")).get("product", ""))
            full = product.split("/", 1)[1] if "/" in product else ""
            major = full.split(".", 1)[0] or "0"
            platform, platform_ver = _platform_info()
            await cdp.send(
                "Emulation.setUserAgentOverride",
                {
                    "userAgent": ua,
                    "acceptLanguage": "en-US,en",
                    "userAgentMetadata": {
                        "brands": [
                            {"brand": "Chromium", "version": major},
                            {"brand": "Google Chrome", "version": major},
                            {"brand": "Not.A/Brand", "version": "99"},
                        ],
                        "fullVersion": full,
                        "platform": platform,
                        "platformVersion": platform_ver,
                        "architecture": "x86",
                        "model": "",
                        "mobile": False,
                    },
                },
            )
        except Exception as exc:
            log.debug("UA override failed: %s", exc)
        return ua
