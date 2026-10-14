# YouTube Downloader for macOS

A small Mac app that downloads YouTube videos as **MP4** in any quality YouTube offers, from 144p up to **4K (2160p)**.

- Paste a link, click **Get Video Info**, pick a quality, click **Download**.
- Only the qualities that exist for that video are listed. You also see the estimated download size.
- Two video format options:
  - **Most compatible (H.264 / HEVC)**, the default. The file plays in QuickTime, Photos, iMovie and on iPhone/iPad. YouTube only offers H.264 up to 1080p, so for 1440p and 4K the app downloads the best stream and converts it to HEVC (H.265). The conversion uses the Mac's hardware video encoder.
  - **Original stream (AV1 / VP9)**. No conversion, so it's the fastest and loses no quality. QuickTime can't play every one of these files, but VLC and IINA can.
- Shows progress, speed and time left. You can cancel a download, and the files are saved to `~/Downloads` by default.
- Optional "Use cookies from" setting (Safari, Chrome, Firefox and others) for age-restricted videos, or when YouTube asks you to sign in.

Everything it needs is inside the app: [yt-dlp](https://github.com/yt-dlp/yt-dlp) for downloading, ffmpeg for merging and converting, and Deno, which yt-dlp needs to solve YouTube's JavaScript challenges. Without Deno, the high-resolution formats go missing.

> Only download videos you have the right to download (for example your own uploads, or videos under a license that allows it). Respect YouTube's Terms of Service and copyright law.

## Installing

1. Download the `.dmg` for your Mac from the [Releases](../../releases) page, or from the latest run under the repository's **Actions** tab (artifact *YouTube-Downloader-macOS-…*):
   - `arm64`: Apple Silicon (M1, M2, M3, M4, …)
   - `x86_64`: Intel Macs
2. Open the `.dmg` and drag **YouTube Downloader** into **Applications**.
3. The app is not notarized by Apple, so the first time you open it macOS says it "could not verify that YouTube Downloader is free of malware". To allow it:
   - Try to open the app once and click **Done** in the warning.
   - Open **System Settings → Privacy & Security**, scroll down to **Security**, click **Open Anyway** next to the YouTube Downloader message, and enter your password.
   - Open the app again and click **Open Anyway** in the dialog. macOS remembers this choice.

   Or run this once in Terminal: `xattr -dr com.apple.quarantine "/Applications/YouTube Downloader.app"`

   On macOS 15 (Sequoia) and later, right-click → Open no longer skips this warning.

It needs macOS 11 (Big Sur) or later.

## Command line

The same executable also works as a command-line tool:

```sh
APP="/Applications/YouTube Downloader.app/Contents/MacOS/YouTube Downloader"
"$APP" "https://www.youtube.com/watch?v=VIDEO_ID" --list          # show available qualities
"$APP" "https://www.youtube.com/watch?v=VIDEO_ID" -q 2160         # best up to 4K, QuickTime-compatible
"$APP" "https://www.youtube.com/watch?v=VIDEO_ID" -q 1080 -m original -o ~/Movies
"$APP" --self-test                                                # check the bundled tools
```

## Building it yourself

GitHub Actions builds the app on every push (`.github/workflows/build-macos.yml`). Pushing a tag like `v0.1.0` publishes a release with both `.dmg` files. The workflow also rebuilds once a week, so the app keeps getting the newest yt-dlp.

To build on a Mac you need Python 3.11+ with Tk (the python.org installer includes Tk):

```sh
python3 -m venv .venv && source .venv/bin/activate
scripts/build_macos.sh
open "dist/YouTube Downloader.app"
```

The script downloads static ffmpeg/ffprobe builds and Deno into `vendor/bin`, draws the icon, bundles everything with PyInstaller, ad-hoc signs the app, and creates `dist/YouTube-Downloader-<version>-macOS-<arch>.dmg`.

### Running from source

```sh
brew install ffmpeg deno
python3 -m pip install -r requirements-dev.txt
PYTHONPATH=src python3 -m ytdownloader          # GUI
PYTHONPATH=src python3 -m ytdownloader URL -q 1080   # CLI
python3 -m pytest                                # tests (need ffmpeg)
```

## Troubleshooting

- **"Sign in to confirm you're not a bot"** or age-restricted videos: choose your browser under *Use cookies from* and make sure you're logged in to YouTube in that browser. Safari cookies only work if the app has Full Disk Access (System Settings → Privacy & Security).
- **Downloads suddenly fail for every video**: YouTube changed something. Get a newer build (the weekly CI build includes the latest yt-dlp).
- **Logs** are in `~/Library/Logs/YouTube Downloader/youtube-downloader.log`.

## Project layout

```
src/ytdownloader/
  formats.py     quality tiers and stream selection (pure, unit-tested)
  downloader.py  yt-dlp integration, progress reporting, HEVC conversion
  tools.py       finds the bundled ffmpeg / ffprobe / deno
  gui.py         Tk user interface
  cli.py         command-line interface and --self-test
packaging/       PyInstaller spec and entry script
scripts/         build_macos.sh, fetch_tools.sh, make_icon.py
tests/           format-selection tests and offline end-to-end pipeline tests
```
