"""MPV stream player with a live playback panel and navigation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
from typing import TYPE_CHECKING
import urllib.parse

import aiohttp
from aiohttp import web
from rich import box
import rich.align
import rich.console
import rich.live
import rich.panel
import rich.progress
import rich.prompt
import rich.rule
import rich.table
import rich.text

from pahebatcher.extract import kwik, m3u8
import pahebatcher.http
from pahebatcher.ui import console

if TYPE_CHECKING:
    from pahebatcher import models
    import pahebatcher.solver

log = logging.getLogger(__name__)


def _clean_ep_title(ep: models.EpisodeInfo, info: models.StreamInfo) -> None:
    """Replaces a missing or site-generated title with the stream's title.

    The stream's page title is stripped of animepahe branding, quality tags
    and SUB/DUB markers first. The episode is only changed if its own title
    is empty or looks site-generated, and the cleaned title is not empty.

    Args:
        ep: The episode. Its ``title`` may be updated in place.
        info: The resolved stream, whose title is the source.
    """
    if not info.title:
        return
    if ep.title and "animepahe" not in ep.title.lower() and "?" not in ep.title:
        return
    t = info.title
    t = re.sub(r"\s*::\s*animepahe.*$", "", t, flags=re.I).strip()
    t = re.sub(r"\s*-\s*animepahe.*$", "", t, flags=re.I).strip()
    t = re.sub(r"\s*[|\u00b7].*$", "", t).strip()
    t = re.sub(r"^Watch\s+.*?Episode\s+\d+.*", "", t, flags=re.I).strip()
    t = re.sub(r"\((?:1080|720|360)p\).*", "", t, flags=re.I).strip()
    t = re.sub(r"\[SubsPlease\].*", "", t, flags=re.I).strip()
    t = re.sub(r"AnimePahe_", "", t, flags=re.I).strip()
    t = t.replace("_", " ").strip()
    t = re.sub(r"\s+(?:DUB|SUB)\s*$", "", t, flags=re.I).strip()
    if t:
        ep.title = t


def _display_title(ep: models.EpisodeInfo) -> str:
    t = re.sub(r"\s+(?:DUB|SUB)\s*$", "", ep.title or "", flags=re.I).strip()
    return t or f"Episode {ep.ep_str}"


def _audio_pill(lang: str) -> str:
    return "[cyan]SUB[/cyan]" if lang == "jpn" else "[yellow]DUB[/yellow]"


async def _prepare_local_hls(
    info: models.StreamInfo, http_client: object, port: int = 0
) -> tuple[str, object, object]:
    """Serves a stream's playlist locally so MPV can play it.

    The playlist is fetched through the ``HttpClient``, which handles the
    HTTP 403 fallback, and segment and key requests are proxied through it.

    Args:
        info: The resolved stream.
        http_client: The ``HttpClient`` used for all fetches.
        port: The local port to listen on. 0 picks a free one.

    Returns:
        A tuple ``(local_url, runner, site)``. The caller must clean up with
        ``runner.cleanup()``. If serving locally fails, the remote URL is
        returned with ``None`` for the runner and site.

    Raises:
        RuntimeError: If the playlist cannot be fetched.
    """

    # Ensure http_client is HttpClient and started
    http: pahebatcher.http.HttpClient
    if isinstance(http_client, pahebatcher.http.HttpClient):
        http = http_client
    else:
        http = pahebatcher.http.HttpClient(hls_workers=8)
        await http.start()

    # Fetch original m3u8 via HttpClient (handles curl fallback for 403)
    try:
        m3u8_text = await m3u8.fetch_m3u8(http, info.url, info.headers)
    except Exception as exc:  # isolation point: keep going
        log.warning(
            "Failed to fetch m3u8 via HttpClient, falling back to remote: %s",
            exc,
        )
        return info.url, None, None

    # Parse and collect segment/key URLs to proxy
    # For now, serve the original m3u8 text rewritten to local URLs
    # We will serve m3u8 at /uwu.m3u8 and proxy segments/keys via same server
    # Rewrite segment URLs and key URI to local
    # Keep original m3u8 text for debugging, but rewrite for local serving

    # Find remote base for segments (for proxying, we need to map local path ->
    # remote URL) We'll create a dict local_path -> remote_url
    remote_map: dict[str, str] = {}
    # Extract key URI
    key_match = re.search(
        r'#EXT-X-KEY:METHOD=AES-128,URI="([^"]+)"', m3u8_text
    )
    if key_match:
        raw_key_url = key_match.group(1)
        # Normalize key URL (may be relative)

        remote_key_url = (
            raw_key_url
            if raw_key_url.startswith("http")
            else urllib.parse.urljoin(info.url, raw_key_url)
        )
        remote_map["/mon.key"] = remote_key_url
        m3u8_text = m3u8_text.replace(
            raw_key_url, "http://127.0.0.1:{port}/mon.key"
        )

    # Extract segment URLs (lines not starting with #)
    lines = m3u8_text.splitlines()
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            remote_seg = line
            # Normalize

            if not remote_seg.startswith("http"):
                remote_seg = urllib.parse.urljoin(info.url, remote_seg)
            # Map to local path based on last part
            parsed = urllib.parse.urlparse(remote_seg)
            local_seg_path = "/" + parsed.path.split("/")[-1]
            # Ensure unique
            if local_seg_path not in remote_map:
                remote_map[local_seg_path] = remote_seg
            # Replace in m3u8 text
            m3u8_text = m3u8_text.replace(
                line, f"http://127.0.0.1:{{port}}{local_seg_path}"
            )

    # Handler to serve m3u8
    async def handle_m3u8(request: web.Request) -> web.Response:
        """Serves the rewritten playlist, answering GET and HEAD.

        Args:
            request: The incoming request.

        Returns:
            The playlist response.
        """
        # Replace port placeholder with actual port
        body = m3u8_text.replace("{port}", str(request.app["port"]))
        if request.method == "HEAD":
            return web.Response(
                status=200,
                headers={
                    "Content-Type": "application/vnd.apple.mpegurl",
                    "Content-Length": str(len(body.encode())),
                },
            )
        return web.Response(
            text=body,
            content_type="application/vnd.apple.mpegurl",
            headers={"Accept-Ranges": "bytes"},
        )

    # Generic handler for segments/keys
    async def handle_proxy(request: web.Request) -> web.Response:
        """Proxies a segment or key request to the remote server.

        Args:
            request: The incoming request, whose path maps to a remote URL.

        Returns:
            The remote content, or a 404 response for unknown paths.
        """
        local_path = request.path
        remote_url = request.app["remote_map"].get(local_path)
        if not remote_url:
            return web.Response(status=404, text="Not found")
        # Fetch remote via HttpClient with original headers (handles fallback)
        try:
            data = await http.get(remote_url, headers=info.headers, timeout=60)
            # Determine content type
            ctype = "application/octet-stream"
            if local_path.endswith(".key"):
                ctype = "application/octet-stream"
            elif local_path.endswith(".jpg") or local_path.endswith(".ts"):
                ctype = "video/MP2T"
            # Handle Range header for MPV probing
            range_hdr = request.headers.get("Range")
            if range_hdr and request.method == "GET":
                # Simple bytes= start-end handling

                m = re.match(r"bytes=(\d*)-(\d*)", range_hdr)
                if m:
                    start_s, end_s = m.groups()
                    start = int(start_s) if start_s else 0
                    end = int(end_s) if end_s else len(data) - 1
                    end = min(end, len(data) - 1)
                    if start <= end:
                        sliced = data[start : end + 1]
                        headers = {
                            "Content-Range": f"bytes {start}-{end}/{len(data)}",
                            "Accept-Ranges": "bytes",
                            "Content-Length": str(len(sliced)),
                            "Content-Type": ctype,
                        }
                        return web.Response(
                            status=206, body=sliced, headers=headers
                        )
            if request.method == "HEAD":
                return web.Response(
                    status=200,
                    headers={
                        "Content-Type": ctype,
                        "Content-Length": str(len(data)),
                        "Accept-Ranges": "bytes",
                    },
                )
            return web.Response(
                body=data,
                content_type=ctype,
                headers={"Accept-Ranges": "bytes"},
            )
        except Exception as exc:  # isolation point: keep going
            log.warning("Local HLS proxy failed for %s: %s", local_path, exc)
            return web.Response(status=502, text=f"Proxy failed: {exc}")

    app = web.Application()
    app["remote_map"] = remote_map
    app["port"] = port  # placeholder, will update after site start
    app.router.add_route("*", "/uwu.m3u8", handle_m3u8)
    app.router.add_route("*", "/{tail:.*}", handle_proxy)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    # Get actual port
    sockets = site._server.sockets if site._server else []  # type: ignore[attr-defined]
    actual_port = sockets[0].getsockname()[1] if sockets else 8080
    app["port"] = actual_port
    local_url = f"http://127.0.0.1:{actual_port}/uwu.m3u8"
    # Verify local m3u8 fetch works (quick check)
    try:
        # Quick fetch via http to ensure server works

        async with (
            aiohttp.ClientSession() as sess,
            sess.get(local_url, timeout=aiohttp.ClientTimeout(total=5)) as resp,
        ):
            if resp.status != 200:
                raise RuntimeError(f"Local server check failed {resp.status}")
    except Exception as exc:  # isolation point: keep going
        log.warning(
            "Local HLS server check failed, falling back to remote: %s", exc
        )
        with contextlib.suppress(Exception):
            await runner.cleanup()
        return info.url, None, None

    return local_url, runner, site


async def run_stream(
    ctx: models.AppContext,
    anime: models.AnimeInfo,
    chosen_episodes: list[models.EpisodeInfo],
    solver: pahebatcher.solver.Solver,
) -> None:
    """Streams episodes through MPV with an interactive control panel.

    Shows a live panel with the current episode and playlist position, and
    lets the user move between episodes, change audio language and quit.
    Does nothing but print a hint if MPV is not installed.

    Args:
        ctx: The settings for this run.
        anime: The series being streamed.
        chosen_episodes: The episodes to queue.
        solver: The Cloudflare solver.
    """

    if not shutil.which("mpv"):
        console.console.print("\n  [red]\u2717 MPV not found![/red]")
        console.console.print(
            "  [dim]Install MPV from https://mpv.io and ensure it is in your "
            "PATH.[/dim]"
        )
        return

    audio_pref = ctx.audio_lang

    def _build_playlist(lang: str) -> list[models.EpisodeInfo]:
        result: list[models.EpisodeInfo] = []
        for ep in chosen_episodes:
            variant = anime.get_variant(ep.number, lang) or ep
            if variant not in result:
                result.append(variant)
        return result

    playlist = _build_playlist(audio_pref)
    idx = 0

    console.console.print()
    console.section("Streaming via MPV")

    def render_play_panel(
        ep: models.EpisodeInfo, state: str = "playing", choices_ui: str = "",
    ) -> rich.panel.Panel:
        """Renders the playback panel.

        Args:
            ep: The episode being played.
            state: The player state, such as ``"playing"``.
            choices_ui: Extra text describing the available controls.

        Returns:
            The panel to display.
        """
        cur_lang = ep.audio
        other_lang = "eng" if cur_lang == "jpn" else "jpn"
        has_alt = anime.get_variant(ep.number, other_lang) is not None
        audio_str = _audio_pill(cur_lang)
        if has_alt:
            alt_name = "DUB" if other_lang == "eng" else "SUB"
            audio_str += (
                f"  [dim]([brand]{alt_name} available \u2014 press "
                "[bold]A[/bold] to switch[/brand])[/dim]"
            )

        ep_title = _display_title(ep)
        if state == "playing":
            content = rich.console.Group(
                rich.text.Text(anime.title, style="brand.bold underline"),
                rich.text.Text.from_markup(
                    f"\u25b6  Ep [brand]{ep.ep_str}[/brand]  [bold "
                    f"white]{ep_title}[/bold white]",
                    style="bold green",
                ),
                rich.text.Text.from_markup(
                    f"   Audio: {audio_str}  \u00b7  Quality: {ctx.quality}p",
                    style="dim",
                ),
                rich.text.Text.from_markup(
                    f"   Episode [brand]{idx + 1}[/brand] of "
                    f"[brand]{len(playlist)}[/brand]",
                    style="dim",
                ),
                rich.rule.Rule(style="dim", characters="\u2500"),
                rich.text.Text(
                    "Close MPV window to return to controls", style="italic dim"
                ),
            )
            title_p = "[brand.bold]\u25b6  Live Playback[/brand.bold]"
            border_c = "green"
        else:
            content = rich.console.Group(
                rich.text.Text(anime.title, style="brand.bold underline"),
                rich.text.Text.from_markup(
                    f"\u25a0  Ep [brand]{ep.ep_str}[/brand]  "
                    f"[dim]{ep_title}[/dim]  {audio_str}",
                    style="dim",
                ),
                rich.rule.Rule(style="dim", characters="\u2500"),
                rich.text.Text.from_markup(choices_ui or ""),
            )
            title_p = "[bold yellow]\u25a0  Playback Ended[/bold yellow]"
            border_c = "yellow"
        return rich.panel.Panel(
            rich.align.Align.center(content),
            title=title_p,
            border_style=border_c,
            box=box.ROUNDED,
            padding=(1, 2),
        )

    # Shared HttpClient for HLS proxy (handles 403 fallback)

    http = pahebatcher.http.HttpClient(hls_workers=8)
    await http.start()

    try:
        while 0 <= idx < len(playlist):
            ep = playlist[idx]
            local_runner = None
            try:
                with rich.progress.Progress(
                    rich.progress.SpinnerColumn(),
                    rich.progress.TextColumn(
                        f"[bold white]({idx + 1}/{len(playlist)})"
                        f"  Resolving Ep [brand]{ep.ep_str}[/brand]"
                        f"  {_audio_pill(audio_pref)}\u2026"
                    ),
                    console=console.console, transient=True,
                ) as prog:
                    prog.add_task("", total=None)
                    info = await kwik.extract_stream(
                        solver,
                        ep.play_url,
                        ctx.quality,
                        audio_pref,
                        ctx.cookie_string,
                    )

                ep.audio = info.audio
                _clean_ep_title(ep, info)
                if info.fansub:
                    ep.fansub = info.fansub

                mpv_title = re.sub(
                    r"\s+(?:DUB|SUB)\s*$", "",
                    ep.title or f"Episode {ep.ep_str}", flags=re.I,
                ).strip()
                audio_tag = "SUB" if ep.audio == "jpn" else "DUB"

                # Try local HLS proxy first (avoids MPV 403 on HTTP/1.1)
                local_url = info.url
                try:
                    local_url, local_runner, _ = await _prepare_local_hls(
                        info, http, port=0
                    )
                    if local_url != info.url:
                        log.debug(
                            "Using local HLS proxy: %s -> %s",
                            info.url,
                            local_url,
                        )
                except Exception as exc:  # isolation point: keep going
                    log.warning(
                        "Local HLS proxy failed, falling back to remote: %s",
                        exc,
                    )
                    local_url = info.url
                    local_runner = None

                # Build MPV command — use local_url, no headers needed for local
                if local_runner is not None:
                    cmd = [
                        "mpv",
                        f"--force-media-title={mpv_title} [{audio_tag}]",
                        "--ytdl=no",
                        "--msg-level=all=warn,lavf=error,ffmpeg=error",
                        local_url,
                    ]
                else:
                    # Fallback to remote with headers (original)
                    # Only include Cookie header if present to avoid malformed
                    http_fields = (
                        f"Cookie: {info.cookie_str}" if info.cookie_str else ""
                    )
                    cmd = [
                        "mpv",
                        f"--user-agent={info.user_agent}",
                        f"--referrer={info.referer}",
                    ]
                    if http_fields:
                        cmd.append(f"--http-header-fields={http_fields}")
                    cmd.extend([
                        "--demuxer-lavf-format=hls",
                        (
                            f"--demuxer-lavf-o=cookies={info.cookie_str},"
                            f"referer={info.referer}"
                            if info.cookie_str
                            else f"--demuxer-lavf-o=referer={info.referer}"
                        ),
                        f"--force-media-title={mpv_title} [{audio_tag}]",
                        "--ytdl=no",
                        "--msg-level=all=warn,lavf=error,ffmpeg=error",
                        info.url,
                    ])

                with rich.live.Live(
                    render_play_panel(ep, "playing"),
                    console=console.console,
                    refresh_per_second=4,
                ) as live:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()
                    # Log MPV result for diagnostics
                    if proc.returncode not in (0, None):
                        err_text = (
                            stderr.decode(errors="ignore")[:800]
                            if stderr
                            else ""
                        )
                        log.warning(
                            "MPV exit %s: %s", proc.returncode, err_text
                        )
                        if ctx.verbose if hasattr(ctx, "verbose") else False:
                            console.console.print(
                                f"[dim]MPV stderr: {err_text}[/dim]"
                            )
                        # Also show to user if failed quickly
                        if err_text and "HTTP error 403" in err_text:
                            console.console.print(
                                "[yellow]MPV got 403 — retrying via local "
                                "proxy may help.[/yellow]"
                            )

                    valid_choices: list[str] = ["r", "s", "q"]
                    prompt_parts: list[str] = []
                    ui_options: list[str] = []

                    if idx > 0:
                        valid_choices.insert(0, "p")
                        prompt_parts.append("[bold](P)[/bold]rev")
                        ui_options.append("[brand]P[/brand]rev")
                    if idx < len(playlist) - 1:
                        valid_choices.insert(0, "n")
                        prompt_parts.append("[bold](N)[/bold]ext")
                        ui_options.append("[green]N[/green]ext")

                    valid_choices.insert(0, "a")
                    prompt_parts.append("[bold](A)[/bold]udio")
                    other_lang = "eng" if audio_pref == "jpn" else "jpn"
                    alt_name = "DUB" if other_lang == "eng" else "SUB"
                    ui_options.append(f"[yellow]A[/yellow]udio\u2192{alt_name}")

                    ui_options += [
                        "[white]R[/white]eplay",
                        "[accent]S[/accent]elect",
                        "[red]Q[/red]uit",
                    ]
                    prompt_parts += [
                        "[bold](R)[/bold]eplay",
                        "[bold](S)[/bold]elect",
                        "[bold](Q)[/bold]uit",
                    ]

                    choices_ui = "  \u00b7  ".join(ui_options)
                    live.update(render_play_panel(ep, "ended", choices_ui))

                default = "n" if idx < len(playlist) - 1 else "q"
                all_choices = valid_choices + [c.upper() for c in valid_choices]
                choice = rich.prompt.Prompt.ask(
                    "  [brand]" + "  ".join(prompt_parts) + "[/brand]",
                    choices=all_choices, default=default, show_choices=False,
                ).lower()

                if choice == "n":
                    idx += 1
                elif choice == "p":
                    idx -= 1
                elif choice == "r":
                    continue
                elif choice == "q":
                    break
                elif choice == "a":
                    other_lang = "eng" if audio_pref == "jpn" else "jpn"
                    audio_pref = other_lang
                    playlist = _build_playlist(audio_pref)
                    idx = max(0, min(idx, len(playlist) - 1))
                    continue
                elif choice == "s":
                    console.console.print()
                    sel = rich.table.Table(
                        box=box.ROUNDED,
                        header_style="brand.bold",
                        title="[bold white]Select Episode \u2014 "
                        f"{anime.title}[/bold white]",
                    )
                    sel.add_column("#", justify="right", style="dim", width=4)
                    sel.add_column("Ep", justify="right", width=6)
                    sel.add_column("Title", ratio=1)
                    for i, e in enumerate(playlist):
                        style_s = "bold green" if i == idx else "dim"
                        pointer = "\u2192 " if i == idx else "  "
                        sel.add_row(
                            f"{pointer}{i + 1}",
                            e.ep_str,
                            _display_title(e),
                            style=style_s,
                        )
                    console.console.print(sel)
                    num = rich.prompt.IntPrompt.ask(
                        f"  [brand]Jump to # (1\u2013{len(playlist)})[/brand]",
                        default=idx + 1,
                    )
                    idx = max(0, min(num - 1, len(playlist) - 1))
            except KeyboardInterrupt:
                break
            except Exception as exc:  # isolation point: keep going
                console.console.print(f"  [red]\u2717 Error:[/red] {exc}")
                if not rich.prompt.Confirm.ask(
                    "  [brand]Try next episode?[/brand]", default=True
                ):
                    break
            finally:
                if local_runner is not None:
                    with contextlib.suppress(Exception):
                        await local_runner.cleanup()  # type: ignore[attr-defined]

        console.console.print("\n  [yellow]Playback session ended.[/yellow]")
    finally:
        with contextlib.suppress(Exception):
            await http.close()
