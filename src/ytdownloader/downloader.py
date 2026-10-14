"""Fetching video information, downloading, and converting to MP4."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yt_dlp
from yt_dlp.utils import DownloadCancelled

from . import formats, tools, upscale
from .formats import Mode, Selection
from .upscale import Content

log = logging.getLogger(__name__)

SUPPORTED_BROWSERS = ("safari", "chrome", "firefox", "brave", "edge", "chromium", "opera", "vivaldi")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class Cancelled(DownloadCancelled):
    """Raised when the user cancels a download."""

    def __init__(self):
        super().__init__("Download cancelled")


class DownloadFailed(Exception):
    pass


@dataclass
class Progress:
    stage: str  # "download", "merge", "convert", "upscale", "finalize"
    fraction: float | None  # 0..1, or None when indeterminate
    detail: str = ""


ProgressCallback = Callable[[Progress], None]


@dataclass
class VideoInfo:
    url: str
    id: str
    title: str
    uploader: str | None
    duration: float | None
    formats: list[dict] = field(repr=False)
    tiers: list[int] = field(default_factory=list)

    def selection_for(self, tier: int, mode: Mode) -> Selection:
        return formats.select(self.formats, tier, mode)


class _YtdlLogger:
    def debug(self, msg: str) -> None:
        # yt-dlp routes both debug and regular info messages through here.
        log.debug(_clean(msg))

    def info(self, msg: str) -> None:
        log.info(_clean(msg))

    def warning(self, msg: str) -> None:
        log.warning(_clean(msg))

    def error(self, msg: str) -> None:
        log.error(_clean(msg))


def _clean(msg: str) -> str:
    msg = _ANSI_RE.sub("", str(msg)).strip()
    return re.sub(r"^ERROR:\s*", "", msg)


def friendly_error(exc: BaseException) -> str:
    text = _clean(str(exc))
    text = re.sub(r"^\[[\w:]+\]\s*[\w-]+:\s*", "", text)  # "[youtube] abc123: ..." prefix
    if "Sign in to confirm" in text:
        text += (
            "\n\nYouTube is asking for a signed-in session. Choose your browser under "
            "\"Use cookies from\" (and make sure you are logged in to YouTube there), then try again."
        )
    return text or exc.__class__.__name__


def format_bytes(num: float | None) -> str:
    if not num:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit in ("B", "KB") else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def base_params(cookies_from_browser: str | None = None) -> dict:
    toolset = tools.discover()
    params: dict = {
        "logger": _YtdlLogger(),
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "color": {"stdout": "no_color", "stderr": "no_color"},
    }
    if toolset.ffmpeg:
        params["ffmpeg_location"] = str(Path(toolset.ffmpeg).parent)
    runtimes = {}
    if toolset.deno:
        runtimes["deno"] = {"path": toolset.deno}
    node = tools.find_executable("node")
    if node:
        runtimes["node"] = {"path": node}
    if runtimes:
        params["js_runtimes"] = runtimes
    if cookies_from_browser:
        params["cookiesfrombrowser"] = (cookies_from_browser,)
    return params


def fetch_info(url: str, cookies_from_browser: str | None = None) -> VideoInfo:
    url = url.strip()
    if not url:
        raise DownloadFailed("Please paste a YouTube link first.")
    try:
        with yt_dlp.YoutubeDL(base_params(cookies_from_browser)) as ydl:
            info = ydl.extract_info(url, download=False, process=True)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadFailed(friendly_error(exc)) from exc

    if info.get("_type") == "playlist" or "entries" in info:
        raise DownloadFailed(
            "That link is a playlist. Open a single video and paste its link instead."
        )

    fmts = info.get("formats") or [info]
    tiers = formats.available_tiers(fmts)
    if not tiers:
        raise DownloadFailed("No video streams were found for this link.")
    return VideoInfo(
        url=info.get("webpage_url") or url,
        id=info.get("id", ""),
        title=info.get("title") or "video",
        uploader=info.get("uploader") or info.get("channel"),
        duration=info.get("duration"),
        formats=fmts,
        tiers=tiers,
    )


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 10_000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise DownloadFailed(f"Could not find a free file name for {path.name}")


def _require_ffmpeg() -> None:
    if not tools.discover().ffmpeg:
        raise DownloadFailed(
            "ffmpeg was not found. It is bundled with the app; when running from source, "
            "install it with `brew install ffmpeg`."
        )


def _run_ytdlp(url: str, params: dict, cancel_event: threading.Event) -> dict:
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            return ydl.extract_info(url, download=True)
    except DownloadCancelled:
        raise Cancelled() from None
    except yt_dlp.utils.DownloadError as exc:
        if cancel_event.is_set():
            raise Cancelled() from None
        raise DownloadFailed(friendly_error(exc)) from exc


def download(
    url: str,
    tier: int,
    mode: Mode,
    output_dir: Path,
    on_progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    cookies_from_browser: str | None = None,
    upscale_to: int | None = None,
    upscale_content: Content = Content.REALISTIC,
) -> Path:
    """Download `url` as an MP4 at (at most) `tier`p and return the final file path.

    With `upscale_to`, a video below that resolution is AI-upscaled afterwards.
    """
    on_progress = on_progress or (lambda p: None)
    cancel_event = cancel_event or threading.Event()
    _require_ffmpeg()

    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="ytdownloader-"))
    chosen: dict[str, Selection] = {}

    def format_selector(ctx):
        selection = formats.select(ctx["formats"], tier, mode)
        chosen["selection"] = selection
        log.info(
            "Selected video %s (%s, %sx%s) + audio %s",
            selection.video.get("format_id"), selection.video.get("vcodec"),
            selection.video.get("width"), selection.video.get("height"),
            selection.audio.get("format_id") if selection.audio else "-",
        )
        yield selection.as_yt_dlp_format()

    tracker = _DownloadTracker(on_progress, cancel_event, chosen)

    def postprocessor_hook(d):
        if cancel_event.is_set():
            raise Cancelled()
        if d.get("status") == "started" and d.get("postprocessor") == "Merger":
            on_progress(Progress("merge", None, "Combining video and audio…"))

    params = base_params(cookies_from_browser) | {
        "format": format_selector,
        "paths": {"home": str(work_dir), "temp": str(work_dir)},
        "outtmpl": {"default": "%(title).150B.%(ext)s"},
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        "progress_hooks": [tracker.hook],
        "postprocessor_hooks": [postprocessor_hook],
        "concurrent_fragment_downloads": 4,
        "overwrites": True,
    }

    try:
        on_progress(Progress("download", None, "Starting download…"))
        info = _run_ytdlp(url, params, cancel_event)

        downloaded = _downloaded_file(info, work_dir)
        selection = chosen.get("selection")
        tier_used = (selection.tier if selection else None) or tier
        title = Path(downloaded).stem

        if upscale_to and tier_used < upscale_to:
            destination = unique_path(output_dir / f"{title} ({upscale_to}p AI upscaled).mp4")
            _upscale(downloaded, destination, upscale_to, upscale_content, on_progress, cancel_event)
            return destination

        destination = unique_path(output_dir / f"{title} ({tier_used}p).mp4")
        if mode is Mode.COMPATIBLE and selection and selection.needs_conversion:
            convert_to_hevc(
                downloaded, destination,
                duration=info.get("duration"),
                tier=tier_used,
                fps=selection.video.get("fps"),
                copy_audio=_audio_is_aac(selection),
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
        else:
            on_progress(Progress("finalize", None, "Saving…"))
            shutil.move(str(downloaded), destination)
        return destination
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def download_audio(
    url: str,
    output_dir: Path,
    on_progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    cookies_from_browser: str | None = None,
) -> Path:
    """Download only the audio as a high-quality MP3 with title and cover art."""
    on_progress = on_progress or (lambda p: None)
    cancel_event = cancel_event or threading.Event()
    _require_ffmpeg()

    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="ytdownloader-"))
    chosen: dict[str, Selection] = {}

    def format_selector(ctx):
        audio = formats.select_audio(ctx["formats"])
        chosen["selection"] = Selection(video=audio, audio=None)
        log.info("Selected audio %s (%s)", audio.get("format_id"), audio.get("acodec"))
        yield audio

    tracker = _DownloadTracker(on_progress, cancel_event, chosen, label="Downloading audio")

    def postprocessor_hook(d):
        if cancel_event.is_set():
            raise Cancelled()
        if d.get("status") == "started" and d.get("postprocessor") == "ExtractAudio":
            on_progress(Progress("convert", None, "Converting to MP3…"))

    params = base_params(cookies_from_browser) | {
        "format": format_selector,
        "paths": {"home": str(work_dir), "temp": str(work_dir)},
        "outtmpl": {"default": "%(title).150B.%(ext)s"},
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        ],
        "progress_hooks": [tracker.hook],
        "postprocessor_hooks": [postprocessor_hook],
        "overwrites": True,
    }

    try:
        on_progress(Progress("download", None, "Starting download…"))
        info = _run_ytdlp(url, params, cancel_event)
        mp3 = next((Path(d["filepath"]) for d in info.get("requested_downloads") or []
                    if d.get("filepath", "").endswith(".mp3") and Path(d["filepath"]).exists()), None)
        mp3 = mp3 or next(iter(work_dir.glob("*.mp3")), None)
        if not mp3:
            raise DownloadFailed("The MP3 file could not be created.")
        destination = unique_path(output_dir / mp3.name)
        shutil.move(str(mp3), destination)
        return destination
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def upscale_file(
    source: Path,
    output_dir: Path,
    target: int,
    content: Content,
    on_progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """AI-upscale a video file that is already on disk."""
    on_progress = on_progress or (lambda p: None)
    cancel_event = cancel_event or threading.Event()
    _require_ffmpeg()
    source = Path(source).expanduser()
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = unique_path(output_dir / f"{source.stem} ({target}p AI upscaled).mp4")
    _upscale(source, destination, target, content, on_progress, cancel_event)
    return destination


def _upscale(source: Path, destination: Path, target: int, content: Content,
             on_progress: ProgressCallback, cancel_event: threading.Event) -> None:
    ffmpeg = tools.discover().ffmpeg
    encoders = encoder_attempts(tools.ffmpeg_encoders(ffmpeg), target, None) if ffmpeg else []
    if not encoders:
        raise DownloadFailed("This ffmpeg build has no H.264/HEVC encoder.")
    try:
        upscale.upscale_video(
            source, destination, target, content,
            video_encoder_args=encoders[0],
            on_progress=lambda fraction, detail: on_progress(Progress("upscale", fraction, detail)),
            cancel_event=cancel_event,
        )
    except upscale.UpscaleCancelled:
        raise Cancelled() from None
    except upscale.UpscaleError as exc:
        raise DownloadFailed(str(exc)) from exc


def _downloaded_file(info: dict, work_dir: Path) -> Path:
    for item in info.get("requested_downloads") or []:
        path = item.get("filepath") or item.get("_filename")
        if path and Path(path).exists():
            return Path(path)
    candidates = sorted(work_dir.glob("*.mp4"), key=lambda p: p.stat().st_size, reverse=True)
    if candidates:
        return candidates[0]
    raise DownloadFailed("The download finished but the output file could not be found.")


def _audio_is_aac(selection: Selection) -> bool:
    audio = selection.audio or selection.video
    return (audio.get("acodec") or "").lower().startswith("mp4a")


class _DownloadTracker:
    """Turns yt-dlp's per-stream progress into one overall percentage."""

    def __init__(self, on_progress: ProgressCallback, cancel_event: threading.Event,
                 chosen: dict[str, Selection], label: str = "Downloading"):
        self.on_progress = on_progress
        self.label = label
        self.cancel_event = cancel_event
        self.chosen = chosen
        self.done: dict[str, float] = {}  # format_id -> weight already completed

    def _weights(self) -> dict[str, float]:
        selection = self.chosen.get("selection")
        if not selection:
            return {}
        parts = [f for f in (selection.video, selection.audio) if f]
        sizes = [f.get("filesize") or f.get("filesize_approx") for f in parts]
        if all(sizes):
            total = sum(sizes)
            return {str(f["format_id"]): s / total for f, s in zip(parts, sizes)}
        if len(parts) == 2:
            return {str(parts[0]["format_id"]): 0.9, str(parts[1]["format_id"]): 0.1}
        return {str(parts[0]["format_id"]): 1.0}

    def hook(self, d: dict) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()
        status = d.get("status")
        format_id = str((d.get("info_dict") or {}).get("format_id", ""))
        weights = self._weights()
        weight = weights.get(format_id, 1.0 if not weights else 0.0)

        if status == "finished":
            self.done[format_id] = weight
            return
        if status != "downloading":
            return

        downloaded = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        stream_fraction = min(downloaded / total, 1.0) if total else None
        base = sum(w for fid, w in self.done.items() if fid != format_id)
        overall = None if stream_fraction is None else min(base + weight * stream_fraction, 1.0)

        pieces = []
        if total:
            pieces.append(f"{format_bytes(downloaded)} of {format_bytes(total)}")
        else:
            pieces.append(format_bytes(downloaded))
        if d.get("speed"):
            pieces.append(f"{format_bytes(d['speed'])}/s")
        if d.get("eta") is not None:
            pieces.append(f"{format_duration(d['eta'])} left")
        selection = self.chosen.get("selection")
        if selection and selection.audio is not None:
            is_audio = format_id == str(selection.audio.get("format_id"))
            label = "Downloading audio" if is_audio else "Downloading video"
        else:
            label = self.label
        self.on_progress(Progress("download", overall, f"{label} — " + " · ".join(pieces)))


# --- Conversion -----------------------------------------------------------

_BITRATES = {2160: 40_000, 1440: 18_000, 1080: 9_000, 720: 5_000, 480: 2_500}


def hevc_bitrate_kbps(tier: int, fps: float | None) -> int:
    kbps = _BITRATES.get(tier, 1_500)
    if fps and fps > 30:
        kbps = int(kbps * 1.4)
    return kbps


def encoder_attempts(available: frozenset[str], tier: int, fps: float | None) -> list[list[str]]:
    """Video encoder argument lists to try, most preferred first."""
    attempts = []
    if "hevc_videotoolbox" in available:
        kbps = hevc_bitrate_kbps(tier, fps)
        attempts.append([
            "-c:v", "hevc_videotoolbox", "-b:v", f"{kbps}k",
            "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k",
            "-allow_sw", "1", "-tag:v", "hvc1",
        ])
    if "libx265" in available:
        attempts.append(["-c:v", "libx265", "-preset", "fast", "-crf", "22", "-tag:v", "hvc1"])
    if "libx264" in available:
        attempts.append(["-c:v", "libx264", "-preset", "fast", "-crf", "20"])
    if "h264_videotoolbox" in available:
        kbps = hevc_bitrate_kbps(tier, fps) * 3 // 2
        attempts.append(["-c:v", "h264_videotoolbox", "-b:v", f"{kbps}k", "-allow_sw", "1"])
    return attempts


def convert_to_hevc(
    source: Path,
    destination: Path,
    duration: float | None,
    tier: int,
    fps: float | None,
    copy_audio: bool,
    on_progress: ProgressCallback,
    cancel_event: threading.Event,
) -> None:
    """Re-encode `source` to an MP4 that plays natively on Apple devices."""
    ffmpeg = tools.discover().ffmpeg
    if not ffmpeg:
        raise DownloadFailed("ffmpeg is required for conversion but was not found.")
    attempts = encoder_attempts(tools.ffmpeg_encoders(ffmpeg), tier, fps)
    if not attempts:
        raise DownloadFailed("This ffmpeg build has no H.264/HEVC encoder.")

    audio_args = ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", "192k"]
    errors = []
    for video_args in attempts:
        encoder = video_args[1]
        partial = destination.with_name(destination.name + ".part.mp4")
        cmd = [
            ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
            "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?",
            *video_args, "-pix_fmt", "yuv420p",
            *audio_args,
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats",
            str(partial),
        ]
        log.info("Converting with %s: %s", encoder, " ".join(cmd))
        on_progress(Progress("convert", 0.0, "Converting for QuickTime compatibility…"))
        ok, error = _run_ffmpeg(cmd, duration, on_progress, cancel_event)
        if ok:
            partial.replace(destination)
            return
        partial.unlink(missing_ok=True)
        if cancel_event.is_set():
            raise Cancelled()
        log.warning("Encoder %s failed: %s", encoder, error)
        errors.append(f"{encoder}: {error}")
    raise DownloadFailed("Converting the video failed.\n" + "\n".join(errors))


def _run_ffmpeg(cmd: list[str], duration: float | None, on_progress: ProgressCallback,
                cancel_event: threading.Event) -> tuple[bool, str]:
    with tempfile.TemporaryFile(mode="w+") as stderr:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr, text=True)
        speed = None
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if cancel_event.is_set():
                    proc.terminate()
                    break
                key, _, value = line.strip().partition("=")
                if key == "speed" and value.endswith("x"):
                    try:
                        speed = float(value[:-1])
                    except ValueError:
                        speed = None
                elif key == "out_time_us" and duration:
                    try:
                        seconds = int(value) / 1_000_000
                    except ValueError:
                        continue
                    fraction = max(0.0, min(seconds / duration, 1.0))
                    detail = f"Converting for QuickTime — {fraction * 100:.0f}%"
                    if speed:
                        remaining = (duration - seconds) / speed
                        detail += f" · {speed:.1f}× · {format_duration(remaining)} left"
                    on_progress(Progress("convert", fraction, detail))
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        stderr.seek(0)
        error = stderr.read().strip().splitlines()
    return proc.returncode == 0 and not cancel_event.is_set(), (error[-1] if error else f"exit code {proc.returncode}")
