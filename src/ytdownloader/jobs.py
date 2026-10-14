"""The download queue: jobs, persistence and the worker that runs them one at a time.

Every download, MP3 and upscale is a Job. Jobs run strictly one after another, so
the GPU never upscales two videos at once. The queue is saved to disk and survives
a restart; a job that was running when the app quit goes back to "waiting".
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable

from . import downloader
from .downloader import Cancelled, DownloadFailed, Progress
from .formats import Mode
from .settings import support_dir
from .upscale import Content

log = logging.getLogger(__name__)

AUDIO_ONLY = 0  # tier value meaning "MP3"

WAITING, RUNNING, PAUSED, DONE, FAILED, CANCELLED = (
    "waiting", "running", "paused", "done", "failed", "cancelled")
FINISHED = (DONE, FAILED, CANCELLED)


@dataclass
class Job:
    title: str
    url: str = ""                # YouTube link (download jobs)
    source: str = ""             # local file (upscale-only jobs)
    tier: int = 2160             # max quality; AUDIO_ONLY for MP3
    mode: str = Mode.COMPATIBLE.value
    upscale_to: int = 0          # 0 = no upscaling
    upscale_content: str = Content.REALISTIC.value
    output_dir: str = ""
    cookies_from_browser: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = WAITING
    detail: str = ""
    fraction: float | None = None
    result: str = ""
    error: str = ""

    @property
    def is_upscale_only(self) -> bool:
        return bool(self.source)

    def describe(self) -> str:
        """Short settings summary for the queue list."""
        if self.is_upscale_only:
            return f"AI upscale → {self.upscale_to}p"
        if self.tier == AUDIO_ONLY:
            return "MP3"
        parts = [f"≤{self.tier}p", "original" if self.mode == Mode.ORIGINAL.value else "compatible"]
        if self.upscale_to:
            parts.append(f"AI → {self.upscale_to}p")
        return " · ".join(parts)

    def run(self, on_progress: Callable[[Progress], None], cancel_event: threading.Event) -> Path:
        cookies = self.cookies_from_browser or None
        out = Path(self.output_dir)
        if self.is_upscale_only:
            return downloader.upscale_file(Path(self.source), out, self.upscale_to, Content(self.upscale_content),
                                           on_progress=on_progress, cancel_event=cancel_event)
        if self.tier == AUDIO_ONLY:
            return downloader.download_audio(self.url, out, on_progress=on_progress,
                                             cancel_event=cancel_event, cookies_from_browser=cookies)
        return downloader.download(self.url, self.tier, Mode(self.mode), out, on_progress=on_progress,
                                   cancel_event=cancel_event, cookies_from_browser=cookies,
                                   upscale_to=self.upscale_to or None,
                                   upscale_content=Content(self.upscale_content))


# Events the queue reports to the GUI (called from the worker thread).
EventCallback = Callable[[str, Job | None], None]


class JobQueue:
    """Ordered jobs plus a single worker thread."""

    def __init__(self, on_event: EventCallback, path: Path | None = None):
        self.path = path or support_dir() / "queue.json"
        self.on_event = on_event
        self.jobs: list[Job] = []
        self.lock = threading.RLock()
        self.running = False              # is the queue started (vs. paused)?
        self.current: Job | None = None
        self.cancel_event = threading.Event()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.load()

    # --- persistence ----------------------------------------------------

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        known = {f.name for f in fields(Job)}
        for item in data.get("jobs", []):
            try:
                job = Job(**{k: v for k, v in item.items() if k in known})
            except TypeError:
                continue
            if job.status == RUNNING:
                job.status, job.detail, job.fraction = WAITING, "", None
            self.jobs.append(job)

    def save(self) -> None:
        with self.lock:
            data = {"jobs": [asdict(j) for j in self.jobs]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("Could not save the queue: %s", exc)

    # --- editing ----------------------------------------------------------

    def add(self, jobs: list[Job]) -> None:
        with self.lock:
            self.jobs.extend(jobs)
        self.save()
        self.on_event("changed", None)
        self._wake.set()

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return next((j for j in self.jobs if j.id == job_id), None)

    def remove(self, job_id: str) -> None:
        with self.lock:
            job = self.get(job_id)
            if job is None:
                return
            if job is self.current:
                self.cancel_event.set()  # stop it; the worker then just finishes
            self.jobs.remove(job)
        self.save()
        self.on_event("changed", None)

    def move(self, job_id: str, offset: int) -> None:
        with self.lock:
            job = self.get(job_id)
            if job is None:
                return
            index = self.jobs.index(job)
            new = max(0, min(len(self.jobs) - 1, index + offset))
            self.jobs.insert(new, self.jobs.pop(index))
        self.save()
        self.on_event("changed", None)

    def set_paused(self, job_id: str, paused: bool) -> None:
        with self.lock:
            job = self.get(job_id)
            if job is None or job.status in (RUNNING, DONE):
                return
            job.status = PAUSED if paused else WAITING
            job.detail = ""
        self.save()
        self.on_event("changed", job)
        self._wake.set()

    def retry(self, job_id: str) -> None:
        with self.lock:
            job = self.get(job_id)
            if job is None or job.status not in (FAILED, CANCELLED):
                return
            job.status, job.detail, job.error, job.fraction = WAITING, "", "", None
        self.save()
        self.on_event("changed", job)
        self._wake.set()

    def clear_finished(self) -> None:
        with self.lock:
            self.jobs = [j for j in self.jobs if j.status not in FINISHED]
        self.save()
        self.on_event("changed", None)

    def cancel_current(self) -> None:
        self.cancel_event.set()

    def pending(self) -> list[Job]:
        with self.lock:
            return [j for j in self.jobs if j.status == WAITING]

    @property
    def busy(self) -> bool:
        return self.current is not None

    # --- running ------------------------------------------------------------

    def start(self) -> None:
        self.running = True
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="job-queue", daemon=True)
            self._thread.start()
        self._wake.set()
        self.on_event("state", None)

    def pause(self) -> None:
        """Finish the current job, then don't start new ones."""
        self.running = False
        self.on_event("state", None)

    def shutdown(self) -> None:
        self._stop.set()
        self.cancel_event.set()
        self._wake.set()

    def _next_job(self) -> Job | None:
        with self.lock:
            return next((j for j in self.jobs if j.status == WAITING), None)

    def _worker(self) -> None:
        while not self._stop.is_set():
            job = self._next_job() if self.running else None
            if job is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            self._run(job)
            if not self.pending() and self.running:
                self.on_event("queue_finished", None)

    def _run(self, job: Job) -> None:
        self.cancel_event = threading.Event()
        with self.lock:
            self.current = job
            job.status, job.detail, job.fraction, job.error = RUNNING, "Starting…", None, ""
        self.save()
        self.on_event("started", job)

        def progress(p: Progress) -> None:
            job.fraction, job.detail = p.fraction, p.detail
            self.on_event("progress", job)

        try:
            result = job.run(progress, self.cancel_event)
            job.status, job.result, job.detail, job.fraction = DONE, str(result), "", 1.0
            event = "done"
        except Cancelled:
            job.status, job.detail, job.fraction = CANCELLED, "", None
            event = "cancelled"
        except DownloadFailed as exc:
            job.status, job.error, job.detail = FAILED, str(exc), ""
            event = "failed"
        except Exception as exc:  # noqa: BLE001 - keep the queue alive
            log.exception("Job %s failed", job.id)
            job.status, job.error, job.detail = FAILED, downloader.friendly_error(exc), ""
            event = "failed"
        finally:
            with self.lock:
                self.current = None
        self.save()
        self.on_event(event, job)
