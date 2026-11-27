"""Tests for the built-in player's proxy and engine."""

from __future__ import annotations

import asyncio
import fractions
import pathlib
import shutil
import subprocess
import threading
import time
from typing import Any
import urllib.parse
import urllib.request

import pytest

from pahebatcher import hls_proxy, models
from pahebatcher.gui import engine

PROBE_TEXT = """\
Input #0, hls, from 'http://127.0.0.1:1/index.m3u8':
  Duration: 00:23:40.12, start: 0.000000, bitrate: N/A
  Stream #0:0: Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709), \
1920x1080 [SAR 1:1 DAR 16:9], 23.98 fps, 23.98 tbr, 90k tbn
  Stream #0:1: Audio: aac (LC), 48000 Hz, stereo, fltp
"""


# ── pure functions ───────────────────────────────────────────────────────
def test_parse_probe() -> None:
    info = engine.parse_probe(PROBE_TEXT)
    assert (info.width, info.height) == (1920, 1080)
    assert info.fps == fractions.Fraction(24000, 1001)
    assert info.duration == pytest.approx(1420.12)
    assert info.has_audio


def test_parse_probe_without_audio_or_duration() -> None:
    text = (
        "  Duration: N/A, start: 0.0\n"
        "  Stream #0:0: Video: h264, yuv420p, 1280x720, 25 fps, 25 tbr\n"
    )
    info = engine.parse_probe(text)
    assert not info.has_audio
    assert info.duration == 0.0
    assert info.fps == 25


def test_parse_probe_requires_video() -> None:
    with pytest.raises(engine.EngineError):
        engine.parse_probe("  Stream #0:0: Audio: aac, 48000 Hz\n")


@pytest.mark.parametrize(
    ("printed", "exact"),
    [
        (23.98, fractions.Fraction(24000, 1001)),
        (29.97, fractions.Fraction(30000, 1001)),
        (24.0, fractions.Fraction(24)),
        (25.0, fractions.Fraction(25)),
        (1000.0, fractions.Fraction(24000, 1001)),
    ],
)
def test_fps_fraction(printed: float, exact: fractions.Fraction) -> None:
    assert engine.fps_fraction(printed) == exact


def test_scaled_size_is_even_and_never_upscales() -> None:
    assert engine.scaled_size(1920, 1080, 960) == (960, 540)
    assert engine.scaled_size(640, 360, 1280) == (640, 360)
    w, h = engine.scaled_size(1919, 1079, 801)
    assert w % 2 == 0
    assert h % 2 == 0


@pytest.mark.parametrize(
    ("header", "status", "body", "content_range"),
    [
        (None, 200, b"0123456789", None),
        ("bytes=2-4", 206, b"234", "bytes 2-4/10"),
        ("bytes=7-", 206, b"789", "bytes 7-9/10"),
        ("bytes=-3", 206, b"789", "bytes 7-9/10"),
        ("bytes=4-99", 206, b"456789", "bytes 4-9/10"),
        ("bytes=20-30", 416, b"", "bytes */10"),
        ("garbage", 200, b"0123456789", None),
    ],
)
def test_apply_range(
    header: str | None, status: int, body: bytes, content_range: str | None
) -> None:
    got_status, got_body, extra = hls_proxy.apply_range(header, b"0123456789")
    assert got_status == status
    assert got_body == body
    assert extra.get("Content-Range") == content_range


# ── playlist rewriting ───────────────────────────────────────────────────
def _info(url: str) -> models.StreamInfo:
    return models.StreamInfo(
        url=url, cookies=[], user_agent="ua", referer="https://ref/"
    )


def test_playlist_is_rewritten_to_local_names() -> None:
    playlist = (
        "#EXTM3U\n"
        "#EXT-X-TARGETDURATION:4\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="../keys/a.key"\n'
        "#EXTINF:4.0,\n"
        "https://cdn.example/v/one.jpg\n"
        "#EXTINF:2.5,\n"
        "two.jpg\n"
        "#EXT-X-ENDLIST\n"
    )
    proxy = hls_proxy.HlsProxy(object(), _info("https://cdn.example/p/x.m3u8"))  # type: ignore[arg-type]
    out = proxy._rewrite(playlist, "https://cdn.example/p/x.m3u8")
    lines = out.splitlines()
    assert 'URI="k0.key"' in out
    assert "s0.ts" in lines
    assert "s1.ts" in lines
    assert proxy.duration == pytest.approx(6.5)
    assert proxy._remote["k0.key"] == "https://cdn.example/keys/a.key"
    assert proxy._remote["s0.ts"] == "https://cdn.example/v/one.jpg"
    assert proxy._remote["s1.ts"] == "https://cdn.example/p/two.jpg"
    assert "http" not in out


# ── integration with a real FFmpeg ───────────────────────────────────────
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"
)


class _FileHttp:
    """Stands in for ``HttpClient`` and reads ``file://`` URLs."""

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> bytes:
        return pathlib.Path(urllib.parse.urlparse(url).path).read_bytes()


class _FakeSound:
    """A sound card that consumes audio at real-time speed."""

    latency = 0.05

    def __init__(self, callback: Any) -> None:
        self._callback = callback
        self._stop = threading.Event()
        self.blocks = 0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        buf = bytearray(1024 * 4)
        next_t = time.monotonic()
        while not self._stop.is_set():
            self._callback(buf, 1024)
            self.blocks += 1
            next_t += 1024 / engine.SAMPLE_RATE
            time.sleep(max(0.0, next_t - time.monotonic()))

    def close(self) -> None:
        self._stop.set()


def _make_hls(
    folder: pathlib.Path, seconds: int = 8, encrypt: bool = False
) -> pathlib.Path:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24000/1001",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(seconds),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-g", "48", "-c:a", "aac",
    ]  # fmt: skip
    if encrypt:
        (folder / "enc.key").write_bytes(bytes(range(16)))
        (folder / "key.info").write_text(f"enc.key\n{folder / 'enc.key'}\n")
        cmd += ["-hls_key_info_file", str(folder / "key.info")]
    cmd += [
        "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(folder / "seg%d.ts"),
        str(folder / "index.m3u8"),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
    return folder / "index.m3u8"


class _Loop:
    """An event loop on a thread, like the GUI's bridge."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(30)


def _wait(cond: Any, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(params=[False, True], ids=["plain", "aes128"])
def served(request: pytest.FixtureRequest, tmp_path: pathlib.Path) -> Any:
    playlist = _make_hls(tmp_path, encrypt=request.param)
    loop = _Loop()
    proxy = hls_proxy.HlsProxy(
        _FileHttp(),  # type: ignore[arg-type]
        _info(playlist.as_uri()),
    )
    url = loop.run(proxy.start())
    yield proxy, url
    loop.run(proxy.close())
    loop.loop.call_soon_threadsafe(loop.loop.stop)


@needs_ffmpeg
def test_proxy_serves_playlist_segments_and_ranges(served: Any) -> None:
    proxy, url = served
    assert proxy.duration == pytest.approx(8.0, abs=0.5)
    text = urllib.request.urlopen(url, timeout=10).read().decode()
    assert "#EXTM3U" in text
    assert "http" not in text
    seg = urllib.parse.urljoin(url, "s0.ts")
    whole = urllib.request.urlopen(seg, timeout=10).read()
    assert len(whole) > 188
    if "AES-128" not in text:
        assert whole[0] == 0x47  # MPEG-TS sync byte
    req = urllib.request.Request(seg, headers={"Range": "bytes=0-187"})
    part = urllib.request.urlopen(req, timeout=10)
    assert part.status == 206
    assert part.read() == whole[:188]


@needs_ffmpeg
def test_engine_plays_in_sync_and_seeks(served: Any) -> None:
    _proxy, url = served
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    info = engine.probe(ffmpeg, url)
    assert (info.width, info.height) == (640, 360)
    assert info.has_audio
    assert info.fps == fractions.Fraction(24000, 1001)

    eng = engine.MediaEngine(ffmpeg, _FakeSound)
    try:
        eng.open(url, info)
        eng.play_from(0, 320)
        assert eng.audio_ok

        # paused: the first frame is shown but time does not move
        assert _wait(lambda: eng.take_frame() is not None, 15)
        assert eng.position() == pytest.approx(0, abs=0.1)

        eng.resume()
        t0 = time.monotonic()
        frames = 0
        last = -1.0
        while time.monotonic() - t0 < 2.0:
            if (f := eng.take_frame()) is not None:
                assert f[:2] == (320, 180)
                assert len(f[2]) == 320 * 180 * 3
                frames += 1
            pos = eng.position()
            assert pos >= last  # the clock never runs backwards
            last = pos
            time.sleep(0.005)
        # ≈ 2 s of audio played (minus start-up and the device latency)
        assert 1.0 < eng.position() < 2.3
        assert frames >= 20  # ≈ 24 fps

        # seek: the position jumps and keeps going from there
        eng.play_from(5.0, 320)
        assert _wait(lambda: eng.take_frame() is not None, 15)
        assert 4.8 < eng.position() < 5.8
        assert _wait(lambda: (eng.take_frame(), eng.position() > 5.5)[1], 5)

        # play to the end (the UI drains frames every tick, so do the same)
        assert _wait(lambda: (eng.take_frame(), eng.ended)[1], 15)
        assert eng.error is None
        assert eng.position() <= info.duration
    finally:
        eng.close()


@needs_ffmpeg
def test_engine_without_audio_device_uses_wall_clock(served: Any) -> None:
    _proxy, url = served
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    info = engine.probe(ffmpeg, url)
    eng = engine.MediaEngine(ffmpeg, lambda _cb: None)
    try:
        eng.open(url, info)
        eng.play_from(0, 320)
        assert not eng.audio_ok
        assert _wait(lambda: eng.take_frame() is not None, 15)
        eng.resume()
        time.sleep(1.5)
        eng.take_frame()
        assert 0.8 < eng.position() < 2.0
        eng.pause()
        frozen = eng.position()
        time.sleep(0.4)
        assert eng.position() == pytest.approx(frozen, abs=0.02)
    finally:
        eng.close()


@needs_ffmpeg
def test_engine_reports_ffmpeg_failure(tmp_path: pathlib.Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    eng = engine.MediaEngine(ffmpeg, lambda _cb: None)
    info = engine.MediaInfo(
        320, 180, fractions.Fraction(24), 10.0, has_audio=False
    )
    try:
        eng.open("http://127.0.0.1:9/none.m3u8", info)
        eng.play_from(0, 320)
        assert _wait(lambda: eng.error is not None, 20)
    finally:
        eng.close()
