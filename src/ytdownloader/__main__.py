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

    from .settings import setup_logging

    if args:
        setup_logging(verbose="-v" in args or "--verbose" in args)
        from .cli import main as cli_main

        return cli_main(args)

    setup_logging()
    from .gui import run

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
