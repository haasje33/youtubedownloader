"""AI upscaling with ONNX Runtime on NVIDIA GPUs (CUDA + cuDNN, float16 tensor cores).

Used by the Windows app when an NVIDIA GPU is available. Unlike the Real-ESRGAN
(Vulkan) engine, frames never touch the disk:

    ffmpeg decoder --raw RGB frames--> reader thread --> GPU (ONNX model) -->
    writer thread --raw RGB frames--> ffmpeg encoder (NVENC)

The models (scripts/convert_to_onnx.py) take uint8 frames and return uint8
frames, so all of the arithmetic runs on the GPU.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import tools

log = logging.getLogger(__name__)

MODEL_FILES = {
    "realesrgan-x4plus": "realesrgan-x4plus.onnx",
    "realesrgan-x2plus": "realesrgan-x2plus.onnx",
    "realesr-general-x4v3": "realesr-general-x4v3.onnx",
    "realesr-animevideov3": "realesr-animevideov3.onnx",
}
PRE_PAD = 10            # reflection padding around each frame, as in Real-ESRGAN
TILE = 512              # tile size (input pixels) for large frames
TILE_PAD = 32           # overlap around each tile; RRDBNet sees far, so be generous
TILE_THRESHOLD = 960 * 540


@dataclass(frozen=True)
class EngineStatus:
    available: bool
    detail: str
    gpu: str | None = None


def models_dir() -> Path | None:
    candidates = []
    if os.environ.get("YTD_ONNX_MODELS"):
        candidates.append(Path(os.environ["YTD_ONNX_MODELS"]))
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "onnx-models")
    candidates.append(Path(__file__).resolve().parents[2] / "vendor" / "onnx-models")
    for directory in candidates:
        if directory.is_dir() and any(directory.glob("*.onnx")):
            return directory
    return None


def _prepare_dll_search() -> None:
    """Let Windows find the bundled CUDA/cuDNN DLLs (they sit next to ONNX Runtime)."""
    if not tools.IS_WINDOWS:
        return
    base = getattr(sys, "_MEIPASS", None)
    dirs = [Path(base) / "onnxruntime" / "capi"] if base else []
    for directory in dirs:
        if directory.is_dir():
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(str(directory))
            except (AttributeError, OSError):
                pass


def gpu_name() -> str | None:
    smi = tools.find_executable("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run([smi, "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=15, check=False,
                             **tools.no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    name = out.stdout.strip().splitlines()[0].strip() if out.returncode == 0 and out.stdout.strip() else None
    return name or None


def _cuda_provider_options(nhwc: bool) -> dict:
    options = {
        "device_id": "0",
        "cudnn_conv_algo_search": "EXHAUSTIVE",
        "cudnn_conv_use_max_workspace": "1",
        "do_copy_in_default_stream": "1",
    }
    if nhwc:
        options["prefer_nhwc"] = "1"  # channels-last convolutions run faster on tensor cores
    return options


def create_session(model_path: Path, use_cuda: bool):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.log_severity_level = 3
    if not use_cuda:
        return ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
    last_error: Exception | None = None
    for nhwc in (True, False):
        try:
            session = ort.InferenceSession(
                str(model_path), options,
                providers=[("CUDAExecutionProvider", _cuda_provider_options(nhwc)), "CPUExecutionProvider"])
        except Exception as exc:  # noqa: BLE001 - unknown option on older builds, etc.
            last_error = exc
            continue
        if session.get_providers()[0] != "CUDAExecutionProvider":
            raise RuntimeError("CUDA could not be initialised (is the NVIDIA driver up to date?)")
        return session
    raise RuntimeError(f"Could not start the CUDA engine: {last_error}")


@lru_cache(maxsize=1)
def cuda_status() -> EngineStatus:
    """Whether the NVIDIA engine can run on this computer (checked once, ~1-3 s)."""
    if os.environ.get("YTD_UPSCALE_ENGINE") == "onnx-cpu":
        return EngineStatus(True, "ONNX Runtime on the CPU (testing only)")
    directory = models_dir()
    if directory is None:
        return EngineStatus(False, "The NVIDIA models are not included in this build.")
    _prepare_dll_search()
    try:
        import onnxruntime as ort
    except ImportError:
        return EngineStatus(False, "ONNX Runtime is not included in this build.")
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        return EngineStatus(False, "This build of ONNX Runtime has no CUDA support.")
    if not getattr(sys, "frozen", False) and hasattr(ort, "preload_dlls"):
        try:
            ort.preload_dlls()  # CUDA/cuDNN from pip's nvidia-* packages when running from source
        except Exception:  # noqa: BLE001
            pass
    gpu = gpu_name()
    if gpu is None and tools.IS_WINDOWS:
        return EngineStatus(False, "No NVIDIA GPU (or driver) was found.")
    smallest = min(directory.glob("*.onnx"), key=lambda p: p.stat().st_size)
    try:
        create_session(smallest, use_cuda=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("CUDA engine unavailable: %s", exc)
        return EngineStatus(False, f"CUDA is not usable: {exc}", gpu)
    return EngineStatus(True, f"{gpu or 'NVIDIA GPU'} (CUDA, tensor cores)", gpu)


class OnnxUpscaler:
    """Upscales single RGB frames (H, W, 3 uint8) with an ONNX Real-ESRGAN model."""

    def __init__(self, model_path: Path, scale: int, use_cuda: bool = True):
        self.session = create_session(model_path, use_cuda)
        self.scale = scale
        self.multiple = 2 if scale == 2 else 1  # x2plus pixel-unshuffles, so sizes must be even

    def _infer(self, tile):
        import numpy as np

        h, w = tile.shape[:2]
        extra_h, extra_w = (-h) % self.multiple, (-w) % self.multiple
        if extra_h or extra_w:
            tile = np.pad(tile, ((0, extra_h), (0, extra_w), (0, 0)), mode="edge")
        out = self.session.run(["image"], {"frame": np.ascontiguousarray(tile)[None]})[0][0]
        return out[: h * self.scale, : w * self.scale]

    def upscale(self, frame):
        import numpy as np

        h, w = frame.shape[:2]
        s = self.scale
        if h * w <= TILE_THRESHOLD:
            p = PRE_PAD
            padded = np.pad(frame, ((p, p), (p, p), (0, 0)), mode="reflect")
            return self._infer(padded)[p * s:(p + h) * s, p * s:(p + w) * s]
        p = TILE_PAD
        padded = np.pad(frame, ((p, p), (p, p), (0, 0)), mode="reflect")
        out = np.empty((h * s, w * s, 3), np.uint8)
        for y in range(0, h, TILE):
            for x in range(0, w, TILE):
                y1, x1 = min(y + TILE, h), min(x + TILE, w)
                result = self._infer(padded[y:y1 + 2 * p, x:x1 + 2 * p])
                out[y * s:y1 * s, x * s:x1 * s] = result[p * s:(p + y1 - y) * s, p * s:(p + x1 - x) * s]
        return out


class UpscaleCancelled(Exception):
    pass


def upscale_video(source: Path, destination: Path, info, plan, video_encoder_args: list[str],
                  on_progress, cancel_event: threading.Event, use_cuda: bool = True) -> None:
    """Run the full decode -> GPU -> encode pipeline. `info`/`plan` come from upscale.py."""
    import numpy as np

    from .upscale import UpscaleError, _hms, _last_line

    directory = models_dir()
    model_file = directory / MODEL_FILES[plan.model] if directory else None
    if not model_file or not model_file.exists():
        raise UpscaleError(f"The AI model {plan.model} is missing from this build.")
    ffmpeg = tools.discover().ffmpeg
    if not ffmpeg:
        raise UpscaleError("ffmpeg was not found.")

    started = time.monotonic()
    on_progress(None, "AI upscaling — loading the AI model on the GPU…")
    upscaler = OnnxUpscaler(model_file, plan.scale, use_cuda)
    w, h, s = info.width, info.height, plan.scale
    frame_size = w * h * 3
    total = info.estimated_frames
    log.info("Upscaling %s (%dx%d @ %s) with %s x%d (%s) to %dx%d", source.name, w, h, info.frame_rate,
             plan.model, s, "CUDA" if use_cuda else "CPU", plan.out_width, plan.out_height)

    import tempfile
    partial = destination.with_name(destination.name + ".part.mp4")
    audio_args = ["-c:a", "copy"] if info.audio_codec == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    decoder_err = tempfile.TemporaryFile(mode="w+")
    encoder_err = tempfile.TemporaryFile(mode="w+")
    decoder = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(source),
         "-map", "0:v:0", "-vf", f"fps={info.frame_rate}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=decoder_err, **tools.no_window())
    encoder = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w * s}x{h * s}",
         "-framerate", info.frame_rate, "-i", "-",
         "-i", str(source), "-map", "0:v:0", "-map", "1:a:0?",
         "-vf", f"scale={plan.out_width}:{plan.out_height}:flags=lanczos,format=yuv420p",
         *video_encoder_args, *audio_args, "-movflags", "+faststart", str(partial)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=encoder_err, **tools.no_window())

    stop = threading.Event()
    frames_in: queue.Queue = queue.Queue(maxsize=8)
    frames_out: queue.Queue = queue.Queue(maxsize=8)
    errors: list[BaseException] = []

    def put(q: queue.Queue, item) -> bool:
        while not stop.is_set():
            try:
                q.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def reader() -> None:
        try:
            while not stop.is_set():
                data = decoder.stdout.read(frame_size)
                if not data or len(data) < frame_size:
                    break
                if not put(frames_in, data):
                    return
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            put(frames_in, None)

    def writer() -> None:
        try:
            while True:
                data = frames_out.get()
                if data is None:
                    break
                encoder.stdin.write(data)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            stop.set()
        finally:
            try:
                encoder.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=reader, daemon=True), threading.Thread(target=writer, daemon=True)]
    for t in threads:
        t.start()

    done = repeats = 0
    first_at: float | None = None
    last_report = 0.0
    previous_in: bytes | None = None
    previous_out: bytes | None = None
    try:
        while True:
            if cancel_event.is_set():
                raise UpscaleCancelled()
            if errors:
                raise errors[0]
            try:
                data = frames_in.get(timeout=0.2)
            except queue.Empty:
                continue
            if data is None:
                break
            if data == previous_in and previous_out is not None:
                result = previous_out  # exact repeat: reuse the upscaled frame
                repeats += 1
            else:
                frame = np.frombuffer(data, np.uint8).reshape(h, w, 3)
                result = np.ascontiguousarray(upscaler.upscale(frame)).tobytes()
                previous_in, previous_out = data, result
            while True:
                try:
                    frames_out.put(result, timeout=0.2)
                    break
                except queue.Full:
                    if cancel_event.is_set():
                        raise UpscaleCancelled()
                    if errors:
                        raise errors[0]
            done += 1
            now = time.monotonic()
            if first_at is None:
                first_at = now
            if now - last_report >= 0.5:
                last_report = now
                measured = now - first_at
                if measured < 2.0 or done < 2:
                    on_progress(min(done / total, 1.0) if total else None,
                                f"AI upscaling — frame {done:,}" + (f" of {total:,}" if total else "")
                                + " · measuring speed…")
                else:
                    rate = (done - 1) / measured
                    if total:
                        on_progress(min(done / total, 1.0),
                                    f"AI upscaling — frame {done:,} of {total:,} · {rate:.1f} frames/s · "
                                    f"{_hms(max(total - done, 0) / rate)} left")
                    else:
                        on_progress(None, f"AI upscaling — {done:,} frames · {rate:.1f} frames/s")

        frames_out.put(None)
        threads[1].join()
        if errors:
            raise errors[0]
        on_progress(None, "Finishing video…")
        if encoder.wait() != 0:
            raise UpscaleError("Encoding the upscaled video failed: " + _last_line(encoder_err))
        if decoder.wait() != 0:
            raise UpscaleError("Reading the source video failed: " + _last_line(decoder_err))
        if done == 0:
            raise UpscaleError("The video contained no frames.")
        log.info("Upscaled %d frames (%d repeats reused) in %.0f s", done, repeats, time.monotonic() - started)
        partial.replace(destination)
    except BrokenPipeError:
        if cancel_event.is_set():
            raise UpscaleCancelled() from None
        raise UpscaleError("Encoding the upscaled video failed: " + _last_line(encoder_err)) from None
    finally:
        stop.set()
        for proc in (decoder, encoder):
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        # Unblock and finish the helper threads.
        for q in (frames_in, frames_out):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        try:
            frames_out.put_nowait(None)
        except queue.Full:
            pass
        for t in threads:
            t.join(timeout=5)
        decoder_err.close()
        encoder_err.close()
        partial.unlink(missing_ok=True)
