"""yt-dlp self-update, against a fake PyPI serving small fake yt-dlp/yt-dlp-ejs wheels."""

import functools
import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from ytdownloader import ytdlp_update

SRC = str(Path(__file__).resolve().parents[1] / "src")


def make_wheel(files: dict[str, str], dist: str, metadata: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
        z.writestr(f"{dist}.dist-info/METADATA", metadata)
    return buf.getvalue()


def padded(version: str) -> str:
    year, *rest = version.split(".")
    return ".".join([year, *(part.zfill(2) for part in rest)])


def fake_ytdlp(version: str, broken: bool = False) -> bytes:
    init = "raise ImportError('broken on purpose')\n" if broken else (
        "from . import version\n"
        "class YoutubeDL:\n"
        "    def __init__(self, params=None): pass\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): return False\n")
    return make_wheel({
        "yt_dlp/__init__.py": init,
        # Like the real thing: PyPI says 2099.1.1, yt_dlp.version says 2099.01.01.
        "yt_dlp/version.py": f"__version__ = {padded(version)!r}\n",
        "yt_dlp/extractor/__init__.py": (
            "class _YT:\n    IE_NAME = 'youtube'\n"
            "def gen_extractor_classes():\n    return [_YT]\n"),
    }, f"yt_dlp-{version}", "Name: yt-dlp\nRequires-Dist: yt-dlp-ejs==0.0.42; extra == \"default\"\n")


@pytest.fixture
def fake_pypi(tmp_path, monkeypatch):
    root = tmp_path / "pypi"
    root.mkdir()
    server_box = {}

    def publish(version: str, broken: bool = False):
        files = {
            f"yt_dlp-{version}-py3-none-any.whl": fake_ytdlp(version, broken),
            "yt_dlp_ejs-0.0.42-py3-none-any.whl": make_wheel(
                {"yt_dlp_ejs/__init__.py": "VERSION = '0.0.42'\n"}, "yt_dlp_ejs-0.0.42", "Name: yt-dlp-ejs\n"),
        }
        for name, data in files.items():
            (root / name).write_bytes(data)
        base = server_box["base"]

        def entry(name):
            return {"packagetype": "bdist_wheel", "filename": name, "url": f"{base}/{name}",
                    "digests": {"sha256": hashlib.sha256(files[name]).hexdigest()}}

        (root / "yt-dlp.json").write_text(json.dumps(
            {"info": {"name": "yt-dlp", "version": version},
             "urls": [entry(f"yt_dlp-{version}-py3-none-any.whl")]}))
        (root / "ejs.json").write_text(json.dumps(
            {"info": {"name": "yt-dlp-ejs", "version": "0.0.42"},
             "urls": [entry("yt_dlp_ejs-0.0.42-py3-none-any.whl")]}))

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server_box["base"] = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(ytdlp_update, "PYPI_JSON", server_box["base"] + "/{name}.json")
    monkeypatch.setattr(ytdlp_update, "PYPI_VERSION_JSON", server_box["base"] + "/ejs.json?{name}{version}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # settings.support_dir() lives under HOME
    monkeypatch.setenv("PYTHONPATH", SRC)
    yield publish
    server.shutdown()


def test_required_ejs_version():
    meta = 'Requires-Dist: yt-dlp-ejs==0.8.0; extra == "default"\nRequires-Dist: certifi; extra == "default"\n'
    assert ytdlp_update.required_ejs_version(meta) == "0.8.0"
    assert ytdlp_update.required_ejs_version("Name: yt-dlp\n") is None


def test_update_installs_validates_and_activates(fake_pypi, tmp_path):
    fake_pypi("2099.1.1")
    assert ytdlp_update.update(force=True) == "2099.1.1"
    installed = ytdlp_update.store_dir() / "2099.1.1"
    assert (installed / "yt_dlp" / "version.py").exists()
    assert (installed / "yt_dlp_ejs" / "__init__.py").exists()
    assert not any(p.name.startswith("2099.1.1-") for p in ytdlp_update.store_dir().iterdir())

    # A fresh process picks the downloaded copy up.
    code = ("from ytdownloader import ytdlp_update as u; print(u.activate()); "
            "import yt_dlp; print(yt_dlp.version.__version__)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ}, check=True).stdout.split()
    assert out == ["2099.1.1", "2099.01.01"]

    # Nothing newer: no second download, and the daily limit applies without force.
    assert ytdlp_update.update(force=True) is None
    assert ytdlp_update.update(force=False) is None


def test_broken_release_is_never_activated(fake_pypi):
    fake_pypi("2099.2.2", broken=True)
    with pytest.raises(ytdlp_update.YtdlpUpdateError, match="failed its check"):
        ytdlp_update.update(force=True)
    assert not (ytdlp_update.store_dir() / "2099.2.2").exists()
    state = json.loads((ytdlp_update.store_dir() / "active.json").read_text())
    assert "path" not in state or not state["path"]


def test_older_than_bundled_is_ignored(fake_pypi):
    fake_pypi("2000.1.1")  # older than the yt-dlp installed for the tests
    assert ytdlp_update.update(force=True) is None


def test_activate_falls_back_when_copy_breaks(fake_pypi):
    fake_pypi("2099.3.3")
    ytdlp_update.update(force=True)
    (ytdlp_update.store_dir() / "2099.3.3" / "yt_dlp" / "__init__.py").write_text("raise ImportError('x')\n")
    code = ("from ytdownloader import ytdlp_update as u; print(u.activate()); "
            "import yt_dlp; print(yt_dlp.version.__version__ != '2099.03.03')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ}, check=True).stdout.split()
    assert out == ["None", "True"]  # bundled yt-dlp used instead
