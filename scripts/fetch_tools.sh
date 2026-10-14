#!/usr/bin/env bash
# Download static macOS builds of ffmpeg, ffprobe and deno into vendor/bin.
#
#   scripts/fetch_tools.sh [arm64|x86_64]
#
# ffmpeg/ffprobe: static builds from https://ffmpeg.martin-riedl.de (LGPL/GPL, see their site)
# realesrgan:     https://github.com/xinntao/Real-ESRGAN (AI upscaling)
# deno:           official release from https://github.com/denoland/deno (needed by yt-dlp
#                 to solve YouTube's JavaScript challenges; without it 4K formats go missing)
set -euo pipefail

ARCH="${1:-$(uname -m)}"
DENO_VERSION="${DENO_VERSION:-v2.9.5}"

case "$ARCH" in
  arm64|aarch64) FF_ARCH=arm64;  DENO_ARCH=aarch64 ;;
  x86_64|amd64)  FF_ARCH=amd64;  DENO_ARCH=x86_64 ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/vendor/bin"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$DEST"

fetch() {  # url output
  curl --fail --location --silent --show-error --retry 4 --retry-delay 3 -o "$2" "$1"
}

for tool in ffmpeg ffprobe; do
  echo "Downloading $tool ($FF_ARCH)…"
  fetch "https://ffmpeg.martin-riedl.de/redirect/latest/macos/$FF_ARCH/release/$tool.zip" "$WORK/$tool.zip"
  mkdir -p "$WORK/$tool"
  unzip -q -o "$WORK/$tool.zip" -d "$WORK/$tool"
  found="$(find "$WORK/$tool" -type f -name "$tool" | head -n 1)"
  [ -n "$found" ] || { echo "$tool binary not found in archive" >&2; exit 1; }
  install -m 0755 "$found" "$DEST/$tool"
done

echo "Downloading deno $DENO_VERSION ($DENO_ARCH)…"
fetch "https://github.com/denoland/deno/releases/download/$DENO_VERSION/deno-$DENO_ARCH-apple-darwin.zip" "$WORK/deno.zip"
unzip -q -o "$WORK/deno.zip" -d "$WORK/deno"
install -m 0755 "$WORK/deno/deno" "$DEST/deno"

# Real-ESRGAN (BSD-3-Clause) for AI upscaling. The macOS build is a universal binary
# with MoltenVK built in, so it runs on the GPU of both Apple Silicon and Intel Macs.
echo "Downloading Real-ESRGAN…"
fetch "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip" "$WORK/realesrgan.zip"
unzip -q -o "$WORK/realesrgan.zip" -d "$WORK/realesrgan"
install -m 0755 "$WORK/realesrgan/realesrgan-ncnn-vulkan" "$DEST/realesrgan-ncnn-vulkan"
MODELS="$ROOT/vendor/realesrgan-models"
rm -rf "$MODELS"
mkdir -p "$MODELS"
for model in realesrgan-x4plus realesr-animevideov3-x2 realesr-animevideov3-x3 realesr-animevideov3-x4; do
  cp "$WORK/realesrgan/models/$model.param" "$WORK/realesrgan/models/$model.bin" "$MODELS/"
done
# The faster real-life model is only published as a PyTorch checkpoint; convert it.
fetch "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth" \
  "$WORK/realesr-general-x4v3.pth"
"${PYTHON:-python3}" "$ROOT/scripts/convert_srvgg_to_ncnn.py" "$WORK/realesr-general-x4v3.pth" \
  "$MODELS/realesr-general-x4v3"

if [ "$(uname -s)" = "Darwin" ]; then
  xattr -cr "$DEST" 2>/dev/null || true
fi

for tool in ffmpeg ffprobe deno realesrgan-ncnn-vulkan; do
  file "$DEST/$tool" || true
done
echo "Tools installed in $DEST"
