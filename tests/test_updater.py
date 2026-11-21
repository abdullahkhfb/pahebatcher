"""Tests for the updater: version checks, download, install and cleanup.

The network is always mocked, and files only live in temporary folders.
"""

from __future__ import annotations

import hashlib
import io
import os
import pathlib
import threading
from typing import Any
import urllib.error

import pytest

from pahebatcher import updater
import pahebatcher.updater

GOOD_URL = "https://github.com/abdullahkhfb/pahebatcher/releases/download/x"
PAYLOAD = b"new executable bytes" * 100


class FakeResponse:
    """A minimal stand-in for what ``urlopen`` returns."""

    def __init__(
        self,
        body: bytes,
        url: str = GOOD_URL,
        length: int | None = None,
    ) -> None:
        self._buf = io.BytesIO(body)
        self._url = url
        n = len(body) if length is None else length
        self.headers = {"Content-Length": str(n)}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def geturl(self) -> str:
        return self._url


def make_info(**overrides: Any) -> pahebatcher.updater.UpdateInfo:
    fields: dict[str, Any] = {
        "version": "9.9.9",
        "tag": "v9.9.9",
        "notes": "",
        "page_url": "https://github.com/x/y/releases/tag/v9.9.9",
        "asset_name": "app.bin",
        "asset_url": GOOD_URL,
        "asset_size": len(PAYLOAD),
        "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
    }
    fields.update(overrides)
    return pahebatcher.updater.UpdateInfo(**fields)


def serve(monkeypatch: pytest.MonkeyPatch, resp: FakeResponse) -> None:
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda *a, **k: resp
    )


class TestVersions:
    def test_parse(self) -> None:
        assert updater.parse_version("v3.10.1") == (3, 10, 1)
        assert updater.parse_version("3.9") == (3, 9)
        assert updater.parse_version("4.0.0-beta") == (4, 0, 0)
        assert updater.parse_version("nightly") == ()

    def test_newer_is_numeric_not_textual(self) -> None:
        assert updater.is_newer("3.10", "3.9.0")
        assert not updater.is_newer("3.9.0", "3.10")

    def test_equal_versions_are_not_newer(self) -> None:
        assert not updater.is_newer("v3.9", "3.9.0")
        assert not updater.is_newer("3.9.0", "3.9.0")

    def test_garbage_is_never_newer(self) -> None:
        assert not updater.is_newer("", "3.9.0")
        assert not updater.is_newer("latest", "3.9.0")


class TestCheck:
    def release(self, tag: str, assets: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "tag_name": tag,
            "body": " notes ",
            "html_url": "https://github.com/x/y/releases/tag/" + tag,
            "assets": assets,
        }

    def test_up_to_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            updater, "_get_json", lambda *a, **k: self.release("v0.0.1", [])
        )
        assert updater.check() is None

    def test_newer_release_with_asset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asset = {
            "name": updater.asset_name(),
            "browser_download_url": GOOD_URL,
            "size": 123,
            "digest": "sha256:ABCDEF",
        }
        monkeypatch.setattr(
            updater,
            "_get_json",
            lambda *a, **k: self.release("v99.0.0", [asset]),
        )
        info = updater.check()
        assert info is not None
        assert info.version == "99.0.0"
        assert info.notes == "notes"
        assert info.asset_url == GOOD_URL
        assert info.asset_size == 123
        assert info.sha256 == "abcdef"

    def test_newer_release_without_matching_asset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = {"name": "other.zip", "browser_download_url": GOOD_URL}
        monkeypatch.setattr(
            updater,
            "_get_json",
            lambda *a, **k: self.release("v99.0.0", [other]),
        )
        info = updater.check()
        assert info is not None
        assert info.asset_url is None
        assert info.asset_name is None

    def test_asset_url_off_github_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        evil = {
            "name": updater.asset_name(),
            "browser_download_url": "https://evil.example/app.exe",
        }
        monkeypatch.setattr(
            updater,
            "_get_json",
            lambda *a, **k: self.release("v99.0.0", [evil]),
        )
        info = updater.check()
        assert info is not None
        assert info.asset_url is None


class TestGetJson:
    def test_http_errors_become_update_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for code, fragment in ((404, "No releases"), (403, "rate-limit")):

            def boom(*a: Any, _code: int = code, **k: Any) -> None:
                raise urllib.error.HTTPError(
                    updater.API_URL, _code, "x", {}, None  # type: ignore[arg-type]
                )

            monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
            with pytest.raises(pahebatcher.updater.UpdateError, match=fragment):
                updater.check()

    def test_network_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a: Any, **k: Any) -> None:
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
        with pytest.raises(
            pahebatcher.updater.UpdateError, match="Could not reach GitHub"
        ):
            updater.check()

    def test_refuses_foreign_hosts(self) -> None:
        with pytest.raises(pahebatcher.updater.UpdateError, match="Refusing"):
            updater._get_json("https://evil.example/x", 1)
        with pytest.raises(pahebatcher.updater.UpdateError, match="Refusing"):
            updater._get_json("http://api.github.com/x", 1)


class TestDownload:
    def test_success_is_verified_and_atomic(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve(monkeypatch, FakeResponse(PAYLOAD))
        seen: list[tuple[str, float | None]] = []
        path = updater.download(
            make_info(), tmp_path, lambda s, f: seen.append((s, f))
        )
        assert path == tmp_path / "app.bin"
        assert path.read_bytes() == PAYLOAD
        assert not list(tmp_path.glob("*.part"))
        assert seen[-1][0] == "Verifying"
        assert any(f == 1.0 for _, f in seen)

    def test_checksum_mismatch_leaves_nothing(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve(monkeypatch, FakeResponse(PAYLOAD))
        with pytest.raises(pahebatcher.updater.UpdateError, match="checksum"):
            updater.download(make_info(sha256="0" * 64), tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_truncated_download(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve(monkeypatch, FakeResponse(PAYLOAD[:50], length=len(PAYLOAD)))
        with pytest.raises(pahebatcher.updater.UpdateError, match="incomplete"):
            updater.download(make_info(), tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_cancel(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve(monkeypatch, FakeResponse(PAYLOAD))
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(pahebatcher.updater.UpdateError, match="cancelled"):
            updater.download(make_info(), tmp_path, cancel=cancel)
        assert list(tmp_path.iterdir()) == []

    def test_redirect_off_github_is_rejected(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve(monkeypatch, FakeResponse(PAYLOAD, url="https://evil.example/a"))
        with pytest.raises(pahebatcher.updater.UpdateError, match="redirected"):
            updater.download(make_info(), tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_no_asset_for_platform(self, tmp_path: pathlib.Path) -> None:
        info = make_info(asset_url=None, asset_name=None)
        with pytest.raises(pahebatcher.updater.UpdateError, match="no build"):
            updater.download(info, tmp_path)

    def test_network_error_is_wrapped(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a: Any, **k: Any) -> None:
            raise urllib.error.URLError("dropped")

        monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
        with pytest.raises(
            pahebatcher.updater.UpdateError, match="Download failed"
        ):
            updater.download(make_info(), tmp_path)


class TestInstall:
    def test_replaces_target_and_removes_old_on_unix(
        self, tmp_path: pathlib.Path
    ) -> None:
        target = tmp_path / "app"
        target.write_bytes(b"old")
        new = tmp_path / "dl" / "app.bin"
        new.parent.mkdir()
        new.write_bytes(b"new")
        assert updater.install(new, target) == target
        assert target.read_bytes() == b"new"
        assert not new.exists()
        old = tmp_path / "app.old"
        assert old.exists() == (os.name == "nt")

    def test_rolls_back_if_the_move_fails(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "app"
        target.write_bytes(b"old")
        new = tmp_path / "app.bin"
        new.write_bytes(b"new")

        def boom(*a: Any, **k: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(updater.shutil, "move", boom)
        with pytest.raises(
            pahebatcher.updater.UpdateError, match="Could not install"
        ):
            updater.install(new, target)
        assert target.read_bytes() == b"old"

    def test_refuses_when_not_packaged(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(updater, "is_frozen", lambda: False)
        with pytest.raises(
            pahebatcher.updater.UpdateError, match="packaged app"
        ):
            updater.install(tmp_path / "x")


class TestCleanup:
    def test_removes_old_exe_and_stale_downloads_only(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        downloads = tmp_path / "updates"
        downloads.mkdir()
        (downloads / "app.bin").write_bytes(b"x")
        (downloads / "app.bin.part").write_bytes(b"x")
        monkeypatch.setattr(updater, "updates_dir", lambda: downloads)
        exe = tmp_path / "app"
        exe.write_bytes(b"current")
        (tmp_path / "app.old").write_bytes(b"previous")
        keep = tmp_path / "pahebatcher.toml"
        keep.write_text("quality = 1080")

        removed = updater.cleanup_leftovers(exe)

        assert len(removed) == 3
        assert exe.exists()
        assert keep.exists()
        assert not (tmp_path / "app.old").exists()
        assert list(downloads.iterdir()) == []

    def test_nothing_to_clean(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(updater, "updates_dir", lambda: tmp_path / "none")
        monkeypatch.setattr(updater, "is_frozen", lambda: False)
        assert updater.cleanup_leftovers() == []


def test_restart_resets_pyinstaller_environment(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_popen(cmd: list[str], **kw: Any) -> None:
        calls.append((cmd, kw))

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
    updater.restart(tmp_path / "app")
    cmd, kw = calls[0]
    assert cmd == [str(tmp_path / "app")]
    assert kw["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
