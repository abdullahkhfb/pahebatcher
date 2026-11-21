"""Locates FFmpeg, or downloads a static build into the app-data folder.

Pure standard library, so the GUI and the CLI can both use it. Nothing
here needs admin rights: the binary lives in
``<app data>/pahebatcher/ffmpeg`` and is found automatically.
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
import zipfile

# (stage, fraction 0..1 or None)
ProgressFn = Callable[[str, float | None], None]

EXE_NAME = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
# no console flash from the GUI
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_UA = {"User-Agent": "pahebatcher-ffmpeg-setup"}
_BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"

_cached: str | None = None


class FFmpegInstallError(Exception):
    """Raised with a readable message when installation fails."""


def app_dir() -> pathlib.Path:
    """Returns pahebatcher's per-user data directory.

    ``PAHEBATCHER_HOME`` overrides the platform default.
    """
    override = os.getenv("PAHEBATCHER_HOME")
    if override:
        return pathlib.Path(override)
    if os.name == "nt":
        return (
            pathlib.Path(
                os.getenv("LOCALAPPDATA")
                or pathlib.Path.home() / "AppData" / "Local"
            )
            / "pahebatcher"
        )
    if sys.platform == "darwin":
        return (
            pathlib.Path.home()
            / "Library"
            / "Application Support"
            / "pahebatcher"
        )
    return (
        pathlib.Path(
            os.getenv("XDG_DATA_HOME")
            or pathlib.Path.home() / ".local" / "share"
        )
        / "pahebatcher"
    )


def ffmpeg_dir() -> pathlib.Path:
    return app_dir() / "ffmpeg"


def _works(path: str) -> bool:
    """Checks that an FFmpeg executable runs.

    Args:
        path: The executable to try.

    Returns:
        True if ``-version`` exits successfully.
    """
    try:
        r = subprocess.run(
            [path, "-version"],
            capture_output=True,
            timeout=15,
            creationflags=NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:
        return False


def find_ffmpeg(refresh: bool = False) -> str | None:
    """Finds an FFmpeg executable.

    Looks on the PATH first, then in our own install, then for a copy next
    to the program. The result is cached.

    Args:
        refresh: Whether to ignore the cached result.

    Returns:
        The path, or None if FFmpeg was not found.
    """
    global _cached
    if _cached and not refresh and pathlib.Path(_cached).exists():
        return _cached
    candidates: list[str] = []
    if on_path := shutil.which("ffmpeg"):
        candidates.append(on_path)
    candidates.append(str(ffmpeg_dir() / EXE_NAME))
    if getattr(sys, "frozen", False):
        candidates.append(str(pathlib.Path(sys.executable).parent / EXE_NAME))
    candidates.append(str(pathlib.Path.cwd() / EXE_NAME))
    for c in candidates:
        if pathlib.Path(c).is_file() and _works(c):
            _cached = c
            return c
    _cached = None
    return None


def ffmpeg_exe() -> str:
    """Returns the command to run FFmpeg.

    Returns:
        The resolved path, or plain ``"ffmpeg"`` if none was found.
    """
    return find_ffmpeg() or "ffmpeg"


def is_installed() -> bool:
    return find_ffmpeg() is not None


def _sources() -> list[tuple[str, str]]:
    """Lists download candidates for this platform.

    Returns:
        ``(url, archive_kind)`` tuples in order of preference.
    """
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if os.name == "nt":
        if arm:
            return [(_BTBN + "ffmpeg-master-latest-winarm64-lgpl.zip", "zip")]
        return [
            (
                "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
                "zip",
            ),
            (_BTBN + "ffmpeg-master-latest-win64-lgpl.zip", "zip"),
        ]
    if sys.platform == "darwin":
        return [("https://evermeet.cx/ffmpeg/getrelease/zip", "zip")]
    arch = "linuxarm64" if arm else "linux64"
    return [(_BTBN + f"ffmpeg-master-latest-{arch}-lgpl.tar.xz", "tar")]


def _download(
    url: str,
    dest: pathlib.Path,
    progress: ProgressFn,
    cancel: threading.Event | None,
) -> None:
    """Downloads a file, reporting progress.

    Args:
        url: The URL to download.
        dest: The file to write.
        progress: Called with a stage name and a fraction from 0 to 1 (or
            None when unknown).
        cancel: An event that aborts the download when set, or None.

    Raises:
        FFmpegInstallError: If the download fails or is cancelled.
    """
    req = urllib.request.Request(url, headers=_UA)
    with (
        urllib.request.urlopen(req, timeout=30) as resp,
        open(dest, "wb") as out,
    ):
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while chunk := resp.read(256 * 1024):
            if cancel is not None and cancel.is_set():
                raise FFmpegInstallError("Cancelled")
            out.write(chunk)
            got += len(chunk)
            progress("Downloading", got / total if total else None)


def _extract(archive: pathlib.Path, kind: str, target: pathlib.Path) -> None:
    """Extracts just the ffmpeg binary from an archive.

    Ignores ffprobe, ffplay and documentation. The binary is written
    atomically through a temporary ``.part`` file.

    Args:
        archive: The downloaded archive.
        kind: The archive type, ``"zip"`` or ``"tar"``.
        target: Where to place the binary.

    Raises:
        FFmpegInstallError: If the archive does not contain ffmpeg.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    if kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            member = next(
                (
                    n
                    for n in zf.namelist()
                    if pathlib.Path(n).name == EXE_NAME and not n.endswith("/")
                ),
                None,
            )
            if member is None:
                raise FFmpegInstallError(
                    "ffmpeg was not found inside the download"
                )
            with zf.open(member) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    else:
        with tarfile.open(archive) as tf:
            tmember = next(
                (
                    m
                    for m in tf
                    if m.isfile() and pathlib.Path(m.name).name == EXE_NAME
                ),
                None,
            )
            if tmember is None:
                raise FFmpegInstallError(
                    "ffmpeg was not found inside the download"
                )
            fobj = tf.extractfile(tmember)
            if fobj is None:
                raise FFmpegInstallError(
                    "could not read ffmpeg from the download"
                )
            with fobj as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    if os.name != "nt":
        tmp.chmod(0o755)
    os.replace(tmp, target)


def install(
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> str:
    """Downloads and installs FFmpeg.

    Each candidate source is tried in turn until one works.

    Args:
        progress: Called with a stage name and a fraction from 0 to 1 (or
            None when unknown).
        cancel: An event that aborts the installation when set, or None.

    Returns:
        The path of the installed binary.

    Raises:
        FFmpegInstallError: If installation fails.
    """
    global _cached
    report: ProgressFn = progress or (lambda _s, _f: None)
    target = ffmpeg_dir() / EXE_NAME
    errors: list[str] = []
    ffmpeg_dir().mkdir(parents=True, exist_ok=True)

    for url, kind in _sources():
        fd, tmp_name = tempfile.mkstemp(suffix=f".{kind}", dir=ffmpeg_dir())
        os.close(fd)
        archive = pathlib.Path(tmp_name)
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
        "Could not download FFmpeg. Check your internet connection and try "
        "again.\n"
        + "\n".join(errors),
    )
