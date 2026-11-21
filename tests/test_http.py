"""Tests for HttpClient."""

from __future__ import annotations

import asyncio
from unittest import mock

import aiohttp
import pytest

from pahebatcher import http


class TestGetCurlSession:
    def test_thread_local_reuse(self) -> None:
        from pahebatcher import http

        with mock.patch("curl_cffi.requests.Session") as mock_sess:
            mock_inst = mock.MagicMock()
            mock_sess.return_value = mock_inst
            s1 = http._get_curl_session()
            s2 = http._get_curl_session()
            assert s1 is s2
            mock_sess.assert_called_once()


class TestHttpClientLifecycle:
    async def test_start_close(self) -> None:
        c = http.HttpClient(hls_workers=8)
        await c.start()
        assert c._session is not None
        assert c.session is not None
        await c.close()
        assert c._session is None

    async def test_session_requires_start(self) -> None:
        c = http.HttpClient()
        with pytest.raises(RuntimeError, match="not started"):
            _ = c.session

    async def test_get_success(self) -> None:
        c = http.HttpClient(hls_workers=8)
        await c.start()
        mock_resp = mock.MagicMock()
        mock_resp.raise_for_status = mock.MagicMock()
        mock_resp.read = mock.AsyncMock(return_value=b"hello")
        mock_ctx = mock.MagicMock()
        mock_ctx.__aenter__ = mock.AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = mock.AsyncMock(return_value=False)
        with mock.patch.object(c.session, "get", return_value=mock_ctx):
            data = await c.get("https://example.com")
            assert data == b"hello"
        await c.close()

    async def test_get_retry_on_client_error(self) -> None:
        c = http.HttpClient(hls_workers=8)
        await c.start()
        # First call raises, second succeeds
        mock_resp = mock.MagicMock()
        mock_resp.raise_for_status = mock.MagicMock()
        mock_resp.read = mock.AsyncMock(return_value=b"ok")
        mock_ctx = mock.MagicMock()
        mock_ctx.__aenter__ = mock.AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = mock.AsyncMock(return_value=False)

        call_count = 0

        def _fake_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Return context that raises ClientError on enter
                err_ctx = mock.MagicMock()
                err_ctx.__aenter__ = mock.AsyncMock(
                    side_effect=aiohttp.ClientError("fail")
                )
                err_ctx.__aexit__ = mock.AsyncMock(return_value=False)
                return err_ctx
            return mock_ctx

        with mock.patch.object(c.session, "get", side_effect=_fake_get):
            with mock.patch("asyncio.sleep", new_callable=mock.AsyncMock):
                data = await c.get("https://example.com")
                assert data == b"ok"
        await c.close()

    async def test_403_fallback_to_curl(self) -> None:
        c = http.HttpClient(hls_workers=8)
        await c.start()
        # Simulate 403
        async def _fake_curl(url, headers, timeout):
            return b"curl_data"

        err = aiohttp.ClientResponseError(
            request_info=mock.MagicMock(),
            history=(),
            status=403,
            message="forbidden",
        )
        err_ctx = mock.MagicMock()
        err_ctx.__aenter__ = mock.AsyncMock(side_effect=err)
        err_ctx.__aexit__ = mock.AsyncMock(return_value=False)
        with mock.patch.object(c.session, "get", return_value=err_ctx):
            with mock.patch(
                "pahebatcher.http._curl_fetch", side_effect=_fake_curl
            ) as mock_curl:
                # Need loop for executor; mock run_in_executor to call directly
                with mock.patch.object(
                    c._loop,
                    "run_in_executor",
                    new_callable=mock.AsyncMock,
                    return_value=b"curl_data",
                ):
                    data = await c.get("https://example.com")
                    assert data == b"curl_data"
        await c.close()


class TestCurlFetch:
    def test_curl_fetch_success(self) -> None:
        from pahebatcher import http

        mock_r = mock.MagicMock()
        mock_r.content = b"data"
        mock_r.raise_for_status = mock.MagicMock()
        mock_sess = mock.MagicMock()
        mock_sess.get.return_value = mock_r
        with mock.patch(
            "pahebatcher.http._get_curl_session", return_value=mock_sess
        ):
            data = http._curl_fetch("https://example.com", None, 30)
            assert data == b"data"

    def test_curl_fetch_timeout_retry(self) -> None:
        import curl_cffi.requests
        from pahebatcher import http

        mock_sess = mock.MagicMock()
        # First call raises Timeout, second succeeds
        mock_r = mock.MagicMock()
        mock_r.content = b"ok"
        mock_r.raise_for_status = mock.MagicMock()
        mock_sess.get.side_effect = [
            curl_cffi.requests.exceptions.Timeout("timeout"),
            mock_r,
        ]
        with mock.patch(
            "pahebatcher.http._get_curl_session", return_value=mock_sess
        ):
            with mock.patch("time.sleep"):
                data = http._curl_fetch("https://example.com", None, 30)
                assert data == b"ok"
