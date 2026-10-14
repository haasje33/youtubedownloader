import io
import shutil
import subprocess
import sys
import textwrap
import threading

import pytest

from ytdownloader import downloader, tools, upscale
from ytdownloader.upscale import Content, UpscaleError

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")


@pytest.mark.parametrize("w,h,target,content,expected", [
    (640, 360, 1080, Content.REALISTIC, ("realesrgan-x4plus", 4, 1920, 1080)),
    (640, 360, 2160, Content.REALISTIC, ("realesrgan-x4plus", 4, 3840, 2160)),
    (640, 360, 1080, Content.FAST, ("realesr-general-x4v3", 4, 1920, 1080)),
    (640, 360, 1080, Content.ANIMATION, ("realesr-animevideov3", 3, 1920, 1080)),
    (854, 480, 1080, Content.ANIMATION, ("realesr-animevideov3", 3, 1922, 1080)),
    (1280, 720, 1440, Content.ANIMATION, ("realesr-animevideov3", 2, 2560, 1440)),
    (256, 144, 2160, Content.ANIMATION, ("realesr-animevideov3", 4, 3840, 2160)),
    (360, 640, 1080, Content.REALISTIC, ("realesrgan-x4plus", 4, 1080, 1920)),  # vertical
])
def test_plan(w, h, target, content, expected):
    p = upscale.plan(w, h, target, content)
    assert (p.model, p.scale, p.out_width, p.out_height) == expected


def test_plan_rejects_already_large_enough():
    with pytest.raises(UpscaleError):
        upscale.plan(1920, 1080, 1080, Content.REALISTIC)


def _png(payload: bytes) -> bytes:
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return upscale.PNG_SIGNATURE + chunk(b"IHDR", payload) + chunk(b"IEND", b"")


def test_read_png_splits_concatenated_stream():
    a, b = _png(b"first"), _png(b"second-image")
    stream = io.BytesIO(a + b)
    assert upscale.read_png(stream) == a
    assert upscale.read_png(stream) == b
    assert upscale.read_png(stream) is None


def test_read_png_truncated():
    with pytest.raises(UpscaleError):
        upscale.read_png(io.BytesIO(_png(b"x")[:-3]))


@pytest.fixture
def fake_realesrgan(tmp_path, monkeypatch):
    """A stand-in for realesrgan-ncnn-vulkan that scales frames with ffmpeg."""
    bindir = tmp_path / "esrgan"
    (bindir / "models").mkdir(parents=True)
    for model in ("realesrgan-x4plus", "realesr-general-x4v3", "realesr-animevideov3-x4"):
        (bindir / "models" / f"{model}.param").write_text("")
    calls = bindir / "calls.log"
    exe = bindir / "realesrgan-ncnn-vulkan"
    exe.write_text(f"#!{sys.executable}\n" + textwrap.dedent(f"""
        import argparse, os, subprocess
        ap = argparse.ArgumentParser()
        for flag in ("-i", "-o", "-n", "-s", "-m", "-f", "-t", "-j"):
            ap.add_argument(flag)
        a = ap.parse_args()
        names = sorted(os.listdir(a.i))
        with open({str(calls)!r}, "a") as log:
            log.write(f"{{a.n}} {{len(names)}}\\n")
        for name in names:
            subprocess.run([{FFMPEG!r}, "-loglevel", "error", "-y", "-i", os.path.join(a.i, name),
                            "-vf", f"scale=iw*{{a.s}}:ih*{{a.s}}", os.path.join(a.o, name)], check=True)
    """))
    exe.chmod(0o755)
    monkeypatch.setenv("YTD_REALESRGAN_NCNN_VULKAN", str(exe))
    tools.discover.cache_clear()
    yield exe
    tools.discover.cache_clear()


@needs_ffmpeg
def test_upscale_file_end_to_end(tmp_path, fake_realesrgan, monkeypatch):
    monkeypatch.setattr(upscale, "CHUNK_MAX_FRAMES", 7)  # exercise several chunks
    source = tmp_path / "small.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)],
                   check=True)
    out_dir = tmp_path / "out"
    events = []
    result = downloader.upscale_file(source, out_dir, 1080, Content.REALISTIC, on_progress=events.append)
    assert result.name == "small (1080p AI upscaled).mp4"
    info = upscale.probe(result)
    assert (info.width, info.height) == (1920, 1080)
    assert info.audio_codec == "aac"
    frames = subprocess.run([FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0",
                             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(result)],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert int(frames) == 25
    assert any(e.stage == "upscale" and e.fraction for e in events)
    assert [p.name for p in out_dir.iterdir()] == [result.name]
    calls = (fake_realesrgan.parent / "calls.log").read_text().split()
    assert calls[0] == "realesrgan-x4plus"
    assert sum(int(n) for n in calls[1::2]) == 25  # every frame upscaled exactly once
    assert len(calls) // 2 == 4  # chunks of 7 + 7 + 7 + 4 frames


@needs_ffmpeg
def test_upscale_cancel_leaves_nothing_behind(tmp_path, fake_realesrgan):
    source = tmp_path / "small.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=320x180:rate=25:duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(source)], check=True)
    cancel = threading.Event()
    cancel.set()
    out_dir = tmp_path / "out"
    with pytest.raises(downloader.Cancelled):
        downloader.upscale_file(source, out_dir, 1080, Content.REALISTIC, cancel_event=cancel)
    assert list(out_dir.iterdir()) == []


@needs_ffmpeg
def test_upscale_reports_upscaler_failure(tmp_path, fake_realesrgan):
    fake_realesrgan.write_text("#!/bin/sh\necho 'vkCreateInstance failed -9' >&2\nexit 255\n")
    source = tmp_path / "small.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=320x180:rate=25:duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(source)], check=True)
    with pytest.raises(downloader.DownloadFailed, match="vkCreateInstance"):
        downloader.upscale_file(source, tmp_path / "out", 1080, Content.REALISTIC)


@needs_ffmpeg
def test_repeated_frames_are_upscaled_once(tmp_path, fake_realesrgan):
    # 2 s of a still image followed by 1 s of moving video, 10 fps.
    source = tmp_path / "still.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=blue:size=320x180:rate=10:duration=2",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=1",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True)
    result = downloader.upscale_file(source, tmp_path / "out", 1080, Content.FAST)
    calls = (fake_realesrgan.parent / "calls.log").read_text().split()
    assert calls[0] == "realesr-general-x4v3"
    upscaled = sum(int(n) for n in calls[1::2])
    assert upscaled < 20  # the 20 still frames were mostly skipped
    frames = subprocess.run([FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0",
                             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(result)],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert int(frames) == 30


def test_png_complete(tmp_path):
    path = tmp_path / "f.png"
    assert not upscale._png_complete(path)
    full = _png(b"data")
    path.write_bytes(full[:-4])
    assert not upscale._png_complete(path)
    path.write_bytes(full)
    assert upscale._png_complete(path)


def test_tuning_env_override(monkeypatch):
    monkeypatch.setenv("YTD_UPSCALE_TILE", "256")
    monkeypatch.setenv("YTD_UPSCALE_THREADS", "2:2:2")
    assert upscale.realesrgan_tuning("realesrgan-x4plus") == ["-t", "256", "-j", "2:2:2"]
