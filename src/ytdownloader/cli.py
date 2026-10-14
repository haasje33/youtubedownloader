"""Command line interface (also used for CI smoke tests of the packaged app)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import APP_NAME, __version__, downloader, tools
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
    parser.add_argument("-l", "--list", action="store_true", help="only list available qualities")
    parser.add_argument("--self-test", action="store_true", help="check bundled tools and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging to stderr")
    return parser


def self_test() -> int:
    import yt_dlp

    print(f"{APP_NAME} {__version__}")
    print(f"yt-dlp {yt_dlp.version.__version__}")
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
    if toolset.ffmpeg:
        encoders = tools.ffmpeg_encoders(toolset.ffmpeg)
        wanted = ["hevc_videotoolbox", "h264_videotoolbox", "libx265", "libx264", "aac"]
        print("encoders: " + ", ".join(f"{e}={'yes' if e in encoders else 'no'}" for e in wanted))
        if not ({"hevc_videotoolbox", "libx265", "libx264"} & encoders):
            ok = False
    print("self-test: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.url:
        build_parser().print_usage(sys.stderr)
        return 2

    try:
        info = downloader.fetch_info(args.url, args.cookies_from_browser)
        print(f"{info.title}" + (f" — {info.uploader}" if info.uploader else ""))
        print("Available: " + ", ".join(tier_label(t) for t in info.tiers))
        if args.list:
            return 0

        mode = Mode(args.mode)
        selection = info.selection_for(args.quality, mode)
        size = selection.estimated_size
        print(f"Downloading {tier_label(selection.tier or args.quality)}"
              f" ({selection.video.get('vcodec')}, ≈{format_bytes(size)})"
              + (" and converting to HEVC" if mode is Mode.COMPATIBLE and selection.needs_conversion else ""))

        last = {"line": ""}

        def show(p: Progress) -> None:
            pct = f"{p.fraction * 100:5.1f}% " if p.fraction is not None else ""
            line = f"\r{pct}{p.detail}"[:120]
            sys.stderr.write(line.ljust(len(last["line"])))
            sys.stderr.flush()
            last["line"] = line

        path = downloader.download(info.url, args.quality, mode, args.output,
                                   on_progress=show, cookies_from_browser=args.cookies_from_browser)
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
