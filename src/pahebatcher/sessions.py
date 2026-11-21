"""Session manager for the ``pahe_cache`` download cache."""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any

from rich import box
import rich.prompt
import rich.table

from pahebatcher import utils


class SessionManager:
    """Lists and manages cached download sessions.

    A session is one series' folder inside the cache directory.
    """

    @staticmethod
    def get_sessions(cache_dir: pathlib.Path) -> list[dict[str, Any]]:
        """Lists the cached sessions, newest first.

        Folders without a readable ``session.json`` are skipped.

        Args:
            cache_dir: The cache directory to scan.

        Returns:
            One dict per session with the keys ``path``, ``title``, ``url``,
            ``ep_count``, ``seg_count``, ``size`` (bytes) and ``updated`` (a
            timestamp).
        """
        sessions: list[dict[str, Any]] = []
        if not cache_dir.exists():
            return sessions
        for folder in sorted(
            cache_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True
        ):
            if not folder.is_dir():
                continue
            meta_file = folder / "session.json"
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                eps = [
                    d
                    for d in folder.iterdir()
                    if d.is_dir() and d.name.startswith("Ep_")
                ]
                segs = sum(len(list(d.glob("*.ts"))) for d in eps)
                size = sum(f.stat().st_size for f in folder.rglob("*"))
                sessions.append({
                    "path": folder,
                    "title": meta.get("title", folder.name),
                    "url": meta.get("url", ""),
                    "ep_count": len(eps),
                    "seg_count": segs,
                    "size": size,
                    "updated": meta.get("updated", folder.stat().st_mtime),
                })
            except Exception:
                continue
        return sessions

    @staticmethod
    def run(cache_dir: pathlib.Path) -> str | None:
        """Runs the interactive session and cache manager.

        Shows the cached sessions in a menu loop where they can be inspected
        and deleted.

        Args:
            cache_dir: The cache directory to manage.

        Returns:
            The URL of the session the user chose to resume, or None if they
            left without choosing one.
        """
        from pahebatcher.ui import console

        while True:
            console.console.clear()
            console.section("Session & Cache Manager")

            sessions = SessionManager.get_sessions(cache_dir)
            total_size = sum(s["size"] for s in sessions)

            if not sessions:
                console.console.print(
                    "\n  [dim]No active sessions or cache found.[/dim]"
                )
                rich.prompt.Prompt.ask(
                    "\n  [brand]Press Enter to return to menu[/brand]"
                )
                return None

            table = rich.table.Table(
                box=box.ROUNDED, header_style="brand.bold", border_style="dim"
            )
            table.add_column("#", justify="right", style="dim")
            table.add_column("Anime Title", ratio=1)
            table.add_column("Eps", justify="center")
            table.add_column("Segments", justify="center")
            table.add_column("Size", justify="right", style="green")
            table.add_column("Status", justify="center")

            for i, s in enumerate(sessions, 1):
                table.add_row(
                    str(i),
                    s["title"],
                    str(s["ep_count"]),
                    str(s["seg_count"]),
                    utils.fmt_bytes(s["size"]),
                    "[yellow]Paused[/yellow]",
                )

            console.console.print(table)
            size_str = utils.fmt_bytes(total_size)
            console.console.print(
                "  [dim]Total Cache Size:[/dim] "
                f"[brand.bold]{size_str}[/brand.bold]\n"
            )

            choices = ["B", "b", "R", "r", "D", "d", "C", "c"]
            prompt_text = (
                "  [brand][R]esume[/brand]  [brand][D]elete[/brand]  "
                "[brand][C]lear All[/brand]  [white][B]ack[/white] > "
            )
            choice = rich.prompt.Prompt.ask(
                prompt_text, choices=choices, default="B", show_choices=False
            ).upper()

            if choice == "B":
                return None
            if choice == "C":
                if rich.prompt.Confirm.ask(
                    "  [red]Wipe entire cache folder?[/red]", default=False
                ):
                    import shutil
                    shutil.rmtree(cache_dir, ignore_errors=True)
                    console.console.print(
                        "  [green]\u2713 Cache cleared.[/green]"
                    )
                    time.sleep(0.5)
                continue
            if choice in ("R", "D"):
                idx = rich.prompt.IntPrompt.ask(
                    f"  Select # to {'Resume' if choice == 'R' else 'Delete'}",
                    default=1,
                )
                if 1 <= idx <= len(sessions):
                    target = sessions[idx - 1]
                    if choice == "R":
                        return str(target["url"])
                    if rich.prompt.Confirm.ask(
                        f"  [red]Delete session for '{target['title']}'?[/red]",
                        default=True,
                    ):
                        import pathlib
                        import shutil

                        target_path = target["path"]
                        if isinstance(target_path, pathlib.Path):
                            shutil.rmtree(target_path, ignore_errors=True)
                        console.console.print(
                            "  [green]\u2713 Deleted "
                            f"'{target['title']}'.[/green]"
                        )
                        time.sleep(0.5)
                continue
