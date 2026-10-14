"""Locate the external helper programs (ffmpeg, ffprobe, deno).

The packaged macOS app ships these inside the bundle. When running from source
we fall back to whatever is installed on the system. GUI apps on macOS do not
inherit the shell PATH, so the usual Homebrew locations are searched as well.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

EXTRA_SEARCH_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    str(Path.home() / ".deno" / "bin"),
)


def bundled_bin_dir() -> Path | None:
    """Directory holding binaries shipped inside the frozen app, if any."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / "bin"
        if candidate.is_dir():
            return candidate
    # Running from a source checkout after scripts/fetch_tools.sh was used.
    candidate = Path(__file__).resolve().parents[2] / "vendor" / "bin"
    if candidate.is_dir():
        return candidate
    return None


def find_executable(name: str) -> str | None:
    env_override = os.environ.get("YTD_" + name.upper().replace("-", "_"))
    if env_override and os.access(env_override, os.X_OK):
        return env_override

    bundled = bundled_bin_dir()
    if bundled:
        path = bundled / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)

    found = shutil.which(name)
    if found:
        return found

    for directory in EXTRA_SEARCH_DIRS:
        path = Path(directory) / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


@dataclass(frozen=True)
class Toolset:
    ffmpeg: str | None
    ffprobe: str | None
    deno: str | None

    @property
    def missing(self) -> list[str]:
        return [name for name in ("ffmpeg", "ffprobe", "deno") if getattr(self, name) is None]


@lru_cache(maxsize=1)
def discover() -> Toolset:
    return Toolset(
        ffmpeg=find_executable("ffmpeg"),
        ffprobe=find_executable("ffprobe"),
        deno=find_executable("deno"),
    )


@lru_cache(maxsize=1)
def ffmpeg_encoders(ffmpeg: str) -> frozenset[str]:
    """Names of the video/audio encoders compiled into the given ffmpeg."""
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        # Lines look like: " V....D libx264   libx264 H.264 / AVC ..."
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


def version_line(executable: str, flag: str = "-version") -> str:
    try:
        out = subprocess.run(
            [executable, flag], capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error: {exc}"
    text = (out.stdout or out.stderr).strip()
    return text.splitlines()[0] if text else f"exit code {out.returncode}"
