"""HLS m3u8 playlist parser."""

from __future__ import annotations

import contextlib
import re
from typing import Any
import urllib.parse

import pahebatcher.http


def parse_m3u8(content: str, base_url: str) -> list[dict[str, Any]]:
    """Parses a media playlist into its segments.

    Tracks AES-128 encryption keys and initialization vectors. Without an
    explicit IV, the segment's sequence number is used, as HLS specifies.

    Args:
        content: The playlist text.
        base_url: The URL used to resolve relative URIs.

    Returns:
        One dict per segment with the keys ``url``, ``key_url`` (None if
        unencrypted) and ``iv`` (bytes or None). The list is empty if the
        playlist is a master playlist, in which case the caller must fetch
        one of its variants instead.
    """
    lines = content.splitlines()

    if "#EXT-X-STREAM-INF" in content:
        variants = [
            urllib.parse.urljoin(base_url, lines[i + 1].strip())
            for i, line in enumerate(lines)
            if line.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines)
        ]
        if variants:
            return []  # caller must re-fetch the variant

    segments: list[dict[str, Any]] = []
    key_url: str | None = None
    key_iv: bytes | None = None
    seq_num = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            with contextlib.suppress(ValueError):
                seq_num = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-KEY:"):
            kv = {
                k: vq or vr
                for k, vq, vr in re.findall(
                    r'([^,="]+)=(?:"([^"]+)"|([^,]+))', line[11:]
                )
            }
            if kv.get("METHOD") == "AES-128":
                if "URI" in kv:
                    key_url = urllib.parse.urljoin(base_url, kv["URI"])
                if iv_hex := kv.get("IV"):
                    h = iv_hex.lstrip("0xX").lstrip("0X")
                    key_iv = bytes.fromhex(("0" + h) if len(h) % 2 else h)
                else:
                    key_iv = None
            else:
                key_url = key_iv = None
        elif not line.startswith("#"):
            iv = key_iv or (seq_num.to_bytes(16, "big") if key_url else None)
            segments.append({
                "url": urllib.parse.urljoin(base_url, line),
                "key_url": key_url,
                "iv": iv,
            })
            seq_num += 1

    return segments


async def fetch_m3u8(
    http: pahebatcher.http.HttpClient,
    url: str,
    headers: dict[str, str] | None = None,
) -> str:
    """Downloads a playlist as text.

    Args:
        http: The HTTP client.
        url: The playlist URL.
        headers: Request headers, or None.

    Returns:
        The playlist text.
    """
    data = await http.get(url, headers=headers)
    return data.decode("utf-8", errors="replace")


async def resolve_m3u8(
    http: pahebatcher.http.HttpClient,
    content: str,
    base_url: str,
    headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Returns the segments for a playlist, following a master playlist.

    If ``content`` is a master playlist, the last listed variant is fetched
    and parsed instead.

    Args:
        http: The HTTP client.
        content: The playlist text.
        base_url: The URL used to resolve relative URIs.
        headers: Request headers, or None.

    Returns:
        The segments, as described in ``parse_m3u8``. Empty if none could be
        found.
    """
    segments = parse_m3u8(content, base_url)
    if segments:
        return segments
    # Must be a master playlist — follow the last variant
    lines = content.splitlines()
    variants = [
        urllib.parse.urljoin(base_url, lines[i + 1].strip())
        for i, line in enumerate(lines)
        if line.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines)
    ]
    if variants:
        sub = await fetch_m3u8(http, variants[-1], headers)
        return parse_m3u8(sub, variants[-1])
    return []
