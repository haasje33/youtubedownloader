#!/usr/bin/env bash
# Build "YouTube Downloader.app" and a .dmg for the current Mac's architecture.
#
#   scripts/build_macos.sh
#
# Output: dist/YouTube Downloader.app and dist/YouTube-Downloader-<version>-macOS-<arch>.dmg
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_NAME="YouTube Downloader"
ARCH="$(uname -m)"
PYTHON="${PYTHON:-python3}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/ytdownloader/__init__.py)"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This script must run on macOS." >&2
  exit 1
fi

"$PYTHON" -c 'import tkinter; print("Tk", tkinter.Tcl().eval("info patchlevel"))'
"$PYTHON" -m pip install --upgrade -r requirements-build.txt

for tool in ffmpeg ffprobe deno realesrgan-ncnn-vulkan; do
  if [ ! -x "vendor/bin/$tool" ] || [ ! -d vendor/realesrgan-models ]; then
    scripts/fetch_tools.sh "$ARCH"
    break
  fi
done

"$PYTHON" scripts/make_icon.py build/icon
rm -rf "dist/$APP_NAME" "dist/$APP_NAME.app"
"$PYTHON" -m PyInstaller --noconfirm --clean --distpath dist --workpath build/pyinstaller \
  packaging/YouTubeDownloader.spec

APP="dist/$APP_NAME.app"
# Ad-hoc signature so the app (and the bundled tools) run on Apple Silicon.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

echo "--- self-test ---"
"$APP/Contents/MacOS/$APP_NAME" --self-test

DMG="dist/YouTube-Downloader-$VERSION-macOS-$ARCH.dmg"
STAGE="build/dmg"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
for attempt in 1 2 3 4; do
  if hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG"; then
    break
  fi
  echo "hdiutil failed (attempt $attempt), retrying…" >&2
  sleep $((attempt * 5))
done
[ -f "$DMG" ] || { echo "Could not create $DMG" >&2; exit 1; }

echo
echo "Built: $APP"
echo "Disk image: $DMG"
