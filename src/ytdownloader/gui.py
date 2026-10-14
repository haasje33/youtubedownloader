"""Tk user interface."""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk

from . import (APP_NAME, __version__, downloader, formats, jobs, notify, nvidia_pack, preview, tools,
               updater, upscale, ytdlp_update)
from .downloader import DownloadFailed, PlaylistInfo, VideoInfo, format_bytes, format_duration
from .formats import Mode, tier_label
from .jobs import AUDIO_ONLY, Job
from .settings import Settings
from .upscale import Content

log = logging.getLogger(__name__)

URL_HINT = re.compile(r"^https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", re.I)

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

if IS_WINDOWS:
    MODE_TEXT = {
        Mode.COMPATIBLE: ("Most compatible (H.264)",
                          "Plays everywhere; 1440p and 4K are converted to H.264 (on the graphics card when possible)."),
        Mode.ORIGINAL: ("Original stream (AV1 / VP9)",
                        "Fastest, no conversion; plays in Windows 11 and VLC, not in some older editors."),
    }
    REVEAL_TEXT = "Show in Explorer"
else:
    MODE_TEXT = {
        Mode.COMPATIBLE: ("Most compatible (H.264 / HEVC)",
                          "Plays in QuickTime, Photos, iMovie and on iPhone; 1440p and 4K are converted to HEVC."),
        Mode.ORIGINAL: ("Original stream (AV1 / VP9)",
                        "Fastest, no conversion; if QuickTime won't play it, use VLC or IINA."),
    }
    REVEAL_TEXT = "Show in Finder" if IS_MAC else "Show in Folder"
BROWSERS = ["None"] + [b.capitalize() for b in downloader.SUPPORTED_BROWSERS]

AUDIO_LABEL = "Audio only (MP3)"
PLAYLIST_TIERS = (2160, 1440, 1080, 720, 480, 360)
UPSCALE_OFF = "Off"
UPSCALE_LABELS = {upscale.target_label(t): t for t in upscale.UPSCALE_TARGETS}
CONTENT_LABELS = {
    "Real-life · best quality": Content.REALISTIC,
    "Real-life · fast": Content.FAST,
    "Animation / cartoons": Content.ANIMATION,
}
CONTENT_NAMES = {v: k for k, v in CONTENT_LABELS.items()}
VIDEO_FILETYPES = [("Video files", "*.mp4 *.m4v *.mov *.mkv *.webm *.avi"), ("All files", "*")]
STATUS_TEXT = {jobs.WAITING: "Waiting", jobs.PAUSED: "Paused", jobs.DONE: "Done",
               jobs.FAILED: "Failed", jobs.CANCELLED: "Cancelled"}


def reveal_in_file_manager(path: Path) -> None:
    if IS_WINDOWS:
        subprocess.run(["explorer", "/select,", str(path)], check=False)
    elif IS_MAC:
        subprocess.run(["open", "-R", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path.parent)], check=False)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = Settings.load()
        self.events: queue.Queue = queue.Queue()
        self.info: VideoInfo | None = None
        self.fetching = False
        self.tier_by_label: dict[str, int] = {}
        self.upscaler_available = upscale.is_available()
        self.caffeinate: subprocess.Popen | None = None
        self.engine_status = None
        self.pending_update: updater.Release | None = None
        self.update_window: UpdateProgress | None = None
        self.ytdlp_retry_offered = False
        self.finished_this_run = 0
        self.queue = jobs.JobQueue(lambda event, job: self.events.put(("job", (event, job.id if job else None))))

        root.title(APP_NAME)
        root.minsize(720, 640)
        self._build()
        self._build_menu()
        self._refresh_queue()
        self._set_controls()
        self._prefill_from_clipboard()
        if IS_WINDOWS and self.upscaler_available:
            self._check_engine()
        self._warn_missing_tools()
        if self.queue.pending():
            self.queue_status_var.set(f"{len(self.queue.pending())} item(s) waiting from last time. "
                                      "Press Start to continue.")
        root.after(100, self._poll_events)
        root.after(2500, self._startup_checks)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    @property
    def busy(self) -> bool:
        """Something is running that an update must not interrupt."""
        return self.queue.busy or self.fetching

    # --- layout -------------------------------------------------------------

    def _build(self) -> None:
        pad = {"padx": 16}
        outer = ttk.Frame(self.root, padding=(0, 12, 0, 12))
        outer.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(5, weight=1)

        heading = font.nametofont("TkDefaultFont").copy()
        heading.configure(size=heading.cget("size") + 6, weight="bold")
        ttk.Label(outer, text=APP_NAME, font=heading).grid(row=0, column=0, sticky="w", **pad)

        # URL row
        url_row = ttk.Frame(outer)
        url_row.grid(row=1, column=0, sticky="ew", pady=(8, 0), **pad)
        url_row.columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", lambda e: self.fetch())
        self.url_var.trace_add("write", lambda *_: self._on_url_changed())
        self.fetch_button = ttk.Button(url_row, text="Get Info", command=self.fetch)
        self.fetch_button.grid(row=0, column=1, padx=(8, 0))

        # Video info
        self.title_var = tk.StringVar(value="Paste a video, playlist or channel link.")
        self.meta_var = tk.StringVar(value="")
        info = ttk.Frame(outer)
        info.grid(row=2, column=0, sticky="ew", pady=(8, 0), **pad)
        info.columnconfigure(0, weight=1)
        title_font = font.nametofont("TkDefaultFont").copy()
        title_font.configure(weight="bold")
        self.title_label = ttk.Label(info, textvariable=self.title_var, font=title_font, justify="left")
        self.title_label.grid(row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.meta_var, foreground="gray").grid(row=1, column=0, sticky="w")
        info.bind("<Configure>", lambda e: self.title_label.configure(wraplength=max(e.width - 8, 200)))

        # Options
        options = ttk.LabelFrame(outer, text="Options", padding=(12, 8))
        options.grid(row=3, column=0, sticky="ew", pady=(8, 0), **pad)
        options.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(options, text="Quality:").grid(row=row, column=0, sticky="w")
        self.quality_var = tk.StringVar()
        self.quality_box = ttk.Combobox(options, textvariable=self.quality_var, state="readonly", width=22)
        self.quality_box.grid(row=row, column=1, sticky="w", padx=(8, 0))
        self.quality_box.bind("<<ComboboxSelected>>", lambda e: self._on_quality_changed())

        row += 1
        ttk.Label(options, text="Video format:").grid(row=row, column=0, sticky="nw", pady=(6, 0))
        self.mode_var = tk.StringVar(value=self.settings.mode if self.settings.mode in
                                     {m.value for m in Mode} else Mode.COMPATIBLE.value)
        mode_frame = ttk.Frame(options)
        mode_frame.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=(6, 0))
        mode_frame.columnconfigure(0, weight=1)
        self.mode_buttons = []
        descriptions = []
        for i, mode in enumerate(Mode):
            title, description = MODE_TEXT[mode]
            rb = ttk.Radiobutton(mode_frame, text=title, value=mode.value,
                                 variable=self.mode_var, command=self._update_estimate)
            rb.grid(row=2 * i, column=0, sticky="w")
            desc = ttk.Label(mode_frame, text=description, foreground="gray", justify="left")
            desc.grid(row=2 * i + 1, column=0, sticky="w", padx=(24, 0), pady=(0, 2))
            self.mode_buttons.append(rb)
            descriptions.append(desc)
        mode_frame.bind("<Configure>", lambda e: [
            d.configure(wraplength=max(e.width - 30, 200)) for d in descriptions])

        row += 1
        ttk.Label(options, text="AI upscale:").grid(row=row, column=0, sticky="w", pady=(6, 0))
        upscale_row = ttk.Frame(options)
        upscale_row.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
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
        self.preview_button = ttk.Button(upscale_row, text="Preview…", command=self.open_preview)
        self.preview_button.grid(row=0, column=3, padx=(8, 0))

        row += 1
        hint_row = ttk.Frame(options)
        hint_row.grid(row=row, column=1, sticky="ew", padx=(8, 0))
        hint_row.columnconfigure(0, weight=1)
        if not self.upscaler_available:
            hint = "The AI upscaler is not included in this build."
        elif IS_WINDOWS:
            hint = self._windows_upscale_hint("checking your graphics card…")
        else:
            hint = ("Real-ESRGAN AI on your Mac's GPU. On an M1: best quality ≈ 0.3 frames/s (hours for a few "
                    "minutes of video), fast ≈ 6 frames/s, animation ≈ 12 frames/s. Use Preview to compare.")
        self.upscale_hint = ttk.Label(hint_row, text=hint, foreground="gray", justify="left")
        self.upscale_hint.grid(row=0, column=0, sticky="w")
        self.pack_button = ttk.Button(hint_row, text="Download NVIDIA support…", command=self.download_nvidia_pack)
        hint_row.bind("<Configure>", lambda e: self.upscale_hint.configure(wraplength=max(e.width - 20, 200)))

        row += 1
        self.estimate_var = tk.StringVar()
        ttk.Label(options, textvariable=self.estimate_var, foreground="gray")\
            .grid(row=row, column=1, sticky="w", padx=(8, 0), pady=(4, 0))

        row += 1
        ttk.Label(options, text="Save to:").grid(row=row, column=0, sticky="w", pady=(6, 0))
        folder_row = ttk.Frame(options)
        folder_row.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=(6, 0))
        folder_row.columnconfigure(0, weight=1)
        self.folder_var = tk.StringVar(value=self.settings.output_dir)
        ttk.Entry(folder_row, textvariable=self.folder_var, state="readonly").grid(row=0, column=0, sticky="ew")
        ttk.Button(folder_row, text="Change…", command=self.choose_folder).grid(row=0, column=1, padx=(8, 0))

        row += 1
        ttk.Label(options, text="Cookies from:").grid(row=row, column=0, sticky="w", pady=(6, 0))
        extra = ttk.Frame(options)
        extra.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        self.cookies_var = tk.StringVar(value=(self.settings.cookies_from_browser or "none").capitalize())
        ttk.Combobox(extra, textvariable=self.cookies_var, values=BROWSERS, state="readonly", width=10)\
            .grid(row=0, column=0)
        ttk.Label(extra, text="(age-restricted videos, sign-in checks)", foreground="gray")\
            .grid(row=0, column=1, padx=(6, 16))
        self.notify_var = tk.BooleanVar(value=self.settings.notifications)
        ttk.Checkbutton(extra, text="Notify me when items finish", variable=self.notify_var,
                        command=self._save_settings).grid(row=0, column=2)

        # Add buttons
        actions = ttk.Frame(outer)
        actions.grid(row=4, column=0, sticky="ew", pady=(8, 0), **pad)
        actions.columnconfigure(1, weight=1)
        self.upscale_file_button = ttk.Button(actions, text="Upscale a Video File…", command=self.upscale_local_file)
        self.upscale_file_button.grid(row=0, column=0, sticky="w")
        self.add_button = ttk.Button(actions, text="Add to Queue", command=self.add_to_queue, default="active")
        self.add_button.grid(row=0, column=2, sticky="e")

        # Queue
        box = ttk.LabelFrame(outer, text="Queue", padding=(12, 8))
        box.grid(row=5, column=0, sticky="nsew", pady=(10, 0), **pad)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(box, columns=("settings", "status"), height=6, selectmode="browse")
        self.tree.heading("#0", text="Title")
        self.tree.heading("settings", text="Settings")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=300, stretch=True)
        self.tree.column("settings", width=240, stretch=False)
        self.tree.column("status", width=100, stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._set_controls())
        self.tree.bind("<Double-1>", lambda e: self._open_selected())

        self.progress = ttk.Progressbar(box, mode="determinate", maximum=1000)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.queue_status_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.queue_status_var).grid(row=2, column=0, columnspan=2, sticky="w",
                                                                pady=(4, 0))
        controls = ttk.Frame(box)
        controls.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        controls.columnconfigure(6, weight=1)
        self.start_button = ttk.Button(controls, text="Start", command=self.toggle_queue)
        self.start_button.grid(row=0, column=0)
        self.cancel_button = ttk.Button(controls, text="Cancel Current", command=self.queue.cancel_current)
        self.cancel_button.grid(row=0, column=1, padx=(6, 0))
        self.up_button = ttk.Button(controls, text="↑", width=3, command=lambda: self._move(-1))
        self.up_button.grid(row=0, column=2, padx=(12, 0))
        self.down_button = ttk.Button(controls, text="↓", width=3, command=lambda: self._move(1))
        self.down_button.grid(row=0, column=3, padx=(4, 0))
        self.item_button = ttk.Button(controls, text="Pause Item", command=self._toggle_item)
        self.item_button.grid(row=0, column=4, padx=(6, 0))
        self.remove_button = ttk.Button(controls, text="Remove", command=self._remove)
        self.remove_button.grid(row=0, column=5, padx=(6, 0))
        self.clear_button = ttk.Button(controls, text="Clear Finished", command=self.queue.clear_finished)
        self.clear_button.grid(row=0, column=7)
        self.reveal_button = ttk.Button(controls, text=REVEAL_TEXT, command=self._reveal_selected)
        self.reveal_button.grid(row=0, column=8, padx=(6, 0))

        self.status_var = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.status_var, foreground="gray").grid(row=6, column=0, sticky="w",
                                                                               pady=(6, 0), **pad)

    # --- state --------------------------------------------------------------

    def _set_controls(self) -> None:
        loaded = self.info is not None
        audio = self._selected_tier() == AUDIO_ONLY
        self.url_entry.configure(state="disabled" if self.fetching else "normal")
        self.fetch_button.configure(state="disabled" if self.fetching else "normal")
        self.quality_box.configure(state="readonly" if loaded else "disabled")
        for rb in self.mode_buttons:
            rb.configure(state="disabled" if audio else "normal")
        upscale_state = "readonly" if self.upscaler_available and not audio else "disabled"
        self.upscale_box.configure(state=upscale_state)
        self.content_box.configure(state=upscale_state)
        self.preview_button.configure(state="normal" if self.upscaler_available and not audio else "disabled")
        self.upscale_file_button.configure(state="normal" if self.upscaler_available else "disabled")
        self.add_button.configure(state="normal" if loaded and not self.fetching else "disabled")

        job = self._selected_job()
        self.start_button.configure(text="Pause Queue" if self.queue.running else "Start")
        self.cancel_button.configure(state="normal" if self.queue.busy else "disabled")
        movable = job is not None and job.status != jobs.RUNNING
        self.up_button.configure(state="normal" if movable else "disabled")
        self.down_button.configure(state="normal" if movable else "disabled")
        if job is not None and job.status in (jobs.FAILED, jobs.CANCELLED):
            self.item_button.configure(text="Retry", state="normal")
        elif job is not None and job.status == jobs.PAUSED:
            self.item_button.configure(text="Resume Item", state="normal")
        else:
            self.item_button.configure(text="Pause Item",
                                       state="normal" if job is not None and job.status == jobs.WAITING else "disabled")
        self.remove_button.configure(state="normal" if job is not None else "disabled")
        done = job is not None and job.status == jobs.DONE and job.result and Path(job.result).exists()
        self.reveal_button.configure(state="normal" if done else "disabled")

    def _on_url_changed(self) -> None:
        if self.info and self.url_var.get().strip() != self.info.url and not self.fetching:
            self.info = None
            self.title_var.set("Paste a video, playlist or channel link.")
            self.meta_var.set("")
            self.estimate_var.set("")
            self.quality_box.configure(values=[])
            self.quality_var.set("")
            self._set_controls()

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
            self.status_var.set("Missing helper programs: " + ", ".join(missing)
                                + ". Install them with Homebrew (brew install ffmpeg deno) when running from source.")

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

    def _save_settings(self) -> None:
        self.settings.output_dir = self.folder_var.get()
        self.settings.mode = self.mode_var.get()
        self.settings.cookies_from_browser = self._cookies() or ""
        self.settings.upscale_content = self._content().value
        self.settings.notifications = bool(self.notify_var.get())
        self.settings.save()

    # --- NVIDIA engine (Windows) ----------------------------------------------

    def _windows_upscale_hint(self, engine: str) -> str:
        return (f"Real-ESRGAN AI adds detail to low-resolution videos. AI engine: {engine}. "
                "Use Preview to compare the models and see how long it will take.")

    def _check_engine(self) -> None:
        def work() -> None:
            from . import onnx_engine

            onnx_engine.cuda_status.cache_clear()
            status = onnx_engine.cuda_status() if upscale.preferred_engine() != "ncnn" or \
                nvidia_pack.needed() else None
            self.events.put(("engine", (upscale.engine_description(), status)))
        threading.Thread(target=work, daemon=True).start()

    def _on_engine(self, payload) -> None:
        description, status = payload
        self.engine_status = status
        if status is not None and status.needs_pack:
            info = nvidia_pack.pack_info() or {}
            size = f" ({updater.describe_size(int(info['size']))})" if info.get("size") else ""
            self.upscale_hint.configure(text=self._windows_upscale_hint(
                f"Real-ESRGAN on Vulkan for now; {status.gpu or 'your NVIDIA GPU'} can be several times "
                f"faster with NVIDIA support{size}"))
            self.pack_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        else:
            self.upscale_hint.configure(text=self._windows_upscale_hint(description))
            self.pack_button.grid_remove()

    def download_nvidia_pack(self) -> None:
        if self.queue.busy:
            messagebox.showinfo(APP_NAME, "Please wait until the queue has finished, or pause it first.")
            return
        PackDownload(self)

    def _on_pack_progress(self, payload) -> None:
        if isinstance(self.pack_window, PackDownload):
            self.pack_window.show_progress(*payload)

    def _on_pack_done(self, error: str | None) -> None:
        if isinstance(self.pack_window, PackDownload):
            self.pack_window.destroy()
        self.pack_window = None
        if error:
            messagebox.showerror(APP_NAME, error)
        self._check_engine()

    pack_window = None

    # --- adding to the queue ------------------------------------------------

    def fetch(self) -> None:
        if self.fetching:
            return
        url = self.url_var.get().strip()
        if not url:
            self.status_var.set("Paste a YouTube link first.")
            return
        self.fetching = True
        self._set_controls()
        self.title_var.set("Looking up…")
        self.meta_var.set("")
        cookies = self._cookies()
        threading.Thread(target=self._fetch_worker, args=(url, cookies), daemon=True).start()

    def _fetch_worker(self, url: str, cookies: str | None) -> None:
        try:
            self.events.put(("fetched", downloader.fetch(url, cookies)))
        except DownloadFailed as exc:
            self.events.put(("fetch_error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - show anything unexpected to the user
            log.exception("Fetching info failed")
            self.events.put(("fetch_error", downloader.friendly_error(exc)))

    def _on_fetched(self, result) -> None:
        self.fetching = False
        if isinstance(result, PlaylistInfo):
            self.info = None
            self.quality_box.configure(values=[])
            self.quality_var.set("")
            self.estimate_var.set("")
            self.title_var.set(result.title)
            self.meta_var.set(f"{len(result.entries)} videos" + (f" · {result.uploader}" if result.uploader else ""))
            self._set_controls()
            PlaylistDialog(self, result)
            return
        info: VideoInfo = result
        self.info = info
        self.url_var.set(info.url)
        self.title_var.set(info.title)
        meta = [m for m in (info.uploader, format_duration(info.duration) if info.duration else None) if m]
        self.meta_var.set(" · ".join(meta))
        self.tier_by_label = {tier_label(t): t for t in info.tiers}
        self.tier_by_label[AUDIO_LABEL] = AUDIO_ONLY
        self.quality_box.configure(values=list(self.tier_by_label))
        self.quality_var.set(tier_label(info.tiers[0]))  # always start at the best quality
        self._set_controls()
        self._update_estimate()

    def _on_fetch_error(self, message: str) -> None:
        self.fetching = False
        self.title_var.set("Could not get the video info.")
        self._set_controls()
        messagebox.showerror(APP_NAME, message)
        self._update_ytdlp_after_error()

    def _on_quality_changed(self) -> None:
        self._set_controls()
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
        if selection.estimated_size:
            parts.append(f"Download ≈ {format_bytes(selection.estimated_size)}")
        codec = (selection.video.get("vcodec") or "?").split(".")[0]
        fps = selection.video.get("fps")
        parts.append(f"stream: {codec}{f' {fps:.0f} fps' if fps else ''}")
        target = self._upscale_target()
        source_tier = selection.tier or tier
        if target and source_tier < target:
            parts.append(f"then AI-upscaled to {target}p")
        elif self._mode() is Mode.COMPATIBLE and selection.needs_conversion:
            parts.append(f"converted to {formats.target_codec_label()}")
        if target and source_tier >= target:
            parts.append(f"already ≥ {target}p, so no upscaling")
        self.estimate_var.set(" · ".join(parts))

    def _job_template(self, **overrides) -> Job:
        target = self._upscale_target() or 0
        job = Job(title="", tier=self._selected_tier() or 2160, mode=self._mode().value, upscale_to=target,
                  upscale_content=self._content().value, output_dir=self.folder_var.get(),
                  cookies_from_browser=self._cookies() or "")
        for key, value in overrides.items():
            setattr(job, key, value)
        return job

    def add_to_queue(self) -> None:
        if not self.info:
            return
        self._save_settings()
        self.queue.add([self._job_template(title=self.info.title, url=self.info.url)])
        self.queue.start()
        self.status_var.set(f"Added “{self.info.title}” to the queue.")

    def add_playlist(self, entries, tier: int, folder: Path) -> None:
        self._save_settings()
        self.queue.add([self._job_template(title=e.title, url=e.url, tier=tier, output_dir=str(folder))
                        for e in entries])
        self.queue.start()
        self.status_var.set(f"Added {len(entries)} videos to the queue.")

    def upscale_local_file(self) -> None:
        target = self._upscale_target()
        if target is None:
            messagebox.showinfo(APP_NAME, "First choose a resolution under “AI upscale” "
                                          "(for example 1080p), then pick the video file.")
            return
        source = filedialog.askopenfilename(title="Choose a video to upscale", filetypes=VIDEO_FILETYPES)
        if not source:
            return
        self._save_settings()
        self.queue.add([self._job_template(title=Path(source).name, source=source)])
        self.queue.start()

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.folder_var.get(), title="Choose download folder")
        if folder:
            self.folder_var.set(folder)
            self._save_settings()

    # --- preview ----------------------------------------------------------------

    def open_preview(self) -> None:
        target = self._upscale_target()
        if target is None:
            messagebox.showinfo(APP_NAME, "First choose a resolution under “AI upscale” "
                                          "(for example 1080p).")
            return
        try:
            if self.info is not None:
                tier = self._selected_tier() or self.info.tiers[0]
                selection = self.info.selection_for(tier if tier != AUDIO_ONLY else self.info.tiers[0], self._mode())
                if (selection.tier or 0) >= target:
                    messagebox.showinfo(APP_NAME, f"This video is already {selection.tier}p or more, so there is "
                                                  f"nothing to upscale to {target}p.")
                    return
                source = preview.source_from_stream(selection.video, self.info.duration)
                title = self.info.title
            else:
                path = filedialog.askopenfilename(title="Choose a video to preview", filetypes=VIDEO_FILETYPES)
                if not path:
                    return
                source = preview.source_from_file(Path(path))
                title = Path(path).name
        except (DownloadFailed, upscale.UpscaleError, KeyError) as exc:
            messagebox.showerror(APP_NAME, f"Can't preview this video: {exc}")
            return
        PreviewWindow(self, source, target, title)

    # --- queue ----------------------------------------------------------------

    def _selected_job(self) -> Job | None:
        selection = self.tree.selection()
        return self.queue.get(selection[0]) if selection else None

    def toggle_queue(self) -> None:
        if self.queue.running:
            self.queue.pause()
            self.queue_status_var.set("Queue paused: the current item finishes, then the queue stops.")
        else:
            self.queue.start()
        self._set_controls()

    def _move(self, offset: int) -> None:
        job = self._selected_job()
        if job:
            self.queue.move(job.id, offset)

    def _toggle_item(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        if job.status in (jobs.FAILED, jobs.CANCELLED):
            self.queue.retry(job.id)
            self.queue.start()
        else:
            self.queue.set_paused(job.id, job.status != jobs.PAUSED)

    def _remove(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        if job.status == jobs.RUNNING and not messagebox.askyesno(APP_NAME, "Stop and remove the item that is running?"):
            return
        self.queue.remove(job.id)

    def _reveal_selected(self) -> None:
        job = self._selected_job()
        if job and job.result:
            reveal_in_file_manager(Path(job.result))

    def _open_selected(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        if job.status == jobs.FAILED and job.error:
            messagebox.showerror(APP_NAME, f"{job.title}\n\n{job.error}")
        elif job.status == jobs.DONE and job.result:
            reveal_in_file_manager(Path(job.result))

    def _status_text(self, job: Job) -> str:
        if job.status == jobs.RUNNING:
            return f"{job.fraction * 100:.0f}%" if job.fraction is not None else "Working…"
        return STATUS_TEXT.get(job.status, job.status)

    def _refresh_queue(self) -> None:
        wanted = [j.id for j in self.queue.jobs]
        for item in self.tree.get_children():
            if item not in wanted:
                self.tree.delete(item)
        for index, job in enumerate(list(self.queue.jobs)):
            values = (job.describe(), self._status_text(job))
            if self.tree.exists(job.id):
                self.tree.item(job.id, text=job.title, values=values)
                self.tree.move(job.id, "", index)
            else:
                self.tree.insert("", index, iid=job.id, text=job.title, values=values)
        self._set_controls()

    def _on_job(self, payload) -> None:
        event, job_id = payload
        job = self.queue.get(job_id) if job_id else None
        if event == "progress" and job is not None:
            if self.tree.exists(job.id):
                self.tree.set(job.id, "status", self._status_text(job))
            if job.fraction is None:
                if str(self.progress.cget("mode")) != "indeterminate":
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(15)
            else:
                if str(self.progress.cget("mode")) != "determinate":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                self.progress.configure(value=job.fraction * 1000)
            self.queue_status_var.set(f"{job.title[:60]} — {job.detail}")
            return
        self._refresh_queue()
        if event == "started":
            self._keep_awake(True)
            self.progress.configure(value=0)
            self.queue_status_var.set(f"{job.title[:60]} — starting…" if job else "")
        elif event in ("done", "failed", "cancelled"):
            self.progress.stop()
            self.progress.configure(mode="determinate", value=1000 if event == "done" else 0)
            if job is not None:
                self.finished_this_run += 1
                if event == "done":
                    self.queue_status_var.set(f"Saved “{Path(job.result).name}”")
                    self._notify(f"Finished: {job.title}")
                elif event == "failed":
                    self.queue_status_var.set(f"Failed: {job.title} — double-click it to see why.")
                    self._notify(f"Failed: {job.title}")
                    if not any(w in job.error for w in ("upscal", "Encoding", "Converting", "ffmpeg")):
                        self._update_ytdlp_after_error()
                else:
                    self.queue_status_var.set(f"Cancelled: {job.title}")
            if not self.queue.pending():
                self._keep_awake(False)
                self._install_pending_update()
        elif event == "queue_finished":
            if self.finished_this_run > 1:
                self._notify(f"The queue is finished ({self.finished_this_run} items).")
            self.finished_this_run = 0
            self.queue_status_var.set("All done.")

    def _notify(self, message: str) -> None:
        if self.notify_var.get():
            notify.notify(message)

    def _keep_awake(self, on: bool) -> None:
        """Stop the computer from sleeping during long downloads and upscales."""
        if IS_WINDOWS:
            _windows_keep_awake(on)
            return
        if on and IS_MAC and self.caffeinate is None:
            try:
                self.caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            except OSError:
                self.caffeinate = None
        elif not on and self.caffeinate is not None:
            self.caffeinate.terminate()
            self.caffeinate = None

    def _on_close(self) -> None:
        if self.queue.busy:
            if not messagebox.askyesno(APP_NAME, "An item is still running. Stop it and quit? "
                                                 "Waiting items stay in the queue for next time."):
                return
        self.queue.shutdown()
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

    # --- updates ------------------------------------------------------------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        if IS_MAC:
            app_menu = tk.Menu(menubar, name="apple")  # the application menu, after "About"
            app_menu.add_command(label="Check for Updates…", command=lambda: self.check_for_updates(True))
            menubar.add_cascade(menu=app_menu)
        else:
            help_menu = tk.Menu(menubar, tearoff=0)
            help_menu.add_command(label="Check for Updates…", command=lambda: self.check_for_updates(True))
            help_menu.add_separator()
            help_menu.add_command(label=f"About {APP_NAME}", command=show_about)
            menubar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menubar)

    def _startup_checks(self) -> None:
        if updater.updates_supported() and time.time() - self.settings.last_update_check > 24 * 3600:
            self.check_for_updates(manual=False)
        threading.Thread(target=self._ytdlp_worker, args=(False,), daemon=True).start()

    def _ytdlp_worker(self, after_error: bool) -> None:
        try:
            version = ytdlp_update.update(force=after_error)
        except Exception as exc:  # noqa: BLE001 - never bother the user with this
            log.warning("yt-dlp update failed: %s", exc)
            version = None
        self.events.put(("ytdlp_checked", (version, after_error)))

    def _update_ytdlp_after_error(self) -> None:
        """A failed download may mean YouTube changed: look for a newer yt-dlp once per session."""
        if not self.ytdlp_retry_offered:
            self.ytdlp_retry_offered = True
            threading.Thread(target=self._ytdlp_worker, args=(True,), daemon=True).start()

    def _on_ytdlp_checked(self, payload) -> None:
        version, after_error = payload
        if not version:
            return
        if after_error:
            if messagebox.askyesno(APP_NAME, f"The download engine (yt-dlp) was just updated to {version}, "
                                             f"which may fix this problem.\n\nRestart {APP_NAME} now to use it?"):
                self._restart()
        elif not self.busy:
            self.status_var.set(f"Download engine updated to yt-dlp {version}; it is used from the next start.")

    def check_for_updates(self, manual: bool) -> None:
        if not updater.updates_supported():
            if manual:
                messagebox.showinfo(APP_NAME, "Automatic updates are only available in the installed app.")
            return
        self.settings.last_update_check = time.time()
        self.settings.save()
        threading.Thread(target=self._update_check_worker, args=(manual,), daemon=True).start()

    def _update_check_worker(self, manual: bool) -> None:
        try:
            self.events.put(("update_checked", (updater.check(), manual, None)))
        except updater.UpdateError as exc:
            self.events.put(("update_checked", (None, manual, str(exc))))

    def _on_update_checked(self, payload) -> None:
        release, manual, error = payload
        if error:
            log.warning("Update check failed: %s", error)
            if manual:
                messagebox.showerror(APP_NAME, error)
            return
        if release is None:
            if manual:
                messagebox.showinfo(APP_NAME, f"You have the latest version ({__version__}).")
            return
        if not manual and release.version == self.settings.skipped_version:
            return
        UpdateDialog(self, release)

    def start_update(self, release: updater.Release) -> None:
        if self.busy:
            self.pending_update = release
            self.status_var.set(f"Version {release.version} will be installed when the current job finishes.")
            return
        self.update_window = UpdateProgress(self, release)

    def _install_pending_update(self) -> None:
        if self.pending_update is not None and not self.busy:
            release, self.pending_update = self.pending_update, None
            self.update_window = UpdateProgress(self, release)

    def _on_update_progress(self, payload) -> None:
        if self.update_window is not None:
            self.update_window.show_progress(*payload)

    def _on_update_failed(self, message: str) -> None:
        if self.update_window is not None:
            self.update_window.destroy()
            self.update_window = None
        messagebox.showerror(APP_NAME, message)

    def _on_update_ready(self, package: Path) -> None:
        if self.update_window is not None:
            self.update_window.show_installing()
        try:
            updater.install_and_restart(package)
        except updater.UpdateError as exc:
            self._on_update_failed(str(exc))
            return
        self._quit_for_update()

    def _quit_for_update(self) -> None:
        self._keep_awake(False)
        self._save_settings()
        self.root.after(300, self.root.destroy)

    def _restart(self) -> None:
        self._save_settings()
        updater.restart_app()
        self.root.destroy()


def _describe_duration(seconds: float | None) -> str:
    if not seconds:
        return "unknown"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


class PlaylistDialog(tk.Toplevel):
    """Pick which videos of a playlist or channel to add, and at which quality."""

    CHECKED, UNCHECKED = "☑", "☐"

    def __init__(self, app: App, playlist: PlaylistInfo):
        super().__init__(app.root)
        self.app, self.playlist = app, playlist
        self.title(playlist.title)
        self.transient(app.root)
        self.minsize(560, 420)
        self.checked: set[str] = {e.id for e in playlist.entries}
        frame = ttk.Frame(self, padding=16)
        frame.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        heading = font.nametofont("TkDefaultFont").copy()
        heading.configure(weight="bold")
        ttk.Label(frame, text=f"{playlist.title} · {len(playlist.entries)} videos", font=heading)\
            .grid(row=0, column=0, columnspan=2, sticky="w")

        self.tree = ttk.Treeview(frame, columns=("duration",), height=14, selectmode="none")
        self.tree.heading("#0", text="Video")
        self.tree.heading("duration", text="Length")
        self.tree.column("#0", width=440, stretch=True)
        self.tree.column("duration", width=80, stretch=False, anchor="e")
        self.tree.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns", pady=(8, 0))
        self.tree.configure(yscrollcommand=scroll.set)
        for entry in playlist.entries:
            self.tree.insert("", "end", iid=entry.id, text=f"{self.CHECKED}  {entry.title}",
                             values=(format_duration(entry.duration) if entry.duration else "",))
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda e: self._toggle(self.tree.focus()))

        select_row = ttk.Frame(frame)
        select_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        select_row.columnconfigure(2, weight=1)
        ttk.Button(select_row, text="Select All", command=lambda: self._set_all(True)).grid(row=0, column=0)
        ttk.Button(select_row, text="Select None", command=lambda: self._set_all(False))\
            .grid(row=0, column=1, padx=(6, 0))
        self.count_var = tk.StringVar()
        ttk.Label(select_row, textvariable=self.count_var, foreground="gray").grid(row=0, column=3, sticky="e")

        options = ttk.Frame(frame)
        options.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(options, text="Quality:").grid(row=0, column=0, sticky="w")
        self.tier_by_label = {tier_label(t): t for t in PLAYLIST_TIERS}
        self.tier_by_label[AUDIO_LABEL] = AUDIO_ONLY
        self.quality_var = tk.StringVar(value=tier_label(PLAYLIST_TIERS[0]))
        ttk.Combobox(options, textvariable=self.quality_var, values=list(self.tier_by_label),
                     state="readonly", width=22).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(options, text="(the best available, up to this; a video without it uses its best)",
                  foreground="gray").grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.subfolder_var = tk.BooleanVar(value=True)
        folder_name = downloader.safe_folder_name(playlist.title)
        ttk.Checkbutton(options, text=f"Save into a folder named “{folder_name}”", variable=self.subfolder_var)\
            .grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        settings = []
        if app._upscale_target():
            settings.append(f"AI upscale to {app._upscale_target()}p ({app.content_var.get()})")
        settings.append(MODE_TEXT[app._mode()][0])
        ttk.Label(options, text="Other settings from the main window: " + ", ".join(settings) + ".",
                  foreground="gray").grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=0, padx=(0, 8))
        self.add_button = ttk.Button(buttons, text="Add to Queue", command=self.add, default="active")
        self.add_button.grid(row=0, column=1)
        self.bind("<Escape>", lambda e: self.destroy())
        self._update_count()

    def _on_click(self, event) -> str | None:
        item = self.tree.identify_row(event.y)
        if item:
            self._toggle(item)
            return "break"
        return None

    def _toggle(self, item: str) -> None:
        if not item:
            return
        self._mark(item, item not in self.checked)
        self._update_count()

    def _mark(self, item: str, on: bool) -> None:
        if on:
            self.checked.add(item)
        else:
            self.checked.discard(item)
        title = self.tree.item(item, "text")[1:].lstrip()
        self.tree.item(item, text=f"{self.CHECKED if on else self.UNCHECKED}  {title}")

    def _set_all(self, on: bool) -> None:
        for entry in self.playlist.entries:
            self._mark(entry.id, on)
        self._update_count()

    def _update_count(self) -> None:
        count = len(self.checked)
        self.count_var.set(f"{count} of {len(self.playlist.entries)} selected")
        self.add_button.configure(state="normal" if count else "disabled")

    def add(self) -> None:
        entries = [e for e in self.playlist.entries if e.id in self.checked]
        if not entries:
            return
        folder = Path(self.app.folder_var.get())
        if self.subfolder_var.get():
            folder = folder / downloader.safe_folder_name(self.playlist.title)
        self.app.add_playlist(entries, self.tier_by_label[self.quality_var.get()], folder)
        self.destroy()


class PreviewWindow(tk.Toplevel):
    """Before/after comparison of the AI models on one frame, with time estimates."""

    PANEL_W, PANEL_H = 400, 300
    THUMB_W = 260

    def __init__(self, app: App, source: preview.FrameSource, target: int, title: str):
        super().__init__(app.root)
        self.app, self.source, self.target = app, source, target
        self.title(f"AI Upscale Preview — {target}p")
        self.transient(app.root)
        self.resizable(False, False)
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.work_dir = Path(tempfile.mkdtemp(prefix="ytd-preview-"))
        self.running = False
        self.images: dict[str, tk.PhotoImage] = {}    # "bicubic" and Content values, full size
        self.results: dict[Content, preview.ModelPreview] = {}
        self.thumb: tk.PhotoImage | None = None
        self.thumb_factor = 1
        self.center = (0.5, 0.5)
        self.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(self, padding=16)
        frame.grid(sticky="nsew")
        bold = font.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        ttk.Label(frame, text=title, font=bold).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text=f"{source.width}×{source.height} → {target}p. Pick a moment, then click "
                              "the small picture to choose which part to inspect.", foreground="gray")\
            .grid(row=1, column=0, columnspan=2, sticky="w")

        moment = ttk.Frame(frame)
        moment.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        moment.columnconfigure(1, weight=1)
        ttk.Label(moment, text="Moment:").grid(row=0, column=0)
        length = source.duration or 600
        self.at_var = tk.DoubleVar(value=min(max(length / 3, 0), max(length - 1, 0)))
        ttk.Scale(moment, from_=0, to=max(length - 1, 1), variable=self.at_var,
                  command=lambda v: self._show_at()).grid(row=0, column=1, sticky="ew", padx=8)
        self.at_label = ttk.Label(moment, width=8)
        self.at_label.grid(row=0, column=2)
        self.generate_button = ttk.Button(moment, text="Make Preview", command=self.generate, default="active")
        self.generate_button.grid(row=0, column=3, padx=(8, 0))
        self._show_at()

        self.status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # Left column: overview and model choice
        side = ttk.Frame(frame)
        side.grid(row=4, column=0, sticky="nw", pady=(8, 0))
        self.thumb_canvas = tk.Canvas(side, width=self.THUMB_W, height=int(self.THUMB_W * 9 / 16),
                                      highlightthickness=1, highlightbackground="gray", cursor="crosshair")
        self.thumb_canvas.grid(row=0, column=0, sticky="w")
        self.thumb_canvas.bind("<Button-1>", self._on_thumb_click)
        self.thumb_canvas.bind("<B1-Motion>", self._on_thumb_click)

        ttk.Label(side, text="Compare with:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.model_var = tk.StringVar(value=app._content().value)
        self.model_buttons: dict[Content, ttk.Radiobutton] = {}
        self.speed_vars = {c: tk.StringVar(value="") for c in preview.PREVIEW_CONTENTS}
        models = ttk.Frame(side)
        models.grid(row=2, column=0, sticky="w")
        for i, content in enumerate(preview.PREVIEW_CONTENTS):
            rb = ttk.Radiobutton(models, text=CONTENT_NAMES[content], value=content.value,
                                 variable=self.model_var, command=self._render, state="disabled")
            rb.grid(row=2 * i, column=0, sticky="w")
            self.model_buttons[content] = rb
            ttk.Label(models, textvariable=self.speed_vars[content], foreground="gray")\
                .grid(row=2 * i + 1, column=0, sticky="w", padx=(24, 0))

        zoom_row = ttk.Frame(side)
        zoom_row.grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Label(zoom_row, text="Zoom:").grid(row=0, column=0)
        self.zoom_var = tk.IntVar(value=1)
        for i, zoom in enumerate((1, 2, 3)):
            ttk.Radiobutton(zoom_row, text=f"{zoom * 100}%", value=zoom, variable=self.zoom_var,
                            command=self._render).grid(row=0, column=1 + i, padx=(6, 0))
        ttk.Label(side, text="Times are for this computer and the whole video;\n"
                             "downloading and encoding add a little.", foreground="gray", justify="left")\
            .grid(row=4, column=0, sticky="w", pady=(10, 0))

        # Right: the two panels
        panels = ttk.Frame(frame)
        panels.grid(row=4, column=1, sticky="nw", padx=(16, 0), pady=(8, 0))
        self.left_title = tk.StringVar(value="Plain enlargement")
        self.right_title = tk.StringVar(value="AI upscaled")
        ttk.Label(panels, textvariable=self.left_title).grid(row=0, column=0, sticky="w")
        ttk.Label(panels, textvariable=self.right_title).grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.panel_images = (tk.PhotoImage(width=self.PANEL_W, height=self.PANEL_H),
                             tk.PhotoImage(width=self.PANEL_W, height=self.PANEL_H))
        for i, image in enumerate(self.panel_images):
            tk.Label(panels, image=image, borderwidth=1, relief="solid", background="black")\
                .grid(row=1, column=i, padx=(8 if i else 0, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.use_button = ttk.Button(buttons, text="Use This Model", command=self.use_model, state="disabled")
        self.use_button.grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="Close", command=self.close).grid(row=0, column=1)

        self.after(100, self._poll)
        self.generate()

    # --- background work ------------------------------------------------------

    def generate(self) -> None:
        if self.running:
            return
        self.running = True
        self.cancel_event = threading.Event()
        self.results.clear()
        self.images.clear()
        for content, rb in self.model_buttons.items():
            rb.configure(state="disabled")
            self.speed_vars[content].set("")
        self.use_button.configure(state="disabled")
        self.generate_button.configure(state="disabled")
        at = float(self.at_var.get())
        threading.Thread(target=self._worker, args=(at, self.cancel_event), daemon=True).start()

    def _worker(self, at: float, cancel_event: threading.Event) -> None:
        put = self.events.put
        try:
            work = Path(tempfile.mkdtemp(dir=self.work_dir))
            preview.preview_models(
                self.source, at, self.target, work,
                on_status=lambda text: put(("status", text)),
                on_frame=lambda frame, plain: put(("frame", (frame, plain))),
                on_result=lambda result: put(("result", result)),
                cancel_event=cancel_event)
            put(("finished", None))
        except Exception as exc:  # noqa: BLE001 - show it in the window
            if not cancel_event.is_set():
                log.exception("Preview failed")
                put(("failed", str(exc) or exc.__class__.__name__))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                getattr(self, f"_on_{kind}")(payload)
        except queue.Empty:
            pass
        except tk.TclError:
            return  # window closed
        if self.winfo_exists():
            self.after(100, self._poll)

    def _on_status(self, text: str) -> None:
        self.status_var.set(text)

    def _on_frame(self, payload) -> None:
        frame, plain = payload
        original = tk.PhotoImage(file=str(frame))
        self.thumb_factor = max(1, -(-original.width() // self.THUMB_W))
        self.thumb = original.subsample(self.thumb_factor)
        self.thumb_canvas.configure(width=self.thumb.width(), height=self.thumb.height())
        self.thumb_canvas.delete("all")
        self.thumb_canvas.create_image(0, 0, image=self.thumb, anchor="nw")
        self.images["bicubic"] = tk.PhotoImage(file=str(plain))
        self._render()

    def _on_result(self, result: preview.ModelPreview) -> None:
        self.results[result.content] = result
        self.images[result.content.value] = tk.PhotoImage(file=str(result.image))
        estimate = result.estimate_seconds(self.source.total_frames)
        speed = f"{result.frames_per_second:.2f}" if result.frames_per_second < 10 else \
            f"{result.frames_per_second:.0f}"
        self.speed_vars[result.content].set(
            f"{speed} frames/s" + (f" · ≈ {_describe_duration(estimate)}" if estimate else ""))
        self.model_buttons[result.content].configure(state="normal")
        if self.model_var.get() not in {c.value for c in self.results}:
            self.model_var.set(result.content.value)
        self.use_button.configure(state="normal")
        self._render()

    def _on_finished(self, _payload) -> None:
        self.running = False
        self.generate_button.configure(state="normal")
        self.status_var.set("Done. Compare the models; the estimates are for the whole video.")

    def _on_failed(self, message: str) -> None:
        self.running = False
        self.generate_button.configure(state="normal")
        self.status_var.set("")
        messagebox.showerror(APP_NAME, f"The preview failed: {message}", parent=self)

    # --- drawing ----------------------------------------------------------------

    def _show_at(self) -> None:
        self.at_label.configure(text=format_duration(self.at_var.get()))

    def _on_thumb_click(self, event) -> None:
        if self.thumb is None:
            return
        self.center = (min(max(event.x / self.thumb.width(), 0), 1), min(max(event.y / self.thumb.height(), 0), 1))
        self._render()

    def _crop(self, image: tk.PhotoImage, zoom: int) -> tuple[int, int, int, int]:
        w, h = image.width(), image.height()
        cw, ch = min(self.PANEL_W // zoom, w), min(self.PANEL_H // zoom, h)
        x0 = int(min(max(self.center[0] * w - cw / 2, 0), w - cw))
        y0 = int(min(max(self.center[1] * h - ch / 2, 0), h - ch))
        return x0, y0, x0 + cw, y0 + ch

    def _draw_panel(self, panel: tk.PhotoImage, image: tk.PhotoImage | None, zoom: int) -> None:
        panel.blank()
        if image is None:
            return
        x0, y0, x1, y1 = self._crop(image, zoom)
        self.tk.call(panel, "copy", image, "-from", x0, y0, x1, y1, "-to", 0, 0, "-zoom", zoom, zoom)

    def _render(self) -> None:
        zoom = int(self.zoom_var.get())
        plain = self.images.get("bicubic")
        model = self.images.get(self.model_var.get())
        self._draw_panel(self.panel_images[0], plain, zoom)
        self._draw_panel(self.panel_images[1], model, zoom)
        content = Content(self.model_var.get())
        result = self.results.get(content)
        self.right_title.set(f"AI: {CONTENT_NAMES[content]}" + (f" ({result.model})" if result else " (working…)"))
        self.left_title.set("Plain enlargement (no AI)")
        self.thumb_canvas.delete("box")
        if self.thumb is not None and plain is not None:
            x0, y0, x1, y1 = self._crop(plain, zoom)
            sx, sy = self.thumb.width() / plain.width(), self.thumb.height() / plain.height()
            self.thumb_canvas.create_rectangle(x0 * sx, y0 * sy, x1 * sx, y1 * sy, outline="#ff3b30", width=2,
                                               tags="box")

    # --- buttons ----------------------------------------------------------------

    def use_model(self) -> None:
        content = Content(self.model_var.get())
        self.app.content_var.set(CONTENT_NAMES[content])
        self.app._save_settings()
        self.app._update_estimate()
        self.close()

    def close(self) -> None:
        self.cancel_event.set()
        self.destroy()
        shutil.rmtree(self.work_dir, ignore_errors=True)


class PackDownload(tk.Toplevel):
    """Downloads and installs the NVIDIA package (CUDA, cuDNN, TensorRT)."""

    def __init__(self, app: App):
        super().__init__(app.root)
        self.app = app
        app.pack_window = self
        self.cancel_event = threading.Event()
        self.title("NVIDIA Support")
        self.transient(app.root)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        frame = ttk.Frame(self, padding=16)
        frame.grid(sticky="nsew")
        info = nvidia_pack.pack_info() or {}
        size = f" ({updater.describe_size(int(info['size']))})" if info.get("size") else ""
        self.label_var = tk.StringVar(value=f"Downloading NVIDIA support{size}…")
        ttk.Label(frame, textvariable=self.label_var).grid(row=0, column=0, sticky="w")
        self.bar = ttk.Progressbar(frame, length=380, maximum=1000)
        self.bar.grid(row=1, column=0, pady=(8, 8))
        self.cancel_button = ttk.Button(frame, text="Cancel", command=self.cancel)
        self.cancel_button.grid(row=2, column=0, sticky="e")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        put = self.app.events.put
        try:
            nvidia_pack.install(lambda done, total: put(("pack_progress", (done, total))), self.cancel_event)
            put(("pack_done", None))
        except nvidia_pack.PackError as exc:
            put(("pack_done", None if self.cancel_event.is_set() else str(exc)))
        except Exception as exc:  # noqa: BLE001
            log.exception("Installing the NVIDIA package failed")
            put(("pack_done", f"Installing NVIDIA support failed: {exc}"))

    def show_progress(self, done: int, total: int) -> None:
        if self.cancel_event.is_set():
            return
        if total:
            self.bar.configure(value=done / total * 1000)
            self.label_var.set(f"Downloading NVIDIA support… {updater.describe_size(done)} of "
                               f"{updater.describe_size(total)}")
            if done >= total:
                self.label_var.set("Unpacking…")
                self.cancel_button.configure(state="disabled")

    def cancel(self) -> None:
        if str(self.cancel_button.cget("state")) == "disabled":
            return
        self.cancel_event.set()
        self.label_var.set("Cancelling…")
        self.cancel_button.configure(state="disabled")


class UpdateDialog(tk.Toplevel):
    """"A new version is available" with release notes and the three choices."""

    def __init__(self, app: App, release: updater.Release):
        super().__init__(app.root)
        self.app, self.release = app, release
        self.title("Update Available")
        self.transient(app.root)
        self.resizable(False, False)
        frame = ttk.Frame(self, padding=16)
        frame.grid(sticky="nsew")
        heading = font.nametofont("TkDefaultFont").copy()
        heading.configure(weight="bold", size=heading.cget("size") + 2)
        ttk.Label(frame, text=f"{APP_NAME} {release.version} is available", font=heading)\
            .grid(row=0, column=0, sticky="w")
        size = f" · download {updater.describe_size(release.size)}" if release.size else ""
        ttk.Label(frame, text=f"You have version {__version__}{size}.", foreground="gray")\
            .grid(row=1, column=0, sticky="w", pady=(2, 8))
        notes = tk.Text(frame, width=64, height=12, wrap="word", relief="flat", borderwidth=1)
        notes.insert("1.0", updater.plain_notes(release.notes) or "No release notes.")
        notes.configure(state="disabled")
        notes.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=notes.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        notes.configure(yscrollcommand=scroll.set)
        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        buttons.columnconfigure(1, weight=1)
        ttk.Button(buttons, text="Skip This Version", command=self.skip).grid(row=0, column=0)
        ttk.Button(buttons, text="Later", command=self.destroy).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(buttons, text="Update Now", command=self.update_now, default="active")\
            .grid(row=0, column=3)
        if IS_WINDOWS:
            ttk.Label(frame, text="Windows will ask for permission to install the update.",
                      foreground="gray").grid(row=4, column=0, sticky="w", pady=(8, 0))

    def skip(self) -> None:
        self.app.settings.skipped_version = self.release.version
        self.app.settings.save()
        self.destroy()

    def update_now(self) -> None:
        self.destroy()
        self.app.start_update(self.release)


class UpdateProgress(tk.Toplevel):
    """Download progress for an update; the app quits once the installer has started."""

    def __init__(self, app: App, release: updater.Release):
        super().__init__(app.root)
        self.app = app
        self.cancel_event = threading.Event()
        self.title("Updating")
        self.transient(app.root)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        frame = ttk.Frame(self, padding=16)
        frame.grid(sticky="nsew")
        self.label_var = tk.StringVar(value=f"Downloading {APP_NAME} {release.version}…")
        ttk.Label(frame, textvariable=self.label_var).grid(row=0, column=0, sticky="w")
        self.bar = ttk.Progressbar(frame, length=360, maximum=1000)
        self.bar.grid(row=1, column=0, pady=(8, 8))
        self.cancel_button = ttk.Button(frame, text="Cancel", command=self.cancel)
        self.cancel_button.grid(row=2, column=0, sticky="e")
        threading.Thread(target=self._worker, args=(release,), daemon=True).start()

    def _worker(self, release: updater.Release) -> None:
        try:
            path = updater.download(release, lambda done, total: self.app.events.put(
                ("update_progress", (done, total))), self.cancel_event)
            self.app.events.put(("update_ready", path))
        except updater.UpdateError as exc:
            if not self.cancel_event.is_set():
                self.app.events.put(("update_failed", str(exc)))

    def show_progress(self, done: int, total: int) -> None:
        if total:
            self.bar.configure(value=done / total * 1000)
            self.label_var.set(f"Downloading… {updater.describe_size(done)} of {updater.describe_size(total)}")

    def show_installing(self) -> None:
        self.cancel_button.configure(state="disabled")
        self.label_var.set("Installing the update…")
        self.update_idletasks()

    def cancel(self) -> None:
        self.cancel_event.set()
        self.app.update_window = None
        self.destroy()


def show_about() -> None:
    try:
        engine = f"yt-dlp {ytdlp_update.active_version()}"
    except Exception:  # noqa: BLE001
        engine = "yt-dlp"
    messagebox.showinfo(APP_NAME, f"{APP_NAME} {__version__}\n\nPowered by {engine}, ffmpeg and Real-ESRGAN.")


def _windows_keep_awake(on: bool) -> None:
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    flags = es_continuous | es_system_required if on else es_continuous
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


def _windows_setup() -> None:
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
    except (AttributeError, OSError):
        pass
    try:
        # Own taskbar icon/grouping instead of Python's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("YouTubeDownloader.App")
    except (AttributeError, OSError):
        pass


def run() -> None:
    if IS_WINDOWS:
        _windows_setup()
    root = tk.Tk()
    if IS_WINDOWS:
        icon = Path(getattr(sys, "_MEIPASS", "")) / "AppIcon.ico"
        if icon.exists():
            root.iconbitmap(default=str(icon))
    root.title(APP_NAME)
    if sys.platform == "darwin":
        # Standard "About" item in the application menu.
        root.createcommand("tkAboutDialog", show_about)
    App(root)
    root.update_idletasks()
    root.geometry(f"+{max((root.winfo_screenwidth() - root.winfo_width()) // 2, 0)}+120")
    root.lift()
    root.focus_force()
    if os.environ.get("YTD_SMOKE_TEST"):
        # Used by CI to prove the bundled Tk can open a window.
        root.after(2000, root.destroy)
    root.mainloop()
