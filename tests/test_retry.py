"""Tests for auto-retry (3 attempts) — episode, resolver, batch."""

from __future__ import annotations

import asyncio
import pathlib
from unittest import mock

import pytest

from pahebatcher import downloader, models
from pahebatcher.ui import dashboard


class TestEpisodeRetry:
    async def test_retry_success_on_second_attempt(
        self,
        tmp_path: pathlib.Path,
        app_context: models.AppContext,
        sample_anime: models.AnimeInfo,
    ) -> None:
        app_context.output_dir = str(tmp_path / "out")
        app_context.cache_dir = tmp_path / "cache"
        app_context.auto_retry = 2
        app_context.hls_workers = 2
        dash = mock.MagicMock(spec=dashboard.Dashboard)
        http = mock.MagicMock()
        solver = mock.MagicMock()

        m3u8_content = "#EXTM3U\n#EXTINF:10.0,\nseg0.ts\n"
        # First http.get for m3u8 succeeds, then segment fetch fails once then
        # succeeds
        call_count = 0

        async def fake_get(url, headers=None, timeout=60):
            nonlocal call_count
            call_count += 1
            if "seg0.ts" in url and call_count == 2:
                # First segment fetch fails
                raise ConnectionError("transient")
            return b"segmentdata"

        http.get = mock.AsyncMock(side_effect=fake_get)

        with mock.patch(
            "pahebatcher.extract.m3u8.fetch_m3u8",
            new_callable=mock.AsyncMock,
            return_value=m3u8_content,
        ):
            with mock.patch(
                "pahebatcher.extract.m3u8.resolve_m3u8",
                new_callable=mock.AsyncMock,
            ) as mock_resolve:
                mock_resolve.return_value = [
                    {"url": "https://cdn/seg0.ts", "key_url": None, "iv": None},
                ]
                with mock.patch(
                    "pahebatcher.store.SegmentStore"
                ) as MockStore:
                    mock_store = mock.MagicMock()
                    mock_store.done_indices.return_value = set()
                    mock_store.save_metadata = mock.MagicMock()
                    mock_store.write_seg = mock.MagicMock()
                    mock_store.assemble.return_value = True
                    mock_store.cleanup = mock.MagicMock()
                    MockStore.return_value = mock_store
                    # Make second attempt succeed by not failing on retry We
                    # need to make fake_get succeed on retry — after first
                    # failure, next call succeeds Reset call_count logic: use
                    # side_effect list
                    http.get = mock.AsyncMock(
                        side_effect=[b"segmentdata", b"segmentdata"]
                    )
                    # Actually for episode retry, we need to mock the whole
                    # segment fetch to fail first attempt Simulate first
                    # attempt's gather fails, second succeeds
                    original_gather = asyncio.gather

                    ep = models.EpisodeInfo(
                        1, "s1", "Retry Ep", "F", "jpn", "https://play/url"
                    )
                    info = models.StreamInfo(
                        "https://cdn.m3u8", [], "UA", "ref", title="T"
                    )
                    dl = downloader.EpisodeDownloader(
                        app_context, sample_anime, dash, http, solver
                    )
                    # First call to http.get for m3u8 is mocked, second is
                    # segment, we want segment to fail once Use patch to make
                    # http.get fail first segment then succeed
                    http.get.reset_mock()
                    http.get.side_effect = [
                        b"segmentdata", ConnectionError("fail"), b"segmentdata"
                    ]
                    # But our EpisodeDownloader does fetch_m3u8 via http.get
                    # indirectly? We mocked fetch_m3u8 So next http.get is
                    # segment To test retry, we make assemble fail first then
                    # succeed? Simpler: make http.get for segment fail Let's
                    # just test that retry is attempted by making http.get raise
                    # once We'll patch asyncio.sleep to avoid delay
                    with mock.patch(
                        "asyncio.sleep", new_callable=mock.AsyncMock
                    ):
                        # Need to handle m3u8 and segment: we have mocked
                        # fetch_m3u8, so only segment matters Make segment fail
                        # first attempt, succeed second
                        seq = [ConnectionError("transient"), b"segmentdata"]

                        async def seg_get(url, headers=None, timeout=60):
                            if not hasattr(seg_get, "called"):
                                seg_get.called = 0  # type: ignore
                            seg_get.called += 1  # type: ignore
                            if seg_get.called == 1:  # type: ignore
                                raise ConnectionError("transient")
                            return b"segmentdata"

                        http.get = mock.AsyncMock(side_effect=seg_get)
                        result = await dl.run(ep, info)
                        # With retry, should succeed on second attempt
                        assert result is not None
                        assert http.get.call_count >= 2

    async def test_no_retry_on_permanent_error(
        self,
        tmp_path: pathlib.Path,
        app_context: models.AppContext,
        sample_anime: models.AnimeInfo,
    ) -> None:
        app_context.output_dir = str(tmp_path / "out")
        app_context.cache_dir = tmp_path / "cache"
        app_context.auto_retry = 2
        dash = mock.MagicMock(spec=dashboard.Dashboard)
        http = mock.MagicMock()
        solver = mock.MagicMock()
        http.get = mock.AsyncMock(
            side_effect=RuntimeError("No Kwik link found")
        )
        with mock.patch(
            "pahebatcher.extract.m3u8.fetch_m3u8",
            new_callable=mock.AsyncMock,
            side_effect=RuntimeError("No Kwik link found"),
        ):
            ep = models.EpisodeInfo(1, "s1", "T", "F", "jpn", "url")
            info = models.StreamInfo("https://cdn.m3u8", [], "UA", "ref")
            dl = downloader.EpisodeDownloader(
                app_context, sample_anime, dash, http, solver
            )
            with mock.patch(
                "asyncio.sleep", new_callable=mock.AsyncMock
            ) as mock_sleep:
                result = await dl.run(ep, info)
                assert result is None
                # Should not sleep (no retry) for permanent error
                mock_sleep.assert_not_called()


class TestResolverRetry:
    async def test_resolver_retries_on_timeout(
        self,
        tmp_path: pathlib.Path,
        app_context: models.AppContext,
        sample_anime: models.AnimeInfo,
    ) -> None:
        app_context.output_dir = str(tmp_path / "out")
        app_context.cache_dir = tmp_path / "cache"
        app_context.auto_retry = 2
        http = mock.MagicMock()
        http.start = mock.AsyncMock()
        http.close = mock.AsyncMock()
        solver = mock.MagicMock()

        anime = models.AnimeInfo(
            session="sess",
            title="Test",
            host="h",
            episodes=[
                models.EpisodeInfo(
                    1, "s1", "Ep1", "F", "jpn", "https://play/s1"
                )
            ],
            total=1,
        )
        orch = downloader.BatchOrchestrator(app_context, anime, http, solver)
        # First extract_stream fails with TimeoutError, second succeeds
        mock_info = models.StreamInfo("https://cdn.m3u8", [], "UA", "ref")
        call = 0

        async def fake_extract(*args, **kwargs):
            nonlocal call
            call += 1
            if call == 1:
                raise TimeoutError("timeout")
            return mock_info

        with mock.patch("pahebatcher.ui.dashboard.Dashboard") as MockDash:
            mock_dash = mock.MagicMock()
            MockDash.return_value = mock_dash
            with mock.patch(
                "pahebatcher.extract.kwik.extract_stream",
                side_effect=fake_extract,
            ):
                with mock.patch(
                    "pahebatcher.downloader.EpisodeDownloader"
                ) as MockDL:
                    mock_dl = mock.MagicMock()
                    mock_dl.run = mock.AsyncMock(
                        return_value=pathlib.Path("/tmp/out.mp4")
                    )
                    MockDL.return_value = mock_dl
                    with mock.patch(
                        "asyncio.sleep", new_callable=mock.AsyncMock
                    ):
                        results = await orch.download(anime.episodes)
                        assert results["s1"] == pathlib.Path("/tmp/out.mp4")
                        assert call == 2


class TestBatchRetry:
    async def test_batch_retries_failed_episodes(
        self,
        tmp_path: pathlib.Path,
        app_context: models.AppContext,
        sample_anime: models.AnimeInfo,
    ) -> None:
        app_context.output_dir = str(tmp_path / "out")
        app_context.cache_dir = tmp_path / "cache"
        app_context.auto_retry = 1  # one batch retry
        (tmp_path / "out").mkdir(parents=True, exist_ok=True)
        http = mock.MagicMock()
        solver = mock.MagicMock()
        anime = models.AnimeInfo(
            session="sess",
            title="Test",
            host="h",
            episodes=[
                models.EpisodeInfo(
                    1, "s1", "Ep1", "F", "jpn", "https://play/s1"
                ),
                models.EpisodeInfo(
                    2, "s2", "Ep2", "F", "jpn", "https://play/s2"
                ),
            ],
            total=2,
        )
        orch = downloader.BatchOrchestrator(app_context, anime, http, solver)
        mock_info = models.StreamInfo("https://cdn.m3u8", [], "UA", "ref")
        # First pass: ep1 fails, ep2 succeeds. Second pass (batch retry): ep1
        # succeeds
        call_counts = {"s1": 0, "s2": 0}

        async def fake_run(ep, info):
            # ep.session determines
            call_counts[ep.session] += 1
            if ep.session == "s1" and call_counts[ep.session] == 1:
                return None
            return pathlib.Path(f"/tmp/{ep.session}.mp4")

        with mock.patch("pahebatcher.ui.dashboard.Dashboard") as MockDash:
            mock_dash = mock.MagicMock()
            MockDash.return_value = mock_dash
            with mock.patch(
                "pahebatcher.extract.kwik.extract_stream",
                new_callable=mock.AsyncMock,
                return_value=mock_info,
            ):
                with mock.patch(
                    "pahebatcher.downloader.EpisodeDownloader"
                ) as MockDL:
                    mock_dl = mock.MagicMock()
                    mock_dl.run = mock.AsyncMock(side_effect=fake_run)
                    MockDL.return_value = mock_dl
                    with mock.patch(
                        "asyncio.sleep", new_callable=mock.AsyncMock
                    ):
                        results = await orch.download(anime.episodes)
                        # Both should succeed after batch retry
                        assert results["s1"] is not None
                        assert results["s2"] is not None
                        assert call_counts["s1"] == 2  # retried
                        assert call_counts["s2"] == 1
