"""The NVIDIA package for Windows: CUDA, cuDNN and TensorRT libraries (~1.5 GB).

They are not part of the app installer. The installer downloads the package when
it finds an NVIDIA GPU; the app can also download it later. The package is
versioned separately, so app updates only fetch it again when it changed.

Location: %ProgramData%\\YouTube Downloader\\nvidia\\<version>\\ (the installer makes
that folder writable for users), or %LOCALAPPDATA%\\YouTube Downloader\\nvidia\\<version>\\.
A folder counts as installed once it contains the `.complete` marker.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from . import APP_NAME, __version__

log = logging.getLogger(__name__)

INFO_FILE = "nvidia_pack.json"   # written into the app by scripts/build_windows.py
MARKER = ".complete"


class PackError(Exception):
    pass


def pack_info() -> dict | None:
    """The package this build needs: {"version", "sha256", "size", "parts": [{"url", "sha256", "size"}]}.

    The archive is split into parts because GitHub release files are limited to 2 GB;
    "sha256" and "size" are those of the whole archive (the parts joined together).
    """
    candidates = []
    if os.environ.get("YTD_NVIDIA_PACK_INFO"):
        candidates.append(Path(os.environ["YTD_NVIDIA_PACK_INFO"]))
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / INFO_FILE)
    for path in candidates:
        try:
            info = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if {"version", "sha256", "parts"} <= info.keys() and info["parts"]:
            return info
    return None


def roots() -> list[Path]:
    result = []
    if os.environ.get("YTD_NVIDIA_PACK_ROOT"):
        result.append(Path(os.environ["YTD_NVIDIA_PACK_ROOT"]))
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        result.append(Path(program_data) / APP_NAME / "nvidia")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        result.append(Path(local) / APP_NAME / "nvidia")
    return result


def pack_dir() -> Path | None:
    """Folder with the installed package's DLLs, if it is installed."""
    info = pack_info()
    if info is None:
        return None
    for root in roots():
        candidate = root / info["version"]
        if (candidate / MARKER).exists():
            return candidate
    return None


def needed() -> bool:
    """True if this build uses the package and it isn't installed yet."""
    return pack_info() is not None and pack_dir() is None


def _writable_root() -> Path:
    for root in roots():
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / f".write-test-{os.getpid()}"
            probe.write_text("")
            probe.unlink()
            return root
        except OSError:
            continue
    raise PackError("There is no folder this app may write the NVIDIA package to.")


def _download(info: dict, archive: Path, on_progress, cancel_event) -> None:
    """Download all parts, one after another, into one archive file."""
    total = int(info.get("size") or sum(int(p.get("size") or 0) for p in info["parts"]))
    done = 0
    with open(archive, "wb") as fh:
        for part in info["parts"]:
            digest = hashlib.sha256()
            request = urllib.request.Request(part["url"], headers={"User-Agent": f"YouTubeDownloader/{__version__}"})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    while chunk := response.read(1 << 20):
                        if cancel_event is not None and cancel_event.is_set():
                            raise PackError("Cancelled.")
                        fh.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if on_progress:
                            on_progress(done, total)
            except OSError as exc:
                raise PackError(f"Downloading the NVIDIA package failed: {exc}") from exc
            if digest.hexdigest() != part["sha256"].lower():
                raise PackError("The NVIDIA package download is damaged (checksum mismatch). Please try again.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 22):
            digest.update(chunk)
    return digest.hexdigest()


def _unpack(archive: Path, info: dict, root: Path, staging: Path) -> Path:
    if _sha256(archive) != info["sha256"].lower():
        raise PackError("The NVIDIA package is damaged (checksum mismatch). Please try again.")
    target = root / info["version"]
    with zipfile.ZipFile(archive) as z:
        for member in z.namelist():
            if ".." in Path(member).parts or Path(member).is_absolute() or ":" in member:
                raise PackError("The NVIDIA package contains unexpected paths.")
        z.extractall(staging)
    (staging / MARKER).write_text(info["version"])
    shutil.rmtree(target, ignore_errors=True)
    staging.replace(target)
    for old in root.iterdir():
        if old.is_dir() and old != target and not old.name.startswith("ytd-nvidia-"):
            shutil.rmtree(old, ignore_errors=True)
    log.info("Installed NVIDIA package %s in %s", info["version"], target)
    return target


def install(on_progress: Callable[[int, int], None] | None = None,
            cancel_event: threading.Event | None = None) -> Path:
    """Download, verify and unpack the package. Returns its folder."""
    info = pack_info()
    if info is None:
        raise PackError("This build does not use the NVIDIA package.")
    root = _writable_root()
    with tempfile.TemporaryDirectory(prefix="ytd-nvidia-", dir=root) as tmp:
        archive = Path(tmp) / "pack.zip"
        _download(info, archive, on_progress, cancel_event)
        return _unpack(archive, info, root, Path(tmp) / "unpacked")


def install_parts(parts: list[Path]) -> Path:
    """Install from parts that were already downloaded (by the Windows installer)."""
    info = pack_info()
    if info is None:
        raise PackError("This build does not use the NVIDIA package.")
    if len(parts) != len(info["parts"]):
        raise PackError(f"Expected {len(info['parts'])} part(s) of the NVIDIA package, got {len(parts)}.")
    root = _writable_root()
    with tempfile.TemporaryDirectory(prefix="ytd-nvidia-", dir=root) as tmp:
        if len(parts) == 1:
            archive = parts[0]
        else:
            archive = Path(tmp) / "pack.zip"
            with open(archive, "wb") as out:
                for part in parts:
                    with open(part, "rb") as fh:
                        shutil.copyfileobj(fh, out, 1 << 22)
        return _unpack(archive, info, root, Path(tmp) / "unpacked")
