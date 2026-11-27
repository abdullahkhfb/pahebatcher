"""Drives the real App and PlayerView against a local HLS stream.

Needs a display (``xvfb-run -a python scripts/gui_player_e2e.py`` on Linux)
and ffmpeg. Not part of pytest because it opens a window.
"""
import os, sys, time, pathlib, tempfile, shutil
os.environ["PAHEBATCHER_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests"))
import test_player_engine as t
from pahebatcher import models
from pahebatcher.gui import app as app_mod, bridge, engine, player

# local episodes: ep1 8 s, ep2 8 s
folders = []
for _ in range(2):
    d = pathlib.Path(tempfile.mkdtemp()); t._make_hls(d, seconds=8); folders.append(d)

calls = []
async def fake_extract(solver, play_url, quality, audio, cookies):
    n = int(play_url.rsplit("/", 1)[1])
    calls.append((n, audio))
    info = models.StreamInfo(url=(folders[(n - 1) % 2] / "index.m3u8").as_uri(),
                             cookies=[], user_agent="ua", referer="r",
                             title=f"Ep {n}", audio=audio)
    return info
bridge.kwik.extract_stream = fake_extract
engine.open_sounddevice = t._FakeSound          # real-time fake sound card
app_mod.App._check_updates_at_startup = lambda self: None

eps = []
for n in (1, 2):
    eps.append(models.EpisodeInfo(n, f"s{n}", f"Episode {n} title", "grp", "jpn", f"x/{n}"))
eps.append(models.EpisodeInfo(1, "d1", "Episode 1 title DUB", "grp", "eng", "x/1"))
anime = models.AnimeInfo("sess", "Test Anime", "h", total=2, episodes=eps)

app = app_mod.App()
app.backend.http = t._FileHttp()
app.solver_ok = True
app.geometry("1000x720+0+0")

def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.update(); time.sleep(0.004)

def shot(name):
    from PIL import ImageGrab
    app.update()
    ImageGrab.grab(xdisplay=os.environ["DISPLAY"]).save(f"/tmp/{name}.png")

def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label); 
    if not cond: check.failed = True
check.failed = False

pump(0.5)
app.show_episodes(anime)
pump(0.3)
shot("1_episodes")
check("Watch button disabled with no selection", not app.v_eps.watch._enabled)
app.v_eps._toggle(0); app.v_eps._toggle(1)
check("Watch label counts selection", app.v_eps.watch.cget("text") == "Watch 2 episodes")
app.v_eps.watch_selected()

pv = app.v_player
# wait for first frame
t0 = time.monotonic()
while time.monotonic() - t0 < 25 and not (pv.eng and pv.eng.playing and pv._photo):
    pump(0.05)
check("player started + drew a frame", pv._photo is not None)
check("audio path active (fake sound card)", pv.eng.audio_ok)
pump(2.0)
shot("2_playing")
pos = pv.eng.position()
check(f"clock advancing in real time (pos={pos:.2f})", 1.0 < pos < 3.2)
check("duration known", 7 < pv.eng.duration < 9)
check("subtitle shows episode", "Episode 1" in pv.subtitle.cget("text"))
check("prev disabled on first, next enabled", (not pv.btn_prev._enabled) and pv.btn_next._enabled)

# frame rate achieved by the UI loop
n0 = 0
orig = pv._draw
def counting(*a):
    global n0; n0 += 1; return orig(*a)
pv._draw = counting
pump(2.0)
pv._draw = orig
check(f"UI drew ~24 fps ({n0/2:.1f} fps)", n0 / 2 > 18)

# pause / resume
pv.toggle_pause(); pump(0.4); p1 = pv.eng.position(); pump(0.8); p2 = pv.eng.position()
check("pause freezes clock", abs(p2 - p1) < 0.05)
check("button says Play when paused", pv.btn_play.cget("text") == "Play")
pv.toggle_pause()

# seek via key (debounced) 
pv.eng.position()
pv._seek_to(5.0, instant=True); pump(1.2)
check(f"seek landed (pos={pv.eng.position():.2f})", 5.0 < pv.eng.position() < 6.8)

# volume keys
v0 = pv._volume_frac
pv._nudge_volume(-0.25)
check("volume down", abs(pv._volume_frac - (v0 - 0.25)) < 1e-6)
check("volume persisted", app.cfg.get("volume") == 75)

# fullscreen toggle -> chrome hidden, controls auto-hide
pv.toggle_fullscreen(); pump(1.2)
check("chrome hidden in fullscreen", app._chrome is False and not app.hdr.winfo_ismapped())
shot("3_fullscreen")
t0 = time.monotonic()
while time.monotonic() - t0 < 8 and not pv._controls_hidden:
    pump(0.1)
check("controls auto-hide when idle in fullscreen", pv._controls_hidden)
pv._activity(); pump(0.2)
check("controls return on activity", not pv._controls_hidden)
pv.toggle_fullscreen(); pump(1.0)
check("chrome restored", app._chrome and app.hdr.winfo_ismapped())

# play to the end of ep1 -> auto-advance to ep2
pv._seek_to(6.5, instant=True)
t0 = time.monotonic()
while time.monotonic() - t0 < 20 and pv.index != 1:
    pump(0.1)
check("auto-advanced to episode 2", pv.index == 1)
t0 = time.monotonic()
while time.monotonic() - t0 < 20 and not (pv.session and pv.eng.playing and pv._photo):
    pump(0.1)
pump(1.0)
check("episode 2 is playing", bool(pv.eng.playing and pv._photo is not None and "Episode 2" in pv.subtitle.cget("text")))
print("   ep2 state: playing=%s photo=%s sub=%r status=%r pos=%.2f failed=%s finished=%s" % (
    pv.eng.playing, pv._photo is not None, pv.subtitle.cget("text"), pv._status, pv.eng.position(), pv._failed, pv._finished))
check("next disabled on last", not pv.btn_next._enabled and pv.btn_prev._enabled)

# the player has no audio switch any more: audio comes from the settings
check("no audio selector in the player", not hasattr(pv, "seg_audio"))
pv.go(-1); pump(3.0)
check("previous episode plays", pv.index == 0 and pv.session is not None)


# Esc returns to the episode list and keeps selection
app._exit_fullscreen(); pump(0.5)
check("Esc leaves player", not pv.winfo_ismapped() and app.v_eps.winfo_ismapped())
check("selection kept after leaving player", app.v_eps.sel == {1, 2})
check("no leftover ffmpeg for the player", pv.eng._run is None)

# error path: resolve failure shows message, Play retries
async def boom(*a, **k): raise RuntimeError("kwik exploded")
bridge.kwik.extract_stream = boom
app.start_stream(anime, eps[:2], 0); pump(1.5)
check("resolve failure shown", pv._failed and "kwik exploded" in pv._status)
shot("5_error")
app._exit_fullscreen(); pump(0.3)

app.on_close()
print("RESULT:", "FAILED" if check.failed else "ALL PASSED")
