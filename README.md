# pahebatcher

![pahebatcher logo by u/HellHarbinger](assets/logo/colored_logo.svg)

Batch-download and stream anime from [animepahe](https://animepahe.pw), from a
desktop app or the terminal.

[![Version 3.9.0][version-badge]][latest-release]
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License MIT](https://img.shields.io/badge/license-MIT-green)

![Search screen](assets/screenshots/gui-search.png)
![Download dashboard](assets/screenshots/gui-downloading.png)

## Highlights

- **Desktop app and terminal UI**, both with a live per-episode download
  dashboard.
- **Fast and safe:** parallel episodes, parallel HLS segments, and downloads
  resume exactly where they stopped.
- **Stream in MPV** from the terminal, and switch between SUB and DUB between
  episodes.
- **Watchlist:** follow airing shows and fetch only the new episodes.
- **No Docker, no extra services.** A built-in browser solver gets past
  Cloudflare using the Chrome or Edge you already have.
- **One-click FFmpeg:** the desktop app installs it for you.

## Install

### Windows (easiest)

1.  Download `pahebatcher-gui-windows-x64.exe` from the Releases page, or
    [build it yourself](#build-the-executables).
2.  Make sure **Microsoft Edge** (included with Windows) or **Google Chrome**
    is installed.
3.  Run it. If FFmpeg is missing, click **Install FFmpeg** once.

The Releases page also has terminal builds: `pahebatcher-windows-x64.exe`,
`pahebatcher-linux-x64` and `pahebatcher-macos-arm64`.

### From source (Windows, macOS, Linux)

```bash
git clone https://github.com/abdullahkhfb/pahebatcher.git
cd pahebatcher
pip install -e ".[browser]"

pahebatcher-gui      # desktop app
pahebatcher          # terminal
```

### Requirements

| Needs          | For                                       | How                                                                                                           |
| -------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Python 3.11+   | running from source                       | [python.org](https://www.python.org/downloads/)                                                               |
| Tkinter        | the desktop app, when running from source | included with most Python installs; some Linux distributions package it separately (for example `python3-tk`) |
| Chrome or Edge | the built-in Cloudflare solver            | usually already installed                                                                                     |
| FFmpeg         | joining downloaded pieces into `.mp4`     | the desktop app installs it; or `winget install Gyan.FFmpeg` / `brew install ffmpeg` / `apt install ffmpeg`   |
| MPV            | streaming only                            | `winget install mpv.net` / `brew install mpv` / `apt install mpv`                                             |

## Desktop app

1.  **Search** for an anime and double-click a result.
2.  **Pick episodes** by clicking them, or use **All**, **None**, or a range
    like `1-12`, `1,4,7`, `5-`.
3.  Press **Download**. Watch progress, then **Open folder**.

Quality, audio (SUB/DUB), parallel downloads and the download folder are along
the bottom of the search screen. They are saved automatically to `gui.toml` in
the app data folder (see [Where files live](#where-files-live)). On first run
the download folder is `Downloads/pahebatcher` in your home folder.

**FFmpeg.** A notice appears when FFmpeg is missing. Press **Install FFmpeg**,
or just press **Download**, and it installs first and then continues. It
downloads a static build into the app data folder (no admin rights, no `PATH`
changes), and the terminal app finds it there too. A copy already on your
`PATH` is used first.

**Full screen.** Press `F11` (or `Alt+Enter`, or click **Full screen** in the
header); `Esc` exits. The window can also be maximized or resized freely, and
the content stays centered on wide screens.

The desktop app doesn't do streaming or the watchlist yet. Use the terminal for
those.

## Terminal

Run `pahebatcher` for the interactive wizard (search, pick episodes, download),
or pass a series URL.

```bash
pahebatcher "https://animepahe.pw/anime/<uuid>" --all -q 720        # whole series
pahebatcher "<url>" --range 1-12 --audio eng -q 1080 -o ~/anime     # dub, 1080p, custom folder
pahebatcher "<url>" --latest 3                                      # newest 3 episodes
pahebatcher "<url>" --list                                          # list episodes and exit
pahebatcher "<url>" --stream                                        # play in MPV
```

Files are saved as `Ep 001 - Title.mp4` in `<output>/<Series>/` (default
`./downloads`).

### Terminal options

| Flag                 | Meaning                                    | Default       |
| -------------------- | ------------------------------------------ | ------------- |
| `-a`, `--all`        | every episode                              |               |
| `-r`, `--range R`    | `1-12`, `1,4,7`, `13-`, `1-6,10`           |               |
| `-n`, `--latest N`   | newest N episodes                          |               |
| `-s`, `--stream`     | stream in MPV                              |               |
| `-l`, `--list`       | list episodes and exit                     |               |
| `-o`, `--output DIR` | output folder                              | `./downloads` |
| `-q`, `--quality`    | `360`, `720`, `1080`                       | `1080`        |
| `--audio`            | `jpn` (sub) or `eng` (dub)                 | `jpn`         |
| `-j`, `--parallel N` | episodes at once (1-6)                     | `2`           |
| `-w`, `--workers N`  | segment fetchers per episode (8-32)        | `24`          |
| `--retry N`          | automatic retries per failed episode (0-2) | `2`           |
| `--keep-temp`        | keep raw `.ts` segments                    | off           |
| `-v`, `--verbose`    | debug logging                              | off           |

`--all`, `--range`, `--latest` and `--stream` are mutually exclusive. Without
one of them, the wizard asks which episodes you want.

Total simultaneous connections are `parallel x workers`. If you see segment
failures, lower `-w` (to 8-12) before lowering `-j`.

### Streaming

MPV opens with a live "Now playing" panel. After an episode: `N` next, `P`
previous, `A` toggle SUB/DUB, `R` replay, `S` jump to an episode, `Q` quit.

### Sessions

Interrupted downloads are cached segment by segment. Pick **Sessions** in the
main menu to resume, delete one, or clear the whole cache (`pahe_cache/`).
Cache folders older than 24 hours are cleaned up automatically.

### Linux, macOS and WSL helper

`make run` sets up a virtual environment (including the browser solver) and
starts the app. `make help` lists the rest (`run`, `watchlist-check`, `test`,
`lint`, ...).

## Watchlist

Follow a show once, then run one command whenever you like (for example from
cron). It downloads only the episodes you don't have yet and is safe to run
repeatedly.

```bash
pahebatcher wl add "https://animepahe.pw/anime/<uuid>" -q 1080 --audio jpn -o ~/anime
pahebatcher wl add Frieren          # search by name
pahebatcher check                   # download anything new
pahebatcher wl list                 # see what's tracked
```

| Command                                       | What it does                                                                                                                         |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `wl add [URL or name]`                        | track a series (uses your saved settings unless you pass flags; accepts `-q`, `--audio`, `-o`, `-j`, `-w`, `--retry`, `--keep-temp`) |
| `check [N]` (or `wl check`, `sync`, `update`) | download new episodes for every tracked series, or only for entry `N`                                                                |
| `wl list` / `wl show N`                       | list entries / show one                                                                                                              |
| `wl remove N`                                 | stop tracking (`-y` skips the confirmation)                                                                                          |
| `wl relink [N] [URL]`                         | repair an entry whose animepahe link died; without `URL` it finds the new one by title, and without `N` it repairs every dead entry  |
| `wl reset N` (or `wl reset --all`)            | forget which episodes you deleted so they download again                                                                             |

`wl` is short for `watchlist` (`w` and `watch` also work). `N` can be the list
number, the URL, or part of the title.

- **Deleted a single episode?** It stays skipped while the series folder
  exists. Delete the whole folder, or run `wl reset N`, to download it again.
- **State** lives in `watchlist.json` in the folder you run from (override
  with `WATCHLIST_PATH`).
- **Cron example** (hourly). On Windows, use Task Scheduler with
  `python -m pahebatcher check` instead.

  ```cron
  0 * * * * cd /path/to/pahebatcher && venv/bin/python -m pahebatcher check >> watchlist.log 2>&1
  ```

## Configuration

Defaults are saved to `pahebatcher.toml` in the folder you run the terminal app
from. The desktop app keeps its own settings in `gui.toml` (see
[Where files live](#where-files-live)). Command-line flags override saved
settings.

```bash
pahebatcher config show
pahebatcher config set quality 720
pahebatcher config set audio_lang eng
pahebatcher config reset
```

| Key             | Default       | Values                                                           |
| --------------- | ------------- | ---------------------------------------------------------------- |
| `quality`       | `1080`        | `360`, `720`, `1080`                                             |
| `audio_lang`    | `jpn`         | `jpn`, `eng`                                                     |
| `max_parallel`  | `2`           | `1`-`6`                                                          |
| `hls_workers`   | `24`          | `8`-`32`                                                         |
| `output_dir`    | `./downloads` | any path                                                         |
| `keep_temp`     | `false`       | `true`, `false`                                                  |
| `auto_retry`    | `2`           | `0`-`2`                                                          |
| `cache_ttl`     | `60`          | minutes to cache scan results; `0` disables                      |
| `resolve_ahead` | `999`         | how many episodes to resolve ahead; `1` is gentler on Cloudflare |
| `browser_mode`  | `auto`        | `auto`, `headless`, `headed`                                     |
| `cookie_string` | empty         | your browser's `cf_clearance=...` cookie                         |
| `clearance_ttl` | `20`          | minutes a solved clearance is reused; `0` disables               |

Not every setting applies everywhere:

- `output_dir` and `keep_temp` are used by the desktop app, and `output_dir`
  is also the default for `wl add`. One-off terminal downloads ignore them:
  use `-o` and `--keep-temp`.
- The other keys apply to both apps and to the watchlist.

See `pahebatcher.toml.example` for a ready-to-copy file. It sets
`resolve_ahead = 1` (the gentler serial setting), while the built-in default is
`999`.

### Where files live

| What                                     | Location                                                                                                                                                                                         |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Terminal settings                        | `pahebatcher.toml` in the folder you run from                                                                                                                                                    |
| Watchlist                                | `watchlist.json` in the folder you run from (or `WATCHLIST_PATH`)                                                                                                                                |
| Download cache                           | `pahe_cache/` in the folder you run from                                                                                                                                                         |
| Desktop settings (`gui.toml`) and FFmpeg | the app data folder: `%LOCALAPPDATA%\pahebatcher` on Windows, `~/Library/Application Support/pahebatcher` on macOS, `$XDG_DATA_HOME/pahebatcher` (default `~/.local/share/pahebatcher`) on Linux |

Set `PAHEBATCHER_HOME` to move the app data folder.

## Cloudflare solver

animepahe sits behind Cloudflare, so pahebatcher needs a solver to get past its
browser check. The built-in browser solver drives your installed Chrome or Edge
with its own profile. Nothing is downloaded or bundled, and the `.exe` builds
include it. From source, install it with `pip install "pahebatcher[browser]"`
(`make run` does this for you).

With `browser_mode = auto` it solves in the background first and opens a
visible window only if Cloudflare needs a human click. To use a specific
browser, set `PAHEBATCHER_BROWSER` to its executable path. To route through a
proxy or VPN, set `PAHEBATCHER_PROXY` (for example
`http://user:pass@host:port`).

> **Upgrading?** FlareSolverr support has been removed, so `FLARESOLVERR_URL`
> and `FLARESOLVERR_PROXY` are no longer read (use `PAHEBATCHER_PROXY`
> instead), and an old `solver_backend` line in your config is ignored.

**Android/Termux:** automatic solving doesn't work there. Solve once in a real
browser on the same network and run
`pahebatcher config set cookie_string "cf_clearance=..."`. Repeat when it
expires.

## Build the executables

On Windows, double-click **`build_exe.bat`** (Python 3.11+ required). It cleans
old output, installs everything and writes:

- `dist\pahebatcher-gui.exe`: the desktop app, no console window
- `dist\pahebatcher.exe`: the terminal app

Both use the app icon from `src/pahebatcher/gui/assets/` (`icon.ico` for the
file, `icon.png` for the window; `icon.svg` is the editable source).

Pushing a tag like `v3.9.1` makes the **Build executables** GitHub workflow run
the tests, build the binaries and attach them to a Release (the Windows desktop
app, plus Windows, Linux and macOS terminal apps). You can also run it by hand
from the Actions tab, which builds the binaries without publishing a Release.

FFmpeg, MPV and Chrome/Edge are not bundled.

## Troubleshooting

| Problem                                                    | Fix                                                                                                                                                |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| "No Cloudflare solver"                                     | Install Chrome or Edge and `pip install "pahebatcher[browser]"`. In WSL, install a Linux Chrome or set `PAHEBATCHER_BROWSER`.                      |
| Solver never gets through                                  | `pahebatcher config set browser_mode headed`, then tick the checkbox in the window. Or try another network, or a proxy via `PAHEBATCHER_PROXY`.    |
| FFmpeg not found                                           | Press **Install FFmpeg** in the desktop app, or install it and make sure `ffmpeg -version` works.                                                  |
| `MPV not found`                                            | Install MPV, or use download mode.                                                                                                                 |
| Many segment failures or slow downloads                    | Lower `-w` to 8-12 and `-j` to 1 if the CDN is rate limiting; raise `-w` to 24-32 if you are just slow.                                            |
| `Resolution timed out`                                     | Try `-j 1`. It already retries each episode automatically (see `--retry`).                                                                         |
| `No Kwik link found`                                       | Try another `--quality`, or set `cookie_string` from your browser.                                                                                 |
| Cache folder keeps growing                                 | Clear it from **Sessions** in the terminal menu. Stale sessions older than 24 hours are cleaned automatically.                                     |
| `watchlist check` says up to date but a new episode is out | Check `wl show N` for the right audio and output folder. If you moved or renamed files, `check` can't match them (names must start with `Ep 001`). |
| Windows shows a SmartScreen warning                        | The `.exe` is unsigned. Choose **More info**, then **Run anyway**.                                                                                 |
| Windows still shows the old icon after a rebuild           | Windows caches icons. Move the `.exe` to a new folder or restart Explorer.                                                                         |
| Browser did not open its debugging port                    | A leftover pahebatcher browser window holds the browser profile. Close it and try again.                                                           |

## Development

```bash
pip install -e ".[dev,browser]"
pytest                # tests
ruff check src        # lint
mypy src              # strict type check
```

`make test`, `make lint`, `make typecheck` and `make benchmark` do the same on
Linux, macOS and WSL.

| Path                             | Purpose                                                                  |
| -------------------------------- | ------------------------------------------------------------------------ |
| `main.py`                        | command-line parsing and the main flow                                   |
| `gui/`                           | desktop app (Tkinter): `app.py`, `bridge.py`, `widgets.py`, `assets/`    |
| `ui/`                            | terminal UI: dashboard, prompts, tables, theme, console                  |
| `downloader.py`                  | two-stage pipeline: resolve stream URLs ahead, then download in parallel |
| `store.py`                       | segment cache with atomic writes, resume, and FFmpeg assembly            |
| `http.py`, `tls.py`, `cache.py`  | HTTP client with retries, TLS context, TTL cache                         |
| `solver.py`, `browser_solver.py` | Cloudflare solving (built-in browser)                                    |
| `ffmpeg_setup.py`                | finds FFmpeg or installs it                                              |
| `extract/`                       | animepahe scanner, Kwik link extraction, M3U8 parsing                    |
| `stream.py`                      | MPV streaming                                                            |
| `watchlist.py`, `sessions.py`    | watchlist and session manager                                            |
| `config.py`, `config_manager.py` | constants and saved settings                                             |
| `models.py`, `utils.py`          | shared data classes and helpers                                          |

All paths are under `src/pahebatcher/`. The code is Python 3.11+, strictly
typed, and linted with ruff.

### Contributing

Fork, branch, and open a pull request. Keep `pytest`, `ruff check src` and
`mypy src` green, add tests for what you change, and use a conventional commit
prefix (`fix:`, `feat:`, `docs:`, `refactor:`, `chore:`).

Ideas welcome: PyPI publishing, finishing active downloads on Ctrl+C, streaming
and watchlist in the desktop app, other audio languages.

## Credits

- **Logo** by [/u/HellHarbinger][hellharbinger]. The app icon is a simplified
  version of it.
- **WSL setup** (`setup-wsl.sh` and the Makefile): idea and first version by
  [/u/HellHarbinger][hellharbinger].

## License

MIT. See [LICENSE](LICENSE). For personal and educational use. You are
responsible for following the terms of the sites you access. Support the
official creators.

## See also

- `pahebatcher.toml.example`: a ready-to-copy configuration file.

[hellharbinger]: https://www.reddit.com/user/HellHarbinger/
[latest-release]: https://github.com/abdullahkhfb/pahebatcher/releases/latest
[version-badge]: https://img.shields.io/badge/version-3.9.0-blue
