"""Persistent HLS segment store with atomic writes and assembly."""

from __future__ import annotations

import contextlib
import json
import pathlib
import subprocess
import time

from pahebatcher import ffmpeg_setup, utils


def _read_box(
    data: bytearray,
    off: int,
    end: int,
) -> tuple[str, int, int] | None:
    """Reads an MP4 box header.

    Args:
        data: The MP4 data.
        off: Offset of the box header.
        end: Offset at which to stop reading.

    Returns:
        A tuple ``(type, payload_offset, box_end)``, or None if the header
        is truncated or invalid.
    """
    if off + 8 > end:
        return None
    size = int.from_bytes(data[off:off + 4], "big")
    typ = bytes(data[off + 4:off + 8]).decode("latin1")
    if size == 1:
        if off + 16 > end:
            return None
        size = int.from_bytes(data[off + 8:off + 16], "big")
        payload_off = off + 16
    elif size == 0:
        size = end - off
        payload_off = off + 8
    else:
        payload_off = off + 8
    if size < 8 or off + size > end:
        return None
    return typ, payload_off, off + size


def _read_descr_len(
    data: bytearray,
    off: int,
    end: int,
) -> tuple[int, int] | None:
    """Reads an MPEG-4 descriptor tag and its length.

    Args:
        data: The data containing the descriptor.
        off: Offset of the descriptor tag.
        end: Offset at which to stop reading.

    Returns:
        A tuple ``(payload_offset, payload_length)``, or None if the length
        is truncated or invalid.
    """
    if off + 1 > end:
        return None
    pos = off + 1  # skip tag
    length = 0
    for _ in range(4):
        if pos >= end:
            return None
        byte = data[pos]
        pos += 1
        length = (length << 7) | (byte & 0x7F)
        if not byte & 0x80:
            break
    else:
        return None
    if pos + length > end:
        return None
    return pos, length


def _patch_aac_main_to_lc(data: bytearray) -> bool:
    """Relabels AAC Main audio as AAC-LC in every audio esds box.

    Walks the box tree for esds boxes declaring AAC Main (audio object type
    1) and changes them to AAC-LC (type 2). A box is only patched when the
    full descriptor chain validates: esds, ES_Descriptor (0x03),
    DecoderConfig (0x04, object type 0x40) and DecSpecificInfo (0x05, at
    least 2 bytes).

    Args:
        data: The MP4 file contents. Modified in place.

    Returns:
        True if at least one box was patched.
    """
    patched_any = False
    # Walk the box tree to find esds boxes. Containers have known child offsets:
    # moov/trak/mdia/minf/stbl start children immediately; stsd skips 8 bytes
    # (version/flags + entry count); mp4a sample entries have fixed fields
    # before child boxes, so esds is located by scanning.
    stack: list[tuple[int, int]] = [(0, len(data))]
    while stack:
        start, end = stack.pop()
        off = start
        while off < end:
            box = _read_box(data, off, end)
            if box is None:
                break
            typ, payload_off, box_end = box
            if typ == "esds":
                if _patch_esds_payload(data, payload_off, box_end):
                    patched_any = True
            elif typ in ("moov", "trak", "mdia", "minf", "stbl"):
                stack.append((payload_off, box_end))
            elif typ == "stsd":
                if payload_off + 8 <= box_end:
                    stack.append((payload_off + 8, box_end))
            elif typ == "mp4a":
                # Sample entry fixed fields precede child boxes; scan for esds.
                # Strict descriptor validation inside _patch_esds_payload
                # rules out false positives.
                pos = payload_off
                while True:
                    idx = data.find(b"esds", pos, box_end)
                    if idx < 0 or idx - 4 < payload_off:
                        break
                    size = int.from_bytes(data[idx - 4:idx], "big")
                    if (
                        size >= 8
                        and idx - 4 + size <= box_end
                        and _patch_esds_payload(data, idx + 4, idx - 4 + size)
                    ):
                        patched_any = True
                    pos = idx + 4
            off = box_end
    return patched_any


def _patch_esds_payload(data: bytearray, off: int, end: int) -> bool:
    """Patches a single esds payload.

    Args:
        data: The MP4 file contents. Modified in place.
        off: Offset of the esds payload.
        end: Offset at which the payload ends.

    Returns:
        True if the payload was patched.
    """
    if off + 4 > end:  # version + flags
        return False
    pos = off + 4
    # ES_Descriptor tag 0x03
    if pos >= end or data[pos] != 0x03:
        return False
    parsed = _read_descr_len(data, pos, end)
    if parsed is None:
        return False
    es_off, es_len = parsed
    es_end = es_off + es_len
    if es_off + 3 > es_end:  # ES_ID(2) + flags(1)
        return False
    pos = es_off + 3
    # DecoderConfigDescriptor tag 0x04
    if pos >= es_end or data[pos] != 0x04:
        return False
    parsed = _read_descr_len(data, pos, es_end)
    if parsed is None:
        return False
    dc_off, dc_len = parsed
    dc_end = dc_off + dc_len
    # objectTypeIndication(1) + streamType(1) + bufferSizeDB(3) + maxBitrate(4)
    # + avgBitrate(4)
    if dc_off + 13 > dc_end:
        return False
    if data[dc_off] != 0x40:  # 0x40 = MPEG-4 AAC; leave other codecs alone
        return False
    pos = dc_off + 13
    # DecSpecificInfo tag 0x05
    if pos >= dc_end or data[pos] != 0x05:
        return False
    parsed = _read_descr_len(data, pos, dc_end)
    if parsed is None:
        return False
    asc_off, asc_len = parsed
    if asc_len < 2 or asc_off + 2 > dc_end:
        return False
    # AudioObjectType = top 5 bits of first ASC byte; 1 = Main -> 2 = LC
    if data[asc_off] >> 3 != 1:
        return False
    data[asc_off] = (data[asc_off] + 0x08) & 0xFF
    return True


class SegmentStore:
    """On-disk store for the HLS segments of one episode.

    Segments are written atomically so an interrupted download can resume.
    Finished segments are assembled into an MP4 with FFmpeg.

    Attributes:
        root: The series directory inside the cache.
        dir: The directory holding this episode's segments.
    """

    def __init__(
        self,
        cache_root: pathlib.Path,
        anime_title: str,
        anime_session: str,
        ep_num: str,
        audio: str = "jpn",
    ) -> None:
        """Creates the episode's segment directory if needed.

        Args:
            cache_root: The cache directory.
            anime_title: The series title, used in the directory name.
            anime_session: The series' session identifier.
            ep_num: The episode number as text.
            audio: The audio language code.
        """
        safe_title = utils.sanitize(anime_title)
        self.root = cache_root / f"{safe_title}_{anime_session}"
        self.dir = self.root / f"Ep_{ep_num}_{audio.upper()}"
        self.dir.mkdir(parents=True, exist_ok=True)

    def save_metadata(self, anime_title: str, url: str) -> None:
        """Writes ``session.json`` for the series unless it already exists.

        Args:
            anime_title: The series title.
            url: The series URL, so the session can be resumed later.
        """
        meta = self.root / "session.json"
        if not meta.exists():
            meta.write_text(
                json.dumps(
                    {"title": anime_title, "url": url, "updated": time.time()},
                    indent=2,
                ),
                encoding="utf-8",
            )

    def seg_path(self, idx: int) -> pathlib.Path:
        return self.dir / f"{idx:06d}.ts"

    def has_seg(self, idx: int) -> bool:
        return self.seg_path(idx).exists()

    def done_indices(self) -> set[int]:
        return {int(p.stem) for p in self.dir.glob("??????.ts")}

    def write_seg(self, idx: int, data: bytes) -> None:
        """Atomically writes one segment.

        Args:
            idx: The segment index.
            data: The segment bytes.
        """
        tmp = self.seg_path(idx).with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.rename(self.seg_path(idx))

    def assemble(self, n_segments: int, out: pathlib.Path) -> bool:
        """Assembles the segments into an MP4.

        Streams all segments to FFmpeg as one contiguous MPEG-TS stream and
        falls back to FFmpeg's concat demuxer if that fails.

        Args:
            n_segments: The number of segments in the episode.
            out: The MP4 file to write.

        Returns:
            True on success, or False if a segment is missing or both methods
            fail.
        """
        missing = [
            i for i in range(n_segments) if not self.seg_path(i).exists()
        ]
        if missing:
            return False

        lst = self.dir / "concat.txt"
        try:
            # Primary: feed segments as one contiguous MPEG-TS stream.
            # The concat demuxer derives boundaries from each file's reported
            # duration, which can stretch the timeline with timestamp gaps;
            # a contiguous input avoids that. Still -c copy (lossless).
            if self._assemble_pipe(n_segments, out):
                fix_aac_esds(out)
                return True
            # Fallback: concat demuxer (handles inputs the pipe path rejects).
            if self._assemble_concat(n_segments, out, lst):
                fix_aac_esds(out)
                return True
            return False
        finally:
            with contextlib.suppress(Exception):
                lst.unlink()

    def _assemble_pipe(self, n_segments: int, out: pathlib.Path) -> bool:
        """Feeds all segments to FFmpeg as one contiguous MPEG-TS stream.

        Args:
            n_segments: The number of segments.
            out: The MP4 file to write.

        Returns:
            True if FFmpeg exited successfully.
        """
        try:
            proc = subprocess.Popen(
                [
                    ffmpeg_setup.ffmpeg_exe(),
                    "-y",
                    "-i",
                    "pipe:0",
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(out),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=ffmpeg_setup.NO_WINDOW,
            )
            assert proc.stdin is not None
            for i in range(n_segments):
                try:
                    proc.stdin.write(self.seg_path(i).read_bytes())
                except BrokenPipeError:
                    break
            proc.stdin.close()
            proc.wait(timeout=600)
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _assemble_concat(
        self,
        n_segments: int,
        out: pathlib.Path,
        lst: pathlib.Path,
    ) -> bool:
        """Concatenates the segments with FFmpeg's concat demuxer.

        Args:
            n_segments: The number of segments.
            out: The MP4 file to write.
            lst: The concat list file to write.

        Returns:
            True if FFmpeg exited successfully.
        """
        with open(lst, "w", encoding="utf-8") as f:
            for i in range(n_segments):
                f.write(f"file '{self.seg_path(i).as_posix()}'\n")

        result = subprocess.run(
            [
                ffmpeg_setup.ffmpeg_exe(),
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(lst),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(out),
            ],
            capture_output=True,
            timeout=600,
            creationflags=ffmpeg_setup.NO_WINDOW,
        )
        return result.returncode == 0

    def cleanup(self) -> None:
        """Deletes the episode's segment directory, ignoring errors."""
        with contextlib.suppress(Exception):
            import shutil
            shutil.rmtree(self.dir)


def fix_aac_esds(out: pathlib.Path) -> bool:
    """Corrects AAC audio mislabeled as AAC Main to AAC-LC.

    Some sources declare AAC Main (audio object type 1) while the payload
    is HE-AAC. Strict decoders such as Android's MediaCodec reject Main,
    while VLC tolerates it. Changing the type from 1 to 2 preserves all
    other bits. Only metadata changes; nothing is re-encoded. This is
    best-effort.

    Args:
        out: The MP4 file to fix in place.

    Returns:
        True if the file was patched. False, leaving the file untouched, if
        it did not match the expected structure.
    """
    try:
        data = bytearray(out.read_bytes())
    except OSError:
        return False
    try:
        patched = _patch_aac_main_to_lc(data)
    except Exception:
        return False
    if not patched:
        return False
    try:
        out.write_bytes(bytes(data))
    except OSError:
        return False
    return True
