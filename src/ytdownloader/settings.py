"""Small persisted preferences and the log file location."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import APP_NAME

log = logging.getLogger(__name__)


def support_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".config" / "youtube-downloader"


def log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP_NAME
    return support_dir() / "logs"


def default_download_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


@dataclass
class Settings:
    output_dir: str = ""
    mode: str = "compatible"
    cookies_from_browser: str = ""
    upscale_content: str = "realistic"

    @classmethod
    def load(cls) -> "Settings":
        path = support_dir() / "settings.json"
        settings = cls()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        known = {f.name for f in fields(cls)}
        for key, value in data.items():
            if key in known:
                setattr(settings, key, value)
        if not settings.output_dir or not Path(settings.output_dir).is_dir():
            settings.output_dir = str(default_download_dir())
        return settings

    def save(self) -> None:
        path = support_dir() / "settings.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), indent=2))
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)


def setup_logging(verbose: bool = False) -> Path | None:
    handlers: list[logging.Handler] = []
    log_file = None
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        log_file = log_dir() / "youtube-downloader.log"
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handlers.append(handler)
    except OSError:
        pass
    if sys.stderr is not None and verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers or [logging.NullHandler()],
    )
    return log_file
