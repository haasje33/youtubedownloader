import functools
import hashlib
import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

from ytdownloader import updater

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_notes  # noqa: E402


@pytest.mark.parametrize("candidate,current,newer", [
    ("v0.5.1", "0.5.0", True),
    ("0.6.0", "0.5.9", True),
    ("v1.0", "0.9.9", True),
    ("v0.5.0", "0.5.0", False),
    ("v0.5", "0.5.0", False),
    ("v0.4.9", "0.5.0", False),
    ("v0.10.0", "0.9.0", True),
])
def test_is_newer(candidate, current, newer):
    assert updater.is_newer(candidate, current) is newer


def test_parse_checksums():
    text = ("a" * 64 + "  YouTube-Downloader-0.5.1-Windows-x64-Setup.exe\n"
            + "B" * 64 + " *YouTube-Downloader-0.5.1-macOS-arm64.zip\n"
            "garbage line\n")
    sums = updater.parse_checksums(text)
    assert sums == {"YouTube-Downloader-0.5.1-Windows-x64-Setup.exe": "a" * 64,
                    "YouTube-Downloader-0.5.1-macOS-arm64.zip": "b" * 64}


def test_asset_suffix_per_platform():
    assert updater.platform_asset_suffix("win32") == "-Windows-x64-Setup.exe"
    assert updater.platform_asset_suffix("darwin", "arm64") == "-macOS-arm64.zip"
    assert updater.platform_asset_suffix("darwin", "x86_64") is None  # Intel Macs: no updates
    assert updater.platform_asset_suffix("linux") is None


@pytest.fixture
def release_server(tmp_path):
    """A fake GitHub releases API serving one release with real files."""
    root = tmp_path / "srv"
    root.mkdir()
    payload = b"new app version " * 50_000
    names = {"win": "YouTube-Downloader-0.9.0-Windows-x64-Setup.exe",
             "mac": "YouTube-Downloader-0.9.0-macOS-arm64.zip"}
    for name in names.values():
        (root / name).write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    (root / "SHA256SUMS.txt").write_text("".join(f"{sha}  {n}\n" for n in names.values()))

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    api = {
        "tag_name": "v0.9.0", "body": "Lots of new things.", "draft": False, "prerelease": False,
        "html_url": f"{base}/release",
        "assets": [{"name": n, "browser_download_url": f"{base}/{n}", "size": len(payload)}
                   for n in [*names.values(), "SHA256SUMS.txt"]],
    }
    (root / "latest.json").write_text(json.dumps(api))
    yield base, names, payload, root
    server.shutdown()


def test_check_and_download(release_server, monkeypatch):
    base, names, payload, _ = release_server
    monkeypatch.setattr(updater, "platform_asset_suffix", lambda *a: "-Windows-x64-Setup.exe")
    release = updater.check(current="0.5.0", url=f"{base}/latest.json")
    assert release.version == "0.9.0"
    assert release.asset_name == names["win"]
    assert release.notes == "Lots of new things."
    progress = []
    path = updater.download(release, lambda d, t: progress.append((d, t)))
    assert path.read_bytes() == payload
    assert progress[-1] == (len(payload), len(payload))
    assert updater.check(current="0.9.0", url=f"{base}/latest.json") is None


def test_damaged_download_is_rejected(release_server, monkeypatch):
    base, names, _, root = release_server
    monkeypatch.setattr(updater, "platform_asset_suffix", lambda *a: "-macOS-arm64.zip")
    release = updater.check(current="0.5.0", url=f"{base}/latest.json")
    (root / names["mac"]).write_bytes(b"tampered")
    with pytest.raises(updater.UpdateError, match="checksum"):
        updater.download(release)


def test_prerelease_is_ignored(release_server, monkeypatch):
    base, _, _, root = release_server
    data = json.loads((root / "latest.json").read_text())
    (root / "latest.json").write_text(json.dumps({**data, "prerelease": True}))
    monkeypatch.setattr(updater, "platform_asset_suffix", lambda *a: "-Windows-x64-Setup.exe")
    assert updater.check(current="0.5.0", url=f"{base}/latest.json") is None


def test_release_without_checksums_is_refused(release_server, monkeypatch):
    base, _, _, root = release_server
    data = json.loads((root / "latest.json").read_text())
    data["assets"] = [a for a in data["assets"] if a["name"] != "SHA256SUMS.txt"]
    (root / "latest.json").write_text(json.dumps(data))
    monkeypatch.setattr(updater, "platform_asset_suffix", lambda *a: "-Windows-x64-Setup.exe")
    with pytest.raises(updater.UpdateError, match="SHA256SUMS"):
        updater.check(current="0.5.0", url=f"{base}/latest.json")


def test_plain_notes():
    md = "- **In-app updates.** The app checks\n  for new versions.\n- Second item."
    assert updater.plain_notes(md) == "• In-app updates. The app checks for new versions.\n\n• Second item."


def test_updates_only_in_packaged_app(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert not updater.updates_supported()


def test_release_notes_come_from_changelog():
    text = "# Changelog\n\n## 0.6.0\n\n- Queue\n\n## 0.5.0\n\n- Updates\n- yt-dlp\n\n## 0.4.0\n\n- Windows\n"
    assert release_notes.section("0.5.0", text) == "- Updates\n- yt-dlp\n"
    with pytest.raises(SystemExit):
        release_notes.section("9.9.9", text)


def test_current_version_has_changelog_entry():
    from ytdownloader import __version__

    changelog = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    assert release_notes.section(__version__, changelog).strip()
