"""AI video upscaling with Real-ESRGAN (runs on the Mac's GPU through Metal).

Frames flow through three processes with bounded disk use:

    ffmpeg (decode, PNG frames on a pipe)
      -> chunks of PNG files -> realesrgan-ncnn-vulkan (upscale a chunk)
      -> ffmpeg (scale to the exact target size, encode HEVC, copy audio)
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import IO, Callable

from . import tools

log = logging.getLogger(__name__)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHUNK_FRAMES = 96
UPSCALE_TARGETS = (1080, 1440, 2160)


class Content(str, Enum):
    REALISTIC = "realistic"  # camera footage: realesrgan-x4plus (slow, best detail)
    ANIMATION = "animation"  # cartoons/anime: realesr-animevideov3 (fast)


class UpscaleError(Exception):
    pass


@dataclass(frozen=True)
class VideoProbe:
    width: int
    height: int
    frame_rate: str  # rational, e.g. "30000/1001"
    duration: float | None
    audio_codec: str | None

    @property
    def fps(self) -> float:
        return float(Fraction(self.frame_rate))

    @property
    def estimated_frames(self) -> int | None:
        return math.ceil(self.duration * self.fps) if self.duration else None


@dataclass(frozen=True)
class Plan:
    model: str
    scale: int
    out_width: int
    out_height: int


def upscaler_paths() -> tuple[str, Path] | None:
    """(executable, models directory) of the bundled Real-ESRGAN, if present."""
    exe = tools.find_executable("realesrgan-ncnn-vulkan")
    if not exe:
        return None
    candidates = []
    bundled = tools.bundled_bin_dir()
    if bundled:
        candidates += [bundled.parent / "realesrgan-models", bundled / "models"]
    candidates.append(Path(exe).resolve().parent / "models")
    for models in candidates:
        if (models / "realesrgan-x4plus.param").exists():
            return exe, models
    return None


def is_available() -> bool:
    return upscaler_paths() is not None


def plan(width: int, height: int, target: int, content: Content) -> Plan:
    """Choose the model/scale and the final size for upscaling to `target`p."""
    short_side = min(width, height)
    if short_side >= target:
        raise UpscaleError(f"The video is already {short_side}p, so there is nothing to upscale to {target}p.")
    factor = target / short_side

    def even(value: float) -> int:
        return max(2, int(round(value / 2)) * 2)

    out_w, out_h = even(width * factor), even(height * factor)
    if content is Content.ANIMATION:
        scale = min(4, max(2, math.ceil(factor - 0.01)))
        return Plan("realesr-animevideov3", scale, out_w, out_h)
    return Plan("realesrgan-x4plus", 4, out_w, out_h)


def target_label(target: int) -> str:
    return {2160: "2160p (4K)", 1440: "1440p (2K)", 1080: "1080p (Full HD)"}.get(target, f"{target}p")


def probe(path: Path) -> VideoProbe:
    ffprobe = tools.discover().ffprobe
    if not ffprobe:
        raise UpscaleError("ffprobe was not found.")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        raise UpscaleError(f"Could not read the video: {out.stderr.strip() or out.returncode}")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video or not video.get("width"):
        raise UpscaleError("The file has no video stream.")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    def valid_rate(rate: str | None) -> bool:
        try:
            value = Fraction(rate) if rate else 0
        except (ValueError, ZeroDivisionError):
            return False
        return 0 < value <= 240

    rate = next((r for r in (video.get("avg_frame_rate"), video.get("r_frame_rate")) if valid_rate(r)), "30/1")
    duration = data.get("format", {}).get("duration")
    return VideoProbe(
        width=int(video["width"]),
        height=int(video["height"]),
        frame_rate=rate,
        duration=float(duration) if duration else None,
        audio_codec=audio.get("codec_name") if audio else None,
    )


def read_png(stream: IO[bytes]) -> bytes | None:
    """Read one PNG image from a stream of concatenated PNGs (None at EOF)."""
    signature = stream.read(8)
    if not signature:
        return None
    if signature != PNG_SIGNATURE:
        raise UpscaleError("Unexpected data while reading video frames.")
    parts = [signature]
    while True:
        header = _read_exact(stream, 8)
        length, chunk_type = struct.unpack(">I4s", header)
        parts += [header, _read_exact(stream, length + 4)]  # data + CRC
        if chunk_type == b"IEND":
            return b"".join(parts)


def _read_exact(stream: IO[bytes], size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        piece = stream.read(size - len(buf))
        if not piece:
            raise UpscaleError("The video frame stream ended unexpectedly.")
        buf += piece
    return bytes(buf)


ProgressFn = Callable[[float | None, str], None]


def upscale_video(
    source: Path,
    destination: Path,
    target: int,
    content: Content,
    video_encoder_args: list[str],
    on_progress: ProgressFn,
    cancel_event: threading.Event,
) -> None:
    """Upscale `source` to `target`p and write an MP4 to `destination`."""
    paths = upscaler_paths()
    if not paths:
        raise UpscaleError("The AI upscaler (Real-ESRGAN) is not installed.")
    exe, models = paths
    ffmpeg = tools.discover().ffmpeg
    if not ffmpeg:
        raise UpscaleError("ffmpeg was not found.")

    info = probe(source)
    p = plan(info.width, info.height, target, content)
    total = info.estimated_frames
    log.info("Upscaling %s (%dx%d @ %s) with %s x%d to %dx%d", source.name, info.width, info.height,
             info.frame_rate, p.model, p.scale, p.out_width, p.out_height)

    work = Path(tempfile.mkdtemp(prefix="ytdownloader-upscale-"))
    in_dir, out_dir = work / "in", work / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    partial = destination.with_name(destination.name + ".part.mp4")
    audio_args = (["-c:a", "copy"] if info.audio_codec == "aac"
                  else ["-c:a", "aac", "-b:a", "192k"])

    decoder_err = open(work / "decoder.log", "w+")
    encoder_err = open(work / "encoder.log", "w+")
    decoder = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(source),
         "-map", "0:v:0", "-vf", f"fps={info.frame_rate}",
         "-f", "image2pipe", "-c:v", "png", "-compression_level", "1", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=decoder_err,
    )
    encoder = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-y", "-loglevel", "error",
         "-f", "image2pipe", "-framerate", info.frame_rate, "-c:v", "png", "-i", "-",
         "-i", str(source),
         "-map", "0:v:0", "-map", "1:a:0?",
         "-vf", f"scale={p.out_width}:{p.out_height}:flags=lanczos,format=yuv420p",
         *video_encoder_args, *audio_args,
         "-movflags", "+faststart", str(partial)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=encoder_err,
    )
    processes = [decoder, encoder]
    started = time.monotonic()
    done = 0
    try:
        assert decoder.stdout is not None and encoder.stdin is not None
        on_progress(0.0 if total else None, "AI upscaling — loading model…")
        finished = False
        while not finished:
            batch = 0
            while batch < CHUNK_FRAMES:
                frame = read_png(decoder.stdout)
                if frame is None:
                    finished = True
                    break
                (in_dir / f"{done + batch:08d}.png").write_bytes(frame)
                batch += 1
            if cancel_event.is_set():
                raise _Cancelled()
            if batch == 0:
                break

            _run_realesrgan(exe, models, p, in_dir, out_dir, cancel_event, processes)

            for frame_path in sorted(out_dir.iterdir()):
                encoder.stdin.write(frame_path.read_bytes())
                frame_path.unlink()
            for frame_path in in_dir.iterdir():
                frame_path.unlink()
            done += batch

            elapsed = time.monotonic() - started
            per_frame = elapsed / done
            if total:
                fraction = min(done / total, 1.0)
                remaining = max(total - done, 0) * per_frame
                on_progress(fraction, f"AI upscaling — frame {done:,} of {total:,} · "
                                      f"{1 / per_frame:.1f} frames/s · {_hms(remaining)} left")
            else:
                on_progress(None, f"AI upscaling — {done:,} frames · {1 / per_frame:.1f} frames/s")

        encoder.stdin.close()
        on_progress(None, "Finishing video…")
        if encoder.wait() != 0:
            raise UpscaleError("Encoding the upscaled video failed: " + _last_line(encoder_err))
        if decoder.wait() != 0:
            raise UpscaleError("Reading the source video failed: " + _last_line(decoder_err))
        if done == 0:
            raise UpscaleError("The video contained no frames.")
        partial.replace(destination)
    except _Cancelled:
        raise UpscaleCancelled() from None
    except BrokenPipeError:
        if cancel_event.is_set():
            raise UpscaleCancelled() from None
        raise UpscaleError("Encoding the upscaled video failed: " + _last_line(encoder_err)) from None
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        decoder_err.close()
        encoder_err.close()
        partial.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


class _Cancelled(Exception):
    pass


class UpscaleCancelled(Exception):
    pass


def _run_realesrgan(exe: str, models: Path, p: Plan, in_dir: Path, out_dir: Path,
                    cancel_event: threading.Event, processes: list) -> None:
    cmd = [exe, "-i", str(in_dir), "-o", str(out_dir), "-n", p.model, "-s", str(p.scale),
           "-m", str(models), "-f", "png"]
    with tempfile.TemporaryFile(mode="w+") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err, cwd=str(models.parent))
        processes.append(proc)
        try:
            while proc.poll() is None:
                if cancel_event.is_set():
                    proc.terminate()
                    proc.wait()
                    raise _Cancelled()
                time.sleep(0.1)
        finally:
            processes.remove(proc)
        expected = len(list(in_dir.iterdir()))
        produced = len(list(out_dir.iterdir()))
        if proc.returncode != 0 or produced != expected:
            err.seek(0)
            lines = [line for line in err.read().splitlines()
                     if line.strip() and "%" not in line]
            detail = lines[-1] if lines else f"exit code {proc.returncode}"
            raise UpscaleError(
                f"The AI upscaler failed ({detail}). Your Mac's GPU may not support it.")


def _last_line(fh) -> str:
    fh.flush()
    fh.seek(0)
    lines = [line for line in fh.read().splitlines() if line.strip()]
    return lines[-1] if lines else "unknown error"


def _hms(seconds: float) -> str:
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
