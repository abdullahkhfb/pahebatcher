"""Extended scanner tests — cache coherence."""

from __future__ import annotations

import json
import pathlib
import time
from unittest import mock

import pytest

from pahebatcher import models
import pahebatcher.extract.scanner


class TestCachePath:
    def test_cache_path_sanitize(self, tmp_path: pathlib.Path) -> None:
        p = pahebatcher.extract.scanner._cache_path(
            tmp_path, "sess123", "My Anime: Test"
        )
        assert p.name == "_scan_cache.json"
        assert "My_Anime_Test_sess123" in str(p)

    def test_load_cache_expired(self, tmp_path: pathlib.Path) -> None:
        cache_path = tmp_path / "Anime_sess" / "_scan_cache.json"
        cache_path.parent.mkdir(parents=True)
        data = {
            "cached_at": time.time() - 7200,  # 2 hours ago
            "title": "Anime",
            "session": "sess",
            "host": "animepahe.com",
            "total": 1,
            "episodes": [],
        }
        cache_path.write_text(json.dumps(data))
        result = pahebatcher.extract.scanner._load_cache(
            cache_path, cache_ttl=60
        )
        assert result is None  # expired

    def test_load_cache_fresh(self, tmp_path: pathlib.Path) -> None:
        cache_path = tmp_path / "Anime_sess" / "_scan_cache.json"
        cache_path.parent.mkdir(parents=True)
        data = {
            "cached_at": time.time(),
            "title": "Anime",
            "session": "sess",
            "host": "animepahe.com",
            "total": 1,
            "episodes": [
                {
                    "number": 1,
                    "session": "s1",
                    "title": "T",
                    "fansub": "F",
                    "audio": "jpn",
                    "play_url": "u",
                },
            ],
        }
        cache_path.write_text(json.dumps(data))
        result = pahebatcher.extract.scanner._load_cache(
            cache_path, cache_ttl=60
        )
        assert result is not None
        assert result.title == "Anime"
        assert len(result.episodes) == 1

    def test_load_cache_disabled(self, tmp_path: pathlib.Path) -> None:
        cache_path = tmp_path / "Anime_sess" / "_scan_cache.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text("{}")
        assert (
            pahebatcher.extract.scanner._load_cache(
                cache_path, cache_ttl=0
            )
            is None
        )

    def test_load_cache_invalid_json(self, tmp_path: pathlib.Path) -> None:
        cache_path = tmp_path / "Anime_sess" / "_scan_cache.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text("not json")
        assert (
            pahebatcher.extract.scanner._load_cache(
                cache_path, cache_ttl=60
            )
            is None
        )


class TestScanCacheStable:
    async def test_scan_uses_glob_for_cache(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        # Pre-populate cache with different title folder than placeholder
        real_title = "Real Anime"
        from pahebatcher import utils

        safe = utils.sanitize(real_title)
        sess = "test-sess-123"
        cache_file = tmp_path / f"{safe}_{sess}" / "_scan_cache.json"
        cache_file.parent.mkdir(parents=True)
        data = {
            "cached_at": time.time(),
            "title": real_title,
            "session": sess,
            "host": "animepahe.com",
            "total": 1,
            "episodes": [
                {
                    "number": 1,
                    "session": "ep1",
                    "title": "Ep1",
                    "fansub": "F",
                    "audio": "jpn",
                    "play_url": "u",
                },
            ],
        }
        cache_file.write_text(json.dumps(data))
        solver = mock.MagicMock()
        scanner = pahebatcher.extract.scanner.AnimePaheScanner(
            solver, "animepahe.com", sess
        )
        # Should hit cache without calling fetch
        anime = await scanner.scan(tmp_path, cache_ttl=60)
        assert anime.title == real_title
        assert len(anime.episodes) == 1
        # Ensure no network call attempted (solver not called)
        solver.fetch_json.assert_not_called()  # type: ignore

    async def test_scan_fallback_when_no_cache(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        solver = mock.MagicMock()
        solver.fetch_json = mock.AsyncMock(return_value=None)
        solver.fetch_html = mock.AsyncMock(return_value=None)
        scanner = pahebatcher.extract.scanner.AnimePaheScanner(
            solver, "animepahe.com", "no-cache-sess"
        )
        # Mock discover to return session itself, and _fetch_title to return
        # Unknown
        with mock.patch.object(
            pahebatcher.extract.scanner.AnimePaheScanner,
            "discover_all_sessions",
            new_callable=mock.AsyncMock,
            return_value=["no-cache-sess"],
        ):
            with mock.patch.object(
                scanner,
                "_fetch_title",
                new_callable=mock.AsyncMock,
                return_value="Unknown Anime",
            ):
                with mock.patch.object(
                    scanner,
                    "_fetch_page",
                    new_callable=mock.AsyncMock,
                    return_value={"data": [], "last_page": 1},
                ):
                    anime = await scanner.scan(tmp_path, cache_ttl=60)
                    assert anime.title == "Unknown Anime"
                    assert anime.episodes == []


def _ep_page(
    episodes: list[dict[str, object]],
    last_page: int = 1,
) -> dict[str, object]:
    return {"data": episodes, "last_page": last_page}


def _ep_item(num: int, sess: str = "s1") -> dict[str, object]:
    return {
        "episode": num,
        "session": sess,
        "title": "T",
        "fansub": "F",
        "audio": "jpn",
    }


class TestScanPaginationResilience:
    async def test_page_retry_then_success(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        solver = mock.MagicMock()
        scanner = pahebatcher.extract.scanner.AnimePaheScanner(
            solver, "animepahe.com", "sess"
        )
        page_ok = _ep_page([_ep_item(1)], last_page=1)
        with mock.patch.object(
            pahebatcher.extract.scanner.AnimePaheScanner,
            "discover_all_sessions",
            new_callable=mock.AsyncMock,
            return_value=["sess"],
        ):
            with mock.patch.object(
                scanner,
                "_fetch_title",
                new_callable=mock.AsyncMock,
                return_value="Anime",
            ):
                with mock.patch.object(
                    pahebatcher.extract.scanner.AnimePaheScanner,
                    "_fetch_page",
                    new_callable=mock.AsyncMock,
                    side_effect=[None, page_ok],
                ) as mock_fetch:
                    anime = await scanner.scan(tmp_path, cache_ttl=60)
                    assert len(anime.episodes) == 1
                    assert mock_fetch.await_count == 2
                    # Successful scan is cached
                    assert list(tmp_path.rglob("_scan_cache.json")) != []

    async def test_mid_pagination_failure_not_cached(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        solver = mock.MagicMock()
        scanner = pahebatcher.extract.scanner.AnimePaheScanner(
            solver, "animepahe.com", "sess"
        )
        page1 = _ep_page([_ep_item(1)], last_page=2)
        with mock.patch.object(
            pahebatcher.extract.scanner.AnimePaheScanner,
            "discover_all_sessions",
            new_callable=mock.AsyncMock,
            return_value=["sess"],
        ):
            with mock.patch.object(
                scanner,
                "_fetch_title",
                new_callable=mock.AsyncMock,
                return_value="Anime",
            ):
                with mock.patch.object(
                    pahebatcher.extract.scanner.AnimePaheScanner,
                    "_fetch_page",
                    new_callable=mock.AsyncMock,
                    side_effect=[page1, None, None, None],
                ):
                    anime = await scanner.scan(tmp_path, cache_ttl=60)
                    # Episodes before the failed page are kept, but nothing is
                    # cached
                    assert len(anime.episodes) == 1
                    assert list(tmp_path.rglob("_scan_cache.json")) == []

    async def test_empty_data_page_breaks_cleanly_and_caches(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        solver = mock.MagicMock()
        scanner = pahebatcher.extract.scanner.AnimePaheScanner(
            solver, "animepahe.com", "sess"
        )
        with mock.patch.object(
            pahebatcher.extract.scanner.AnimePaheScanner,
            "discover_all_sessions",
            new_callable=mock.AsyncMock,
            return_value=["sess"],
        ):
            with mock.patch.object(
                scanner,
                "_fetch_title",
                new_callable=mock.AsyncMock,
                return_value="Anime",
            ):
                with mock.patch.object(
                    pahebatcher.extract.scanner.AnimePaheScanner,
                    "_fetch_page",
                    new_callable=mock.AsyncMock,
                    return_value={"data": [], "last_page": 1},
                ):
                    anime = await scanner.scan(tmp_path, cache_ttl=60)
                    assert anime.episodes == []
                    assert list(tmp_path.rglob("_scan_cache.json")) != []

    async def test_page1_failure_not_cached(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        solver = mock.MagicMock()
        scanner = pahebatcher.extract.scanner.AnimePaheScanner(
            solver, "animepahe.com", "sess"
        )
        with mock.patch.object(
            pahebatcher.extract.scanner.AnimePaheScanner,
            "discover_all_sessions",
            new_callable=mock.AsyncMock,
            return_value=["sess"],
        ):
            with mock.patch.object(
                scanner,
                "_fetch_title",
                new_callable=mock.AsyncMock,
                return_value="Anime",
            ):
                with mock.patch.object(
                    pahebatcher.extract.scanner.AnimePaheScanner,
                    "_fetch_page",
                    new_callable=mock.AsyncMock,
                    return_value=None,
                ):
                    anime = await scanner.scan(tmp_path, cache_ttl=60)
                    assert anime.episodes == []
                    assert list(tmp_path.rglob("_scan_cache.json")) == []
