"""Tests for per-domain clearance store + direct fast path (solver)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from pahebatcher.browser_solver import BrowserEngine
from pahebatcher.solver import Solver, _host_of, _is_challenge_body

URL = "https://animepahe.pw/api?m=search&q=x"


def _solver(**kwargs: Any) -> Solver:
    return Solver(**kwargs)


def _solution(cookies: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"response": '{"data": []}', "cookies": cookies or [], "userAgent": "UA"}


def _browser(sol: dict[str, Any] | None = None) -> Any:
    """Patch the real browser: ``solve`` returns ``sol`` (a normal solution by default)."""
    return patch.object(BrowserEngine, "solve", AsyncMock(return_value=_solution() if sol is None else sol))


def _challenge() -> str:
    return "<html><head><title>Just a moment...</title></head><body>__cf_chl</body></html>"


class TestHelpers:
    def test_host_of(self) -> None:
        assert _host_of("https://AnimePahe.PW/api?m=search&q=x") == "animepahe.pw"

    def test_is_challenge_body(self) -> None:
        assert _is_challenge_body(_challenge()) is True
        assert _is_challenge_body('{"data": [], "total": 0}') is False
        assert _is_challenge_body("") is False


class TestFastPath:
    async def test_hit_skips_browser(self) -> None:
        solver = _solver(user_cookies="cf_clearance=abc")
        with patch(
            "pahebatcher.solver._direct_fetch_sync", return_value=(200, '{"data": []}', []),
        ) as mock_direct, patch.object(
            BrowserEngine, "solve", AsyncMock(side_effect=AssertionError("browser must not be hit")),
        ):
            sol = await solver.request(URL, cache=False)
            assert sol is not None
            assert sol["response"] == '{"data": []}'
        _, header, _ = mock_direct.call_args[0]
        assert "cf_clearance=abc" in header

    async def test_transport_failure_falls_back(self) -> None:
        solver = _solver(user_cookies="cf_clearance=abc")
        with patch("pahebatcher.solver._direct_fetch_sync", return_value=None), _browser() as solve:
            sol = await solver.request(URL, cache=False)
            assert sol is not None
            assert solve.called

    async def test_challenge_body_falls_back_and_invalidates(self) -> None:
        solver = _solver(user_cookies="cf_clearance=stale")
        with patch("pahebatcher.solver._direct_fetch_sync", return_value=(403, _challenge(), [])), _browser() as solve:
            sol = await solver.request(URL, cache=False)
            assert sol is not None
            assert solve.called
        # Transplant marked dead, nothing harvested (browser solution had no cookies)
        assert "animepahe.pw" in solver._clearance_dead
        assert await solver._clearance.get("animepahe.pw") is None
        # Next request goes straight to the browser (no cookies left to try direct)
        with patch(
            "pahebatcher.solver._direct_fetch_sync",
            side_effect=AssertionError("no direct attempt expected"),
        ), _browser():
            sol2 = await solver.request("https://animepahe.pw/api?m=search&q=y", cache=False)
            assert sol2 is not None

    async def test_non_challenge_error_keeps_store(self) -> None:
        solver = _solver(user_cookies="cf_clearance=abc")
        await solver._note_clearance_success("animepahe.pw", [{"name": "cf_clearance", "value": "abc"}])
        with patch("pahebatcher.solver._direct_fetch_sync", return_value=(429, "slow down", [])), _browser():
            sol = await solver.request(URL, cache=False)
            assert sol is not None
        # Transient 429 must not invalidate or mark dead
        assert "animepahe.pw" not in solver._clearance_dead
        assert await solver._clearance.get("animepahe.pw") == [{"name": "cf_clearance", "value": "abc"}]

    async def test_solution_cache_still_works(self) -> None:
        solver = _solver()
        with _browser() as solve:
            first = await solver.request(URL, cache=True)
            calls_after_first = solve.call_count
            assert calls_after_first > 0
            second = await solver.request(URL, cache=True)
            assert first is not None and second is not None
            assert solve.call_count == calls_after_first


class TestHarvestAndPrecedence:
    async def test_harvest_on_browser_success(self) -> None:
        solver = _solver()
        sol_in = _solution([{"name": "cf_clearance", "value": "fresh"}, {"name": "other", "value": "x"}])
        with patch("pahebatcher.solver._direct_fetch_sync", return_value=None), _browser(sol_in):
            sol = await solver.request(URL, cache=False)
            assert sol is not None
        # Only cf_clearance harvested, other cookies dropped
        assert await solver._clearance.get("animepahe.pw") == [{"name": "cf_clearance", "value": "fresh"}]

    async def test_transplant_wins_until_dead(self) -> None:
        solver = _solver(user_cookies="cf_clearance=user1")
        await solver._note_clearance_success("animepahe.pw", [{"name": "cf_clearance", "value": "harv"}])
        with patch(
            "pahebatcher.solver._direct_fetch_sync", return_value=(200, '{"data": []}', []),
        ) as mock_direct:
            await solver.request(URL, cache=False)
        _, header, _ = mock_direct.call_args[0]
        assert "cf_clearance=user1" in header
        assert "harv" not in header

    async def test_harvested_used_when_no_transplant(self) -> None:
        solver = _solver()
        await solver._note_clearance_success("animepahe.pw", [{"name": "cf_clearance", "value": "harv"}])
        with patch(
            "pahebatcher.solver._direct_fetch_sync", return_value=(200, '{"data": []}', []),
        ) as mock_direct:
            await solver.request(URL, cache=False)
        _, header, _ = mock_direct.call_args[0]
        assert "cf_clearance=harv" in header

    async def test_domain_isolation(self) -> None:
        solver = _solver()
        await solver._note_clearance_success("animepahe.pw", [{"name": "cf_clearance", "value": "pw1"}])
        with patch(
            "pahebatcher.solver._direct_fetch_sync",
            side_effect=AssertionError("must not try direct for other domain"),
        ), _browser():
            sol = await solver.request("https://animepahe.com/api?m=search&q=x", cache=False)
            assert sol is not None


class TestBrowserInvalidation:
    async def test_challenge_drops_harvested(self) -> None:
        solver = _solver()
        await solver._note_clearance_success("animepahe.pw", [{"name": "cf_clearance", "value": "old"}])
        with patch("pahebatcher.solver._direct_fetch_sync", return_value=(403, _challenge(), [])), _browser():
            sol = await solver.request(URL, cache=False)
            assert sol is not None
        assert await solver._clearance.get("animepahe.pw") is None


class TestTtl:
    async def test_expiry(self) -> None:
        solver = _solver(clearance_ttl=0.05)
        await solver._note_clearance_success("animepahe.pw", [{"name": "cf_clearance", "value": "v"}])
        assert await solver._clearance.get("animepahe.pw") == [{"name": "cf_clearance", "value": "v"}]
        await asyncio.sleep(0.1)
        with patch(
            "pahebatcher.solver._direct_fetch_sync",
            side_effect=AssertionError("expired, must not try direct"),
        ), _browser():
            sol = await solver.request(URL, cache=False)
            assert sol is not None

    async def test_zero_disables_store_and_fast_path(self) -> None:
        solver = _solver(user_cookies="cf_clearance=u", clearance_ttl=0)
        sol_in = _solution([{"name": "cf_clearance", "value": "new"}])
        with patch(
            "pahebatcher.solver._direct_fetch_sync",
            side_effect=AssertionError("disabled, must not try direct"),
        ), _browser(sol_in):
            sol = await solver.request(URL, cache=False)
            assert sol is not None
        # Nothing harvested when the store is disabled
        assert await solver._clearance.get("animepahe.pw") is None
