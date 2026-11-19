"""Tests for the built-in browser solver (unit tests + a real-Chromium run against a fake challenge)."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from pahebatcher import browser_solver
from pahebatcher.browser_solver import BrowserEngine, BrowserSolverError, _is_challenge, _proxy_dict
from pahebatcher.config_manager import ConfigManager
from pahebatcher.solver import Solver, _cookie_applies, _impersonate_for


class TestHelpers:
    def test_is_challenge(self) -> None:
        assert _is_challenge("<title>Just a moment...</title>")
        assert not _is_challenge('<pre>{"data": []}</pre>')

    def test_proxy_dict_splits_credentials(self) -> None:
        assert _proxy_dict(None) is None
        assert _proxy_dict("http://u:p@10.0.0.1:8080") == {
            "server": "http://10.0.0.1:8080", "username": "u", "password": "p",
        }
        assert _proxy_dict("socks5://h:1080") == {"server": "socks5://h:1080"}

    def test_cookie_applies(self) -> None:
        assert _cookie_applies(".animepahe.pw", "animepahe.pw")
        assert _cookie_applies("animepahe.pw", "www.animepahe.pw")
        assert not _cookie_applies("kwik.si", "animepahe.pw")
        assert _cookie_applies("127.0.0.1", "127.0.0.1:8000")

    def test_impersonate_tracks_ua_major(self) -> None:
        assert _impersonate_for("Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36") == "chrome131"
        # between two profiles -> newest one not newer than the browser
        assert _impersonate_for("Mozilla/5.0 Chrome/132.0.0.0 Safari/537.36") == "chrome131"
        assert _impersonate_for("Firefox/130") == "chrome120"


class TestConfig:
    def test_defaults_and_validation(self, tmp_path: Path) -> None:
        cm = ConfigManager(tmp_path / "c.toml")
        assert cm.get("browser_mode") == "auto"
        assert "solver_backend" not in cm.show()
        cm.set("browser_mode", "headed")
        with pytest.raises(ValueError):
            cm.set("browser_mode", "invisible")
        cm.save()
        cm2 = ConfigManager(tmp_path / "c.toml")
        cm2.load()
        assert cm2.get("browser_mode") == "headed"

    def test_from_config(self, tmp_path: Path) -> None:
        cm = ConfigManager(tmp_path / "c.toml")
        s = Solver.from_config(cm)
        assert s.backend_name == "built-in browser"


class TestBackendSelection:
    async def test_ping_ok_when_browser_starts(self, tmp_path: Path) -> None:
        s = Solver(profile_dir=tmp_path)
        with patch.object(browser_solver, "available", return_value=True), \
             patch.object(BrowserEngine, "start", AsyncMock()):
            assert await s.ping() is True
        assert s.backend_name == "built-in browser"

    async def test_ping_fails_when_nothing_available(self) -> None:
        s = Solver()
        with patch.object(browser_solver, "available", return_value=False):
            assert await s.ping() is False

    async def test_ping_fails_when_browser_cannot_launch(self, tmp_path: Path) -> None:
        s = Solver(profile_dir=tmp_path)
        with patch.object(browser_solver, "available", return_value=True), \
             patch.object(BrowserEngine, "start", AsyncMock(side_effect=BrowserSolverError("no chrome"))):
            assert await s.ping() is False

    async def test_browser_request_harvests_only_matching_host_cookies(self, tmp_path: Path) -> None:
        s = Solver(profile_dir=tmp_path)
        sol = {
            "response": "ok", "userAgent": "Mozilla/5.0 Chrome/150.0.0.0",
            "cookies": [
                {"name": "cf_clearance", "value": "mine", "domain": ".animepahe.pw"},
                {"name": "cf_clearance", "value": "other", "domain": ".kwik.si"},
            ],
        }
        with patch.object(BrowserEngine, "solve", AsyncMock(return_value=sol)):
            got = await s.request("https://animepahe.pw/api?m=x", cache=False)
        assert got is sol
        cookies, source = await s._clearance_for("animepahe.pw")
        assert cookies == [{"name": "cf_clearance", "value": "mine"}] and source == "harvested"
        assert s._clearance_ua["animepahe.pw"] == "Mozilla/5.0 Chrome/150.0.0.0"

    async def test_browser_request_failure_returns_none(self, tmp_path: Path) -> None:
        s = Solver(profile_dir=tmp_path)
        with patch.object(BrowserEngine, "solve", AsyncMock(return_value=None)):
            assert await s.request("https://animepahe.pw/x", cache=False) is None


class TestDirectPathUsesBrowserUA:
    async def test_harvested_ua_and_impersonation_passed(self, tmp_path: Path) -> None:
        s = Solver(profile_dir=tmp_path)
        ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
        await s._note_clearance_success("h.test", [{"name": "cf_clearance", "value": "v"}], ua)
        with patch("pahebatcher.solver._direct_fetch_sync", return_value=(200, "ok", [])) as m:
            sol = await s.request("https://h.test/a", cache=False)
        assert sol is not None and sol["userAgent"] == ua
        assert m.call_args.kwargs == {"user_agent": ua, "impersonate": "chrome131"}


# ── real Chromium vs. a fake Cloudflare-style challenge ───────────────────

CHALLENGE = (
    "<html><head><title>Just a moment...</title></head><body>Checking your browser"
    "<script>setTimeout(function(){document.cookie='cf_clearance=ok123; path=/';"
    "location.reload();},1500);</script></body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict[str, str]] = []

    def do_GET(self) -> None:
        hdrs = {k.lower(): v for k, v in self.headers.items()}
        type(self).seen.append(hdrs)
        cleared = "cf_clearance=ok123" in hdrs.get("cookie", "")
        # a plain HTTP client with no clearance gets the challenge (403), like Cloudflare
        status, body = (200, '{"data": [{"title": "Naruto"}]}') if cleared else (403, CHALLENGE)
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a: Any) -> None:
        pass


@pytest.fixture
def challenge_server() -> Iterator[str]:
    _Handler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


async def test_real_browser_clears_challenge_and_feeds_fast_path(
    challenge_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = _any_chromium_binary()
    if exe is None or not browser_solver.available():
        pytest.skip("no Chromium-family browser / playwright available")
    monkeypatch.setenv("PAHEBATCHER_BROWSER", exe)
    s = Solver(browser_mode="headless", profile_dir=tmp_path / "prof")
    await s.start()
    try:
        if not await s.ping():
            pytest.skip(f"no launchable browser: {s._engine().last_error}")
        url = f"{challenge_server}/api?m=search"
        sol = await s.request(url, cache=False, max_timeout=30_000, wait=200)
        assert sol is not None
        assert "Naruto" in sol["response"]
        assert "HeadlessChrome" not in sol["userAgent"]
        assert any(c["name"] == "cf_clearance" for c in sol["cookies"])

        # Same host again, different URL: must go via the direct fast path (no new browser page)
        _Handler.seen.clear()
        with patch.object(BrowserEngine, "solve", AsyncMock(side_effect=AssertionError("browser reused"))):
            again = await s.request(f"{challenge_server}/api?m=other", cache=False)
        assert again is not None and "Naruto" in again["response"]
        direct = _Handler.seen[-1]
        assert "cf_clearance=ok123" in direct["cookie"]
        assert direct["user-agent"] == sol["userAgent"]  # clearance is UA-bound
    finally:
        await s.close()


class TestEngineFallback:
    async def test_auto_retries_headed_after_headless_fails_then_sticks(self, tmp_path: Path) -> None:
        eng = BrowserEngine(tmp_path, mode="auto")
        started: list[bool | None] = []

        async def fake_start(headless: bool | None = None) -> None:
            started.append(headless)

        sol = {"response": "ok", "cookies": [], "userAgent": "UA"}
        pages = AsyncMock(side_effect=[None, sol, sol])
        with patch.object(eng, "start", fake_start), patch.object(eng, "_solve_in_page", pages):
            assert await eng.solve("https://h/x") == sol
            assert started == [True, False]
            started.clear()
            assert await eng.solve("https://h/y") == sol
            assert started == [False]  # headless already proved useless this session

    async def test_headless_mode_never_opens_window(self, tmp_path: Path) -> None:
        eng = BrowserEngine(tmp_path, mode="headless")
        started: list[bool | None] = []

        async def fake_start(headless: bool | None = None) -> None:
            started.append(headless)

        with patch.object(eng, "start", fake_start), \
             patch.object(eng, "_solve_in_page", AsyncMock(return_value=None)):
            assert await eng.solve("https://h/x") is None
        assert started == [True]

    async def test_start_without_playwright_raises(self, tmp_path: Path) -> None:
        eng = BrowserEngine(tmp_path)
        with patch.object(browser_solver, "_import_playwright", return_value=None), \
             pytest.raises(BrowserSolverError):
            await eng.start()
        assert "not installed" in eng.last_error

    async def test_solve_returns_none_when_launch_fails(self, tmp_path: Path) -> None:
        eng = BrowserEngine(tmp_path)
        with patch.object(eng, "start", AsyncMock(side_effect=BrowserSolverError("boom"))):
            assert await eng.solve("https://h/x") is None


class TestSystemBrowser:
    def test_override_env_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        exe = tmp_path / "mybrowser"
        exe.write_text("")
        monkeypatch.setenv("PAHEBATCHER_BROWSER", str(exe))
        assert browser_solver.find_system_browser() == ("custom", str(exe))

    def test_none_when_nothing_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PAHEBATCHER_BROWSER", raising=False)
        with patch.object(browser_solver.shutil, "which", return_value=None), \
             patch.object(browser_solver.os.path, "isfile", return_value=False):
            assert browser_solver.find_system_browser() is None

    def test_chrome_preferred_over_edge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PAHEBATCHER_BROWSER", raising=False)
        which = {"google-chrome": "/usr/bin/google-chrome", "microsoft-edge": "/usr/bin/microsoft-edge"}
        with patch.object(browser_solver.sys, "platform", "linux"), \
             patch.object(browser_solver.shutil, "which", side_effect=which.get), \
             patch.object(browser_solver.os.path, "isfile", return_value=True):
            assert browser_solver.find_system_browser() == ("chrome", "/usr/bin/google-chrome")

    async def test_no_system_browser_falls_back_to_playwright_launcher(self, tmp_path: Path) -> None:
        eng = BrowserEngine(tmp_path)
        with patch.object(browser_solver, "find_system_browser", return_value=None):
            assert await eng._start_system_browser(True) is False

    async def test_credentialed_proxy_skips_cdp_mode(self, tmp_path: Path) -> None:
        eng = BrowserEngine(tmp_path, proxy="http://u:p@10.0.0.1:8080")
        with patch.object(browser_solver, "find_system_browser", return_value=("chrome", "/x")):
            assert await eng._start_system_browser(True) is False

    async def test_budget_fits_inside_timeout(self, tmp_path: Path) -> None:
        eng = BrowserEngine(tmp_path, mode="auto")
        budgets: list[int] = []

        async def fake_page(url: str, timeout_ms: int, settle_ms: int) -> None:
            budgets.append(timeout_ms)

        with patch.object(eng, "start", AsyncMock()), patch.object(eng, "_solve_in_page", fake_page):
            await eng.solve("https://h/x", timeout_ms=100_000)
        assert len(budgets) == 2 and sum(budgets) <= 100_000


def _any_chromium_binary() -> str | None:
    """A system browser, or (CI/sandbox) a cached Chromium binary usable as a stand-in."""
    import glob

    if found := browser_solver.find_system_browser():
        return found[1]
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), "/opt/pw-browsers"]
    for root in filter(None, roots):
        hits = glob.glob(f"{root}/chromium-*/chrome-linux*/chrome")
        if hits:
            return hits[0]
    return None


@pytest.mark.parametrize("headless", [True])
async def test_real_browser_via_own_process_cdp(
    challenge_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headless: bool
) -> None:
    exe = _any_chromium_binary()
    if exe is None or not browser_solver.available():
        pytest.skip("no Chromium binary / playwright available")
    monkeypatch.setenv("PAHEBATCHER_BROWSER", exe)
    eng = BrowserEngine(tmp_path / "prof", mode="headless")
    try:
        sol = await eng.solve(f"{challenge_server}/api?m=search", timeout_ms=30_000, settle_ms=200)
        assert sol is not None, eng.last_error
        assert eng.launch_kind.endswith("(own process)")
        assert "Naruto" in sol["response"]
        assert "HeadlessChrome" not in sol["userAgent"]
        assert any(c["name"] == "cf_clearance" for c in sol["cookies"])
        proc = eng._proc
        assert proc is not None and proc.poll() is None
    finally:
        await eng.close()
    assert proc.poll() is not None  # the browser we spawned is shut down with us
