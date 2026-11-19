"""Locate FFmpeg, or download a static build into the user's app-data folder.

Pure standard library so the GUI and the CLI can both use it. Nothing here needs admin
rights: the binary lives in ``<app data>/pahebatcher/ffmpeg`` and is found automatically.
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

ProgressFn = Callable[[str, float | None], None]  # (stage, fraction 0..1 or None)

EXE_NAME = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash from the GUI
_UA = {"User-Agent": "pahebatcher-ffmpeg-setup"}
_BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"

_cached: str | None = None


class FFmpegInstallError(Exception):
    """Raised with a human-readable message when installation fails."""


def app_dir() -> Path:
    override = os.getenv("PAHEBATCHER_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "pahebatcher"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "pahebatcher"
    return Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "pahebatcher"


def ffmpeg_dir() -> Path:
    return app_dir() / "ffmpeg"


def _works(path: str) -> bool:
    try:
        r = subprocess.run(
            [path, "-version"], capture_output=True, timeout=15, creationflags=NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:
        return False


def find_ffmpeg(refresh: bool = False) -> str | None:
    """PATH first, then our own install, then a copy sitting next to the program."""
    global _cached
    if _cached and not refresh and Path(_cached).exists():
        return _cached
    candidates: list[str] = []
    if on_path := shutil.which("ffmpeg"):
        candidates.append(on_path)
    candidates.append(str(ffmpeg_dir() / EXE_NAME))
    if getattr(sys, "frozen", False):
        candidates.append(str(Path(sys.executable).parent / EXE_NAME))
    candidates.append(str(Path.cwd() / EXE_NAME))
    for c in candidates:
        if Path(c).is_file() and _works(c):
            _cached = c
            return c
    _cached = None
    return None


def ffmpeg_exe() -> str:
    """Command to run FFmpeg: the resolved path, or plain ``ffmpeg`` if none was found."""
    return find_ffmpeg() or "ffmpeg"


def is_installed() -> bool:
    return find_ffmpeg() is not None


def _sources() -> list[tuple[str, str]]:
    """Ordered download candidates as (url, archive kind) for this platform."""
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if os.name == "nt":
        if arm:
            return [(_BTBN + "ffmpeg-master-latest-winarm64-lgpl.zip", "zip")]
        return [
            ("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip", "zip"),
            (_BTBN + "ffmpeg-master-latest-win64-lgpl.zip", "zip"),
        ]
    if sys.platform == "darwin":
        return [("https://evermeet.cx/ffmpeg/getrelease/zip", "zip")]
    arch = "linuxarm64" if arm else "linux64"
    return [(_BTBN + f"ffmpeg-master-latest-{arch}-lgpl.tar.xz", "tar")]


def _download(url: str, dest: Path, progress: ProgressFn, cancel: threading.Event | None) -> None:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while chunk := resp.read(256 * 1024):
            if cancel is not None and cancel.is_set():
                raise FFmpegInstallError("Cancelled")
            out.write(chunk)
            got += len(chunk)
            progress("Downloading", got / total if total else None)


def _extract(archive: Path, kind: str, target: Path) -> None:
    """Pull just the ffmpeg binary out of the archive (ignores ffprobe/ffplay/docs)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    if kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            member = next(
                (n for n in zf.namelist() if Path(n).name == EXE_NAME and not n.endswith("/")), None,
            )
            if member is None:
                raise FFmpegInstallError("ffmpeg was not found inside the download")
            with zf.open(member) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    else:
        with tarfile.open(archive) as tf:
            tmember = next(
                (m for m in tf if m.isfile() and Path(m.name).name == EXE_NAME), None,
            )
            if tmember is None:
                raise FFmpegInstallError("ffmpeg was not found inside the download")
            fobj = tf.extractfile(tmember)
            if fobj is None:
                raise FFmpegInstallError("could not read ffmpeg from the download")
            with fobj as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    if os.name != "nt":
        tmp.chmod(0o755)
    os.replace(tmp, target)


def install(progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> str:
    """Download and install FFmpeg. Returns the path. Raises FFmpegInstallError on failure."""
    global _cached
    report: ProgressFn = progress or (lambda _s, _f: None)
    target = ffmpeg_dir() / EXE_NAME
    errors: list[str] = []
    ffmpeg_dir().mkdir(parents=True, exist_ok=True)

    for url, kind in _sources():
        fd, tmp_name = tempfile.mkstemp(suffix=f".{kind}", dir=ffmpeg_dir())
        os.close(fd)
        archive = Path(tmp_name)
        try:
            report("Downloading", 0.0)
            _download(url, archive, report, cancel)
            report("Extracting", None)
            _extract(archive, kind, target)
            report("Verifying", None)
            if not _works(str(target)):
                raise FFmpegInstallError("the downloaded ffmpeg did not start")
            _cached = str(target)
            report("Done", 1.0)
            return str(target)
        except FFmpegInstallError as exc:
            if str(exc) == "Cancelled":
                raise
            errors.append(f"{url}: {exc}")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
        finally:
            with contextlib.suppress(OSError):
                archive.unlink()
    raise FFmpegInstallError(
        "Could not download FFmpeg. Check your internet connection and try again.\n"
        + "\n".join(errors),
    )
