"""Entry point: GUI when started without arguments, CLI otherwise."""

from __future__ import annotations

import os
import sys


def main() -> int:
    # A windowed macOS app has no usable stdout/stderr; yt-dlp must not crash on them.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    # Finder may pass a "-psn_..." process serial number; ignore it.
    args = [a for a in sys.argv[1:] if not a.startswith("-psn_")]

    if args[:1] == ["--validate-ytdlp"] and len(args) == 3:
        from .ytdlp_update import run_validation

        return run_validation(args[1], args[2])

    from .settings import setup_logging
    from .ytdlp_update import activate

    setup_logging(verbose="-v" in args or "--verbose" in args)
    activate()  # use a newer downloaded yt-dlp; must run before anything imports yt_dlp

    if args:
        from .cli import main as cli_main

        return cli_main(args)

    from .gui import run

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
