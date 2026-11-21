"""Tests for UI helpers."""

from __future__ import annotations

import pathlib
from unittest import mock

import rich.table

from pahebatcher import models
from pahebatcher.ui import tables


class TestTables:
    def test_episode_table(self, sample_anime: models.AnimeInfo) -> None:
        t = tables.episode_table(
            sample_anime, sample_anime.episodes, show_audio=True
        )
        assert isinstance(t, rich.table.Table)
        # Should have 4 columns (check, Ep, Title, Audio) when show_audio True
        assert len(t.columns) == 4

    def test_episode_table_no_audio(
        self,
        sample_anime: models.AnimeInfo,
    ) -> None:
        t = tables.episode_table(
            sample_anime, sample_anime.episodes, show_audio=False
        )
        assert len(t.columns) == 3

    def test_episode_table_dedup(self) -> None:
        anime = models.AnimeInfo(session="s", title="T", host="h", episodes=[
            models.EpisodeInfo(1, "sess-a", "T1", "F", "jpn", "u"),
            models.EpisodeInfo(1, "sess-b", "T1", "F", "eng", "u"),
            models.EpisodeInfo(2, "sess-c", "T2", "F", "jpn", "u"),
        ])
        t = tables.episode_table(anime, anime.episodes)
        # Should dedup by number, so 2 rows
        assert t.row_count == 2

    def test_search_results_table(self) -> None:
        results = [
            {
                "title": "Anime A",
                "type": "TV",
                "year": "2020",
                "episodes": "12",
                "score": "8.5",
            },
            {
                "title": "Anime B",
                "type": "Movie",
                "year": "2021",
                "episodes": "1",
                "score": "7.0",
            },
        ]
        t = tables.search_results_table(results, "Test")
        assert isinstance(t, rich.table.Table)
        assert t.row_count == 2

    def test_summary_table(self, tmp_path: pathlib.Path) -> None:
        out = tmp_path / "out.mp4"
        out.write_bytes(b"data")
        results = {"s1": out, "s2": None}
        eps = [
            models.EpisodeInfo(1, "s1", "T1", "F", "jpn", "u"),
            models.EpisodeInfo(2, "s2", "T2", "F", "jpn", "u"),
        ]
        t = tables.summary_table(results, eps, str(tmp_path))
        assert isinstance(t, rich.table.Table)
        assert t.row_count == 2
