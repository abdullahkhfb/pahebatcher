"""Tests for utility functions."""

from __future__ import annotations

from pahebatcher import models, utils


class TestSanitize:
    def test_basic(self) -> None:
        assert utils.sanitize("My Anime: Season 1") == "My_Anime_Season_1"

    def test_special_chars(self) -> None:
        assert utils.sanitize("...!!File??") == "...File"

    def test_multiple_underscores(self) -> None:
        assert utils.sanitize("Test___Name") == "Test_Name"

    def test_spaces_only(self) -> None:
        assert utils.sanitize("  hello  world  ") == "hello_world"


class TestEpPrefix:
    def test_single_digit(self) -> None:
        assert utils.ep_prefix("5") == "005"

    def test_double_digit(self) -> None:
        assert utils.ep_prefix("12") == "012"

    def test_float(self) -> None:
        assert utils.ep_prefix("5.5") == "005.5"

    def test_invalid(self) -> None:
        assert utils.ep_prefix("abc") == "abc"


class TestFmtBytes:
    def test_bytes(self) -> None:
        assert utils.fmt_bytes(500) == "500.0 B"

    def test_kb(self) -> None:
        assert utils.fmt_bytes(1024) == "1.0 KB"

    def test_mb(self) -> None:
        assert utils.fmt_bytes(1048576) == "1.0 MB"

    def test_gb(self) -> None:
        assert utils.fmt_bytes(1073741824) == "1.0 GB"


class TestCompactEpRange:
    def test_empty(self) -> None:
        assert utils.compact_ep_range([]) == "none"

    def test_single(self) -> None:
        eps = [models.EpisodeInfo(1, "s", "T", "F", "jpn", "u")]
        assert utils.compact_ep_range(eps) == "1"

    def test_consecutive(self) -> None:
        eps = [
            models.EpisodeInfo(1, "s1", "T", "F", "jpn", "u"),
            models.EpisodeInfo(2, "s2", "T", "F", "jpn", "u"),
            models.EpisodeInfo(3, "s3", "T", "F", "jpn", "u"),
        ]
        result = utils.compact_ep_range(eps)
        assert "1" in result
        assert "3" in result

    def test_with_gaps(self) -> None:
        eps = [
            models.EpisodeInfo(1, "s1", "T", "F", "jpn", "u"),
            models.EpisodeInfo(3, "s3", "T", "F", "jpn", "u"),
            models.EpisodeInfo(5, "s5", "T", "F", "jpn", "u"),
        ]
        result = utils.compact_ep_range(eps)
        assert "1" in result
        assert "3" in result
        assert "5" in result


class TestAudioBadge:
    def test_jpn(self) -> None:
        assert utils.audio_badge("jpn") == "SUB"

    def test_eng(self) -> None:
        assert utils.audio_badge("eng") == "DUB"
