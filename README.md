# YouTube Downloader for macOS and Windows

A small app for Macs with Apple Silicon and for Windows PCs that downloads YouTube videos as **MP4** in any quality YouTube offers, from 144p up to **4K (2160p)**, or as **MP3** audio. It can also **upscale low-resolution videos with AI**.

- Paste a link, click **Get Video Info**, pick a quality, click **Download**.
- The highest available quality is selected by default. Only the qualities that exist for that video are listed, with the estimated download size.
- **Audio only (MP3)**, the last entry in the quality list, saves a high-quality MP3 (VBR ~245 kbps) with the title, channel and cover art.
- Two video format options:
  - **Most compatible (H.264 / HEVC)**, the default. The file plays in QuickTime, Photos, iMovie and on iPhone/iPad. YouTube only offers H.264 up to 1080p, so for 1440p and 4K the app downloads the best stream and converts it to HEVC (H.265). The conversion uses the Mac's hardware video encoder.
  - **Original stream (AV1 / VP9)**. No conversion, so it's the fastest and loses no quality. QuickTime can't play every one of these files, but VLC and IINA can.
- Shows progress, speed and time left. You can cancel a download, and the files are saved to `~/Downloads` by default.
- **AI upscale** (to 1080p, 1440p or 4K) uses [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) on the Mac's own graphics chip, through Metal. It adds real detail instead of just enlarging the picture.
  - Three models:
    - **Real-life · best quality** (`realesrgan-x4plus`) gives the most detail and is the slowest.
    - **Real-life · fast** (`realesr-general-x4v3`) is about 17× quicker, with slightly less fine detail.
    - **Animation / cartoons** (`realesr-animevideov3`) is fast and made for drawn content.
  - Measured speeds on an Apple Silicon Mac (GitHub's M1-class machine), upscaling 360p video 4×:

    | Model | Frames per second | 3-minute video at 30 fps |
    |---|---|---|
    | Real-life · best quality | ~0.37 | ~4 hours |
    | Real-life · fast | ~6.3 | ~15 minutes |
    | Animation / cartoons | ~11.7 | ~8 minutes |

    A fanless MacBook Air can be slower on long jobs, once it heats up and throttles.
  - It works on downloads that are below the chosen resolution, and on videos already on your Mac, through **Upscale a Video File…**.
  - It is slow: every frame goes through the AI model. With the best-quality model, a few minutes of video can take an hour or more.
  - The app shows frames per second and time left from the first frame on, and keeps the Mac awake meanwhile.
  - Repeated identical frames, such as still images, are upscaled once and reused.
  - The model upscales 4×. From 360p it really adds detail up to 1440p; going to 4K enlarges that result further.
  - On Windows, see [AI upscaling on Windows](#ai-upscaling-on-windows).
- Optional "Use cookies from" setting (Safari, Chrome, Firefox and others) for age-restricted videos, or when YouTube asks you to sign in.

Everything it needs is inside the app: [yt-dlp](https://github.com/yt-dlp/yt-dlp) for downloading, ffmpeg for merging and converting, Real-ESRGAN (BSD-3-Clause) for AI upscaling, and Deno, which yt-dlp needs to solve YouTube's JavaScript challenges. Without Deno, the high-resolution formats go missing.

> Only download videos you have the right to download (for example your own uploads, or videos under a license that allows it). Respect YouTube's Terms of Service and copyright law.

## Windows

The Windows app has the same features, with Windows-specific video handling and its own AI upscaling engine.

- **Most compatible** converts 1440p/4K to **H.264**, which plays in every Windows player and editor. HEVC would need a paid extension on Windows.
  - Conversions and upscaled videos are encoded on the graphics card when possible: NVIDIA NVENC, Intel Quick Sync or AMD AMF, with software x264 as the fallback.
- Settings are stored in `%APPDATA%\YouTube Downloader`, logs in `%LOCALAPPDATA%\YouTube Downloader\Logs`.

### AI upscaling on Windows

With an **NVIDIA GeForce RTX** card, the app upscales with ONNX Runtime on **CUDA + cuDNN**.
- The Real-ESRGAN models run in float16 on the tensor cores.
- Frames go straight from the decoder to the GPU and on to the NVENC encoder, without temporary files.
- The upscale hint in the app shows which engine is in use, for example *"NVIDIA GeForce RTX 4070 Ti SUPER (CUDA, tensor cores)"*.

Windows-specific models (ONNX, converted from the official Real-ESRGAN checkpoints by `scripts/convert_to_onnx.py`):

| Model | Used for |
|---|---|
| `realesrgan-x4plus` | Real-life · best quality, enlarging more than 2× |
| `realesrgan-x2plus` | Real-life · best quality, enlarging up to 2× (for example 720p → 1440p, 1080p → 4K): a native 2× model, sharper and about 4× less work than 4× followed by shrinking |
| `realesr-general-x4v3` | Real-life · fast |
| `realesr-animevideov3` | Animation / cartoons |

Requirements and fallbacks:
- The NVIDIA engine needs an RTX 20-series or newer card and **NVIDIA driver 580 or newer** (CUDA 13).
- On other graphics cards (AMD, Intel), or when CUDA is unavailable, the app uses Real-ESRGAN on Vulkan, like the Mac version.
- The CUDA and cuDNN libraries make the Windows download large (about 1.3 GB).
- NVIDIA's own *RTX Video Super Resolution* isn't used: its SDK is only available to registered NVIDIA developers and can't be bundled.

### Installing on Windows

1. Download `YouTube-Downloader-<version>-Windows-x64-Setup.exe` from the [releases page](https://github.com/haasje33/youtubedownloader-releases/releases).
2. Run it. The app is not code-signed, so Windows SmartScreen will say *"Windows protected your PC"*. Click **More info → Run anyway**.
3. Start **YouTube Downloader** from the Start menu.

Command line: `"C:\Program Files\YouTube Downloader\youtube-downloader-cli.exe" --help`. To check the NVIDIA engine and encoders, run `--self-test`.

To build it yourself on Windows (Python 3.11+ from python.org), run `python scripts\build_windows.py`. It needs Inno Setup 6 for the installer; without it, you get a zip instead.

## Installing on macOS

1. Download `YouTube-Downloader-<version>-macOS-arm64.dmg` from the [releases page](https://github.com/haasje33/youtubedownloader-releases/releases). It runs on Macs with Apple Silicon (M1 and newer); Intel Macs are no longer supported from v0.5.0.
2. Open the `.dmg` and drag **YouTube Downloader** into **Applications**.
3. The app is not notarized by Apple, so the first time you open it macOS says it "could not verify that YouTube Downloader is free of malware". To allow it:
   - Try to open the app once and click **Done** in the warning.
   - Open **System Settings → Privacy & Security**, scroll down to **Security**, click **Open Anyway** next to the YouTube Downloader message, and enter your password.
   - Open the app again and click **Open Anyway** in the dialog. macOS remembers this choice.

   Or run this once in Terminal: `xattr -dr com.apple.quarantine "/Applications/YouTube Downloader.app"`

   On macOS 15 (Sequoia) and later, right-click → Open no longer skips this warning.

It needs macOS 11 (Big Sur) or later.

## Updates

The app checks for a new version at startup (once a day) and under **Check for Updates…**
(in the app menu on a Mac, under **Help** on Windows).
- You see what's new and can choose **Update Now**, **Later** or **Skip This Version**.
- The update is downloaded, its SHA-256 checksum is verified, and the app restarts with the new version.
- On Windows, you confirm the administrator prompt.
- An update never interrupts a running download or upscale. It waits until that job is done.

The download engine, yt-dlp, updates itself separately, once a day, from PyPI. Each new version is checked in a separate process before it is used, from the next start on. If a download fails, the app looks for a newer yt-dlp right away and offers to restart with it. If a downloaded yt-dlp ever doesn't work, the app falls back to the version it shipped with.

## Command line

The same executable also works as a command-line tool:

```sh
APP="/Applications/YouTube Downloader.app/Contents/MacOS/YouTube Downloader"
"$APP" "https://www.youtube.com/watch?v=VIDEO_ID" --list          # show available qualities
"$APP" "https://www.youtube.com/watch?v=VIDEO_ID" -q 2160         # best up to 4K, QuickTime-compatible
"$APP" "https://www.youtube.com/watch?v=VIDEO_ID" -q 1080 -m original -o ~/Movies
"$APP" "https://www.youtube.com/watch?v=VIDEO_ID" --mp3                   # audio only
"$APP" "https://www.youtube.com/watch?v=VIDEO_ID" --upscale-to 1080       # AI upscale if below 1080p
"$APP" --upscale-file old-video.mp4 --upscale-to 1080 --content fast     # upscale a local file
                                                   # (--content realistic | fast | animation)
"$APP" --self-test                                                # check the bundled tools
"$APP" --check-update                                             # is there a newer app version?
"$APP" --update-ytdlp                                             # fetch the newest yt-dlp now
```

## Building it yourself

`.github/workflows/build.yml` runs the tests on every push. The apps (Apple Silicon `.dmg` plus update `.zip`, and the Windows installer) are built when you push a version tag, or when you start the workflow by hand (Actions → Build apps → Run workflow, optionally with the upscaler benchmark).

To release:
1. Add a section for the version to `CHANGELOG.md`; it becomes the release notes.
2. Set `__version__` in `src/ytdownloader/__init__.py`.
3. Push a tag `vX.Y.Z`.

The workflow publishes the files and `SHA256SUMS.txt` to `haasje33/youtubedownloader-releases`, where the in-app updater finds them. This needs the `RELEASES_TOKEN` secret: a fine-grained token with *Contents: read and write* on that repository.

To build on a Mac you need Python 3.11+ with Tk (the python.org installer includes Tk):

```sh
python3 -m venv .venv && source .venv/bin/activate
scripts/build_macos.sh
open "dist/YouTube Downloader.app"
```

The script downloads static ffmpeg/ffprobe builds, Deno and Real-ESRGAN into `vendor/`, draws the icon, bundles everything with PyInstaller, ad-hoc signs the app, and creates `dist/YouTube-Downloader-<version>-macOS-<arch>.dmg`.

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
- **Downloads suddenly fail for every video**: YouTube changed something. The app updates yt-dlp by itself; to force it, run `--update-ytdlp` and restart the app.
- **AI upscale fails** with a Vulkan/GPU error: the graphics card isn't supported by Real-ESRGAN.
- **Logs** are in `~/Library/Logs/YouTube Downloader/youtube-downloader.log` (Mac) and `%LOCALAPPDATA%\YouTube Downloader\Logs\youtube-downloader.log` (Windows).

## Project layout

```
src/ytdownloader/
  formats.py     quality tiers and stream selection (pure, unit-tested)
  downloader.py  yt-dlp integration, MP3, progress reporting, HEVC conversion
  upscale.py     AI upscaling: model choice, Real-ESRGAN (Vulkan/Metal) pipeline
  onnx_engine.py AI upscaling on NVIDIA GPUs (ONNX Runtime, CUDA, float16)
  tools.py       finds the bundled ffmpeg / ffprobe / deno / realesrgan
  gui.py         Tk user interface
  cli.py         command-line interface and --self-test
  updater.py     in-app updates from the releases repository
  ytdlp_update.py  keeps yt-dlp current between app updates
packaging/       PyInstaller specs (macOS, windows/), Inno Setup script, entry script
scripts/         build_macos.sh, fetch_tools.sh, build_windows.py, make_icon.py,
                 convert_srvgg_to_ncnn.py (Fast model for Real-ESRGAN/ncnn),
                 convert_to_onnx.py (models for the NVIDIA engine), bench_upscale.py
tests/           format-selection tests and offline end-to-end pipeline tests
```
