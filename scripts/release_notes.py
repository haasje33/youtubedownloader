"""Print the CHANGELOG.md section for a version (used as the release notes).

    python scripts/release_notes.py 0.5.0
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def section(version: str, text: str) -> str:
    match = re.search(rf"^## \[?v?{re.escape(version)}\]?[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    return match.group(1).strip() + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    sys.stdout.write(section(sys.argv[1].lstrip("v"), CHANGELOG.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
