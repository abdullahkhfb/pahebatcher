"""Test fixtures for pahebatcher."""

from __future__ import annotations

import json
import pathlib
from unittest import mock

import pytest

from pahebatcher import models


@pytest.fixture
def sample_episodes() -> list[models.EpisodeInfo]:
    return [
        models.EpisodeInfo(
            1,
            "s1",
            "Episode 1",
            "SubsPlease",
            "jpn",
            "https://animepahe.com/play/uuid/s1",
        ),
        models.EpisodeInfo(
            2,
            "s2",
            "Episode 2",
            "SubsPlease",
            "jpn",
            "https://animepahe.com/play/uuid/s2",
        ),
        models.EpisodeInfo(
            3,
            "s3",
            "Episode 3",
            "SubsPlease",
            "jpn",
            "https://animepahe.com/play/uuid/s3",
        ),
        models.EpisodeInfo(
            4,
            "s4",
            "Episode 4",
            "SubsPlease",
            "jpn",
            "https://animepahe.com/play/uuid/s4",
        ),
    ]


@pytest.fixture
def sample_anime(sample_episodes: list[models.EpisodeInfo]) -> models.AnimeInfo:
    return models.AnimeInfo(
        session="test-session",
        title="Test Anime",
        host="animepahe.com",
        total=4,
        episodes=sample_episodes,
        has_session=False,
    )


@pytest.fixture
def sample_stream_info() -> models.StreamInfo:
    return models.StreamInfo(
        url="http://test.m3u8",
        cookies=[{"name": "test", "value": "val"}],
        user_agent="TestUA/1.0",
        referer="http://ref",
        title="Test Episode",
        audio="jpn",
        fansub="SubsPlease",
    )


@pytest.fixture
def app_context(tmp_path: pathlib.Path) -> models.AppContext:
    return models.AppContext(
        output_dir=str(tmp_path / "downloads"),
        cache_dir=tmp_path / "cache",
        quality=1080,
        audio_lang="jpn",
        max_parallel=2,
        hls_workers=8,
        keep_temp=False,
        list_only=False,
    )


@pytest.fixture
def mock_solver() -> mock.MagicMock:
    solver = mock.MagicMock()
    solver.request = mock.AsyncMock()
    solver.fetch_json = mock.AsyncMock()
    solver.fetch_html = mock.AsyncMock()
    solver.ping = mock.AsyncMock(return_value=True)
    solver.start = mock.AsyncMock()
    solver.close = mock.AsyncMock()
    solver.destroy_session = mock.AsyncMock()
    return solver


@pytest.fixture
def mock_http() -> mock.MagicMock:
    http = mock.MagicMock()
    http.get = mock.AsyncMock(return_value=b"fake segment data")
    http.start = mock.AsyncMock()
    http.close = mock.AsyncMock()
    return http


@pytest.fixture
def sample_m3u8_content() -> str:
    return (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:10\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXTINF:10.0,\n"
        "segment1.ts\n"
        "#EXTINF:10.0,\n"
        "segment2.ts\n"
    )


@pytest.fixture
def sample_m3u8_aes() -> str:
    return (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:10\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXT-X-KEY:METHOD=AES-128,URI=\"https://key.url\",IV=0x1234567890ABCDEF1234567890ABCDEF\n"
        "#EXTINF:10.0,\n"
        "segment1.ts\n"
    )
