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
    (bindir / "models" / "realesrgan-x4plus.param").write_text("")
    exe = bindir / "realesrgan-ncnn-vulkan"
    exe.write_text(f"#!{sys.executable}\n" + textwrap.dedent(f"""
        import argparse, os, subprocess
        ap = argparse.ArgumentParser()
        for flag in ("-i", "-o", "-n", "-s", "-m", "-f"):
            ap.add_argument(flag)
        a = ap.parse_args()
        for name in sorted(os.listdir(a.i)):
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
    monkeypatch.setattr(upscale, "CHUNK_FRAMES", 7)  # exercise several chunks
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
