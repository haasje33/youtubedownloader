"""AI video upscaling with Real-ESRGAN (runs on the Mac's GPU through Metal).

Frames flow through a pipeline with bounded disk use:

    ffmpeg decoder --PNG frames on a pipe--> producer thread
        writes large chunks of frames to disk, skipping exact repeats,
        and prepares the next chunk while the GPU works on the current one
    realesrgan-ncnn-vulkan, one process per chunk (the model loads once per chunk)
    main thread streams each finished frame into
    ffmpeg encoder (scale to the exact target size, encode HEVC, copy audio)
"""

from __future__ import annotations

import json
import logging
import math
import os
import platform
import queue
import shutil
import struct
import subprocess
import sys
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
PNG_TRAILER = b"IEND\xaeB`\x82"
# A chunk ends at whichever limit comes first; restarting Real-ESRGAN (reloading the
# model on the GPU) only happens once per chunk.
CHUNK_MAX_FRAMES = 1000
CHUNK_MAX_BYTES = 250 * 1024 * 1024
UPSCALE_TARGETS = (1080, 1440, 2160)


class Content(str, Enum):
    REALISTIC = "realistic"  # camera footage, best detail: realesrgan-x4plus (slow)
    FAST = "fast"            # camera footage, faster: realesr-general-x4v3
    ANIMATION = "animation"  # cartoons/anime: realesr-animevideov3 (fast)


# Real-ESRGAN tuning: tile size (-t) and load:process:save threads (-j), from
# scripts/bench_upscale.py on an Apple Silicon Mac (640x360 frames, x4):
#   model                  auto tile   tile 512
#   realesrgan-x4plus        0.31 f/s   0.37 f/s
#   realesr-general-x4v3     5.3  f/s   6.3  f/s
#   realesr-animevideov3     9.4  f/s  11.7  f/s
# Apple Silicon shares memory between CPU and GPU, so large tiles are safe there.
# Intel Macs keep the automatic choice, which is sized to the GPU's own memory.
# Override with YTD_UPSCALE_TILE / YTD_UPSCALE_THREADS.
APPLE_SILICON = platform.system() == "Darwin" and platform.machine() == "arm64"
DEFAULT_TILE = "512" if APPLE_SILICON else "0"
DEFAULT_THREADS = "2:2:2"


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


def preferred_engine() -> str:
    """"cuda" (NVIDIA, Windows), "ncnn" (Real-ESRGAN on Vulkan/Metal) or "onnx-cpu" (tests)."""
    forced = os.environ.get("YTD_UPSCALE_ENGINE")
    if forced in ("ncnn", "cuda", "onnx-cpu"):
        return forced
    if tools.IS_WINDOWS:
        from . import onnx_engine

        if onnx_engine.cuda_status().available:
            return "cuda"
    return "ncnn"


def engine_description() -> str:
    """Human-readable name of the engine that will be used (may take a second on Windows)."""
    engine = preferred_engine()
    if engine in ("cuda", "onnx-cpu"):
        from . import onnx_engine

        return onnx_engine.cuda_status().detail
    if upscaler_paths() is None:
        return "not available"
    return "Real-ESRGAN on the GPU (Metal)" if sys.platform == "darwin" else "Real-ESRGAN on the GPU (Vulkan)"


def is_available() -> bool:
    if upscaler_paths() is not None:
        return True
    if tools.IS_WINDOWS or os.environ.get("YTD_UPSCALE_ENGINE") == "onnx-cpu":
        from . import onnx_engine

        return onnx_engine.models_dir() is not None
    return False


def plan(width: int, height: int, target: int, content: Content, engine: str = "ncnn") -> Plan:
    """Choose the model/scale and the final size for upscaling to `target`p."""
    short_side = min(width, height)
    if short_side >= target:
        raise UpscaleError(f"The video is already {short_side}p, so there is nothing to upscale to {target}p.")
    factor = target / short_side

    def even(value: float) -> int:
        return max(2, int(round(value / 2)) * 2)

    out_w, out_h = even(width * factor), even(height * factor)
    if engine != "ncnn":  # ONNX models: x4 versions, plus a native x2 model for real-life video
        if content is Content.ANIMATION:
            return Plan("realesr-animevideov3", 4, out_w, out_h)
        if content is Content.FAST:
            return Plan("realesr-general-x4v3", 4, out_w, out_h)
        if factor <= 2:
            return Plan("realesrgan-x2plus", 2, out_w, out_h)
        return Plan("realesrgan-x4plus", 4, out_w, out_h)
    if content is Content.ANIMATION:
        scale = min(4, max(2, math.ceil(factor - 0.01)))
        return Plan("realesr-animevideov3", scale, out_w, out_h)
    if content is Content.FAST:
        return Plan("realesr-general-x4v3", 4, out_w, out_h)
    return Plan("realesrgan-x4plus", 4, out_w, out_h)


def realesrgan_tuning(model: str) -> list[str]:
    tile = os.environ.get("YTD_UPSCALE_TILE") or DEFAULT_TILE
    threads = os.environ.get("YTD_UPSCALE_THREADS") or DEFAULT_THREADS
    return ["-t", tile, "-j", threads]


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
        capture_output=True, text=True, check=False, **tools.no_window(),
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


class UpscaleCancelled(Exception):
    pass


class _Stop(Exception):
    pass


def _png_complete(path: Path) -> bool:
    """True once Real-ESRGAN has finished writing `path`."""
    try:
        with open(path, "rb") as fh:
            fh.seek(-len(PNG_TRAILER), os.SEEK_END)
            return fh.read() == PNG_TRAILER
    except OSError:  # missing, or still shorter than the trailer
        return False


class _FrameProducer(threading.Thread):
    """Reads decoded frames and groups them into chunk directories.

    Exact repeats of the previous frame (still images, paused scenes, frames that
    ffmpeg duplicated for a constant frame rate) are not written: the consumer
    reuses the previous upscaled frame, which gives an identical result.
    Each queued item is (directory, order) where `order` lists, for every output
    frame, the number of the unique frame to show.
    """

    def __init__(self, stream: IO[bytes], work: Path, stop: threading.Event):
        super().__init__(daemon=True)
        self.stream = stream
        self.work = work
        self.stop_event = stop
        self.chunks: queue.Queue = queue.Queue(maxsize=1)  # at most one chunk prepared ahead
        self.unique = 0
        self.repeats = 0

    def run(self) -> None:
        try:
            previous = None
            index = 0
            finished = False
            while not finished:
                directory = self.work / f"chunk{index:05d}"
                directory.mkdir()
                order: list[int] = []
                written = size = 0
                while written < CHUNK_MAX_FRAMES and size < CHUNK_MAX_BYTES:
                    if self.stop_event.is_set():
                        return
                    frame = read_png(self.stream)
                    if frame is None:
                        finished = True
                        break
                    if frame == previous:
                        order.append(self.unique - 1)
                        self.repeats += 1
                        continue
                    (directory / f"{self.unique:08d}.png").write_bytes(frame)
                    order.append(self.unique)
                    self.unique += 1
                    written += 1
                    size += len(frame)
                    previous = frame
                if order:
                    self._put((directory, order))
                index += 1
            self._put(None)
        except _Stop:
            pass
        except Exception as exc:  # noqa: BLE001 - handed to the consumer
            try:
                self._put(exc)
            except _Stop:
                pass

    def _put(self, item) -> None:
        while not self.stop_event.is_set():
            try:
                self.chunks.put(item, timeout=0.2)
                return
            except queue.Full:
                continue
        raise _Stop()


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
    engine = preferred_engine()
    info = probe(source)
    p = plan(info.width, info.height, target, content, engine)
    if engine in ("cuda", "onnx-cpu"):
        from . import onnx_engine

        try:
            onnx_engine.upscale_video(source, destination, info, p, video_encoder_args, on_progress,
                                      cancel_event, use_cuda=engine == "cuda")
        except onnx_engine.UpscaleCancelled:
            raise UpscaleCancelled() from None
        return
    _upscale_video_ncnn(source, destination, info, p, video_encoder_args, on_progress, cancel_event)


def _upscale_video_ncnn(source: Path, destination: Path, info: VideoProbe, p: Plan,
                        video_encoder_args: list[str], on_progress: ProgressFn,
                        cancel_event: threading.Event) -> None:
    paths = upscaler_paths()
    if not paths:
        raise UpscaleError("The AI upscaler (Real-ESRGAN) is not installed.")
    exe, models = paths
    ffmpeg = tools.discover().ffmpeg
    if not ffmpeg:
        raise UpscaleError("ffmpeg was not found.")
    if not (models / f"{p.model}.param").exists() and not (models / f"{p.model}-x{p.scale}.param").exists():
        raise UpscaleError(f"The AI model {p.model} is missing from this build.")
    total = info.estimated_frames
    log.info("Upscaling %s (%dx%d @ %s) with %s x%d to %dx%d", source.name, info.width, info.height,
             info.frame_rate, p.model, p.scale, p.out_width, p.out_height)

    work = Path(tempfile.mkdtemp(prefix="ytdownloader-upscale-"))
    out_dir = work / "out"
    out_dir.mkdir()
    partial = destination.with_name(destination.name + ".part.mp4")
    audio_args = (["-c:a", "copy"] if info.audio_codec == "aac"
                  else ["-c:a", "aac", "-b:a", "192k"])

    decoder_err = open(work / "decoder.log", "w+")
    encoder_err = open(work / "encoder.log", "w+")
    esrgan_err = open(work / "realesrgan.log", "w+")
    decoder = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(source),
         "-map", "0:v:0", "-vf", f"fps={info.frame_rate}",
         "-f", "image2pipe", "-c:v", "png", "-compression_level", "1", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=decoder_err, **tools.no_window(),
    )
    encoder = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-y", "-loglevel", "error",
         "-f", "image2pipe", "-framerate", info.frame_rate, "-c:v", "png", "-i", "-",
         "-i", str(source),
         "-map", "0:v:0", "-map", "1:a:0?",
         "-vf", f"scale={p.out_width}:{p.out_height}:flags=lanczos,format=yuv420p",
         *video_encoder_args, *audio_args,
         "-movflags", "+faststart", str(partial)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=encoder_err, **tools.no_window(),
    )
    processes: list[subprocess.Popen] = [decoder, encoder]
    stop = threading.Event()
    assert decoder.stdout is not None and encoder.stdin is not None
    producer = _FrameProducer(decoder.stdout, work, stop)
    producer.start()

    started = time.monotonic()
    first_frame_at: float | None = None
    last_report = 0.0
    done = 0
    last_unique, last_frame = -1, b""

    def report(force: bool = False) -> None:
        nonlocal last_report
        now = time.monotonic()
        if not force and now - last_report < 0.5:
            return
        last_report = now
        if first_frame_at is None or done == 0:
            on_progress(0.0 if total else None,
                        f"AI upscaling — preparing the AI model on the GPU… ({_hms(now - started)})")
            return
        measured = now - first_frame_at
        if measured < 2.0 or done < 2:
            # Too early for a meaningful speed (frames finished together would show nonsense).
            on_progress(min(done / total, 1.0) if total else None,
                        f"AI upscaling — frame {done:,}" + (f" of {total:,}" if total else "")
                        + " · measuring speed…")
            return
        rate = (done - 1) / measured  # frames finished since the first one appeared
        if total:
            remaining = max(total - done, 0) / rate
            on_progress(min(done / total, 1.0),
                        f"AI upscaling — frame {done:,} of {total:,} · {rate:.1f} frames/s · "
                        f"{_hms(remaining)} left")
        else:
            on_progress(None, f"AI upscaling — {done:,} frames · {rate:.1f} frames/s")

    def check_cancel() -> None:
        if cancel_event.is_set():
            raise UpscaleCancelled()

    try:
        report(force=True)
        while True:
            try:
                item = producer.chunks.get(timeout=0.2)
            except queue.Empty:
                check_cancel()
                report()
                continue
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            chunk_dir, order = item

            esrgan = None
            if any(chunk_dir.iterdir()):
                esrgan_err.seek(0)
                esrgan_err.truncate()
                esrgan = subprocess.Popen(
                    [exe, "-i", str(chunk_dir), "-o", str(out_dir), "-n", p.model, "-s", str(p.scale),
                     "-m", str(models), "-f", "png", *realesrgan_tuning(p.model)],
                    stdout=subprocess.DEVNULL, stderr=esrgan_err, cwd=str(models.parent),
                    **tools.no_window(),
                )
                processes.append(esrgan)

            for unique in order:
                if unique == last_unique:
                    frame = last_frame  # exact repeat of the previous frame
                else:
                    path = out_dir / f"{unique:08d}.png"
                    while not _png_complete(path):
                        check_cancel()
                        if esrgan is None or esrgan.poll() is not None:
                            if _png_complete(path):
                                break
                            raise UpscaleError(
                                "The AI upscaler stopped unexpectedly ("
                                + _last_line(esrgan_err, skip_progress=True)
                                + "). Your Mac's GPU may not support it.")
                        report()
                        time.sleep(0.05)
                    frame = path.read_bytes()
                    path.unlink()
                    last_unique, last_frame = unique, frame
                    if first_frame_at is None:
                        first_frame_at = time.monotonic()
                encoder.stdin.write(frame)
                done += 1
                check_cancel()
                report()

            if esrgan is not None:
                esrgan.wait()
                processes.remove(esrgan)
            shutil.rmtree(chunk_dir, ignore_errors=True)

        encoder.stdin.close()
        on_progress(None, "Finishing video…")
        if encoder.wait() != 0:
            raise UpscaleError("Encoding the upscaled video failed: " + _last_line(encoder_err))
        if decoder.wait() != 0:
            raise UpscaleError("Reading the source video failed: " + _last_line(decoder_err))
        if done == 0:
            raise UpscaleError("The video contained no frames.")
        log.info("Upscaled %d frames (%d unique, %d repeats skipped) in %.0f s", done,
                 producer.unique, producer.repeats, time.monotonic() - started)
        partial.replace(destination)
    except BrokenPipeError:
        if cancel_event.is_set():
            raise UpscaleCancelled() from None
        raise UpscaleError("Encoding the upscaled video failed: " + _last_line(encoder_err)) from None
    finally:
        stop.set()
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        producer.join(timeout=5)
        for fh in (decoder_err, encoder_err, esrgan_err):
            fh.close()
        partial.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


def _last_line(fh, skip_progress: bool = False) -> str:
    fh.flush()
    fh.seek(0)
    lines = [line for line in fh.read().splitlines() if line.strip()]
    if skip_progress:
        lines = [line for line in lines if not line.strip().endswith("%")]
    return lines[-1] if lines else "unknown error"


def _hms(seconds: float) -> str:
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
