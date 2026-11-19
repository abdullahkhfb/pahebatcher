"""Brand theme — animepahe pink. Single source of truth for chrome colors.

Semantic colors stay where they are used: green/red/yellow for status,
cyan for SUB vs yellow for DUB. Everything that was bare `cyan` chrome
(rules, panel borders, table headers, highlights) uses `brand`/`accent`.
"""

from __future__ import annotations

from rich.theme import Theme

BRAND = "#ff3f90"
ACCENT = "#fb6cab"
SOFT = "#f9a8cd"
PALE = "#fbdcef"

# Minimal-UI palette (matches the animepahe look: black ground, one pink accent).
BAR = "#d5015a"      # progress fill / solid pink surfaces
MUTED = "#8a8a96"    # secondary text, hints, queued rows
TRACK = "#1c1c22"    # empty progress track
OK = "#3ddc97"       # done / healthy
WARN = "#ffb020"     # resolving / retrying

THEME = Theme({
    # NOTE: Rich >=15 cannot combine an attribute with a theme name
    # ("bold brand" fails to parse), so combined variants are registered
    # as dotted names. Always use these — never "bold brand"/"dim brand".
    "brand": BRAND,
    "brand.bold": f"bold {BRAND}",
    "brand.dim": f"dim {BRAND}",
    "accent": ACCENT,
    "accent.bold": f"bold {ACCENT}",
    "soft": SOFT,
    "soft.bold": f"bold {SOFT}",
    "pale": PALE,
    "pale.bold": f"bold {PALE}",
    "muted": MUTED,
    "ok": OK,
    "ok.bold": f"bold {OK}",
    "warn": WARN,
    "bar": BAR,
    "track": TRACK,
})
