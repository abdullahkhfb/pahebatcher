"""Tests for the Cloudflare solver (built-in browser)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from pahebatcher import browser_solver
from pahebatcher.solver import Solver, no_solver_hint


class TestHints:
    def test_no_solver_hint_points_at_browser_extra(self) -> None:
        hint = no_solver_hint()
        assert "pahebatcher[browser]" in hint
        assert "docker" not in hint.lower()
        assert "flaresolverr" not in hint.lower()


class TestParseCookieString:
    def test_empty(self) -> None:
        assert Solver._parse_cookie_string("") == []

    def test_single(self) -> None:
        result = Solver._parse_cookie_string("a=1")
        assert result == [{"name": "a", "value": "1"}]

    def test_multiple(self) -> None:
        result = Solver._parse_cookie_string("a=1; b=2; c=3")
        assert len(result) == 3
        assert result[0]["name"] == "a"
        assert result[1]["value"] == "2"

    def test_with_spaces_and_equals_in_value(self) -> None:
        result = Solver._parse_cookie_string("cf_clearance=abc123; session=xyz=123")
        assert result[0] == {"name": "cf_clearance", "value": "abc123"}
        assert result[1]["value"] == "xyz=123"

    def test_trailing_semicolon(self) -> None:
        result = Solver._parse_cookie_string("a=1;")
        assert len(result) == 1


class TestSolverLifecycle:
    async def test_start_and_close(self) -> None:
        s = Solver()
        await s.start()
        await s.close()

    async def test_backend_name(self) -> None:
        assert Solver().backend_name == "built-in browser"

    async def test_ping_false_without_playwright(self) -> None:
        with patch.object(browser_solver, "available", return_value=False):
            assert await Solver().ping() is False


class TestSolverRequest:
    async def test_request_cache_hit(self) -> None:
        s = Solver()
        await s.start()
        await s._solver_cache.set("https://example.com", {"response": "cached"})
        result = await s.request("https://example.com", cache=True)
        assert result == {"response": "cached"}
        await s.close()

    async def test_fetch_json_pre_tag(self) -> None:
        s = Solver()
        await s.start()
        html = '<html><pre>{"key": "value"}</pre></html>'
        with patch.object(s, "request", new_callable=AsyncMock, return_value={"response": html}):
            result = await s.fetch_json("https://example.com")
            assert result == {"key": "value"}
        await s.close()

    async def test_fetch_json_stripped_html(self) -> None:
        s = Solver()
        await s.start()
        html = '{"direct": "json"}'
        with patch.object(s, "request", new_callable=AsyncMock, return_value={"response": html}):
            result = await s.fetch_json("https://example.com")
            assert result == {"direct": "json"}
        await s.close()

    async def test_fetch_html(self) -> None:
        s = Solver()
        await s.start()
        with patch.object(
            s, "request", new_callable=AsyncMock, return_value={"response": "<h1>Title</h1>", "cookies": []},
        ):
            result = await s.fetch_html("https://example.com")
            assert result is not None
            html, cookies = result
            assert "<h1>Title</h1>" in html
        await s.close()

    async def test_fetch_html_none(self) -> None:
        s = Solver()
        await s.start()
        with patch.object(s, "request", new_callable=AsyncMock, return_value=None):
            result = await s.fetch_html("https://example.com")
            assert result is None
        await s.close()
