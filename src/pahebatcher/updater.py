"""Checks GitHub for a newer release and installs it over the running app.

Pure standard library, like ``ffmpeg_setup``, so the GUI and the CLI can
both use it. The flow is:

1.  ``check`` asks the GitHub Releases API for the latest release and
    compares its version with the running one.
2.  ``download`` fetches the build for this platform into the app-data
    folder, verifying its size and SHA-256 checksum.
3.  ``install`` swaps the running executable for the new one and rolls
    back if anything goes wrong.
4.  ``restart`` launches the new version.
5.  ``cleanup_leftovers`` removes what an update leaves behind, namely
    the previous executable and any stale or partial downloads.

Only the packaged (frozen) executables can replace themselves. A source
or pip install cannot be updated safely from inside the program, so
``can_self_update`` is False there and callers should send the user to
``RELEASES_URL`` instead.
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from pahebatcher import config, ffmpeg_setup

REPO = "abdullahkhfb/pahebatcher"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"

_ALLOWED_HOSTS = ("github.com", "githubusercontent.com")
_HEADERS = {
    "User-Agent": f"pahebatcher/{config.VERSION}",
    "Accept": "application/vnd.github+json",
}
_MAX_BYTES = 500 * 1024 * 1024
_CHUNK = 256 * 1024

ProgressFn = Callable[[str, float | None], None]


class UpdateError(Exception):
    """Raised with a readable message when checking or installing fails."""


@dataclasses.dataclass(frozen=True)
class UpdateInfo:
    """A release that is newer than the running version.

    Attributes:
        version: The release's version without a leading ``v``.
        tag: The release's git tag.
        notes: The release notes. May be empty.
        page_url: The release's web page.
        asset_name: The file name of the build for this platform, or None
            if the release has none.
        asset_url: Where to download that build, or None.
        asset_size: The build's size in bytes, or 0 if unknown.
        sha256: The build's expected SHA-256 digest in lowercase hex, or
            None if the release does not publish one.
    """

    version: str
    tag: str
    notes: str
    page_url: str
    asset_name: str | None
    asset_url: str | None
    asset_size: int
    sha256: str | None


def parse_version(text: str) -> tuple[int, ...]:
    """Parses a version such as ``"v3.10.1"`` into numbers.

    Args:
        text: The version text. A leading ``v`` is allowed, and anything
            after the dotted numbers (such as ``-beta``) is ignored.

    Returns:
        The numeric parts, or an empty tuple if there are none.
    """
    match = re.match(r"\s*v?(\d+(?:\.\d+)*)", text)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _trim(parts: tuple[int, ...]) -> tuple[int, ...]:
    """Drops trailing zeros so that 3.9 and 3.9.0 compare equal."""
    end = len(parts)
    while end > 0 and parts[end - 1] == 0:
        end -= 1
    return parts[:end]


def is_newer(candidate: str, current: str = config.VERSION) -> bool:
    """Whether one version is newer than another.

    Args:
        candidate: The version to test.
        current: The version to compare against. Defaults to the running
            version.

    Returns:
        True if ``candidate`` is a valid version greater than
        ``current``.
    """
    new = _trim(parse_version(candidate))
    return bool(new) and new > _trim(parse_version(current))


def is_frozen() -> bool:
    """Whether the program runs as a packaged executable."""
    return bool(getattr(sys, "frozen", False))


def can_self_update() -> bool:
    """Whether the running program can replace itself.

    Returns:
        True for packaged executables, False for source and pip installs.
    """
    return is_frozen()


def asset_name() -> str:
    """Returns the release asset name for this platform's build."""
    platform = sys.platform  # a variable, so type checkers do not prune
    if platform == "win32":
        return "pahebatcher-gui-windows-x64.exe"
    if platform == "darwin":
        return "pahebatcher-macos-arm64"
    return "pahebatcher-linux-x64"


def updates_dir() -> pathlib.Path:
    """Returns the folder that holds downloaded updates."""
    return ffmpeg_setup.app_dir() / "updates"


def _allowed(url: str) -> bool:
    """Whether a URL is HTTPS and on a GitHub host."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and any(
        host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS
    )


def _get_json(url: str, timeout: float) -> dict[str, Any]:
    """Fetches a JSON object from the GitHub API.

    Args:
        url: The API URL.
        timeout: The request timeout in seconds.

    Returns:
        The decoded object.

    Raises:
        UpdateError: If the request fails or the answer is not a JSON
            object.
    """
    if not _allowed(url):
        raise UpdateError(f"Refusing to contact {url}")
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("No releases were found.") from exc
        if exc.code in (403, 429):
            raise UpdateError(
                "GitHub is rate-limiting requests. Try again later."
            ) from exc
        raise UpdateError(f"GitHub answered with HTTP {exc.code}.") from exc
    except OSError as exc:
        raise UpdateError(f"Could not reach GitHub: {exc}") from exc
    except ValueError as exc:
        raise UpdateError("GitHub sent an unreadable answer.") from exc
    if not isinstance(data, dict):
        raise UpdateError("GitHub sent an unexpected answer.")
    return data


def check(timeout: float = 15.0) -> UpdateInfo | None:
    """Looks for a release newer than the running version.

    Args:
        timeout: The network timeout in seconds.

    Returns:
        The newer release, or None if the running version is current.

    Raises:
        UpdateError: If GitHub cannot be reached or answers badly.
    """
    data = _get_json(API_URL, timeout)
    tag = str(data.get("tag_name") or "")
    if not is_newer(tag):
        return None
    wanted = asset_name()
    url: str | None = None
    size = 0
    digest: str | None = None
    for asset in data.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == wanted:
            candidate = str(asset.get("browser_download_url") or "")
            url = candidate if _allowed(candidate) else None
            size = int(asset.get("size") or 0)
            raw = str(asset.get("digest") or "")
            if raw.lower().startswith("sha256:"):
                digest = raw.split(":", 1)[1].strip().lower() or None
            break
    return UpdateInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        notes=str(data.get("body") or "").strip(),
        page_url=str(data.get("html_url") or RELEASES_URL),
        asset_name=wanted if url else None,
        asset_url=url,
        asset_size=size,
        sha256=digest,
    )


def download(
    info: UpdateInfo,
    dest_dir: pathlib.Path | None = None,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> pathlib.Path:
    """Downloads the release's build for this platform.

    The file is written to a temporary ``.part`` file and only renamed
    once its size and checksum have been verified.

    Args:
        info: The release to download.
        dest_dir: The folder to download into. Defaults to
            ``updates_dir()``.
        progress: Called with a stage name and a fraction from 0 to 1 (or
            None when unknown).
        cancel: An event that aborts the download when set, or None.

    Returns:
        The path of the verified download.

    Raises:
        UpdateError: If the release has no build for this platform, the
            download fails or is cancelled, or verification fails.
    """
    if not info.asset_url or not info.asset_name:
        raise UpdateError("This release has no build for your platform.")
    report: ProgressFn = progress or (lambda _stage, _frac: None)
    folder = dest_dir or updates_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / info.asset_name
    part = target.with_name(target.name + ".part")
    request = urllib.request.Request(info.asset_url, headers=_HEADERS)
    sha = hashlib.sha256()
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            if not _allowed(resp.geturl()):
                raise UpdateError("The download was redirected off GitHub.")
            total = int(resp.headers.get("Content-Length") or 0)
            total = total or info.asset_size
            if total > _MAX_BYTES:
                raise UpdateError("The download is unexpectedly large.")
            with open(part, "wb") as out:
                while chunk := resp.read(_CHUNK):
                    if cancel is not None and cancel.is_set():
                        raise UpdateError("Update cancelled.")
                    written += len(chunk)
                    if written > _MAX_BYTES:
                        raise UpdateError("The download is too large.")
                    out.write(chunk)
                    sha.update(chunk)
                    report("Downloading", written / total if total else None)
        if info.asset_size and written != info.asset_size:
            raise UpdateError("The download is incomplete. Try again.")
        report("Verifying", None)
        if info.sha256 and sha.hexdigest() != info.sha256:
            raise UpdateError("The download failed its checksum.")
        os.replace(part, target)
    except UpdateError:
        raise
    except OSError as exc:
        raise UpdateError(f"Download failed: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            part.unlink()
    return target


def install(
    downloaded: pathlib.Path,
    target: pathlib.Path | None = None,
) -> pathlib.Path:
    """Replaces the running executable with a downloaded build.

    The old executable is first renamed to ``<name>.old`` (Windows allows
    renaming a running program but not overwriting it), then the new one
    is moved into place. If that fails, the old one is restored.

    Args:
        downloaded: The verified download.
        target: The executable to replace. Defaults to the running one.

    Returns:
        The path of the installed executable.

    Raises:
        UpdateError: If the program cannot replace itself, or the files
            cannot be moved. The old executable is left in place.
    """
    if target is None:
        if not can_self_update():
            raise UpdateError(
                "Only the packaged app can update itself. Download the "
                "new version from the releases page instead."
            )
        target = pathlib.Path(sys.executable)
    old = target.with_name(target.name + ".old")
    with contextlib.suppress(OSError):
        old.unlink()
    try:
        if os.name != "nt":
            mode = target.stat().st_mode | 0o111
            os.chmod(downloaded, mode)
        os.replace(target, old)
    except OSError as exc:
        raise UpdateError(f"Cannot replace the running app: {exc}") from exc
    try:
        shutil.move(str(downloaded), str(target))
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.replace(old, target)
        raise UpdateError(f"Could not install the update: {exc}") from exc
    if os.name != "nt":
        # A running Unix program keeps working after it is unlinked.
        with contextlib.suppress(OSError):
            old.unlink()
    return target


def cleanup_leftovers(target: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Removes files that an update no longer needs.

    These are the previous executable (``<name>.old``) and everything in
    the downloads folder, such as old builds and partial downloads. Call
    it at startup, before any download begins.

    Args:
        target: The executable whose ``.old`` copy to remove. Defaults to
            the running executable when frozen.

    Returns:
        The paths that were removed.
    """
    removed: list[pathlib.Path] = []
    candidates: list[pathlib.Path] = []
    if target is None and is_frozen():
        target = pathlib.Path(sys.executable)
    if target is not None:
        candidates.append(target.with_name(target.name + ".old"))
    folder = updates_dir()
    if folder.is_dir():
        candidates.extend(folder.iterdir())
    for path in candidates:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed


def restart(target: pathlib.Path | None = None) -> None:
    """Starts a fresh copy of the app. The caller should then exit.

    Args:
        target: The executable to start. Defaults to the running one.
    """
    exe = target or pathlib.Path(sys.executable)
    env = dict(os.environ)
    # Tell a PyInstaller one-file build to unpack itself again instead of
    # reusing the folder of the process that is about to exit.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        [str(exe)],
        env=env,
        close_fds=True,
        creationflags=flags,
    )
