"""Keep yt-dlp current without an app update.

YouTube changes often and old yt-dlp versions stop working, so the app downloads
the newest yt-dlp (plus the yt-dlp-ejs version it requires) from PyPI into its data
folder, checks it in a separate process and then uses it from the next start on.
If anything about the downloaded copy fails, the copy bundled with the app is used.

`activate()` must run before anything imports yt_dlp.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from . import __version__, tools
from .settings import support_dir

log = logging.getLogger(__name__)

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
PYPI_VERSION_JSON = "https://pypi.org/pypi/{name}/{version}/json"
USER_AGENT = f"YouTubeDownloader/{__version__}"
CHECK_INTERVAL = 24 * 3600
KEEP_VERSIONS = 2


class YtdlpUpdateError(Exception):
    pass


def store_dir() -> Path:
    return support_dir() / "yt-dlp"


def _state_file() -> Path:
    return store_dir() / "active.json"


def _read_state() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    _state_file().parent.mkdir(parents=True, exist_ok=True)
    tmp = _state_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(_state_file())


def _version_key(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text))


def bundled_version() -> str:
    """Version of the yt-dlp that ships with the app (read without importing it)."""
    from importlib import metadata

    try:
        return metadata.version("yt-dlp")
    except metadata.PackageNotFoundError:
        return "0"


def activate() -> str | None:
    """Put the downloaded yt-dlp first on sys.path if it is valid and newer than the
    bundled one. Returns its version."""
    if os.environ.get("YTD_NO_YTDLP_UPDATE") or "yt_dlp" in sys.modules:
        return None
    state = _read_state()
    path = Path(state.get("path", ""))
    if not state.get("version") or not (path / "yt_dlp" / "__init__.py").is_file():
        return None
    if _version_key(state["version"]) <= _version_key(bundled_version()):
        return None  # an app update brought a newer yt-dlp already
    sys.path.insert(0, str(path))
    try:
        import yt_dlp  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - fall back to the bundled copy
        log.warning("Downloaded yt-dlp %s does not load (%s); using the bundled one", state["version"], exc)
        sys.path.remove(str(path))
        for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith(("yt_dlp.", "yt_dlp_ejs"))]:
            del sys.modules[name]
        _write_state({**state, "path": "", "broken": state["version"]})
        return None
    log.info("Using downloaded yt-dlp %s from %s", state["version"], path)
    return state["version"]


def active_version() -> str:
    import yt_dlp

    return yt_dlp.version.__version__


def _get(url: str, timeout: float = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _wheel_info(data: dict) -> tuple[str, str, str]:
    """(version, wheel url, sha256) of the pure-Python wheel in a PyPI JSON response."""
    version = data["info"]["version"]
    for file in data.get("urls", []):
        if file.get("packagetype") == "bdist_wheel" and file["filename"].endswith("-py3-none-any.whl"):
            return version, file["url"], file["digests"]["sha256"]
    raise YtdlpUpdateError(f"No universal wheel for {data['info']['name']} {version}")


def _download_wheel(url: str, sha256: str) -> bytes:
    data = _get(url)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise YtdlpUpdateError(f"Checksum mismatch for {url}")
    return data


def _extract_wheel(data: bytes, target: Path) -> str:
    """Unpack a wheel's packages into `target`; returns its METADATA text."""
    import io

    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        metadata = ""
        for name in wheel.namelist():
            if name.endswith(".dist-info/METADATA"):
                metadata = wheel.read(name).decode("utf-8", "replace")
            if ".dist-info/" in name or ".data/" in name or name.endswith("/") or ".." in Path(name).parts:
                continue
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(wheel.read(name))
    return metadata


def required_ejs_version(metadata: str) -> str | None:
    match = re.search(r"^Requires-Dist: yt-dlp-ejs==([\w.]+); .*extra == .default.", metadata, re.M)
    return match.group(1) if match else None


def validator_command(path: Path, result_file: Path) -> list[str]:
    """How to start this app to check a yt-dlp copy in a separate process."""
    args = ["--validate-ytdlp", str(path), str(result_file)]
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        cli = exe.with_name("youtube-downloader-cli.exe")
        return [str(cli if cli.exists() else exe), *args]
    return [sys.executable, "-m", "ytdownloader", *args]


def validate_copy(path: Path, expected_version: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result_file = Path(tmp) / "result.txt"
        env = {**os.environ, "YTD_NO_YTDLP_UPDATE": "1"}
        src = str(Path(__file__).resolve().parents[1])
        if not getattr(sys, "frozen", False):
            env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(validator_command(path, result_file), capture_output=True, text=True,
                              timeout=180, env=env, check=False, **tools.no_window())
        found = result_file.read_text().strip() if result_file.exists() else ""
    # PyPI says "2026.8.19", yt-dlp itself says "2026.08.19": compare as numbers.
    if proc.returncode != 0 or not found or _version_key(found) != _version_key(expected_version):
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        raise YtdlpUpdateError(f"yt-dlp {expected_version} failed its check: {detail[0]}")


def run_validation(path: str, result_file: str) -> int:
    """Entry point for `--validate-ytdlp` (runs in a separate process)."""
    sys.path.insert(0, path)
    import yt_dlp
    from yt_dlp.extractor import gen_extractor_classes

    if not Path(yt_dlp.__file__).resolve().is_relative_to(Path(path).resolve()):
        print(f"wrong yt_dlp imported: {yt_dlp.__file__}", file=sys.stderr)
        return 1
    extractors = list(gen_extractor_classes())
    if not any(ie.IE_NAME == "youtube" for ie in extractors):
        print("YouTube extractor missing", file=sys.stderr)
        return 1
    with yt_dlp.YoutubeDL({"quiet": True}):
        pass
    import yt_dlp_ejs  # noqa: F401 - must be importable too

    Path(result_file).write_text(yt_dlp.version.__version__)
    return 0


def update(force: bool = False) -> str | None:
    """Install a newer yt-dlp if there is one. Returns the new version, or None."""
    state = _read_state()
    if not force and time.time() - state.get("last_check", 0) < CHECK_INTERVAL:
        return None
    _write_state({**state, "last_check": time.time()})
    try:
        version, url, sha = _wheel_info(json.loads(_get(PYPI_JSON.format(name="yt-dlp"))))
    except (OSError, ValueError, KeyError) as exc:
        raise YtdlpUpdateError(f"Could not check for a yt-dlp update: {exc}") from exc
    current = max(state.get("version") or "0", bundled_version(), key=_version_key)
    if _version_key(version) <= _version_key(current) or version == state.get("broken"):
        return None

    store_dir().mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{version}-", dir=store_dir()))
    try:
        metadata = _extract_wheel(_download_wheel(url, sha), staging)
        ejs = required_ejs_version(metadata)
        if ejs:
            ejs_data = json.loads(_get(PYPI_VERSION_JSON.format(name="yt-dlp-ejs", version=ejs)))
            _, ejs_url, ejs_sha = _wheel_info(ejs_data)
            _extract_wheel(_download_wheel(ejs_url, ejs_sha), staging)
        validate_copy(staging, version)
        final = store_dir() / version
        shutil.rmtree(final, ignore_errors=True)
        staging.rename(final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _write_state({"version": version, "path": str(final), "last_check": time.time()})
    _prune(keep={final.name})
    log.info("Installed yt-dlp %s (active from the next start)", version)
    return version


def _prune(keep: set[str]) -> None:
    versions = sorted((d for d in store_dir().iterdir() if d.is_dir() and re.fullmatch(r"[\d.]+", d.name)),
                      key=lambda d: _version_key(d.name), reverse=True)
    for directory in versions[KEEP_VERSIONS:]:
        if directory.name not in keep:
            shutil.rmtree(directory, ignore_errors=True)
    for leftover in store_dir().glob("*-*"):
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
