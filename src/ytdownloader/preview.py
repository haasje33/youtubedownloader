"""Before/after preview of AI upscaling, with a speed measurement per model.

One frame is taken from the video (straight from YouTube's stream, or from a local
file), upscaled with each model, and compared with a plain (bicubic) enlargement.
The measured speed gives an estimate of how long the whole video would take.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import tools, upscale
from .upscale import Content, Plan, UpscaleError

log = logging.getLogger(__name__)

PREVIEW_CONTENTS = (Content.REALISTIC, Content.FAST, Content.ANIMATION)


@dataclass
class FrameSource:
    """Where to take the preview frame from."""
    width: int
    height: int
    duration: float | None
    fps: float | None
    path: str = ""            # local file, or
    url: str = ""             # direct stream URL (from yt-dlp)
    headers: dict | None = None

    @property
    def total_frames(self) -> int | None:
        return int(self.duration * (self.fps or 30)) if self.duration else None


@dataclass
class ModelPreview:
    content: Content
    model: str
    image: Path               # upscaled frame at the target size (PNG)
    frames_per_second: float

    def estimate_seconds(self, total_frames: int | None) -> float | None:
        if not total_frames or self.frames_per_second <= 0:
            return None
        return total_frames / self.frames_per_second


def source_from_stream(fmt: dict, duration: float | None) -> FrameSource:
    return FrameSource(width=int(fmt.get("width") or 0), height=int(fmt.get("height") or 0),
                       duration=duration, fps=fmt.get("fps"), url=fmt["url"],
                       headers=fmt.get("http_headers") or {})


def source_from_file(path: Path) -> FrameSource:
    info = upscale.probe(path)
    return FrameSource(width=info.width, height=info.height, duration=info.duration,
                       fps=info.fps, path=str(path))


def _ffmpeg() -> str:
    ffmpeg = tools.discover().ffmpeg
    if not ffmpeg:
        raise UpscaleError("ffmpeg was not found.")
    return ffmpeg


def _run(cmd: list[str], timeout: float = 300) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False,
                            **tools.no_window())
    if result.returncode != 0:
        lines = (result.stderr or "").strip().splitlines()
        raise UpscaleError(lines[-1] if lines else f"exit code {result.returncode}")


def grab_frame(source: FrameSource, at: float, out_png: Path) -> None:
    """Save the frame at `at` seconds as PNG (fast seek, works on streams too)."""
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    if source.url:
        if source.headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in source.headers.items())]
        cmd += ["-ss", f"{max(at, 0):.3f}", "-i", source.url]
    else:
        cmd += ["-ss", f"{max(at, 0):.3f}", "-i", source.path]
    cmd += ["-frames:v", "1", "-pix_fmt", "rgb24", str(out_png)]
    _run(cmd, timeout=120)
    if not out_png.exists():
        raise UpscaleError("Could not read a frame at that moment of the video.")


def enlarge(frame_png: Path, width: int, height: int, out_png: Path, flags: str = "bicubic") -> None:
    _run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(frame_png),
          "-vf", f"scale={width}:{height}:flags={flags}", str(out_png)])


def _time_ncnn(frame_png: Path, p: Plan, work: Path) -> tuple[Path, float]:
    paths = upscale.upscaler_paths()
    if not paths:
        raise UpscaleError("The AI upscaler (Real-ESRGAN) is not installed.")
    exe, models = paths

    def run(copies: int) -> tuple[float, Path]:
        src, dst = work / f"in{copies}", work / f"out{copies}"
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)
        src.mkdir()
        dst.mkdir()
        for i in range(copies):
            shutil.copy(frame_png, src / f"{i:03d}.png")
        start = time.monotonic()
        _run([exe, "-i", str(src), "-o", str(dst), "-n", p.model, "-s", str(p.scale), "-m", str(models),
              "-f", "png", *upscale.realesrgan_tuning(p.model)], timeout=1800)
        return time.monotonic() - start, dst / "000.png"

    one, output = run(1)
    three, _ = run(3)
    per_frame = max((three - one) / 2, 1e-3)  # model loading cancels out
    return output, 1.0 / per_frame


def _time_onnx(frame_png: Path, p: Plan, work: Path, use_cuda: bool) -> tuple[Path, float]:
    import numpy as np

    from . import onnx_engine

    directory = onnx_engine.models_dir()
    if directory is None:
        raise UpscaleError("The NVIDIA models are not included in this build.")
    probe = upscale.probe(frame_png)
    raw = subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(frame_png),
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True,
                         **tools.no_window()).stdout
    frame = np.frombuffer(raw, np.uint8).reshape(probe.height, probe.width, 3)
    upscaler = onnx_engine.OnnxUpscaler(directory / onnx_engine.MODEL_FILES[p.model], p.scale,
                                        use_cuda=use_cuda, allow_tensorrt=False)
    result = upscaler.upscale(frame)  # warm-up (CUDA picks its fastest kernels here)
    timings = []
    for _ in range(2):
        start = time.monotonic()
        result = upscaler.upscale(frame)
        timings.append(time.monotonic() - start)
    out_png = work / "onnx.png"
    h, w = result.shape[:2]
    subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                    "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-i", "-", str(out_png)],
                   input=np.ascontiguousarray(result).tobytes(), check=True, **tools.no_window())
    return out_png, 1.0 / max(min(timings), 1e-3)


def preview_models(source: FrameSource, at: float, target: int, work_dir: Path,
                   contents=PREVIEW_CONTENTS, on_status=None, on_frame=None, on_result=None,
                   cancel_event: threading.Event | None = None) -> tuple[Path, Path, list[ModelPreview]]:
    """Returns (original frame, bicubic enlargement, one ModelPreview per model).

    on_frame(frame, bicubic) and on_result(ModelPreview) report results as soon as
    they are ready, so a window can show them while the other models still run.
    """
    on_status = on_status or (lambda text: None)
    work_dir.mkdir(parents=True, exist_ok=True)
    frame = work_dir / "frame.png"
    on_status("Taking a frame from the video…")
    grab_frame(source, at, frame)
    info = upscale.probe(frame)
    engine = upscale.preferred_engine()
    base = upscale.plan(info.width, info.height, target, Content.REALISTIC, engine)
    plain = work_dir / "bicubic.png"
    enlarge(frame, base.out_width, base.out_height, plain)
    if on_frame:
        on_frame(frame, plain)

    results = []
    for content in contents:
        if cancel_event is not None and cancel_event.is_set():
            break
        p = upscale.plan(info.width, info.height, target, content, engine)
        on_status(f"Upscaling with “{content.value}” ({p.model})…")
        with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
            if engine in ("cuda", "onnx-cpu"):
                raw_out, fps = _time_onnx(frame, p, Path(tmp), use_cuda=engine == "cuda")
            else:
                raw_out, fps = _time_ncnn(frame, p, Path(tmp))
            final = work_dir / f"{content.value}.png"
            enlarge(raw_out, p.out_width, p.out_height, final, flags="lanczos")
        results.append(ModelPreview(content, p.model, final, fps))
        if on_result:
            on_result(results[-1])
        log.info("Preview %s: %.2f frames/s", p.model, fps)
    return frame, plain, results
