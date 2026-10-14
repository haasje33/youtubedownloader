"""Benchmark Real-ESRGAN tile size (-t) and thread (-j) settings.

    python scripts/bench_upscale.py "dist/YouTube Downloader.app"

Upscales a set of 640x360 test frames with each model and setting and prints the
frames per second (model loading excluded) and the model load time. Used to pick
DEFAULT_TILE / DEFAULT_THREADS in src/ytdownloader/upscale.py.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CONFIGS = [(tile, threads) for tile in ("0", "128", "256", "512") for threads in ("1:2:2", "2:2:2", "2:4:4")]
MODELS = [("realesrgan-x4plus", 4, 6), ("realesr-general-x4v3", 4, 24), ("realesr-animevideov3", 2, 24)]


def run(exe: Path, models: Path, frames: Path, out: Path, model: str, scale: int, tile: str, threads: str) -> float:
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    start = time.monotonic()
    proc = subprocess.run([str(exe), "-i", str(frames), "-o", str(out), "-n", model, "-s", str(scale),
                           "-m", str(models), "-f", "png", "-t", tile, "-j", threads],
                          capture_output=True, text=True)
    elapsed = time.monotonic() - start
    if proc.returncode != 0 or len(list(out.iterdir())) != len(list(frames.iterdir())):
        raise RuntimeError(proc.stderr.strip().splitlines()[-1:] or proc.returncode)
    return elapsed


def main() -> int:
    app = Path(sys.argv[1])
    exe = app / "Contents/Frameworks/bin/realesrgan-ncnn-vulkan"
    ffmpeg = app / "Contents/Frameworks/bin/ffmpeg"
    models = app / "Contents/Resources/realesrgan-models"
    if not models.exists():
        models = app / "Contents/Frameworks/realesrgan-models"
    work = Path(tempfile.mkdtemp())
    one, many = work / "one", work / "many"
    one.mkdir()
    many.mkdir()
    subprocess.run([str(ffmpeg), "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=640x360:rate=24:duration=1", str(many / "%08d.png")], check=True)
    shutil.copy(many / "00000001.png", one / "00000001.png")

    print(f"{'model':24} {'tile':>5} {'threads':>8} {'load s':>7} {'frames/s':>9}")
    for model, scale, count in MODELS:
        subset = work / f"subset-{model}"
        subset.mkdir()
        for frame in sorted(many.iterdir())[:count]:
            shutil.copy(frame, subset / frame.name)
        for tile, threads in CONFIGS:
            try:
                load = run(exe, models, one, work / "out", model, scale, tile, threads)
                total = run(exe, models, subset, work / "out", model, scale, tile, threads)
            except RuntimeError as exc:
                print(f"{model:24} {tile:>5} {threads:>8}  failed: {exc}")
                continue
            fps = (count - 1) / max(total - load, 1e-3)
            print(f"{model:24} {tile:>5} {threads:>8} {load:7.1f} {fps:9.2f}", flush=True)
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
