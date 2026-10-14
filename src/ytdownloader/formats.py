"""Quality tiers and stream selection.

Everything in this module works on plain yt-dlp format dictionaries so it can
be unit tested without touching the network.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum

# Standard quality tiers, in pixels of the "p" number (e.g. 1080p).
TIERS = (144, 240, 360, 480, 720, 1080, 1440, 2160, 4320)
MAX_TIER = 2160

TIER_NAMES = {
    4320: "8K",
    2160: "4K",
    1440: "2K",
    1080: "Full HD",
    720: "HD",
}


class Mode(str, Enum):
    """How the video stream should end up inside the MP4."""

    # H.264 where YouTube offers it (up to 1080p), otherwise the best stream is
    # converted so it plays in the system player: HEVC for QuickTime on a Mac,
    # H.264 on Windows (HEVC playback needs a paid extension there).
    COMPATIBLE = "compatible"
    # Keep YouTube's original stream (usually AV1 or VP9 above 1080p). Fastest
    # and lossless, but QuickTime may not play VP9.
    ORIGINAL = "original"


def target_codec(platform: str | None = None) -> str:
    """Codec used when converting or upscaling."""
    return "h264" if (platform or sys.platform) == "win32" else "hevc"


def target_codec_label(platform: str | None = None) -> str:
    return {"h264": "H.264", "hevc": "HEVC"}[target_codec(platform)]


def tier_label(tier: int) -> str:
    name = TIER_NAMES.get(tier)
    return f"{tier}p ({name})" if name else f"{tier}p"


def _is_none(codec: str | None) -> bool:
    return codec is None or codec == "none"


def has_video(fmt: dict) -> bool:
    if not _is_none(fmt.get("vcodec")):
        return True
    # Some extractors leave vcodec unset but do report a resolution.
    return fmt.get("vcodec") is None and bool(fmt.get("height"))


def has_audio(fmt: dict) -> bool:
    return not _is_none(fmt.get("acodec"))


def is_video_only(fmt: dict) -> bool:
    return has_video(fmt) and _is_none(fmt.get("acodec"))


def is_audio_only(fmt: dict) -> bool:
    return has_audio(fmt) and _is_none(fmt.get("vcodec"))


def _usable(fmt: dict) -> bool:
    if fmt.get("has_drm"):
        return False
    if fmt.get("ext") in ("mhtml",):  # storyboards
        return False
    return True


def format_tier(fmt: dict) -> int | None:
    """Map a format's dimensions onto a standard tier like 1080 or 2160.

    Uses both sides so that cinema-scope (1920x800) and vertical (1080x1920)
    videos land on the tier people expect.
    """
    width, height = fmt.get("width"), fmt.get("height")
    if not height and not width:
        return None
    if width and height:
        short_side = min(width, height)
        from_long_side = max(width, height) * 9 / 16
        value = max(short_side, from_long_side)
    else:
        value = height or (width * 9 / 16)
    return min(TIERS, key=lambda t: abs(t - value))


def video_codec_family(vcodec: str | None) -> str:
    codec = (vcodec or "").lower()
    if codec.startswith(("avc1", "avc3", "h264")):
        return "h264"
    if codec.startswith(("hvc1", "hev1", "hevc", "h265")):
        return "hevc"
    if codec.startswith(("av01", "av1")):
        return "av1"
    if codec.startswith("vp09") or codec.startswith("vp9"):
        return "vp9"
    if codec.startswith(("vp08", "vp8")):
        return "vp8"
    return "other"


def is_apple_compatible(vcodec: str | None) -> bool:
    return video_codec_family(vcodec) in ("h264", "hevc")


def is_compatible(vcodec: str | None, platform: str | None = None) -> bool:
    """Plays in the system's own player without extra codecs."""
    if (platform or sys.platform) == "win32":
        return video_codec_family(vcodec) == "h264"
    return is_apple_compatible(vcodec)


# Higher is better.
_CODEC_RANK = {
    # Prefer streams QuickTime plays as-is; among the rest prefer VP9 because
    # every ffmpeg build can decode it quickly for the HEVC conversion.
    Mode.COMPATIBLE: {"h264": 5, "hevc": 4, "vp9": 3, "av1": 2, "vp8": 1, "other": 0},
    # Best compression/quality first.
    Mode.ORIGINAL: {"av1": 5, "vp9": 4, "hevc": 3, "h264": 2, "vp8": 1, "other": 0},
}


def _video_sort_key(fmt: dict, mode: Mode):
    return (
        fmt.get("dynamic_range") in (None, "SDR"),  # HDR converts badly; prefer SDR
        _CODEC_RANK[mode][video_codec_family(fmt.get("vcodec"))],
        fmt.get("fps") or 0,
        fmt.get("ext") == "mp4",
        fmt.get("tbr") or fmt.get("vbr") or 0,
    )


def _audio_sort_key(fmt: dict):
    format_id = str(fmt.get("format_id", ""))
    note = (fmt.get("format_note") or "").lower()
    acodec = (fmt.get("acodec") or "").lower()
    return (
        "drc" not in format_id and "drc" not in note,  # avoid dynamic-range-compressed copies
        fmt.get("language_preference") or 0,  # the original audio track ranks highest
        acodec.startswith("mp4a") or fmt.get("ext") == "m4a",  # AAC belongs in MP4
        fmt.get("abr") or fmt.get("tbr") or 0,
    )


def _mp3_source_key(fmt: dict):
    format_id = str(fmt.get("format_id", ""))
    note = (fmt.get("format_note") or "").lower()
    return (
        "drc" not in format_id and "drc" not in note,
        fmt.get("language_preference") or 0,
        fmt.get("abr") or fmt.get("tbr") or 0,  # converting anyway, so just take the richest source
    )


def select_audio(formats: list[dict]) -> dict:
    """Best source stream for an MP3 (audio-only if possible)."""
    usable = [f for f in formats if _usable(f)]
    audio_only = [f for f in usable if is_audio_only(f)]
    if audio_only:
        return max(audio_only, key=_mp3_source_key)
    combined = [f for f in usable if has_audio(f)]
    if combined:
        # Smallest video tier with the best audio keeps the download small.
        return max(combined, key=lambda f: (_mp3_source_key(f), -(format_tier(f) or 0)))
    raise NoSuitableFormat("This video has no audio stream.")


def available_tiers(formats: list[dict], cap: int = MAX_TIER) -> list[int]:
    """Tiers that actually exist for this video, best first."""
    tiers = {
        tier
        for fmt in formats
        if _usable(fmt) and has_video(fmt)
        for tier in [format_tier(fmt)]
        if tier is not None and tier <= cap
    }
    return sorted(tiers, reverse=True)


@dataclass
class Selection:
    video: dict
    audio: dict | None  # None when `video` is a single file that already has audio

    @property
    def tier(self) -> int | None:
        return format_tier(self.video)

    @property
    def needs_conversion(self) -> bool:
        """True if the result must be re-encoded for Mode.COMPATIBLE."""
        return not is_compatible(self.video.get("vcodec"))

    @property
    def estimated_size(self) -> int | None:
        total = 0
        for fmt in (self.video, self.audio):
            if fmt is None:
                continue
            size = fmt.get("filesize") or fmt.get("filesize_approx")
            if not size:
                return None
            total += size
        return total

    def as_yt_dlp_format(self) -> dict:
        """Shape the selection the way a yt-dlp `format` callable must yield it."""
        if self.audio is None:
            return self.video
        video, audio = self.video, self.audio
        merged = {
            "format_id": f"{video['format_id']}+{audio['format_id']}",
            "format": f"{video.get('format')} + {audio.get('format')}",
            "ext": "mp4",
            "requested_formats": [video, audio],
            "protocol": f"{video.get('protocol')}+{audio.get('protocol')}",
        }
        for key in ("width", "height", "resolution", "fps", "dynamic_range", "vcodec",
                    "vbr", "stretched_ratio", "aspect_ratio"):
            if video.get(key) is not None:
                merged[key] = video[key]
        for key in ("acodec", "abr", "asr", "audio_channels", "language"):
            if audio.get(key) is not None:
                merged[key] = audio[key]
        if video.get("tbr") and audio.get("tbr"):
            merged["tbr"] = video["tbr"] + audio["tbr"]
        return merged


class NoSuitableFormat(Exception):
    pass


def select(formats: list[dict], max_tier: int, mode: Mode) -> Selection:
    """Pick the best video (+ audio) at or below `max_tier`."""
    usable = [f for f in formats if _usable(f)]

    def best_at_or_below(candidates: list[dict]) -> list[dict]:
        ranked = [(format_tier(f), f) for f in candidates]
        ranked = [(t, f) for t, f in ranked if t is not None and t <= max_tier]
        if not ranked:
            # Nothing small enough: fall back to the lowest quality available.
            ranked = [(t, f) for t, f in ((format_tier(f), f) for f in candidates) if t is not None]
            if not ranked:
                return []
            target = min(t for t, _ in ranked)
        else:
            target = max(t for t, _ in ranked)
        return [f for t, f in ranked if t == target]

    video_only = [f for f in usable if is_video_only(f)]
    audio_only = [f for f in usable if is_audio_only(f)]

    if video_only and audio_only:
        video = max(best_at_or_below(video_only), key=lambda f: _video_sort_key(f, mode))
        audio = max(audio_only, key=_audio_sort_key)
        return Selection(video=video, audio=audio)

    # Sites (or old videos) that only offer files with audio and video combined.
    combined = [f for f in usable if has_video(f) and has_audio(f)]
    pool = combined or video_only
    candidates = best_at_or_below(pool)
    if not candidates:
        raise NoSuitableFormat("No downloadable video stream was found for this URL.")
    video = max(candidates, key=lambda f: _video_sort_key(f, mode))
    audio = None
    if not has_audio(video) and audio_only:
        audio = max(audio_only, key=_audio_sort_key)
    return Selection(video=video, audio=audio)
