"""Data models shared across pahebatcher."""

from __future__ import annotations

import dataclasses
import pathlib
import re


@dataclasses.dataclass
class EpisodeInfo:
    """One episode in one language and fansub variant.

    Attributes:
        number: The episode number. It is fractional for specials, such as
            5.5.
        session: animepahe's session identifier for this episode.
        title: The episode title. May be empty.
        fansub: The fansub group's name. May be empty.
        audio: The audio language code, ``"jpn"`` or ``"eng"``.
        play_url: URL of the episode's play page.
    """

    number: float
    session: str
    title: str
    fansub: str
    audio: str
    play_url: str

    @property
    def ep_str(self) -> str:
        """The episode number without a trailing ``.0``.

        For example ``"5"`` for 5.0 and ``"5.5"`` for 5.5.
        """
        return (
            str(int(self.number))
            if self.number == int(self.number)
            else str(self.number)
        )

    @property
    def label(self) -> str:
        """The episode as a Rich-markup line for lists and menus.

        A trailing SUB/DUB marker is stripped from the title (an em dash is
        shown if no title remains), and dubbed episodes get a DUB tag.
        """
        num = (
            int(self.number) if self.number == int(self.number) else self.number
        )
        dub = "  [yellow]DUB[/yellow]" if self.audio == "eng" else ""

        titl = (
            re.sub(
                r"\s+(?:DUB|SUB)\s*$", "", self.title or "", flags=re.I
            ).strip()
            or "\u2014"
        )
        return f"Ep [brand]{num:>4}[/brand]  {titl}{dub}"


@dataclasses.dataclass
class AnimeInfo:
    """A series together with its discovered episodes.

    Attributes:
        session: animepahe's session identifier for the series.
        title: The series title.
        host: The animepahe host the series was found on.
        total: The number of distinct episode numbers found.
        episodes: Every discovered episode variant (all languages).
        has_session: Whether a download session directory for this series
            already exists in the cache directory.
    """

    session: str
    title: str
    host: str
    total: int = 0
    episodes: list[EpisodeInfo] = dataclasses.field(default_factory=list)
    has_session: bool = False

    def get_variant(self, number: float, audio: str) -> EpisodeInfo | None:
        """Finds the variant of an episode in a given language.

        Args:
            number: The episode number.
            audio: The audio language code, ``"jpn"`` or ``"eng"``.

        Returns:
            The matching episode, or None if there is none.
        """
        for ep in self.episodes:
            if ep.number == number and ep.audio == audio:
                return ep
        return None

    def get_all_variants(self, number: float) -> list[EpisodeInfo]:
        """Finds every language variant of an episode.

        Args:
            number: The episode number.

        Returns:
            The matching episodes. The list is empty if there are none.
        """
        return [ep for ep in self.episodes if ep.number == number]


@dataclasses.dataclass
class StreamInfo:
    """A resolved HLS stream and what is needed to fetch it.

    Attributes:
        url: URL of the stream's m3u8 playlist.
        cookies: Cookies to send, each a dict with ``name`` and ``value``.
        user_agent: The User-Agent header to send.
        referer: The Referer header to send.
        title: The episode title, if known.
        audio: The audio language code, ``"jpn"`` or ``"eng"``.
        fansub: The fansub group's name, if known.
    """

    url: str
    cookies: list[dict[str, str]]
    user_agent: str
    referer: str
    title: str = ""
    audio: str = "jpn"
    fansub: str = ""

    @property
    def headers(self) -> dict[str, str]:
        """The HTTP headers needed to fetch the stream.

        Always contains User-Agent and Referer; Cookie is added when there are
        cookies.
        """
        hdrs: dict[str, str] = {
            "User-Agent": self.user_agent, "Referer": self.referer
        }
        if self.cookies:
            hdrs["Cookie"] = self.cookie_str
        return hdrs

    @property
    def cookie_str(self) -> str:
        """The cookies formatted as a Cookie header value.

        An empty string if there are no cookies.
        """
        if not self.cookies:
            return ""
        return "; ".join(f"{c['name']}={c['value']}" for c in self.cookies)


@dataclasses.dataclass
class AppContext:
    """Settings that apply to one download run.

    Attributes:
        output_dir: Directory the finished videos are written to.
        cache_dir: Directory for scan caches and temporary segments.
        quality: The preferred vertical resolution, such as 1080.
        audio_lang: The preferred audio language, ``"jpn"`` or ``"eng"``.
        max_parallel: How many episodes to download at once.
        hls_workers: How many segments to download at once per episode.
        keep_temp: Whether to keep temporary segment files after assembly.
        list_only: Whether to only list episodes instead of downloading.
        resolve_ahead: How many episodes to resolve ahead of the downloads.
        cache_ttl: Minutes a cached series scan stays valid.
        cookie_string: A user-supplied Cookie header value, or empty.
        auto_retry: How many times a failed episode is retried.
    """

    output_dir: str
    cache_dir: pathlib.Path
    quality: int
    audio_lang: str
    max_parallel: int
    hls_workers: int
    keep_temp: bool
    list_only: bool
    resolve_ahead: int = 999
    cache_ttl: int = 60
    cookie_string: str = ""
    auto_retry: int = 2

    @classmethod
    def defaults(cls) -> AppContext:
        """Creates a context with the built-in default settings.

        Returns:
            A new context.
        """

        return cls(
            output_dir="./downloads",
            cache_dir=pathlib.Path("pahe_cache"),
            quality=1080,
            audio_lang="jpn",
            max_parallel=2,
            hls_workers=24,
            keep_temp=False,
            list_only=False,
            resolve_ahead=999,
            cache_ttl=60,
            cookie_string="",
            auto_retry=2,
        )
