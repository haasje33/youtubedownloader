"""Locate the external helper programs (ffmpeg, ffprobe, deno, realesrgan).

The packaged apps ship these inside the bundle (macOS) or install folder
(Windows). When running from source we fall back to whatever is installed on the
system. GUI apps on macOS do not inherit the shell PATH, so the usual Homebrew
locations are searched as well.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""

EXTRA_SEARCH_DIRS = () if IS_WINDOWS else (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    str(Path.home() / ".deno" / "bin"),
)


def no_window() -> dict:
    """subprocess keyword arguments that stop a console window from flashing up
    on Windows whenever the GUI app starts ffmpeg and friends."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


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
        path = bundled / (name + EXE_SUFFIX)
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
            capture_output=True, text=True, timeout=30, check=False, **no_window(),
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
            **no_window(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error: {exc}"
    text = (out.stdout or out.stderr).strip()
    return text.splitlines()[0] if text else f"exit code {out.returncode}"


_SOFTWARE_ENCODERS = {"libx264", "libx265"}
_encoder_cache: dict[tuple[str, str], bool] = {}


def encoder_works(ffmpeg: str, video_args: list[str]) -> bool:
    """Whether a (hardware) encoder actually works on this computer.

    ffmpeg builds include NVENC, Quick Sync and AMF, but they only work with the
    matching GPU and driver, so each is tried once on a tiny test clip.
    """
    name = video_args[video_args.index("-c:v") + 1]
    if name in _SOFTWARE_ENCODERS:
        return True
    key = (ffmpeg, name)
    if key not in _encoder_cache:
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=640x360:rate=30:duration=0.2",
                 *video_args, "-pix_fmt", "yuv420p", "-f", "null", "-"],
                capture_output=True, text=True, timeout=60, check=False, **no_window(),
            )
            _encoder_cache[key] = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _encoder_cache[key] = False
    return _encoder_cache[key]
