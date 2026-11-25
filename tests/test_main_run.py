"""Characterisation test for ``main.run`` (the CLI session driver).

Runs ``run`` against scripted fakes (config, solver, HTTP client, scanner,
prompts, menu answers, watchlist, sessions, streaming, downloads, console and
clock) across many scenarios and compares everything it does with a recording
made from the original, un-split implementation. A refactor must not change
any of it.

To re-record after an intentional behaviour change, run the test once with
the environment variable ``UPDATE_MAIN_GOLDEN=1``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import pathlib
from typing import Any

import pytest
import rich.panel
import rich.prompt
import rich.table

from pahebatcher import config_manager, downloader, http, models, solver, stream
from pahebatcher import main as cli
from pahebatcher import sessions as sessions_mod
from pahebatcher import watchlist as wl
from pahebatcher.extract import scanner
from pahebatcher.ui import console as ui_console
from pahebatcher.ui import prompts

GOLDEN = pathlib.Path(__file__).parent / "data" / "main_run_golden.json"
URL1 = "https://animepahe.pw/anime/123e4567-e89b-12d3-a456-426614174000"
S1 = "123e4567-e89b-12d3-a456-426614174000"
URL2 = "https://animepahe.pw/anime/223e4567-e89b-12d3-a456-426614174000"
S2 = "223e4567-e89b-12d3-a456-426614174000"


class _Run:
    def __init__(self, tmp: str, script: dict[str, Any]) -> None:
        self.tmp, self.script, self.events = tmp, script, []  # type: ignore[var-annotated]

    def norm(self, x: Any) -> Any:
        if isinstance(x, str):
            for v in {self.tmp, os.path.realpath(self.tmp), self.tmp.replace("\\", "/")}:
                x = x.replace(v, "<T>")
            return x.replace("\\", "/")
        if isinstance(x, (list, tuple)):
            return [self.norm(i) for i in x]
        if isinstance(x, dict):
            return {str(k): self.norm(v) for k, v in x.items()}
        if x is None or isinstance(x, (int, float, bool)):
            return x
        return self.norm(str(x))

    def ev(self, *a: Any) -> None:
        self.events.append([self.norm(x) for x in a])


class _Table:
    def __init__(self, *a: Any, **k: Any) -> None:
        self.cols: list[str] = []
        self.rows: list[list[str]] = []

    def add_column(self, name: str, **k: Any) -> None:
        self.cols.append(name)

    def add_row(self, *r: str) -> None:
        self.rows.append(list(r))

    def __repr__(self) -> str:
        return f"Table({self.cols},{self.rows})"


class _Panel:
    def __init__(self, body: str, **k: Any) -> None:
        self.body, self.title, self.border = body, str(k.get("title")), str(k.get("border_style"))

    def __repr__(self) -> str:
        return f"Panel({self.body!r},{self.title},{self.border})"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def time(self) -> float:
        self.t += 1.0
        return self.t


def _install(mp: pytest.MonkeyPatch, run: _Run) -> None:
    ev, s = run.ev, run.script

    class Console:
        def print(self, *a: Any, **k: Any) -> None:
            ev("print", " ".join(str(x) for x in a), {str(p): str(q) for p, q in k.items()})

    class CM:
        def load(self) -> None:
            ev("cm.load")

        def get(self, key: str) -> Any:
            v = {"quality": 1080, "audio_lang": "jpn", "max_parallel": 3, "hls_workers": 16,
                 "resolve_ahead": 2, "cache_ttl": 30, "cookie_string": "c=1", "auto_retry": 1}
            v.update(s["cm"])
            return v[key]

        def set(self, key: str, value: Any) -> None:
            ev("cm.set", key, value)

        def save(self) -> None:
            ev("cm.save")
            if s["save_raises"]:
                raise OSError("read-only")

        def is_customized(self) -> bool:
            return bool(s["customized"])

    class Solver:
        backend_name = "fake-browser"

        @classmethod
        def from_config(cls, cm: Any) -> Solver:
            ev("solver.from_config")
            return cls()

        async def start(self) -> None:
            ev("solver.start")

        async def ping(self) -> bool:
            ev("solver.ping")
            return bool(s["ping"])

        async def close(self) -> None:
            ev("solver.close")

    class Http:
        def __init__(self, n: int) -> None:
            ev("http.init", n)

        async def start(self) -> None:
            ev("http.start")

        async def close(self) -> None:
            ev("http.close")

    class Scanner:
        def __init__(self, solver_: Any, host: str, session: str) -> None:
            self.host, self.session = host, session

        async def scan(self, cache_dir: Any, prefer_audio: Any = None, cache_ttl: int = 0) -> Any:
            ev("scan", self.host, self.session, prefer_audio, cache_ttl, str(cache_dir))
            return s["scans"][self.session]

    class Orch:
        def __init__(self, ctx: Any, anime: Any, http_: Any, solver_: Any) -> None:
            ev("orch.init", *dataclasses.astuple(ctx), anime.title)

        async def download(self, chosen: Any) -> Any:
            ev("download", [(e.number, e.audio) for e in chosen])
            return {e.session: (None if e.number in s["dl_fail"] else "/out") for e in chosen}

    asks = list(s["asks"])

    def ask(prompt: str, **k: Any) -> str:
        ev("ask", prompt, {p: str(q) for p, q in k.items()})
        return asks.pop(0)

    async def discovery(solver_: Any, host: str, initial: Any = None) -> Any:
        ev("discovery", host)
        return s["discovery"]

    def select(anime: Any) -> Any:
        ev("select_episodes")
        return list(s["selected"](anime))

    def wizard(defaults: Any, mode: str = "download") -> Any:
        ev("wizard", mode)
        return dataclasses.replace(defaults, quality=720, audio_lang="eng", max_parallel=2, hls_workers=9)

    def confirm(anime: Any, chosen: Any, ctx: Any) -> bool:
        ev("confirm", [e.number for e in chosen])
        return bool(s["confirm"])

    async def browser() -> Any:
        ev("wl.browser")
        url, entry = s["wl_browser"]
        return url, (WE(run.tmp, S2) if entry == "ENTRY2" else entry)

    def load_entries(path: Any = None) -> Any:
        ev("wl.load")
        if isinstance(s["wl_count"], Exception):
            raise s["wl_count"]
        return [object()] * s["wl_count"]

    def record(session: str, title: str, nums: Any) -> bool:
        ev("wl.record", session, title, nums)
        if isinstance(s["record"], Exception):
            raise s["record"]
        return bool(s["record"])

    def get_sessions(cache_dir: Any) -> Any:
        ev("sessions.get", str(cache_dir))
        return s["sessions"]

    def sessions_run(cache_dir: Any) -> Any:
        ev("sessions.run", str(cache_dir))
        return s["sessions_run"]

    async def run_stream(ctx: Any, anime: Any, chosen: Any, solver_: Any) -> None:
        ev("stream", *dataclasses.astuple(ctx), [e.number for e in chosen])

    mp.setattr(ui_console, "console", Console())
    mp.setattr(ui_console, "print_banner", lambda: ev("banner"))
    mp.setattr(ui_console, "section", lambda t: ev("section", t))
    mp.setattr(rich.table, "Table", _Table)
    mp.setattr(rich.panel, "Panel", _Panel)
    mp.setattr(rich.prompt.Prompt, "ask", staticmethod(ask))
    mp.setattr(config_manager, "ConfigManager", CM)
    mp.setattr(solver, "Solver", Solver)
    mp.setattr(solver, "no_solver_hint", lambda: "HINT")
    mp.setattr(http, "HttpClient", Http)
    mp.setattr(scanner, "AnimePaheScanner", Scanner)
    mp.setattr(downloader, "BatchOrchestrator", Orch)
    mp.setattr(prompts, "interactive_discovery", discovery)
    mp.setattr(prompts, "select_episodes", select)
    mp.setattr(prompts, "wizard_config", wizard)
    mp.setattr(prompts, "confirm_download", confirm)
    mp.setattr(wl, "run_watchlist_browser", browser)
    mp.setattr(wl, "load_entries", load_entries)
    mp.setattr(wl, "record_watchlist_download", record)
    mp.setattr(sessions_mod, "get_sessions", get_sessions)
    mp.setattr(sessions_mod, "run", sessions_run)
    mp.setattr(stream, "run_stream", run_stream)
    mp.setattr(cli, "time", _Clock())
    mp.setattr("logging.basicConfig", lambda *a, **k: ev("logging.basicConfig"))


def eps(nums: Any, audios: Any = ("jpn",)) -> list[models.EpisodeInfo]:
    return [models.EpisodeInfo(number=float(n), session=f"s{n}{a}", title=f"T{n}" if n != 2 else "",
                               fansub="F", audio=a, play_url=f"https://x/{n}{a}")
            for n in nums for a in audios]


def A(session: str = S1, title: str = "Show A", ep: Any = None, partial: bool = False) -> models.AnimeInfo:
    ep = eps([1, 2, 3, 5]) if ep is None else ep
    return models.AnimeInfo(session=session, title=title, host="animepahe.pw", total=len(ep),
                            episodes=ep, has_session=partial)


def WE(t: str, session: str = S1, **kw: Any) -> wl.WatchlistEntry:
    d: dict[str, Any] = dict(
        url=f"https://animepahe.pw/anime/{session}", session=session, host="animepahe.pw",
        title="Show A", quality=480, audio_lang="eng", output_dir=os.path.join(t, "wlout"),
        max_parallel=5, hls_workers=40, keep_temp=True, auto_retry=2, added_at=1.0,
        last_checked=None, downloaded=[])
    d.update(kw)
    return wl.WatchlistEntry(**d)


SCEN: list[dict[str, Any]] = []


def sc(name: str, argv: Any, **kw: Any) -> None:
    base: dict[str, Any] = dict(
        name=name, argv=list(argv), asks=[], scans={S1: A(), S2: A(S2, "Show B")}, cm={}, customized=False,
        save_raises=False, ping=True, discovery=(URL1, "Show A"), selected=lambda a: a.episodes[:2],
        confirm=True, sessions=[], sessions_run=None, wl_browser=(None, None), wl_count=0, record=True,
        dl_fail=(), wl_entry=None, old_ts=False)
    base.update(kw)
    SCEN.append(base)


sc("all_download", [URL1, "--all"])
sc("range_match", [URL1, "--range", "1-2"])
sc("range_nomatch_exit", [URL1, "--range", "40-50"])
sc("latest_two", [URL1, "--latest", "2"])
sc("stream_scripted", [URL1, "--stream"])
sc("list_only", [URL1, "--list"])
sc("no_url_discovery_none", [], discovery=(None, None))
sc("no_url_discovery_ok_all", ["--all"])
sc("ping_fail", [URL1, "--all"], ping=False)
sc("scripted_empty_break", [URL1, "--all"], scans={S1: A(ep=[])})
sc("partial_badge", [URL1, "--all"], scans={S1: A(partial=True)})
sc("explicit_flags_clamped", [URL1, "--all", "--parallel", "99", "--workers", "1", "--retry", "2", "-q", "720", "-o", "/x", "--keep-temp"])
sc("cm_out_of_range", [URL1, "--all"], cm={"max_parallel": 0, "hls_workers": 99, "auto_retry": -3})
sc("cm_high_values", [URL1, "--all"], cm={"max_parallel": 99, "hls_workers": 1, "auto_retry": 5})
sc("verbose", [URL1, "--all", "--verbose"])
sc("partial_dl_fail", [URL1, "--all"], dl_fail=(2.0,))
sc("menu_exit", [URL1], asks=["5"])
sc("menu_list_then_exit", [URL1], asks=["4", "5"])
sc("menu_sessions_same_url", [URL1], asks=["3", "5"], sessions_run=URL1)
sc("menu_sessions_new_url_relaunch", [URL1], asks=["3", "5"], sessions_run=URL2)
sc("menu_sessions_none", [URL1], asks=["3", "5"], sessions_run=None)
sc("menu_watchlist_relaunch", [URL1], asks=["w", "5"], wl_browser=(URL2, "ENTRY2"))
sc("menu_watchlist_back", [URL1], asks=["W", "5"], wl_browser=(None, None))
sc("menu_watchlist_same_url", [URL1], asks=["W", "5"], wl_browser=(URL1, None))
sc("menu_download_customized", [URL1], asks=["1"], customized=True)
sc("menu_download_wizard", [URL1], asks=["1"])
sc("menu_download_wizard_save_fails", [URL1], asks=["1"], save_raises=True)
sc("menu_download_declined", [URL1], asks=["1", "5"], confirm=False, customized=True)
sc("menu_stream_customized", [URL1], asks=["2"], customized=True)
sc("menu_stream_wizard", [URL1], asks=["2"])
sc("menu_select_empty_then_exit", [URL1], asks=["1", "5"], selected=lambda a: [], customized=True)
sc("menu_cache_and_watchlist_hints", [URL1], asks=["5"], sessions=[{"size": 2048}, {"size": 4096}], wl_count=3)
sc("menu_watchlist_count_raises", [URL1], asks=["5"], wl_count=RuntimeError("boom"))
sc("wl_entry_download_record", [URL1], asks=["1"], wl_entry="match", record=True)
sc("wl_entry_record_false", [URL1], asks=["1"], wl_entry="match", record=False, dl_fail=(1.0,))
sc("wl_entry_record_raises", [URL1], asks=["1"], wl_entry="match", record=RuntimeError("x"))
sc("wl_entry_session_mismatch", [URL1], asks=["1"], wl_entry="other", customized=True)
sc("wl_entry_cli_flags_win", [URL1, "-q", "720", "--audio", "jpn", "--parallel", "2", "--workers", "10"], asks=["1"], wl_entry="match")
sc("wl_entry_scripted", [URL1, "--all"], wl_entry="match")
sc("cache_cleanup", [URL1, "--all"], old_ts=True)


def _run_scenario(s: dict[str, Any], tmp: pathlib.Path, mp: pytest.MonkeyPatch) -> dict[str, Any]:
    t = str(tmp)
    script = dict(s)
    script["scans"] = dict(s["scans"])
    run = _Run(t, script)
    _install(mp, run)
    cwd = os.getcwd()
    os.chdir(t)
    try:
        if s["old_ts"]:
            p = os.path.join("pahe_cache", "x", "y")
            os.makedirs(p)
            f = os.path.join(p, "old.ts")
            pathlib.Path(f).write_text("x")
            os.utime(f, (1, 1))
        args = cli.build_parser().parse_args(s["argv"])
        if s["wl_entry"] == "match":
            args._watchlist_entry = WE(t)
        elif s["wl_entry"] == "other":
            args._watchlist_entry = WE(t, session=S2)
        outcome = None
        try:
            asyncio.run(cli.run(args))
        except SystemExit as e:
            outcome = f"exit:{e.code}"
        except BaseException as e:  # noqa: BLE001
            outcome = f"{type(e).__name__}:{e}"
        return dict(outcome=outcome, events=run.events, args_url=run.norm(args.url),
                    cache_left=os.path.exists(os.path.join("pahe_cache", "x")))
    finally:
        os.chdir(cwd)


def test_main_run_matches_recording(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    results: dict[str, Any] = {}
    for i, s in enumerate(SCEN):
        d = tmp_path / f"s{i}"
        d.mkdir()
        with monkeypatch.context() as mp:
            results[s["name"]] = _run_scenario(s, d, mp)
    results = json.loads(json.dumps(results, default=str))
    if os.environ.get("UPDATE_MAIN_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(results, sort_keys=True, indent=1))
    golden = json.loads(GOLDEN.read_text())
    assert sorted(results) == sorted(golden)
    differing = [k for k in golden if results[k] != golden[k]]
    assert not differing, f"behaviour changed in scenarios: {differing}"
