"""Tk user interface."""

from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk

from . import APP_NAME, __version__, downloader, formats, tools, upscale
from .downloader import Cancelled, DownloadFailed, Progress, VideoInfo, format_bytes, format_duration
from .formats import Mode, tier_label
from .settings import Settings
from .upscale import Content

log = logging.getLogger(__name__)

URL_HINT = re.compile(r"^https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", re.I)

MODE_TEXT = {
    Mode.COMPATIBLE: (
        "Most compatible (H.264 / HEVC)",
        "Plays in QuickTime, Photos, iMovie and on iPhone. 1440p and 4K are converted "
        "to HEVC after downloading, which takes extra time.",
    ),
    Mode.ORIGINAL: (
        "Original stream (AV1 / VP9)",
        "Fastest, no conversion. 1440p and 4K use AV1 or VP9; if QuickTime won't play "
        "the file, use VLC or IINA.",
    ),
}
BROWSERS = ["None"] + [b.capitalize() for b in downloader.SUPPORTED_BROWSERS]

AUDIO_ONLY = 0  # quality-list value meaning "MP3 audio only"
AUDIO_LABEL = "Audio only (MP3)"
UPSCALE_OFF = "Off"
UPSCALE_LABELS = {upscale.target_label(t): t for t in upscale.UPSCALE_TARGETS}
CONTENT_LABELS = {
    "Real-life · best quality": Content.REALISTIC,
    "Real-life · fast": Content.FAST,
    "Animation / cartoons": Content.ANIMATION,
}
VIDEO_FILETYPES = [("Video files", "*.mp4 *.m4v *.mov *.mkv *.webm *.avi"), ("All files", "*")]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = Settings.load()
        self.events: queue.Queue = queue.Queue()
        self.info: VideoInfo | None = None
        self.cancel_event = threading.Event()
        self.busy = False
        self.last_file: Path | None = None
        self.tier_by_label: dict[str, int] = {}
        self.upscaler_available = upscale.is_available()
        self.caffeinate: subprocess.Popen | None = None

        root.title(APP_NAME)
        root.minsize(640, 0)
        self._build()
        self._set_state("idle")
        self._prefill_from_clipboard()
        self._warn_missing_tools()
        root.after(100, self._poll_events)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # --- layout -------------------------------------------------------------

    def _build(self) -> None:
        pad = {"padx": 16}
        outer = ttk.Frame(self.root, padding=(0, 16, 0, 16))
        outer.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        heading = font.nametofont("TkDefaultFont").copy()
        heading.configure(size=heading.cget("size") + 6, weight="bold")
        ttk.Label(outer, text=APP_NAME, font=heading).grid(row=0, column=0, sticky="w", **pad)
        ttk.Label(outer, text="Paste a YouTube link, pick a quality and download it as MP4 or MP3.")\
            .grid(row=1, column=0, sticky="w", pady=(2, 12), **pad)

        # URL row
        url_row = ttk.Frame(outer)
        url_row.grid(row=2, column=0, sticky="ew", **pad)
        url_row.columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", lambda e: self.fetch())
        self.url_var.trace_add("write", lambda *_: self._on_url_changed())
        self.fetch_button = ttk.Button(url_row, text="Get Video Info", command=self.fetch)
        self.fetch_button.grid(row=0, column=1, padx=(8, 0))

        # Video info
        self.title_var = tk.StringVar(value="")
        self.meta_var = tk.StringVar(value="")
        info = ttk.Frame(outer)
        info.grid(row=3, column=0, sticky="ew", pady=(12, 0), **pad)
        info.columnconfigure(0, weight=1)
        title_font = font.nametofont("TkDefaultFont").copy()
        title_font.configure(weight="bold")
        self.title_label = ttk.Label(info, textvariable=self.title_var, font=title_font,
                                     wraplength=600, justify="left")
        self.title_label.grid(row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.meta_var, foreground="gray").grid(row=1, column=0, sticky="w")
        info.bind("<Configure>", lambda e: self.title_label.configure(wraplength=max(e.width - 8, 200)))

        # Options
        options = ttk.LabelFrame(outer, text="Options", padding=12)
        options.grid(row=4, column=0, sticky="ew", pady=(12, 0), **pad)
        options.columnconfigure(1, weight=1)

        ttk.Label(options, text="Quality:").grid(row=0, column=0, sticky="w")
        self.quality_var = tk.StringVar()
        self.quality_box = ttk.Combobox(options, textvariable=self.quality_var, state="readonly", width=22)
        self.quality_box.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.quality_box.bind("<<ComboboxSelected>>", lambda e: self._on_quality_changed())

        ttk.Label(options, text="Video format:").grid(row=1, column=0, sticky="nw", pady=(10, 0))
        self.mode_var = tk.StringVar(value=self.settings.mode if self.settings.mode in
                                     {m.value for m in Mode} else Mode.COMPATIBLE.value)
        mode_frame = ttk.Frame(options)
        mode_frame.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(10, 0))
        mode_frame.columnconfigure(0, weight=1)
        self.mode_buttons = []
        descriptions = []
        for i, mode in enumerate(Mode):
            title, description = MODE_TEXT[mode]
            rb = ttk.Radiobutton(mode_frame, text=title, value=mode.value,
                                 variable=self.mode_var, command=self._update_estimate)
            rb.grid(row=2 * i, column=0, sticky="w")
            desc = ttk.Label(mode_frame, text=description, foreground="gray", wraplength=480,
                             justify="left")
            desc.grid(row=2 * i + 1, column=0, sticky="w", padx=(24, 0), pady=(0, 6))
            self.mode_buttons.append(rb)
            descriptions.append(desc)
        mode_frame.bind("<Configure>", lambda e: [
            d.configure(wraplength=max(e.width - 30, 200)) for d in descriptions])

        ttk.Label(options, text="AI upscale:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        upscale_row = ttk.Frame(options)
        upscale_row.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(4, 0))
        # Deliberately not remembered: upscaling takes long, so it must be chosen each time.
        self.upscale_var = tk.StringVar(value=UPSCALE_OFF)
        self.upscale_box = ttk.Combobox(upscale_row, textvariable=self.upscale_var, state="readonly",
                                        values=[UPSCALE_OFF, *UPSCALE_LABELS], width=16)
        self.upscale_box.grid(row=0, column=0)
        self.upscale_box.bind("<<ComboboxSelected>>", lambda e: self._update_estimate())
        ttk.Label(upscale_row, text="Model:").grid(row=0, column=1, padx=(12, 0))
        saved_content = next((k for k, v in CONTENT_LABELS.items()
                              if v.value == self.settings.upscale_content), "Real-life · best quality")
        self.content_var = tk.StringVar(value=saved_content)
        self.content_box = ttk.Combobox(upscale_row, textvariable=self.content_var, state="readonly",
                                        values=list(CONTENT_LABELS), width=22)
        self.content_box.grid(row=0, column=2, padx=(8, 0))
        upscale_hint = ("Uses Real-ESRGAN AI on your Mac's GPU to add detail to low-resolution videos. "
                        "\u201cBest quality\u201d is slow (a few minutes of video can take an hour or more); "
                        "\u201cfast\u201d is several times quicker with a little less fine detail."
                        if self.upscaler_available else "The AI upscaler is not included in this build.")
        self.upscale_hint = ttk.Label(options, text=upscale_hint, foreground="gray", wraplength=480,
                                      justify="left")
        self.upscale_hint.grid(row=3, column=1, sticky="w", padx=(8, 0))
        options.bind("<Configure>", lambda e: self.upscale_hint.configure(
            wraplength=max(e.width - 160, 200)))

        self.estimate_var = tk.StringVar()
        ttk.Label(options, textvariable=self.estimate_var, foreground="gray")\
            .grid(row=4, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        ttk.Label(options, text="Save to:").grid(row=5, column=0, sticky="w", pady=(10, 0))
        folder_row = ttk.Frame(options)
        folder_row.grid(row=5, column=1, sticky="ew", padx=(8, 0), pady=(10, 0))
        folder_row.columnconfigure(0, weight=1)
        self.folder_var = tk.StringVar(value=self.settings.output_dir)
        ttk.Entry(folder_row, textvariable=self.folder_var, state="readonly")\
            .grid(row=0, column=0, sticky="ew")
        self.folder_button = ttk.Button(folder_row, text="Change…", command=self.choose_folder)
        self.folder_button.grid(row=0, column=1, padx=(8, 0))

        ttk.Label(options, text="Use cookies from:").grid(row=6, column=0, sticky="w", pady=(10, 0))
        self.cookies_var = tk.StringVar(
            value=(self.settings.cookies_from_browser or "none").capitalize())
        self.cookies_box = ttk.Combobox(options, textvariable=self.cookies_var, values=BROWSERS,
                                        state="readonly", width=12)
        self.cookies_box.grid(row=6, column=1, sticky="w", padx=(8, 0), pady=(10, 0))
        ttk.Label(options, text="Only needed for age-restricted videos or when YouTube asks you to sign in.",
                  foreground="gray").grid(row=7, column=1, sticky="w", padx=(8, 0))

        # Progress
        progress = ttk.Frame(outer)
        progress.grid(row=5, column=0, sticky="ew", pady=(16, 0), **pad)
        progress.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress, mode="determinate", maximum=1000)
        self.progress.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.status_var = tk.StringVar(value="")
        ttk.Label(progress, textvariable=self.status_var).grid(row=1, column=0, sticky="w", pady=(6, 0))

        # Buttons
        bottom = ttk.Frame(outer)
        bottom.grid(row=6, column=0, sticky="ew", pady=(12, 0), **pad)
        bottom.columnconfigure(1, weight=1)
        self.upscale_file_button = ttk.Button(bottom, text="Upscale a Video File…",
                                              command=self.upscale_local_file)
        self.upscale_file_button.grid(row=0, column=0, sticky="w")
        buttons = ttk.Frame(bottom)
        buttons.grid(row=0, column=2, sticky="e")
        self.reveal_button = ttk.Button(buttons, text="Show in Finder", command=self.reveal)
        self.reveal_button.grid(row=0, column=0, padx=(0, 8))
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self.cancel)
        self.cancel_button.grid(row=0, column=1, padx=(0, 8))
        self.download_button = ttk.Button(buttons, text="Download", command=self.download, default="active")
        self.download_button.grid(row=0, column=2)

    # --- state --------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        self.state = state
        self.busy = state in ("fetching", "downloading")
        ready = state in ("ready", "done")
        self.url_entry.configure(state="disabled" if self.busy else "normal")
        self.fetch_button.configure(state="disabled" if self.busy else "normal")
        self.quality_box.configure(state="readonly" if ready else "disabled")
        audio = self._selected_tier() == AUDIO_ONLY
        for rb in self.mode_buttons:
            rb.configure(state="disabled" if self.busy or audio else "normal")
        upscale_state = "readonly" if self.upscaler_available and not self.busy and not audio else "disabled"
        self.upscale_box.configure(state=upscale_state)
        self.content_box.configure(state=upscale_state)
        self.upscale_file_button.configure(
            state="normal" if self.upscaler_available and not self.busy else "disabled")
        self.folder_button.configure(state="disabled" if self.busy else "normal")
        self.cookies_box.configure(state="disabled" if self.busy else "readonly")
        self.download_button.configure(state="normal" if ready else "disabled")
        self.cancel_button.configure(state="normal" if state == "downloading" else "disabled")
        self.reveal_button.configure(state="normal" if self.last_file and state == "done" else "disabled")
        if state == "fetching":
            self.progress.configure(mode="indeterminate")
            self.progress.start(15)
        elif state != "downloading":
            self.progress.stop()
            self.progress.configure(mode="determinate")

    def _on_url_changed(self) -> None:
        if self.info and self.url_var.get().strip() != self.info.url and not self.busy:
            if self.state in ("ready", "done"):
                self.info = None
                self.title_var.set("")
                self.meta_var.set("")
                self.estimate_var.set("")
                self.quality_box.configure(values=[])
                self.quality_var.set("")
                self._set_state("idle")

    def _prefill_from_clipboard(self) -> None:
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        if URL_HINT.match(text) and len(text) < 500:
            self.url_var.set(text)
        self.url_entry.focus_set()

    def _warn_missing_tools(self) -> None:
        missing = tools.discover().missing
        if missing:
            self.status_var.set(
                "Missing helper programs: " + ", ".join(missing)
                + ". Install them with Homebrew (brew install ffmpeg deno) when running from source."
            )

    def _cookies(self) -> str | None:
        value = self.cookies_var.get().lower()
        return None if value in ("", "none") else value

    def _mode(self) -> Mode:
        return Mode(self.mode_var.get())

    def _selected_tier(self) -> int | None:
        return self.tier_by_label.get(self.quality_var.get())

    def _upscale_target(self) -> int | None:
        return UPSCALE_LABELS.get(self.upscale_var.get())

    def _content(self) -> Content:
        return CONTENT_LABELS.get(self.content_var.get(), Content.REALISTIC)

    def _resting_state(self) -> str:
        return "ready" if self.info else "idle"

    # --- actions ------------------------------------------------------------

    def fetch(self) -> None:
        if self.busy:
            return
        url = self.url_var.get().strip()
        if not url:
            self.status_var.set("Paste a YouTube link first.")
            return
        self._set_state("fetching")
        self.status_var.set("Looking up video…")
        cookies = self._cookies()
        threading.Thread(target=self._fetch_worker, args=(url, cookies), daemon=True).start()

    def _fetch_worker(self, url: str, cookies: str | None) -> None:
        try:
            info = downloader.fetch_info(url, cookies)
            self.events.put(("info", info))
        except DownloadFailed as exc:
            self.events.put(("fetch_error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - show anything unexpected to the user
            log.exception("Fetching info failed")
            self.events.put(("fetch_error", downloader.friendly_error(exc)))

    def download(self) -> None:
        if self.busy or not self.info:
            return
        tier = self._selected_tier()
        if tier is None:
            return
        url, folder, cookies = self.info.url, Path(self.folder_var.get()), self._cookies()
        if tier == AUDIO_ONLY:
            job = lambda progress, cancel: downloader.download_audio(  # noqa: E731
                url, folder, on_progress=progress, cancel_event=cancel, cookies_from_browser=cookies)
        else:
            mode, target, content = self._mode(), self._upscale_target(), self._content()
            job = lambda progress, cancel: downloader.download(  # noqa: E731
                url, tier, mode, folder, on_progress=progress, cancel_event=cancel,
                cookies_from_browser=cookies, upscale_to=target, upscale_content=content)
        self._start_job(job)

    def upscale_local_file(self) -> None:
        if self.busy:
            return
        target = self._upscale_target()
        if target is None:
            messagebox.showinfo(APP_NAME, "First choose a resolution under \u201cAI upscale\u201d "
                                          "(for example 1080p), then pick the video file.")
            return
        source = filedialog.askopenfilename(title="Choose a video to upscale", filetypes=VIDEO_FILETYPES)
        if not source:
            return
        folder, content = Path(self.folder_var.get()), self._content()
        self._start_job(lambda progress, cancel: downloader.upscale_file(
            Path(source), folder, target, content, on_progress=progress, cancel_event=cancel))

    def _start_job(self, job) -> None:
        self._save_settings()
        self.cancel_event = threading.Event()
        self.last_file = None
        self._set_state("downloading")
        self.progress.configure(value=0)
        self.status_var.set("Starting…")
        self._keep_awake(True)
        threading.Thread(target=self._job_worker, args=(job, self.cancel_event), daemon=True).start()

    def _job_worker(self, job, cancel_event) -> None:
        try:
            path = job(lambda p: self.events.put(("progress", p)), cancel_event)
            self.events.put(("done", path))
        except Cancelled:
            self.events.put(("cancelled", None))
        except DownloadFailed as exc:
            self.events.put(("download_error", str(exc)))
        except Exception as exc:  # noqa: BLE001
            log.exception("Job failed")
            self.events.put(("download_error", downloader.friendly_error(exc)))

    def _keep_awake(self, on: bool) -> None:
        """Stop the Mac from sleeping during long downloads and upscales."""
        if on and sys.platform == "darwin" and self.caffeinate is None:
            try:
                self.caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            except OSError:
                self.caffeinate = None
        elif not on and self.caffeinate is not None:
            self.caffeinate.terminate()
            self.caffeinate = None

    def cancel(self) -> None:
        if self.state == "downloading":
            self.cancel_event.set()
            self.status_var.set("Cancelling…")
            self.cancel_button.configure(state="disabled")

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.folder_var.get(), title="Choose download folder")
        if folder:
            self.folder_var.set(folder)
            self._save_settings()

    def reveal(self) -> None:
        if not self.last_file:
            return
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(self.last_file)], check=False)
        else:
            subprocess.run(["xdg-open", str(self.last_file.parent)], check=False)

    def _save_settings(self) -> None:
        self.settings.output_dir = self.folder_var.get()
        self.settings.mode = self.mode_var.get()
        self.settings.cookies_from_browser = self._cookies() or ""
        self.settings.upscale_content = self._content().value
        self.settings.save()

    def _on_close(self) -> None:
        if self.state == "downloading":
            if not messagebox.askyesno(APP_NAME, "A download is in progress. Cancel it and quit?"):
                return
            self.cancel_event.set()
        self._keep_awake(False)
        self._save_settings()
        self.root.destroy()

    # --- events from worker threads ----------------------------------------

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                getattr(self, f"_on_{kind}")(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _on_info(self, info: VideoInfo) -> None:
        self.info = info
        self.url_var.set(info.url)
        self.title_var.set(info.title)
        meta = [m for m in (info.uploader, format_duration(info.duration) if info.duration else None) if m]
        self.meta_var.set(" · ".join(meta))
        self.tier_by_label = {tier_label(t): t for t in info.tiers}
        self.tier_by_label[AUDIO_LABEL] = AUDIO_ONLY
        self.quality_box.configure(values=list(self.tier_by_label))
        self.quality_var.set(tier_label(info.tiers[0]))  # always start at the best quality
        self._set_state("ready")
        best = info.tiers[0]
        self.status_var.set(f"Ready. Best available quality: {tier_label(best)}.")
        self._update_estimate()

    def _on_quality_changed(self) -> None:
        self._set_state(self.state)  # enable/disable the video-only options
        self._update_estimate()

    def _update_estimate(self) -> None:
        if not self.info:
            self.estimate_var.set("")
            return
        tier = self._selected_tier()
        if tier is None:
            return
        if tier == AUDIO_ONLY:
            try:
                source = formats.select_audio(self.info.formats)
            except formats.NoSuitableFormat:
                self.estimate_var.set("")
                return
            size = source.get("filesize") or source.get("filesize_approx")
            parts = ["MP3, best quality (VBR ~245 kbps), with title and cover art"]
            if size:
                parts.append(f"download ≈ {format_bytes(size)}")
            self.estimate_var.set(" · ".join(parts))
            return
        try:
            selection = self.info.selection_for(tier, self._mode())
        except Exception:  # noqa: BLE001
            self.estimate_var.set("")
            return
        parts = []
        size = selection.estimated_size
        if size:
            parts.append(f"Download size ≈ {format_bytes(size)}")
        codec = (selection.video.get("vcodec") or "?").split(".")[0]
        fps = selection.video.get("fps")
        parts.append(f"stream: {codec}{f' {fps:.0f} fps' if fps else ''}")
        target = self._upscale_target()
        source_tier = selection.tier or tier
        if target and source_tier < target:
            parts.append(f"then AI-upscaled to {target}p (slow)")
        elif self._mode() is Mode.COMPATIBLE and selection.needs_conversion:
            parts.append("will be converted to HEVC")
        if target and source_tier >= target:
            parts.append(f"already ≥ {target}p, so no upscaling")
        self.estimate_var.set(" · ".join(parts))

    def _on_fetch_error(self, message: str) -> None:
        self._set_state("idle")
        self.status_var.set("Could not get video info.")
        messagebox.showerror(APP_NAME, message)

    def _on_progress(self, p: Progress) -> None:
        if self.state != "downloading":
            return
        if p.fraction is None:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(15)
        else:
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress.configure(value=p.fraction * 1000)
        if not self.cancel_event.is_set():
            self.status_var.set(p.detail)

    def _on_done(self, path: Path) -> None:
        self._keep_awake(False)
        self.last_file = path
        self._set_state("done")
        self.progress.configure(value=1000)
        self.status_var.set(f"Saved “{path.name}”")
        self.root.bell()

    def _on_cancelled(self, _payload) -> None:
        self._keep_awake(False)
        self._set_state(self._resting_state())
        self.progress.configure(value=0)
        self.status_var.set("Cancelled.")

    def _on_download_error(self, message: str) -> None:
        self._keep_awake(False)
        self._set_state(self._resting_state())
        self.progress.configure(value=0)
        self.status_var.set("Failed.")
        messagebox.showerror(APP_NAME, message)


def run() -> None:
    root = tk.Tk()
    root.title(APP_NAME)
    if sys.platform == "darwin":
        # Standard "About" item in the application menu.
        root.createcommand("tkAboutDialog", lambda: messagebox.showinfo(
            APP_NAME, f"{APP_NAME} {__version__}\n\nPowered by yt-dlp, ffmpeg and Real-ESRGAN."))
    App(root)
    root.update_idletasks()
    root.geometry(f"+{max((root.winfo_screenwidth() - root.winfo_width()) // 2, 0)}+120")
    root.lift()
    root.focus_force()
    if os.environ.get("YTD_SMOKE_TEST"):
        # Used by CI to prove the bundled Tk can open a window.
        root.after(2000, root.destroy)
    root.mainloop()
