"""Command line interface (also used for CI smoke tests of the packaged app)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import APP_NAME, __version__, downloader, tools, upscale
from .downloader import Cancelled, DownloadFailed, Progress, format_bytes
from .formats import MAX_TIER, Mode, tier_label
from .settings import default_download_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="youtube-downloader", description=f"{APP_NAME} {__version__}")
    parser.add_argument("url", nargs="?", help="YouTube video URL")
    parser.add_argument("-q", "--quality", type=int, default=MAX_TIER,
                        help="maximum quality in p, e.g. 2160, 1440, 1080, 720 (default: best up to 2160)")
    parser.add_argument("-m", "--mode", choices=[m.value for m in Mode], default=Mode.COMPATIBLE.value,
                        help="compatible: H.264/HEVC that plays in QuickTime (default); "
                             "original: keep YouTube's AV1/VP9 stream, no conversion")
    parser.add_argument("-o", "--output", type=Path, default=default_download_dir(),
                        help="folder to save into (default: ~/Downloads)")
    parser.add_argument("--cookies-from-browser", choices=downloader.SUPPORTED_BROWSERS,
                        help="use cookies from this browser (age-restricted videos, bot checks)")
    parser.add_argument("--mp3", action="store_true", help="download only the audio as MP3")
    parser.add_argument("--upscale-to", type=int, choices=upscale.UPSCALE_TARGETS,
                        help="AI-upscale videos below this resolution (Real-ESRGAN, slow)")
    parser.add_argument("--content", choices=[c.value for c in upscale.Content],
                        default=upscale.Content.REALISTIC.value,
                        help="AI upscaler model: realistic (best quality, slow), fast (real-life, "
                             "several times faster) or animation (default: realistic)")
    parser.add_argument("--upscale-file", type=Path, metavar="VIDEO",
                        help="AI-upscale a local video file instead of downloading (needs --upscale-to)")
    parser.add_argument("-l", "--list", action="store_true", help="only list available qualities")
    parser.add_argument("--self-test", action="store_true", help="check bundled tools and exit")
    parser.add_argument("--check-update", action="store_true", help="check for a new app version")
    parser.add_argument("--update-ytdlp", action="store_true",
                        help="download the newest yt-dlp now (used from the next start)")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging to stderr")
    return parser


def self_test() -> int:
    import yt_dlp

    from . import ytdlp_update

    print(f"{APP_NAME} {__version__}")
    print(f"yt-dlp {yt_dlp.version.__version__} ({yt_dlp.__file__})")
    print(f"    bundled: {ytdlp_update.bundled_version()}")
    ok = True
    try:
        import yt_dlp_ejs  # noqa: F401
        print("yt-dlp-ejs: available")
    except ImportError:
        print("yt-dlp-ejs: MISSING")
        ok = False
    toolset = tools.discover()
    for name, flag in (("ffmpeg", "-version"), ("ffprobe", "-version"), ("deno", "--version")):
        path = getattr(toolset, name)
        if path:
            print(f"{name}: {path}\n    {tools.version_line(path, flag)}")
        else:
            print(f"{name}: MISSING")
            ok = False
    paths = upscale.upscaler_paths()
    if paths:
        print(f"realesrgan: {paths[0]}\n    models: {paths[1]}")
        for model in ("realesrgan-x4plus", "realesr-general-x4v3", "realesr-animevideov3-x2",
                      "realesr-animevideov3-x3", "realesr-animevideov3-x4"):
            if not (paths[1] / f"{model}.bin").exists():
                print(f"    model {model}: MISSING")
                ok = False
    else:
        print("realesrgan: MISSING")
        ok = False
    if tools.IS_WINDOWS:
        from . import onnx_engine

        directory = onnx_engine.models_dir()
        print(f"onnx models: {directory or 'MISSING'}")
        if directory is None:
            ok = False
        else:
            for name, filename in onnx_engine.MODEL_FILES.items():
                if not (directory / filename).exists():
                    print(f"    model {name}: MISSING")
                    ok = False
        try:
            import onnxruntime

            print(f"onnxruntime {onnxruntime.__version__}: {', '.join(onnxruntime.get_available_providers())}")
            if "CUDAExecutionProvider" not in onnxruntime.get_available_providers():
                ok = False
        except ImportError:
            print("onnxruntime: MISSING")
            ok = False
        # Informational: CI machines have no NVIDIA GPU.
        status = onnx_engine.cuda_status()
        print(f"NVIDIA engine: {'ready' if status.available else 'not available'} — {status.detail}")
    if toolset.ffmpeg:
        encoders = tools.ffmpeg_encoders(toolset.ffmpeg)
        wanted = ["hevc_videotoolbox", "h264_videotoolbox", "h264_nvenc", "hevc_nvenc",
                  "libx265", "libx264", "aac"]
        print("encoders: " + ", ".join(f"{e}={'yes' if e in encoders else 'no'}" for e in wanted))
        usable = downloader.working_encoder_attempts(1080, 30)
        print("usable on this computer: " + (", ".join(a[1] for a in usable) or "none"))
        if not usable:
            ok = False
    print("self-test: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # "—" and "…" on Windows consoles
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if args.self_test:
        return self_test()
    if args.check_update:
        from . import updater

        print(f"{APP_NAME} {__version__}")
        try:
            release = updater.check()
        except updater.UpdateError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Update available: {release.version} ({release.asset_name})" if release else "Up to date.")
        return 0
    if args.update_ytdlp:
        from . import ytdlp_update

        try:
            version = ytdlp_update.update(force=True)
        except ytdlp_update.YtdlpUpdateError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Installed yt-dlp {version}; used from the next start." if version
              else f"yt-dlp is up to date ({ytdlp_update.active_version()}).")
        return 0

    last = {"line": ""}

    def show(p: Progress) -> None:
        pct = f"{p.fraction * 100:5.1f}% " if p.fraction is not None else ""
        line = f"\r{pct}{p.detail}"[:120]
        sys.stderr.write(line.ljust(len(last["line"])))
        sys.stderr.flush()
        last["line"] = line

    if args.upscale_file:
        if not args.upscale_to:
            print("--upscale-file needs --upscale-to (1080, 1440 or 2160)", file=sys.stderr)
            return 2
        try:
            path = downloader.upscale_file(args.upscale_file, args.output, args.upscale_to,
                                           upscale.Content(args.content), on_progress=show)
        except DownloadFailed as exc:
            print(f"\nError: {exc}", file=sys.stderr)
            return 1
        except (Cancelled, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
            return 130
        sys.stderr.write("\n")
        print(f"Saved: {path}")
        return 0

    if not args.url:
        build_parser().print_usage(sys.stderr)
        return 2

    try:
        info = downloader.fetch_info(args.url, args.cookies_from_browser)
        print(f"{info.title}" + (f" — {info.uploader}" if info.uploader else ""))
        print("Available: " + ", ".join(tier_label(t) for t in info.tiers))
        if args.list:
            return 0

        if args.mp3:
            path = downloader.download_audio(info.url, args.output, on_progress=show,
                                             cookies_from_browser=args.cookies_from_browser)
            sys.stderr.write("\n")
            print(f"Saved: {path}")
            return 0

        mode = Mode(args.mode)
        selection = info.selection_for(args.quality, mode)
        size = selection.estimated_size
        print(f"Downloading {tier_label(selection.tier or args.quality)}"
              f" ({selection.video.get('vcodec')}, ≈{format_bytes(size)})"
              + (" and converting to HEVC" if mode is Mode.COMPATIBLE and selection.needs_conversion else ""))

        path = downloader.download(info.url, args.quality, mode, args.output,
                                   on_progress=show, cookies_from_browser=args.cookies_from_browser,
                                   upscale_to=args.upscale_to, upscale_content=upscale.Content(args.content))
        sys.stderr.write("\n")
        print(f"Saved: {path}")
        return 0
    except Cancelled:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except DownloadFailed as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
