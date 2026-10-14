"""Tests for the download queue, playlists, notifications and the upscale preview."""

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from ytdownloader import downloader, jobs, notify, preview, upscale
from ytdownloader.downloader import Cancelled, DownloadFailed, PlaylistInfo, Progress
from ytdownloader.jobs import Job, JobQueue

FFMPEG = shutil.which("ffmpeg")


class Recorder:
    def __init__(self):
        self.events = []
        self.finished = threading.Event()

    def __call__(self, event, job):
        self.events.append((event, job.title if job else None))
        if event == "queue_finished":
            self.finished.set()


def make_queue(tmp_path, recorder=None):
    return JobQueue(recorder or Recorder(), path=tmp_path / "queue.json")


def test_jobs_run_in_order_and_report_results(tmp_path, monkeypatch):
    ran = []

    def fake_download(url, tier, mode, out, on_progress, cancel_event, **kwargs):
        ran.append((url, tier, kwargs["upscale_to"]))
        on_progress(Progress("download", 0.5, "half"))
        if url == "bad":
            raise DownloadFailed("nope")
        return out / f"{url}.mp4"

    monkeypatch.setattr(downloader, "download", fake_download)
    monkeypatch.setattr(downloader, "download_audio",
                        lambda url, out, **kw: ran.append((url, "mp3")) or out / "a.mp3")
    recorder = Recorder()
    q = make_queue(tmp_path, recorder)
    q.add([Job(title="one", url="u1", output_dir=str(tmp_path)),
           Job(title="two", url="bad", output_dir=str(tmp_path)),
           Job(title="three", url="u3", tier=jobs.AUDIO_ONLY, output_dir=str(tmp_path)),
           Job(title="four", url="u4", upscale_to=2160, output_dir=str(tmp_path))])
    q.start()
    assert recorder.finished.wait(10)
    q.shutdown()
    assert ran == [("u1", 2160, None), ("bad", 2160, None), ("u3", "mp3"), ("u4", 2160, 2160)]
    assert [j.status for j in q.jobs] == [jobs.DONE, jobs.FAILED, jobs.DONE, jobs.DONE]
    assert q.jobs[1].error == "nope"
    assert q.jobs[0].result == str(tmp_path / "u1.mp4")
    assert ("progress", "one") in recorder.events
    assert ("failed", "two") in recorder.events


def test_queue_persists_and_running_jobs_restart(tmp_path):
    q = make_queue(tmp_path)
    q.add([Job(title="a", url="u", status=jobs.RUNNING, detail="50%"), Job(title="b", url="v", status=jobs.DONE)])
    data = json.loads((tmp_path / "queue.json").read_text())
    assert [j["title"] for j in data["jobs"]] == ["a", "b"]
    data["jobs"][0]["unknown_future_field"] = 1
    (tmp_path / "queue.json").write_text(json.dumps(data))
    again = make_queue(tmp_path)
    assert [(j.title, j.status, j.detail) for j in again.jobs] == [("a", jobs.WAITING, ""), ("b", jobs.DONE, "")]


def test_queue_editing(tmp_path):
    q = make_queue(tmp_path)
    a, b, c = Job(title="a"), Job(title="b"), Job(title="c", status=jobs.FAILED, error="x")
    q.add([a, b, c])
    q.move(c.id, -5)
    assert [j.title for j in q.jobs] == ["c", "a", "b"]
    q.set_paused(a.id, True)
    assert a.status == jobs.PAUSED and [j.title for j in q.pending()] == ["b"]
    q.set_paused(a.id, False)
    q.retry(c.id)
    assert c.status == jobs.WAITING and c.error == ""
    b.status = jobs.DONE
    q.clear_finished()
    assert [j.title for j in q.jobs] == ["c", "a"]
    q.remove(c.id)
    assert [j.title for j in q.jobs] == ["a"]


def test_cancel_current_job(tmp_path, monkeypatch):
    started = threading.Event()

    def slow(url, tier, mode, out, on_progress, cancel_event, **kwargs):
        started.set()
        if cancel_event.wait(10):
            raise Cancelled()
        return out / "x.mp4"

    monkeypatch.setattr(downloader, "download", slow)
    recorder = Recorder()
    q = make_queue(tmp_path, recorder)
    q.add([Job(title="slow", url="u", output_dir=str(tmp_path))])
    q.start()
    assert started.wait(5)
    assert q.busy
    q.cancel_current()
    assert recorder.finished.wait(5)
    q.shutdown()
    assert q.jobs[0].status == jobs.CANCELLED and not q.busy


def test_paused_queue_does_not_start_jobs(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(downloader, "download", lambda url, *a, **k: ran.append(url) or Path("x.mp4"))
    recorder = Recorder()
    q = make_queue(tmp_path, recorder)
    q.start()
    q.pause()
    q.add([Job(title="a", url="u")])
    time.sleep(1.5)  # longer than the worker's idle wait
    assert ran == [] and q.jobs[0].status == jobs.WAITING
    q.start()
    assert recorder.finished.wait(5)
    q.shutdown()
    assert ran == ["u"]


def test_job_descriptions():
    assert Job(title="", tier=1080).describe() == "≤1080p · compatible"
    assert Job(title="", tier=jobs.AUDIO_ONLY).describe() == "MP3"
    assert Job(title="", tier=2160, mode="original", upscale_to=2160).describe() == "≤2160p · original · AI → 2160p"
    assert Job(title="", source="/x.mp4", upscale_to=1440).describe() == "AI upscale → 1440p"


# --- playlists ------------------------------------------------------------------

def test_playlist_info_from_flat_extraction():
    info = {"_type": "playlist", "title": "Mix", "uploader": "Me", "webpage_url": "https://y/p",
            "entries": [{"id": "a1", "url": "https://www.youtube.com/watch?v=a1", "title": "First", "duration": 61},
                        None,
                        {"id": "b2", "title": "Second"},
                        {"title": "no id or url"}]}
    result = downloader._playlist_info(info, "https://y/p")
    assert isinstance(result, PlaylistInfo)
    assert result.title == "Mix" and result.uploader == "Me"
    assert [(e.id, e.url, e.title, e.duration) for e in result.entries] == [
        ("a1", "https://www.youtube.com/watch?v=a1", "First", 61),
        ("b2", "https://www.youtube.com/watch?v=b2", "Second", None)]


def test_empty_playlist_is_an_error():
    with pytest.raises(DownloadFailed):
        downloader._playlist_info({"entries": []}, "u")


def test_fetch_uses_the_videos_tab_of_a_channel(monkeypatch):
    calls = []

    class FakeYDL:
        def __init__(self, params):
            assert params["extract_flat"] == "in_playlist"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download, process):
            calls.append(url)
            if url.endswith("/videos"):
                return {"_type": "playlist", "title": "Chan - Videos", "entries": [{"id": "v1", "title": "V"}]}
            return {"_type": "playlist", "title": "Chan", "entries": [
                {"url": "https://www.youtube.com/@chan/videos", "title": "Videos"},
                {"url": "https://www.youtube.com/@chan/shorts", "title": "Shorts"}]}

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYDL)
    result = downloader.fetch("https://www.youtube.com/@chan")
    assert calls == ["https://www.youtube.com/@chan", "https://www.youtube.com/@chan/videos"]
    assert [e.id for e in result.entries] == ["v1"]


def test_safe_folder_name():
    assert downloader.safe_folder_name('My: "Best" / of?') == "My_ _Best_ _ of_"
    assert downloader.safe_folder_name(" . ") == "Playlist"


# --- notifications --------------------------------------------------------------

def test_notification_commands():
    mac = notify.command("Title", 'Done: "x"', "darwin")
    assert mac[0] == "osascript" and '"Done: \\"x\\""' in mac[2]
    win = notify.command("T", "It's <done> & ok", "win32")
    assert win[0] == "powershell.exe"
    script = win[-1]
    assert "It''s &lt;done&gt; &amp; ok" in script
    assert notify.APP_USER_MODEL_ID in script
    assert notify.command("T", "m", "linux") is None


# --- preview --------------------------------------------------------------------

@pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")
def test_preview_models(tmp_path, monkeypatch):
    video = tmp_path / "in.mp4"
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
    source = preview.source_from_file(video)
    assert (source.width, source.height) == (160, 90) and source.total_frames == 20

    def fake_time(frame_png, plan, work):
        out = work / "up.png"
        preview.enlarge(frame_png, 160 * plan.scale, 90 * plan.scale, out)
        return out, {"realesrgan-x4plus": 0.5}.get(plan.model, 10.0)

    monkeypatch.setattr(upscale, "preferred_engine", lambda: "ncnn")
    monkeypatch.setattr(preview, "_time_ncnn", fake_time)
    seen = []
    frame, plain, results = preview.preview_models(source, 1.0, 1080, tmp_path / "work",
                                                   on_frame=lambda f, p: seen.append("frame"),
                                                   on_result=lambda r: seen.append(r.content))
    assert seen == ["frame", *preview.PREVIEW_CONTENTS]
    assert upscale.probe(plain).height == 1080
    for result in results:
        assert upscale.probe(result.image).height == 1080
    best = results[0]
    assert best.model == "realesrgan-x4plus" and best.estimate_seconds(source.total_frames) == 40
