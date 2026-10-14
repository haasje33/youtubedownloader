"""End-to-end tests of the download pipeline against a local HTTP server.

yt-dlp's extraction step is replaced with a fake YouTube-like info dict whose
formats point at small files generated with ffmpeg, so no network is needed.
"""

import functools
import http.server
import json
import shutil
import subprocess
import threading

import pytest
import yt_dlp

from ytdownloader import downloader, tools
from ytdownloader.formats import Mode

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")


def _ffmpeg(*args):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def media_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")
    src = ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2"]
    _ffmpeg(*src, "-c:v", "libvpx-vp9", "-b:v", "300k", "-an", str(root / "video.webm"))
    _ffmpeg(*src, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(root / "video.mp4"))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "aac", str(root / "audio.m4a"))

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _fake_info(base, root):
    def size(name):
        return (root / name).stat().st_size

    return {
        "id": "fakeid12345",
        "title": "Test: Video / with odd chars",
        "duration": 2,
        "extractor": "generic",
        "extractor_key": "Generic",
        "webpage_url": "https://www.youtube.com/watch?v=fakeid12345",
        "formats": [
            {"format_id": "140", "url": f"{base}/audio.m4a", "ext": "m4a", "vcodec": "none",
             "acodec": "mp4a.40.2", "abr": 128, "protocol": "https", "filesize": size("audio.m4a")},
            {"format_id": "134", "url": f"{base}/video.mp4", "ext": "mp4", "vcodec": "avc1.4d401e",
             "acodec": "none", "width": 640, "height": 360, "fps": 30, "protocol": "https",
             "filesize": size("video.mp4")},
            {"format_id": "243", "url": f"{base}/video.webm", "ext": "webm", "vcodec": "vp9",
             "acodec": "none", "width": 640, "height": 360, "fps": 30, "protocol": "https",
             "filesize": size("video.webm")},
        ],
    }


@pytest.fixture
def fake_youtube(monkeypatch, media_server):
    root, base = media_server
    info = _fake_info(base, root)

    def extract_info(self, url, download=True, **kwargs):
        return self.process_ie_result(json.loads(json.dumps(info)), download=download)

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    tools.discover.cache_clear()
    yield info
    tools.discover.cache_clear()


def _probe(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_name,codec_tag_string,codec_type",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


def test_fetch_info(fake_youtube):
    info = downloader.fetch_info("https://www.youtube.com/watch?v=fakeid12345")
    assert info.tiers == [360]
    assert info.title.startswith("Test")


def test_compatible_download_uses_h264_without_conversion(fake_youtube, tmp_path):
    events = []
    path = downloader.download("https://youtu.be/fakeid12345", 360, Mode.COMPATIBLE, tmp_path,
                               on_progress=events.append)
    assert path.parent == tmp_path and path.suffix == ".mp4"
    assert path.name.endswith("(360p).mp4")
    streams = _probe(path)
    assert streams["video"]["codec_name"] == "h264"
    assert streams["audio"]["codec_name"] == "aac"
    assert not any(e.stage == "convert" for e in events)
    assert any(e.stage == "download" and e.fraction for e in events)


def test_original_download_keeps_vp9_in_mp4(fake_youtube, tmp_path):
    fake_youtube["formats"] = [f for f in fake_youtube["formats"] if f["format_id"] != "134"]
    path = downloader.download("https://youtu.be/fakeid12345", 360, Mode.ORIGINAL, tmp_path)
    assert _probe(path)["video"]["codec_name"] == "vp9"


def test_compatible_download_converts_vp9_to_hevc(fake_youtube, tmp_path):
    fake_youtube["formats"] = [f for f in fake_youtube["formats"] if f["format_id"] != "134"]
    events = []
    path = downloader.download("https://youtu.be/fakeid12345", 2160, Mode.COMPATIBLE, tmp_path,
                               on_progress=events.append)
    video = _probe(path)["video"]
    assert video["codec_name"] in ("hevc", "h264")
    if video["codec_name"] == "hevc":
        assert video["codec_tag_string"] == "hvc1"  # required for QuickTime
    assert any(e.stage == "convert" for e in events)
    assert not list(tmp_path.glob("*.part*"))


def test_existing_file_is_not_overwritten(fake_youtube, tmp_path):
    first = downloader.download("https://youtu.be/fakeid12345", 360, Mode.COMPATIBLE, tmp_path)
    second = downloader.download("https://youtu.be/fakeid12345", 360, Mode.COMPATIBLE, tmp_path)
    assert first != second and first.exists() and second.exists()
    assert second.name.endswith("(360p) (2).mp4")


def test_cancel(fake_youtube, tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(downloader.Cancelled):
        downloader.download("https://youtu.be/fakeid12345", 360, Mode.COMPATIBLE, tmp_path,
                            cancel_event=cancel)
    assert not list(tmp_path.iterdir())
