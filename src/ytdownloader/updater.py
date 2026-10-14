"""In-app updates from the public releases repository.

Releases live in https://github.com/haasje33/youtubedownloader-releases (the code
repository is private). Each release has:
    YouTube-Downloader-<ver>-Windows-x64-Setup.exe   (Windows installer)
    YouTube-Downloader-<ver>-macOS-arm64.zip         (the .app, for the updater)
    YouTube-Downloader-<ver>-macOS-arm64.dmg         (for manual installs)
    SHA256SUMS.txt
The updater downloads the right file, checks it against SHA256SUMS.txt, installs
it and restarts the app.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import APP_NAME, __version__

log = logging.getLogger(__name__)

RELEASES_REPO = "haasje33/youtubedownloader-releases"
LATEST_URL = f"https://api.github.com/repos/{RELEASES_REPO}/releases/latest"
USER_AGENT = f"YouTubeDownloader/{__version__}"
CHECKSUMS_NAME = "SHA256SUMS.txt"


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    asset_name: str
    asset_url: str
    size: int
    sha256: str
    page_url: str


def parse_version(text: str) -> tuple[int, ...]:
    match = re.match(r"^v?(\d+(?:\.\d+)*)", text.strip())
    if not match:
        raise ValueError(f"Not a version: {text!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(candidate: str, current: str = __version__) -> bool:
    def padded(v: tuple[int, ...]) -> tuple[int, ...]:
        return v + (0,) * (4 - len(v))
    return padded(parse_version(candidate)) > padded(parse_version(current))


def platform_asset_suffix(platform: str | None = None, machine: str | None = None) -> str | None:
    platform = platform or sys.platform
    if platform == "win32":
        return "-Windows-x64-Setup.exe"
    if platform == "darwin":
        import platform as _platform

        if (machine or _platform.machine()) == "arm64":
            return "-macOS-arm64.zip"
    return None


def updates_supported() -> bool:
    """Only the packaged app updates itself (not a source checkout)."""
    return bool(getattr(sys, "frozen", False)) and platform_asset_suffix() is not None


def _get(url: str, timeout: float = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def parse_checksums(text: str) -> dict[str, str]:
    """Parse `sha256sum` output: '<hex>  <name>' per line."""
    sums = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            sums[parts[1].lstrip("*").strip()] = parts[0].lower()
    return sums


def release_from_api(data: dict, suffix: str, fetch: Callable[[str], bytes] = _get) -> Release:
    version = data.get("tag_name", "")
    assets = {a["name"]: a for a in data.get("assets", [])}
    asset = next((a for name, a in assets.items() if name.endswith(suffix)), None)
    if asset is None:
        raise UpdateError(f"Release {version} has no download for this computer.")
    checksums = assets.get(CHECKSUMS_NAME)
    if checksums is None:
        raise UpdateError(f"Release {version} has no {CHECKSUMS_NAME}.")
    sums = parse_checksums(fetch(checksums["browser_download_url"]).decode("utf-8", "replace"))
    sha = sums.get(asset["name"])
    if not sha:
        raise UpdateError(f"{CHECKSUMS_NAME} does not list {asset['name']}.")
    return Release(
        version=version.lstrip("v"),
        notes=(data.get("body") or "").strip(),
        asset_name=asset["name"],
        asset_url=asset["browser_download_url"],
        size=int(asset.get("size") or 0),
        sha256=sha,
        page_url=data.get("html_url", ""),
    )


def check(current: str = __version__, url: str = LATEST_URL) -> Release | None:
    """The newest release if it is newer than `current`, else None."""
    suffix = platform_asset_suffix()
    if suffix is None:
        return None
    try:
        data = json.loads(_get(url))
    except OSError as exc:
        raise UpdateError(f"Could not reach the update server: {exc}") from exc
    if data.get("draft") or data.get("prerelease"):
        return None
    if not is_newer(data.get("tag_name", "0"), current):
        return None
    return release_from_api(data, suffix)


def download(release: Release, on_progress: Callable[[int, int], None] | None = None,
             cancel_event: threading.Event | None = None) -> Path:
    """Download and verify the release file; returns its path in a temp folder."""
    target_dir = Path(tempfile.mkdtemp(prefix="ytdownloader-update-"))
    target = target_dir / release.asset_name
    digest = hashlib.sha256()
    done = 0
    request = urllib.request.Request(release.asset_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as fh:
            total = int(response.headers.get("Content-Length") or release.size or 0)
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise UpdateError("Update cancelled.")
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)
    except OSError as exc:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise UpdateError(f"Downloading the update failed: {exc}") from exc
    except UpdateError:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    if digest.hexdigest() != release.sha256:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise UpdateError("The downloaded update is damaged (checksum mismatch). Please try again.")
    return target


# --- Installing ---------------------------------------------------------------

def install_and_restart(package: Path) -> None:
    """Start installing `package`. The caller must quit the app right after this."""
    if sys.platform == "win32":
        _install_windows(package)
    elif sys.platform == "darwin":
        _install_macos(package)
    else:
        raise UpdateError("Automatic updates are not supported on this system.")


def _install_windows(installer: Path) -> None:
    # The installer asks for administrator rights (UAC); ShellExecute shows that prompt.
    # /RELAUNCH=1 makes it start the updated app again when it is done.
    arguments = "/SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /RELAUNCH=1"
    try:
        os.startfile(str(installer), "open", arguments)  # type: ignore[attr-defined]
    except OSError as exc:
        raise UpdateError(f"Could not start the installer: {exc}") from exc


def current_app_bundle() -> Path | None:
    """…/YouTube Downloader.app when running from a macOS app bundle."""
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def _install_macos(archive: Path) -> None:
    bundle = current_app_bundle()
    if bundle is None:
        raise UpdateError("Could not find the installed app to replace.")
    staging = archive.parent / "new"
    staging.mkdir()
    result = subprocess.run(["ditto", "-x", "-k", str(archive), str(staging)],
                            capture_output=True, text=True, check=False)
    new_app = next(staging.glob("*.app"), None)
    if result.returncode != 0 or new_app is None:
        raise UpdateError(f"Could not unpack the update: {result.stderr.strip() or 'no app found'}")
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(new_app)], check=False)

    # A helper script waits for this app to quit, swaps the bundles and starts the new one.
    old = bundle.with_name(bundle.name + ".old")
    script = "\n".join([
        f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.3; done",
        f"rm -rf {shlex.quote(str(old))}",
        f"mv {shlex.quote(str(bundle))} {shlex.quote(str(old))} &&"
        f" mv {shlex.quote(str(new_app))} {shlex.quote(str(bundle))} &&"
        f" rm -rf {shlex.quote(str(old))}"
        f" || {{ [ -e {shlex.quote(str(bundle))} ] || mv {shlex.quote(str(old))} {shlex.quote(str(bundle))}; }}",
        f"open {shlex.quote(str(bundle))}",
    ])
    if os.access(bundle.parent, os.W_OK) and (not bundle.exists() or os.access(bundle, os.W_OK)):
        subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        # /Applications not writable for this user: ask for an administrator password.
        apple_script = (f'do shell script {json.dumps(script)} with administrator privileges '
                        f'with prompt "{APP_NAME} needs your password to install the update."')
        subprocess.Popen(["osascript", "-e", apple_script], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def restart_app() -> None:
    """Start a fresh copy of the app (the caller quits the current one)."""
    bundle = current_app_bundle() if sys.platform == "darwin" else None
    if bundle is not None:
        subprocess.Popen(["open", "-n", str(bundle)], start_new_session=True)
    elif getattr(sys, "frozen", False):
        subprocess.Popen([sys.executable], start_new_session=True)
    else:
        subprocess.Popen([sys.executable, "-m", "ytdownloader"], start_new_session=True)


def plain_notes(markdown: str) -> str:
    """Release notes for a plain text box: join wrapped lines, drop Markdown emphasis."""
    text = re.sub(r"\n[ \t]+(?=\S)", " ", markdown.strip())   # continuation lines
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^- ", "• ", text, flags=re.M)
    return re.sub(r"\n(?=• )", "\n\n", text)


def describe_size(num: int) -> str:
    return f"{num / 1e6:.0f} MB" if num < 1e9 else f"{num / 1e9:.2f} GB"

