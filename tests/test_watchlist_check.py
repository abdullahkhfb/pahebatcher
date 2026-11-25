"""Characterisation test for ``watchlist.run_watchlist_check``.

Runs the check against scripted fakes (config, solver, HTTP client, scanner,
orchestrator, console and clock) across 32 scenarios and compares everything
it does with a recording made from the original, un-split implementation:
every message printed, every watchlist save, every scan and download call,
the results table and the final file. A refactor must not change any of it.

To re-record after an intentional behaviour change, run the test once with
the environment variable ``UPDATE_WATCHLIST_GOLDEN=1``.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
from typing import Any

import pytest
import rich.panel
import rich.table

from pahebatcher import config_manager, downloader, http, models, solver
from pahebatcher import watchlist as wl
from pahebatcher.extract import scanner
from pahebatcher.ui import console as ui_console

GOLDEN = pathlib.Path(__file__).parent / "data" / "watchlist_check_golden.json"
_REAL_SAVE = wl.save_entries  # captured before any test patches it


class _Run:
    """Per-scenario state shared by the fakes."""

    def __init__(self, tmp: str, script: dict[str, Any]) -> None:
        self.tmp = tmp
        self.script = script
        self.events: list[list[Any]] = []

    def norm(self, x: Any) -> Any:
        if isinstance(x, str):
            for variant in {self.tmp, os.path.realpath(self.tmp), self.tmp.replace("\\", "/")}:
                x = x.replace(variant, "<T>")
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
    def __init__(self, status: str, **k: Any) -> None:
        self.status, self.border = status, str(k.get("border_style"))

    def __repr__(self) -> str:
        return f"Panel({self.status!r},{self.border})"


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
            return {"resolve_ahead": 2, "cookie_string": "c=1"}[key]

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
            r = s["scans"][self.session]
            if isinstance(r, list):
                r = r.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    class Orch:
        def __init__(self, ctx: Any, anime: Any, http_: Any, solver_: Any) -> None:
            ev("orch.init", ctx.output_dir, str(ctx.cache_dir), ctx.quality, ctx.audio_lang,
               ctx.max_parallel, ctx.hls_workers, ctx.keep_temp, ctx.list_only, ctx.resolve_ahead,
               ctx.cache_ttl, ctx.cookie_string, ctx.auto_retry, anime.title)

        def _find_existing(self, ep: Any) -> Any:
            ev("exists?", ep.number, ep.audio)
            if ep.number in s["exist_raise"]:
                raise OSError("stat failed")
            return "/x" if ep.number in s["exists"] else None

        async def download(self, pending: Any) -> Any:
            ev("download", [(e.number, e.audio) for e in pending])
            if s["dl_raise"]:
                raise RuntimeError(s["dl_raise"])
            return {e.session: (None if e.number in s["dl_fail"] else "/out") for e in pending}

    async def find(solver_: Any, host: str, title: str, exclude: str) -> Any:
        ev("find", host, title, exclude)
        r = s["find"]
        if isinstance(r, Exception):
            raise r
        return r

    real_save = _REAL_SAVE

    def save(entries: Any, path: Any = None) -> None:
        ev("save", [e.to_dict() for e in entries])
        real_save(entries, path)

    mp.setattr(ui_console, "console", Console())
    mp.setattr(ui_console, "print_banner", lambda: ev("banner"))
    mp.setattr(ui_console, "section", lambda t: ev("section", t))
    mp.setattr(rich.table, "Table", _Table)
    mp.setattr(rich.panel, "Panel", _Panel)
    mp.setattr(config_manager, "ConfigManager", CM)
    mp.setattr(solver, "Solver", Solver)
    mp.setattr(solver, "no_solver_hint", lambda: "HINT")
    mp.setattr(http, "HttpClient", Http)
    mp.setattr(scanner, "AnimePaheScanner", Scanner)
    mp.setattr(downloader, "BatchOrchestrator", Orch)
    mp.setattr(wl, "_find_single_new_session", find)
    mp.setattr(wl, "save_entries", save)
    mp.setattr(wl, "time", _Clock())
    mp.setattr(wl, "_recover_title_from_cache",
               lambda session, cache_dir: ev("recover", session) or s["recover"])
    mp.setattr("logging.basicConfig", lambda *a, **k: None)


def E(t: str, n: int, title: str = "Show A", **kw: Any) -> wl.WatchlistEntry:
    session = f"aaaa000{n}-0000"
    d: dict[str, Any] = dict(
        url=f"https://animepahe.pw/anime/{session}", session=session, host="animepahe.pw",
        title=title, quality=1080, audio_lang="jpn", output_dir=os.path.join(t, "out"),
        max_parallel=2, hls_workers=8, keep_temp=False, auto_retry=2, added_at=1.0,
        last_checked=None, downloaded=[])
    d.update(kw)
    return wl.WatchlistEntry(**d)


def eps(nums: Any, audios: Any = ("jpn",)) -> list[models.EpisodeInfo]:
    return [models.EpisodeInfo(number=float(n), session=f"s{n}{a}", title=f"T{n}", fansub="F",
                               audio=a, play_url=f"https://x/{n}{a}") for n in nums for a in audios]


def A(title: str = "Show A", ep: Any = None, session: str = "aaaa0001-0000") -> models.AnimeInfo:
    ep = eps([1, 2, 3]) if ep is None else ep
    return models.AnimeInfo(session=session, title=title, host="animepahe.pw", total=len(ep),
                            episodes=ep, has_session=False)


def DEAD() -> models.AnimeInfo:
    return A("Unknown Anime", [])


SCEN: list[dict[str, Any]] = []


def sc(name: str, entries: Any, scans: Any = None, ping: bool = True, find: Any = None,
       exists: Any = (), exist_raise: Any = (), dl_fail: Any = (), dl_raise: Any = None,
       filter_id: Any = None, verbose: bool = False, folders: Any = (), recover: Any = None,
       old_ts: bool = False) -> None:
    SCEN.append(dict(name=name, entries=entries, scans=scans or {}, ping=ping, find=find,
                     exists=set(exists), exist_raise=set(exist_raise), dl_fail=set(dl_fail),
                     dl_raise=dl_raise, filter_id=filter_id, verbose=verbose, folders=folders,
                     recover=recover, old_ts=old_ts))


a1 = 'aaaa0001-0000'; a2 = 'aaaa0002-0000'
sc('no_entries', lambda t: [])
sc('filter_miss', lambda t: [E(t, 1)], filter_id='zzz')
sc('filter_hit', lambda t: [E(t, 1), E(t, 2, 'Show B')], {a2: A('Show B', session=a2)}, filter_id='Show B')
sc('ping_fail', lambda t: [E(t, 1)], ping=False)
sc('all_success', lambda t: [E(t, 1)], {a1: A(ep=eps([1, 2, 3], ('jpn', 'eng')))})
sc('partial_fail', lambda t: [E(t, 1)], {a1: A()}, dl_fail=(2.0,))
sc('download_raises', lambda t: [E(t, 1)], {a1: A()}, dl_raise='net down')
sc('download_raises_verbose', lambda t: [E(t, 1)], {a1: A()}, dl_raise='net down', verbose=True)
sc('up_to_date_backfill', lambda t: [E(t, 1, downloaded=[1.0])], {a1: A()}, exists=(1.0, 2.0, 3.0), folders=('Show A',))
sc('skipped_deleted', lambda t: [E(t, 1, downloaded=[1.0, 2.0])], {a1: A()}, folders=('Show A',))
sc('all_deleted_skipped', lambda t: [E(t, 1, downloaded=[1.0, 2.0, 3.0])], {a1: A()}, folders=('Show A',))
sc('folder_present_history_kept', lambda t: [E(t, 1, downloaded=[1.0, 2.0])], {a1: A()}, exists=(1.0, 2.0), folders=('Show A',))
sc('folder_missing_reset', lambda t: [E(t, 1, downloaded=[1.0, 2.0])], {a1: A()})
sc('scan_fail', lambda t: [E(t, 1)], {a1: RuntimeError('boom')})
sc('title_update', lambda t: [E(t, 1)], {a1: A('Show A (2024)')})
sc('dedupe_audio', lambda t: [E(t, 1)], {a1: A(ep=eps([1, 2], ('eng', 'jpn')))})
sc('prefer_fallback', lambda t: [E(t, 1, audio_lang='eng')], {a1: A()})
sc('dead_migrate_ok', lambda t: [E(t, 1)], {a1: DEAD(), 'NEW00000-0000': A('Show A New', session='NEW00000-0000')}, find=('animepahe.si', 'NEW00000-0000', 'ok'))
sc('dead_migrate_still_dead', lambda t: [E(t, 1)], {a1: DEAD(), 'NEW00000-0000': DEAD()}, find=('animepahe.si', 'NEW00000-0000', 'ok'))
sc('dead_none', lambda t: [E(t, 1)], {a1: DEAD()}, find=(None, None, 'none'))
sc('dead_unknown_title_status', lambda t: [E(t, 1)], {a1: DEAD()}, find=(None, None, 'unknown_title'))
sc('dead_ambiguous', lambda t: [E(t, 1)], {a1: DEAD()}, find=(None, None, 'ambiguous'))
sc('dead_find_raises', lambda t: [E(t, 1)], {a1: DEAD()}, find=RuntimeError('search failed'))
sc('dead_title_placeholder', lambda t: [E(t, 1, title='aaaa0001-0000', downloaded=[1.0])], {a1: DEAD()})
sc('dead_404_recovered', lambda t: [E(t, 1, title='404 Not Found')], {a1: A('404 Not Found', [])}, find=(None, None, 'none'), recover='Show A')
sc('dead_404_unrecovered', lambda t: [E(t, 1, title='404 Not Found')], {a1: A('404 Not Found', [])}, recover=None)
sc('dead_no_episodes', lambda t: [E(t, 1, downloaded=[1.0])], {a1: A('Show A', [])}, find=(None, None, 'none'))
sc('mixed_two_entries', lambda t: [E(t, 1), E(t, 2, 'Show B')], {a1: RuntimeError('x'), a2: A('Show B', session=a2)}, dl_fail=(3.0,))
sc('exists_raises', lambda t: [E(t, 1)], {a1: A()}, exist_raise=(2.0,))
sc('cache_cleanup', lambda t: [E(t, 1)], {a1: A()}, exists=(1.0, 2.0, 3.0), old_ts=True)
sc('dead_migrate_dedupe', lambda t: [E(t, 1)], {a1: DEAD(), 'NEW00000-0000': A('Show A New', eps([1, 2], ('eng', 'jpn')), 'NEW00000-0000')}, find=('animepahe.si', 'NEW00000-0000', 'ok'))
sc('two_ok_all_new', lambda t: [E(t, 1, hls_workers=4), E(t, 2, 'Show B', hls_workers=16)], {a1: A(), a2: A('Show B', eps([1, 2]), a2)})



def _run_scenario(s: dict[str, Any], tmp: pathlib.Path, mp: pytest.MonkeyPatch) -> dict[str, Any]:
    t = str(tmp)
    script = dict(ping=s["ping"], scans={k: (list(v) if isinstance(v, list) else v) for k, v in s["scans"].items()},
                  find=s["find"], exists=s["exists"], exist_raise=s["exist_raise"], dl_fail=s["dl_fail"],
                  dl_raise=s["dl_raise"], recover=s["recover"])
    run = _Run(t, script)
    _install(mp, run)
    entries = s["entries"](t)
    for name in s["folders"]:
        os.makedirs(os.path.join(t, "out", wl.utils.sanitize(name)))
    cwd = os.getcwd()
    os.chdir(t)
    try:
        if s["old_ts"]:
            p = os.path.join("pahe_cache", "x", "y")
            os.makedirs(p)
            f = os.path.join(p, "old.ts")
            pathlib.Path(f).write_text("x")
            os.utime(f, (1, 1))
        path = tmp / "wl.json"
        if entries:
            _REAL_SAVE(entries, path)
        outcome = None
        try:
            asyncio.run(wl.run_watchlist_check(path, s["verbose"], s["filter_id"]))
        except SystemExit as e:
            outcome = f"exit:{e.code}"
        except BaseException as e:  # noqa: BLE001
            outcome = f"{type(e).__name__}:{e}"
        final = json.loads(path.read_text()) if path.exists() else None
        out_dir = os.path.join(t, "out")
        return dict(
            outcome=outcome,
            events=run.events,
            final=run.norm(final),
            cache_left=os.path.exists(os.path.join("pahe_cache", "x")),
            folders=sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else None,
        )
    finally:
        os.chdir(cwd)


def test_watchlist_check_matches_recording(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    results: dict[str, Any] = {}
    for i, s in enumerate(SCEN):
        d = tmp_path / f"s{i}"
        d.mkdir()
        with monkeypatch.context() as mp:
            results[s["name"]] = _run_scenario(s, d, mp)
    results = json.loads(json.dumps(results, default=str))
    if os.environ.get("UPDATE_WATCHLIST_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(results, sort_keys=True, indent=1))
    golden = json.loads(GOLDEN.read_text())
    assert sorted(results) == sorted(golden)
    differing = [k for k in golden if results[k] != golden[k]]
    assert not differing, f"behaviour changed in scenarios: {differing}"
