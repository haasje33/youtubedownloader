# Roadmap: v0.5.0 → v1.0.0

Platforms from v0.5.1 on: **macOS on Apple Silicon** and **Windows (x64)**. The Intel Mac build is dropped.
Releases are published on this repository's
[releases page](https://github.com/haasje33/youtubedownloader/releases),
which is where the in-app updater downloads them from.

Each item links to its issue.

## v0.4.x: bug fixes
Fixes for problems found while testing the Windows app (v0.4.0).

## v0.5.0: Updates
- Release pipeline: Apple Silicon and Windows only, releases with checksums on this repository's releases page (#1)
- In-app updater: check, download, verify, install and restart, on Windows with the normal administrator prompt (#2)
- Automatic yt-dlp updates (#3)
- Released as **v0.5.1**, the last manual install. v0.6.0 is the first update installed by the updater.

## v0.6.0: Queue, playlists, notifications, smarter upscaling
(The planned v0.6.0 and v0.7.0, combined.)
- Download queue (#4)
- Playlists and channels (#5)
- Notifications when items and the queue finish (#6)
- Before/after preview with time estimates (#7)
- TensorRT engine for NVIDIA, with the same quality as CUDA (#8)
- Small Windows installer; the installer downloads the NVIDIA package (#9)

## v0.8.0: Long jobs and extras
- Resume interrupted upscales (#10)
- Subtitles (#11)
- Clips: download only part of a video (#12)

## v0.9.0: Release candidate
- Feature freeze, polish, user guide, test checklist (#13)

## v1.0.0: Stable release

## Backlog (after v1.0)
- 60 fps frame interpolation, RIFE (#14)
- Code signing, if certificates are ever available (#15)
