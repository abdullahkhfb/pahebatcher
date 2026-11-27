"""The built-in player's engine: FFmpeg decodes, this module keeps time.

One FFmpeg process demuxes and decodes the stream and writes two raw
outputs: RGB frames on its stdout, and 16-bit PCM audio to a local TCP
socket (a second pipe is not portable to Windows). Reader threads fill a
small frame queue and an audio buffer.

The sound card is the master clock: the playback position is the amount of
audio the device has consumed, so picture and sound cannot drift apart and
a stall in the audio (buffering) pauses the picture too. The user interface
asks ``take_frame`` which frame is due, drops any that are late and draws
the latest one. Without an audio device the clock falls back to wall time.

Nothing here imports Tk, so the engine can be tested without a display.
"""

from __future__ import annotations

import array
import collections
from collections.abc import Callable
import dataclasses
import fractions
import logging
import re
import socket
import subprocess
import threading
import time
from typing import Any, Protocol

from pahebatcher import ffmpeg_setup

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
_BYTES_PER_FRAME = 2 * CHANNELS  # 16-bit samples
_AUDIO_BYTES_PER_SEC = SAMPLE_RATE * _BYTES_PER_FRAME
_MAX_AUDIO_BYTES = 8 * _AUDIO_BYTES_PER_SEC
_MAX_FRAMES = 24
_DEFAULT_FPS = fractions.Fraction(24000, 1001)


class EngineError(Exception):
    """Raised with a readable message when playback cannot start."""


@dataclasses.dataclass(frozen=True)
class MediaInfo:
    """What FFmpeg reports about a stream.

    Attributes:
        width: The video width in pixels.
        height: The video height in pixels.
        fps: The frame rate as an exact fraction.
        duration: The length in seconds, or 0.0 if unknown.
        has_audio: Whether the stream has an audio track.
    """

    width: int
    height: int
    fps: fractions.Fraction
    duration: float
    has_audio: bool


# ── probing ──────────────────────────────────────────────────────────────
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\S+.*?:\s*Video:(.*)")
_AUDIO_RE = re.compile(r"Stream #\S+.*?:\s*Audio:")
_SIZE_RE = re.compile(r"[\s,](\d{2,5})x(\d{2,5})(?=[\s,\[])")
_FPS_RE = re.compile(r"([\d.]+)\s*fps")
_TBR_RE = re.compile(r"([\d.]+)\s*tbr")


def fps_fraction(value: float) -> fractions.Fraction:
    """Turns a printed frame rate into the exact rate FFmpeg should use.

    FFmpeg prints 23.976 as ``23.98``, and ``fps=23.98`` would slowly
    drift against the audio, so the common NTSC rates are snapped to their
    exact ratios.

    Args:
        value: The frame rate as FFmpeg printed it.

    Returns:
        The exact rate. Rates outside 5 to 60 fall back to 24000/1001.
    """
    if not 5 <= value <= 60:
        return _DEFAULT_FPS
    for num in (24000, 30000, 60000):
        if abs(value - num / 1001) < 0.01:
            return fractions.Fraction(num, 1001)
    return fractions.Fraction(value).limit_denominator(1000)


def parse_probe(text: str) -> MediaInfo:
    """Reads the report ``ffmpeg -i`` prints on stderr.

    Args:
        text: The stderr text.

    Returns:
        The stream's properties.

    Raises:
        EngineError: If there is no video stream in the report.
    """
    video = _VIDEO_RE.search(text)
    if video is None:
        raise EngineError("The stream has no video track.")
    line = video.group(1)
    size = _SIZE_RE.search(line)
    if size is None:
        raise EngineError("Could not read the video size.")
    fps_m = _FPS_RE.search(line) or _TBR_RE.search(line)
    fps = fps_fraction(float(fps_m.group(1))) if fps_m else _DEFAULT_FPS
    dur = 0.0
    if d := _DURATION_RE.search(text):
        dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))
    return MediaInfo(
        width=int(size.group(1)),
        height=int(size.group(2)),
        fps=fps,
        duration=dur,
        has_audio=_AUDIO_RE.search(text) is not None,
    )


def probe(ffmpeg: str, url: str, timeout: float = 45.0) -> MediaInfo:
    """Asks FFmpeg what a stream contains.

    This blocks, so call it from a worker thread.

    Args:
        ffmpeg: The FFmpeg executable.
        url: The stream URL.
        timeout: How long to wait, in seconds.

    Returns:
        The stream's properties.

    Raises:
        EngineError: If FFmpeg cannot open the stream.
    """
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-i", url],
            capture_output=True,
            timeout=timeout,
            creationflags=ffmpeg_setup.NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise EngineError("Timed out while opening the stream.")
    except OSError as exc:
        raise EngineError(f"Could not run FFmpeg: {exc}")
    return parse_probe(proc.stderr.decode("utf-8", errors="replace"))


def scaled_size(src_w: int, src_h: int, target_w: int) -> tuple[int, int]:
    """Picks the size FFmpeg decodes to.

    Decoding to roughly the window size keeps the pipe and the drawing
    cheap. FFmpeg's RGB conversion wants even numbers.

    Args:
        src_w: The video's width.
        src_h: The video's height.
        target_w: The wanted width, such as the window's.

    Returns:
        A tuple ``(width, height)``, never larger than the source and never
        smaller than 160 pixels wide.
    """
    w = max(160, min(src_w, target_w))
    h = max(2, round(w * src_h / src_w))
    return w - w % 2, h - h % 2


# ── audio output ─────────────────────────────────────────────────────────
class AudioSink(Protocol):
    """What the engine needs from an audio device."""

    latency: float

    def close(self) -> None:
        """Stops the device and releases it."""


AudioFactory = Callable[[Callable[[Any, int], None]], "AudioSink | None"]


def open_sounddevice(
    callback: Callable[[Any, int], None],
) -> AudioSink | None:
    """Opens the default output device with ``sounddevice``.

    Args:
        callback: Called as ``callback(buffer, frames)`` from the audio
            thread. It must fill ``buffer`` with ``frames`` stereo frames.

    Returns:
        The running stream, or None if there is no usable audio device or
        PortAudio is not installed.
    """
    try:
        # Lazy: loads PortAudio, which can be missing; video-only then
        import sounddevice

        stream = sounddevice.RawOutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=1024,
            latency="high",
            callback=lambda out, frames, _t, _status: callback(out, frames),
        )
        stream.start()
        return stream  # type: ignore[no-any-return]
    except Exception as exc:  # no device / no PortAudio: play silently
        log.warning("Audio output unavailable: %s", exc)
        return None


def _scale_volume(chunk: bytes, volume: float) -> bytes:
    """Scales 16-bit samples.

    Args:
        chunk: Interleaved native-endian 16-bit samples.
        volume: The gain, from 0.0 to 1.0.

    Returns:
        The scaled samples.
    """
    samples = array.array("h")
    samples.frombytes(chunk)
    return array.array("h", [int(s * volume) for s in samples]).tobytes()


# ── one FFmpeg run ───────────────────────────────────────────────────────
class _Run:
    """The state of one FFmpeg process, from a seek to the next one."""

    def __init__(
        self,
        start: float,
        width: int,
        height: int,
        fps: fractions.Fraction,
        has_audio: bool,
    ) -> None:
        self.start = start
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_bytes = width * height * 3
        self.has_audio = has_audio
        self.frames: collections.deque[tuple[float, bytes]] = (
            collections.deque()
        )
        self.cond = threading.Condition()
        self.video_eof = False
        self.frames_read = 0
        self.audio = bytearray()
        self.audio_lock = threading.Lock()
        self.audio_eof = not has_audio
        self.consumed = 0  # bytes of audio the device has played
        self.audio_done = False  # the audio ran out: time now follows the wall
        self.stopped = False
        self.proc: subprocess.Popen[bytes] | None = None
        self.server: socket.socket | None = None
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=8)

    def stop(self) -> None:
        """Kills FFmpeg and wakes every thread that waits on this run."""
        self.stopped = True
        if self.proc is not None:
            try:
                self.proc.kill()
            except OSError:
                pass
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
        with self.cond:
            self.cond.notify_all()


class MediaEngine:
    """Plays one HLS stream through FFmpeg.

    All public methods are meant to be called from one thread, the user
    interface's. The audio callback and the reader threads touch only the
    run they belong to.
    """

    def __init__(
        self,
        ffmpeg: str,
        audio_factory: AudioFactory | None = None,
    ) -> None:
        """Initializes the engine without playing anything.

        Args:
            ffmpeg: The FFmpeg executable.
            audio_factory: Opens the audio device. Defaults to
                ``open_sounddevice``; tests pass a fake.
        """
        self._ffmpeg = ffmpeg
        self._audio_factory = audio_factory or open_sounddevice
        self._sink: AudioSink | None = None
        self._sink_tried = False
        self._url = ""
        self._info: MediaInfo | None = None
        self._run: _Run | None = None
        self._playing = False
        self._volume = 1.0
        self._underrun = False
        self._wall_pos = 0.0
        self._wall_t = time.monotonic()

    # ── properties ───────────────────────────────────────────────────────
    @property
    def audio_ok(self) -> bool:
        """Whether sound is playing through a device."""
        return self._sink is not None

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        self._volume = max(0.0, min(1.0, value))

    @property
    def duration(self) -> float:
        """The stream's length in seconds, or 0.0 if unknown."""
        return self._info.duration if self._info else 0.0

    @property
    def buffering(self) -> bool:
        """Whether playback is waiting for data."""
        run = self._run
        if not self._playing or run is None:
            return False
        if run.video_eof and not run.frames:
            return False
        if self._sink is not None and run.has_audio:
            return self._underrun
        return not run.frames

    @property
    def ended(self) -> bool:
        """Whether the whole stream has been played."""
        run = self._run
        if run is None or run.proc is None:
            return False
        if not (run.video_eof and not run.frames):
            return False
        if run.has_audio and self._sink is not None:
            with run.audio_lock:
                return run.audio_eof and len(run.audio) < _BYTES_PER_FRAME
        return True

    @property
    def error(self) -> str | None:
        """A readable reason why FFmpeg stopped unexpectedly, or None."""
        run = self._run
        if run is None or run.proc is None or run.stopped:
            return None
        if not run.video_eof:
            return None
        code = run.proc.poll()
        if code in (None, 0):
            return None
        tail = " ".join(run.stderr_tail).strip()
        return tail[-300:] or f"FFmpeg stopped (exit code {code})."

    # ── control ──────────────────────────────────────────────────────────
    def open(self, url: str, info: MediaInfo) -> None:
        """Loads a stream, stopping the previous one. Does not start it.

        Args:
            url: The playlist URL.
            info: The result of ``probe`` for that URL.
        """
        self._stop_run()
        self._url, self._info = url, info
        self._playing = False

    def play_from(self, position: float, target_width: int) -> None:
        """Starts decoding at a position, for a first start or a seek.

        The play or pause state is kept, so seeking while paused shows the
        new frame and stays paused.

        Args:
            position: Where to start, in seconds.
            target_width: The width to decode to, such as the window's.

        Raises:
            EngineError: If nothing is loaded or FFmpeg cannot start.
        """
        info = self._info
        if info is None:
            raise EngineError("Nothing is loaded.")
        if not self._sink_tried:
            self._sink_tried = True
            self._sink = self._audio_factory(self._audio_callback)
        self._stop_run()
        position = max(0.0, position)
        if info.duration:
            position = min(position, max(0.0, info.duration - 0.5))
        w, h = scaled_size(info.width, info.height, target_width)
        use_audio = info.has_audio and self._sink is not None
        run = _Run(position, w, h, info.fps, use_audio)
        self._spawn(run)
        self._underrun = use_audio
        self._wall_pos, self._wall_t = position, time.monotonic()
        self._run = run

    def resume(self) -> None:
        self._playing = True
        self._wall_t = time.monotonic()

    def pause(self) -> None:
        self.position()  # bank the time that has passed
        self._playing = False

    def toggle(self) -> None:
        if self._playing:
            self.pause()
        else:
            self.resume()

    def stop(self) -> None:
        """Stops decoding. The stream stays loaded for ``play_from``."""
        self._stop_run()
        self._playing = False

    def close(self) -> None:
        """Stops playback and releases the audio device."""
        self._stop_run()
        sink, self._sink = self._sink, None
        if sink is not None:
            try:
                sink.close()
            except Exception:  # best-effort cleanup; must not raise
                pass

    # ── clock and frames ─────────────────────────────────────────────────
    def position(self) -> float:
        """The current playback position in seconds.

        The sound card's progress while there is audio to play. When the
        audio has run out (it often ends a little before the video, or never
        arrives), time continues by the wall clock so the picture can finish.
        """
        run = self._run
        if run is None:
            return 0.0
        now = time.monotonic()
        if self._sink is not None and run.has_audio and not run.audio_done:
            played = run.consumed / _AUDIO_BYTES_PER_SEC
            pos = run.start + max(0.0, played - self._sink.latency)
            with run.audio_lock:
                drained = run.audio_eof and len(run.audio) < _BYTES_PER_FRAME
            if drained:
                run.audio_done = True
                self._wall_pos, self._wall_t = pos, now
        else:
            # With no sound the picture is the clock, so time waits while no
            # frame is queued (buffering); after the sound it simply runs on.
            if self._playing and (run.frames or run.audio_done):
                self._wall_pos += now - self._wall_t
            self._wall_t = now
            pos = self._wall_pos
        if self._info and self._info.duration:
            pos = min(pos, self._info.duration)
        return pos

    def take_frame(self) -> tuple[int, int, bytes] | None:
        """Returns the frame that is due now, if it has not been shown.

        Frames whose time has already passed are dropped, so a slow draw
        never makes the picture fall behind the sound.

        Returns:
            A tuple ``(width, height, rgb_bytes)``, or None if there is no
            new frame yet.
        """
        run = self._run
        if run is None:
            return None
        pos = self.position()
        due: bytes | None = None
        with run.cond:
            while run.frames and run.frames[0][0] <= pos + 0.002:
                due = run.frames.popleft()[1]
            if due is not None:
                run.cond.notify_all()
        return (run.width, run.height, due) if due is not None else None

    # ── internals ────────────────────────────────────────────────────────
    def _audio_callback(self, outdata: Any, frames: int) -> None:
        """Fills the sound card's buffer. Runs on the audio thread."""
        need = frames * _BYTES_PER_FRAME
        run = self._run
        chunk = b""
        if run is not None and run.has_audio and self._playing:
            with run.audio_lock:
                avail = len(run.audio)
                take = min(need, avail - avail % _BYTES_PER_FRAME)
                chunk = bytes(run.audio[:take])
                del run.audio[:take]
                run.consumed += take
            if chunk and self._volume < 0.999:
                chunk = _scale_volume(chunk, self._volume)
        self._underrun = (
            run is not None
            and run.has_audio
            and len(chunk) < need
            and not run.audio_eof
        )
        outdata[:need] = chunk + bytes(need - len(chunk))

    def _stop_run(self) -> None:
        run, self._run = self._run, None
        if run is not None:
            run.stop()

    def _spawn(self, run: _Run) -> None:
        """Starts FFmpeg and the threads that read its output.

        Args:
            run: The run to start.

        Raises:
            EngineError: If FFmpeg cannot be started.
        """
        cmd = [self._ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if run.start > 0.05:
            cmd += ["-ss", f"{run.start:.3f}"]
        cmd += [
            "-i", self._url,
            "-map", "0:v:0",
            "-vf",
            f"fps={run.fps.numerator}/{run.fps.denominator},"
            f"scale={run.width}:{run.height}:flags=fast_bilinear",
            "-pix_fmt", "rgb24",
            "-f", "rawvideo", "pipe:1",
        ]  # fmt: skip
        if run.has_audio:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            run.server = server
            cmd += [
                "-map", "0:a:0", "-vn",
                "-af", "aresample=async=1000:first_pts=0",
                "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                "-f", "s16le", f"tcp://127.0.0.1:{server.getsockname()[1]}",
            ]  # fmt: skip
        try:
            run.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=ffmpeg_setup.NO_WINDOW,
            )
        except OSError as exc:
            run.stop()
            raise EngineError(f"Could not start FFmpeg: {exc}")
        threads = [
            (self._read_video, "pb-video"),
            (self._read_stderr, "pb-stderr"),
        ]
        if run.has_audio:
            threads.append((self._read_audio, "pb-audio"))
        for target, name in threads:
            threading.Thread(
                target=target, args=(run,), daemon=True, name=name
            ).start()

    @staticmethod
    def _read_video(run: _Run) -> None:
        """Reads raw frames and queues them with their presentation time."""
        assert run.proc is not None and run.proc.stdout is not None
        index = 0
        try:
            while not run.stopped:
                data = run.proc.stdout.read(run.frame_bytes)
                if len(data) < run.frame_bytes:
                    break
                pts = run.start + index / float(run.fps)
                index += 1
                with run.cond:
                    while len(run.frames) >= _MAX_FRAMES and not run.stopped:
                        run.cond.wait(0.1)
                    if run.stopped:
                        break  # type: ignore[unreachable]  # other thread
                    run.frames.append((pts, data))
                    run.frames_read += 1
        except (OSError, ValueError):
            pass  # the pipe closed because the run was stopped
        finally:
            with run.cond:
                run.video_eof = True
                run.cond.notify_all()

    @staticmethod
    def _read_audio(run: _Run) -> None:
        """Accepts FFmpeg's audio connection and buffers the PCM data."""
        server = run.server
        assert server is not None
        try:
            server.settimeout(20)
            conn, _ = server.accept()
        except OSError:
            run.audio_eof = True
            return
        try:
            with conn:
                conn.settimeout(1.0)
                while not run.stopped:
                    try:
                        data = conn.recv(65536)
                    except TimeoutError:
                        continue
                    if not data:
                        break
                    while not run.stopped:
                        with run.audio_lock:
                            if len(run.audio) < _MAX_AUDIO_BYTES:
                                run.audio += data
                                break
                        time.sleep(0.02)  # the device has not drained it yet
        except OSError:
            pass  # the connection closed because the run was stopped
        finally:
            run.audio_eof = True

    @staticmethod
    def _read_stderr(run: _Run) -> None:
        """Keeps FFmpeg's last error lines for the failure message."""
        assert run.proc is not None and run.proc.stderr is not None
        try:
            for raw in run.proc.stderr:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    run.stderr_tail.append(line)
        except (OSError, ValueError):
            pass
