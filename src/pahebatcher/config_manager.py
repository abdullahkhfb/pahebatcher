"""Persistent TOML configuration manager for pahebatcher."""

from __future__ import annotations

import pathlib
import tomllib
from typing import Any

from rich import box
import rich.table

from pahebatcher.ui import console


class ConfigManager:
    """Reads and writes persistent settings in ``pahebatcher.toml``.

    Attributes:
        DEFAULTS: The default value of every known setting.
        path: The configuration file.
        data: The current settings.
    """

    DEFAULT_PATH = pathlib.Path("pahebatcher.toml")

    DEFAULTS: dict[str, Any] = {
        "quality": 1080,
        "audio_lang": "jpn",
        "max_parallel": 2,
        "hls_workers": 24,
        "output_dir": "./downloads",
        "keep_temp": False,
        "resolve_ahead": 999,
        "cache_ttl": 60,
        "cookie_string": "",
        "auto_retry": 2,
        "clearance_ttl": 20,
        "browser_mode": "auto",
    }

    def __init__(self, path: pathlib.Path | None = None) -> None:
        """Initializes the manager with the default settings.

        Args:
            path: The configuration file. Defaults to ``DEFAULT_PATH``.
        """
        self.path = path or self.DEFAULT_PATH
        self.data: dict[str, Any] = dict(self.DEFAULTS)

    def load(self) -> None:
        """Loads known settings from the file, if it exists.

        Unknown keys in the file are ignored.
        """
        if not self.path.exists():
            return
        raw = self.path.read_bytes()
        loaded = tomllib.loads(raw.decode("utf-8"))
        for key in self.DEFAULTS:
            if key in loaded:
                self.data[key] = loaded[key]

    def save(self) -> None:
        """Writes all settings to the configuration file."""
        lines: list[str] = ["# pahebatcher configuration", ""]
        for key, default in self.DEFAULTS.items():
            val = self.data.get(key, default)
            lines.append(f"{key} = {_format_value(val)}")
        lines.append("")
        self.path.write_text("\n".join(lines), encoding="utf-8")

    def get(self, key: str) -> Any:
        return self.data.get(key, self.DEFAULTS.get(key))

    def set(self, key: str, value: Any) -> None:
        """Validates and stores a setting.

        Args:
            key: The setting name.
            value: The new value.

        Raises:
            KeyError: If the setting is unknown.
            ValueError: If the value is not valid for the setting.
        """
        validated = self._validate(key, value)
        self.data[key] = validated

    def show(self) -> dict[str, Any]:
        return dict(self.data)

    def is_customized(self) -> bool:
        """Whether the configuration file exists.

        This means the user has completed first-run setup.
        """
        return self.path.exists()

    # ── CLI interface ──────────────────────────────────────────────────────

    # ── internal helpers ───────────────────────────────────────────────────

    def _validate(self, key: str, value: Any) -> Any:
        """Validates a value and clamps it to the allowed range.

        Args:
            key: The setting name.
            value: The value to validate.

        Returns:
            The normalized value.

        Raises:
            KeyError: If the setting is unknown.
            ValueError: If the value is not valid for the setting.
        """
        validators: dict[str, Any] = {
            "quality": lambda v: v if v in (360, 720, 1080) else None,
            "audio_lang": lambda v: v if v in ("jpn", "eng") else None,
            "max_parallel": lambda v: max(1, min(6, int(v))),
            "hls_workers": lambda v: max(8, min(32, int(v))),
            "output_dir": lambda v: str(v),
            "keep_temp": lambda v: bool(v),
            "resolve_ahead": lambda v: max(0, int(v)),
            "cache_ttl": lambda v: max(0, int(v)),
            "cookie_string": lambda v: str(v),
            "auto_retry": lambda v: max(0, min(2, int(v))),
            "clearance_ttl": lambda v: max(0, int(v)),
            "browser_mode": lambda v: v
            if v in ("auto", "headless", "headed")
            else None,
        }
        if key not in validators:
            raise KeyError(f"Unknown config key: {key}")
        result = validators[key](value)
        if result is None:
            raise ValueError(f"Invalid value for {key}: {value}")
        return result

    def _convert(self, key: str, raw: str) -> Any:
        """Converts command-line text to the setting's type.

        Args:
            key: The setting name.
            raw: The value as text.

        Returns:
            The converted value.

        Raises:
            KeyError: If the setting is unknown.
        """
        converters: dict[str, Any] = {
            "quality": int,
            "audio_lang": str,
            "max_parallel": int,
            "hls_workers": int,
            "output_dir": str,
            "keep_temp": lambda v: v.lower() in ("true", "yes", "1", "on"),
            "resolve_ahead": int,
            "cache_ttl": int,
            "cookie_string": str,
            "auto_retry": int,
            "clearance_ttl": int,
            "browser_mode": lambda v: v.strip().lower(),
        }
        if key not in converters:
            raise KeyError(f"Unknown config key: {key}")
        return converters[key](raw)


def cli_show(path: pathlib.Path | None = None) -> None:
    """Prints the current settings as a table.

    Args:
        path: The configuration file. Defaults to ``DEFAULT_PATH``.
    """

    cm = ConfigManager(path)
    cm.load()
    t = rich.table.Table(
        box=box.ROUNDED,
        header_style="brand.bold",
        title="pahebatcher configuration",
    )
    t.add_column("Key", style="brand")
    t.add_column("Value", style="white")
    t.add_column("Default", style="dim")
    for key in cm.DEFAULTS:
        t.add_row(key, str(cm.get(key)), str(cm.DEFAULTS[key]))
    console.console.print(t)


def cli_set(key: str, value: str, path: pathlib.Path | None = None) -> None:
    """Converts, validates and saves one setting from the command line.

    Args:
        key: The setting name.
        value: The new value as text.
        path: The configuration file. Defaults to ``DEFAULT_PATH``.
    """

    cm = ConfigManager(path)
    cm.load()
    converted = cm._convert(key, value)
    cm.set(key, converted)
    cm.save()
    console.console.print(f"  [green]\u2713[/green] {key} = {converted}")


def cli_reset(path: pathlib.Path | None = None) -> None:
    """Resets every setting to its default and saves.

    Args:
        path: The configuration file. Defaults to ``DEFAULT_PATH``.
    """

    cm = ConfigManager(path)
    cm.data = dict(cm.DEFAULTS)
    cm.save()
    console.console.print(
        "  [green]\u2713[/green] Configuration reset to defaults"
    )


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return repr(value)
    return str(value)
