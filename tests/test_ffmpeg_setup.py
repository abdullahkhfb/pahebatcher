"""FFmpeg locate/install logic (no real downloads) and the GUI's thread bridge."""

from __future__ import annotations

import io
import tarfile
import time
import zipfile
from pathlib import Path

import pytest

from pahebatcher import ffmpeg_setup as fs


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAHEBATCHER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fs, "_cached", None)


def _fake_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ffmpeg-7.0/doc/readme.txt", "docs")
        zf.writestr("ffmpeg-7.0/bin/ffprobe", "no")
        zf.writestr(f"ffmpeg-7.0/bin/{fs.EXE_NAME}", "BINARY")


class TestFind:
    def test_prefers_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        exe = tmp_path / fs.EXE_NAME
        exe.write_text("x")
        monkeypatch.setattr(fs.shutil, "which", lambda _n: str(exe))
        monkeypatch.setattr(fs, "_works", lambda _p: True)
        assert fs.find_ffmpeg() == str(exe)
        assert fs.is_installed()

    def test_falls_back_to_app_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fs.shutil, "which", lambda _n: None)
        monkeypatch.setattr(fs, "_works", lambda _p: True)
        local = fs.ffmpeg_dir() / fs.EXE_NAME
        local.parent.mkdir(parents=True)
        local.write_text("x")
        assert fs.find_ffmpeg(refresh=True) == str(local)

    def test_missing_returns_none_and_plain_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fs.shutil, "which", lambda _n: None)
        monkeypatch.setattr(fs.Path, "cwd", staticmethod(lambda: Path("/nonexistent-dir")))
        assert fs.find_ffmpeg(refresh=True) is None
        assert fs.ffmpeg_exe() == "ffmpeg"
        assert not fs.is_installed()

    def test_broken_binary_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fs.shutil, "which", lambda _n: None)
        monkeypatch.setattr(fs.Path, "cwd", staticmethod(lambda: Path("/nonexistent-dir")))
        monkeypatch.setattr(fs, "_works", lambda _p: False)
        local = fs.ffmpeg_dir() / fs.EXE_NAME
        local.parent.mkdir(parents=True)
        local.write_text("corrupt")
        assert fs.find_ffmpeg(refresh=True) is None


class TestExtract:
    def test_zip_pulls_only_ffmpeg(self, tmp_path: Path) -> None:
        archive = tmp_path / "a.zip"
        _fake_zip(archive)
        target = tmp_path / "out" / fs.EXE_NAME
        fs._extract(archive, "zip", target)
        assert target.read_text() == "BINARY"
        assert not list(target.parent.glob("*.part"))

    def test_tar(self, tmp_path: Path) -> None:
        archive = tmp_path / "a.tar.xz"
        data = b"BINARY"
        with tarfile.open(archive, "w:xz") as tf:
            info = tarfile.TarInfo(f"ffmpeg-x/bin/{fs.EXE_NAME}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        target = tmp_path / "out" / fs.EXE_NAME
        fs._extract(archive, "tar", target)
        assert target.read_bytes() == data

    def test_missing_binary_raises(self, tmp_path: Path) -> None:
        archive = tmp_path / "a.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("readme.txt", "nothing here")
        with pytest.raises(fs.FFmpegInstallError):
            fs._extract(archive, "zip", tmp_path / "out" / fs.EXE_NAME)


class TestInstall:
    def test_success_reports_progress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fs, "_sources", lambda: [("http://example/ffmpeg.zip", "zip")])
        monkeypatch.setattr(fs, "_works", lambda _p: True)

        def fake_dl(_url: str, dest: Path, progress: fs.ProgressFn, _c: object) -> None:
            _fake_zip(dest)
            progress("Downloading", 1.0)

        monkeypatch.setattr(fs, "_download", fake_dl)
        stages: list[str] = []
        path = fs.install(lambda s, _f: stages.append(s))
        assert Path(path).read_text() == "BINARY"
        assert stages[0] == "Downloading"
        assert stages[-1] == "Done"
        assert not list(fs.ffmpeg_dir().glob("*.zip"))  # temp archive cleaned up

    def test_falls_through_to_second_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fs, "_sources", lambda: [("http://bad", "zip"), ("http://good", "zip")])
        monkeypatch.setattr(fs, "_works", lambda _p: True)

        def fake_dl(url: str, dest: Path, _p: fs.ProgressFn, _c: object) -> None:
            if "bad" in url:
                raise OSError("boom")
            _fake_zip(dest)

        monkeypatch.setattr(fs, "_download", fake_dl)
        assert Path(fs.install()).exists()

    def test_all_sources_fail_has_friendly_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fs, "_sources", lambda: [("http://bad", "zip")])

        def fake_dl(*_a: object) -> None:
            raise OSError("offline")

        monkeypatch.setattr(fs, "_download", fake_dl)
        with pytest.raises(fs.FFmpegInstallError, match="internet connection"):
            fs.install()

    def test_cancel_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import threading

        monkeypatch.setattr(fs, "_sources", lambda: [("http://a", "zip"), ("http://b", "zip")])
        calls: list[str] = []

        def fake_dl(url: str, *_a: object) -> None:
            calls.append(url)
            raise fs.FFmpegInstallError("Cancelled")

        monkeypatch.setattr(fs, "_download", fake_dl)
        with pytest.raises(fs.FFmpegInstallError, match="Cancelled"):
            fs.install(cancel=threading.Event())
        assert calls == ["http://a"]


class TestBridge:
    def test_coroutine_result_reaches_callback(self) -> None:
        from pahebatcher.gui.bridge import Bridge

        bridge = Bridge()
        got: list[int] = []

        async def work() -> int:
            return 42

        bridge.run(work(), ok=got.append)
        deadline = time.time() + 3
        while not got and time.time() < deadline:
            bridge.drain()
            time.sleep(0.01)
        bridge.shutdown()
        assert got == [42]

    def test_errors_reach_err_callback(self) -> None:
        from pahebatcher.gui.bridge import Bridge

        bridge = Bridge()
        errs: list[BaseException] = []

        async def work() -> None:
            raise ValueError("nope")

        bridge.run(work(), err=errs.append)
        deadline = time.time() + 3
        while not errs and time.time() < deadline:
            bridge.drain()
            time.sleep(0.01)
        bridge.shutdown()
        assert isinstance(errs[0], ValueError)
