# Changelog

## 0.5.1

- **In-app updates.** The app checks for new versions at startup (once a day) and under
  "Check for Updates…". It shows what's new, then downloads, verifies and installs the
  update and restarts. On Windows, you confirm the administrator prompt.
- **The download engine (yt-dlp) updates itself** once a day, so downloads keep working
  when YouTube changes something. If a download fails, the app looks for a newer
  yt-dlp right away and offers to restart with it.
- Releases, with SHA-256 checksums, are published at github.com/haasje33/youtubedownloader/releases.
- macOS: only Apple Silicon Macs are supported from this version on.
- Windows: the size estimate now says "converted to H.264" instead of HEVC.

## 0.4.0

- Windows app, with AI upscaling on NVIDIA RTX graphics cards (CUDA, tensor cores),
  a native 2× model, and hardware video encoding (NVENC, Quick Sync, AMF).

## 0.3.0

- Faster AI upscaling with live progress and time left, and a new "Real-life · fast" model.

## 0.2.0

- Highest quality selected by default, MP3 downloads, AI upscaling with Real-ESRGAN.

## 0.1.0

- First version: YouTube downloads as MP4 up to 4K on macOS.
