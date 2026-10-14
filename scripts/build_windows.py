"""Build the Windows app and installer.

    python scripts/build_windows.py

Run on Windows (x64) with Python 3.11+ that includes Tk (python.org or GitHub's
setup-python). Produces:
    dist/YouTube Downloader/                       the app folder
    dist/YouTube-Downloader-<ver>-Windows-x64-Setup.exe   (if Inno Setup 6 is installed)
    dist/YouTube-Downloader-<ver>-Windows-x64.zip          (otherwise)

Bundled:
  * ffmpeg/ffprobe (BtbN GPL build: NVENC, Quick Sync, AMF, x264, x265)
  * deno (yt-dlp's JavaScript runtime for YouTube)
  * Real-ESRGAN ncnn-vulkan + models (works on any GPU)
  * ONNX Runtime with CUDA 13 + cuDNN 9, and Real-ESRGAN ONNX models in float16
    for NVIDIA RTX GPUs (tensor cores)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "YouTube Downloader"
DENO_VERSION = os.environ.get("DENO_VERSION", "v2.9.5")

FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
DENO_URL = f"https://github.com/denoland/deno/releases/download/{DENO_VERSION}/deno-x86_64-pc-windows-msvc.zip"
ESRGAN = "https://github.com/xinntao/Real-ESRGAN/releases/download"
REALESRGAN_NCNN_URL = f"{ESRGAN}/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip"
CHECKPOINTS = {  # onnx model name -> checkpoint URL
    "realesrgan-x4plus": f"{ESRGAN}/v0.1.0/RealESRGAN_x4plus.pth",
    "realesrgan-x2plus": f"{ESRGAN}/v0.2.1/RealESRGAN_x2plus.pth",
    "realesr-general-x4v3": f"{ESRGAN}/v0.2.5.0/realesr-general-x4v3.pth",
    "realesr-animevideov3": f"{ESRGAN}/v0.2.5.0/realesr-animevideov3.pth",
}
# CUDA DLLs that onnxruntime_providers_cuda.dll does not need.
SKIP_DLLS = re.compile(r"^(curand|nvblas|cufftw)", re.I)
MSVC_RUNTIME = ["msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll",
                "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll", "vcomp140.dll"]


def step(message: str) -> None:
    print(f"\n=== {message}", flush=True)


def run(*cmd, **kwargs) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    for attempt in range(1, 5):
        try:
            print(f"  downloading {url}", flush=True)
            with urllib.request.urlopen(url, timeout=120) as response, open(dest, "wb") as fh:
                shutil.copyfileobj(response, fh, 1 << 20)
            return dest
        except OSError as exc:
            print(f"  attempt {attempt} failed: {exc}")
            dest.unlink(missing_ok=True)
            time.sleep(2 ** attempt)
    raise SystemExit(f"Could not download {url}")


def version() -> str:
    text = (ROOT / "src" / "ytdownloader" / "__init__.py").read_text()
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def fetch_tools() -> None:
    cache = BUILD / "downloads"
    bin_dir = VENDOR / "bin"
    shutil.rmtree(VENDOR, ignore_errors=True)
    bin_dir.mkdir(parents=True)

    step("ffmpeg")
    with zipfile.ZipFile(download(FFMPEG_URL, cache / "ffmpeg.zip")) as z:
        for name in z.namelist():
            if re.search(r"/bin/(ffmpeg|ffprobe)\.exe$", name):
                (bin_dir / Path(name).name).write_bytes(z.read(name))

    step("deno")
    with zipfile.ZipFile(download(DENO_URL, cache / "deno.zip")) as z:
        (bin_dir / "deno.exe").write_bytes(z.read("deno.exe"))

    step("Real-ESRGAN (Vulkan)")
    models = VENDOR / "realesrgan-models"
    models.mkdir()
    with zipfile.ZipFile(download(REALESRGAN_NCNN_URL, cache / "realesrgan-ncnn.zip")) as z:
        for name in z.namelist():
            base = Path(name).name
            if base.endswith((".exe", ".dll")) and "/" not in name.strip("/"):
                (bin_dir / base).write_bytes(z.read(name))
            elif name.startswith("models/") and re.match(
                    r"(realesrgan-x4plus|realesr-animevideov3-x[234])\.(param|bin)$", base):
                (models / base).write_bytes(z.read(name))
    if not (bin_dir / "realesrgan-ncnn-vulkan.exe").exists():
        raise SystemExit("realesrgan-ncnn-vulkan.exe not found in the archive")

    step("AI models (ONNX for NVIDIA, ncnn for the fast real-life model)")
    onnx_dir = VENDOR / "onnx-models"
    onnx_dir.mkdir()
    for name, url in CHECKPOINTS.items():
        checkpoint = download(url, cache / Path(url).name)
        run(sys.executable, ROOT / "scripts" / "convert_to_onnx.py", checkpoint, onnx_dir / f"{name}.onnx")
    run(sys.executable, ROOT / "scripts" / "convert_srvgg_to_ncnn.py",
        cache / "realesr-general-x4v3.pth", models / "realesr-general-x4v3")


def copy_cuda_libraries(app_dir: Path) -> None:
    """Put CUDA/cuDNN next to onnxruntime_providers_cuda.dll so Windows finds them."""
    import onnxruntime

    site = Path(onnxruntime.__file__).resolve().parents[1]
    capi = app_dir / "_internal" / "onnxruntime" / "capi"
    if not (capi / "onnxruntime_providers_cuda.dll").exists():
        raise SystemExit(f"onnxruntime_providers_cuda.dll is missing from {capi}")
    total = 0
    for dll in sorted((site / "nvidia").rglob("*.dll")):
        if SKIP_DLLS.match(dll.name):
            continue
        shutil.copy2(dll, capi / dll.name)
        total += dll.stat().st_size
        print(f"  {dll.name} ({dll.stat().st_size / 1e6:.0f} MB)")
    if total == 0:
        raise SystemExit("No NVIDIA DLLs found; is onnxruntime-gpu[cuda,cudnn] installed?")
    print(f"  CUDA libraries: {total / 1e9:.2f} GB")
    # App-local Visual C++ runtime, in case the PC doesn't have the redistributable.
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    for name in MSVC_RUNTIME:
        for target in (capi, app_dir / "_internal"):
            if (system32 / name).exists() and not (target / name).exists():
                shutil.copy2(system32 / name, target / name)


def find_iscc() -> Path | None:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs")):
        if base:
            candidate = Path(base) / "Inno Setup 6" / "ISCC.exe"
            if candidate.exists():
                return candidate
    found = shutil.which("iscc")
    return Path(found) if found else None


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("Run this on Windows.")
    ver = version()
    run(sys.executable, "-c", "import tkinter; print('Tk', tkinter.Tcl().eval('info patchlevel'))")

    step("Python packages")
    run(sys.executable, "-m", "pip", "install", "--upgrade", "-r", ROOT / "requirements-windows-build.txt")

    fetch_tools()

    step("Icon")
    run(sys.executable, ROOT / "scripts" / "make_icon.py", BUILD / "icon")

    step("PyInstaller")
    app_dir = DIST / APP_NAME
    shutil.rmtree(app_dir, ignore_errors=True)
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", DIST,
        "--workpath", BUILD / "pyinstaller", ROOT / "packaging" / "windows" / "YouTubeDownloader.spec")

    step("CUDA libraries")
    copy_cuda_libraries(app_dir)

    step("Self-test")
    run(app_dir / "youtube-downloader-cli.exe", "--self-test")

    size = sum(f.stat().st_size for f in app_dir.rglob("*") if f.is_file())
    print(f"App folder: {size / 1e9:.2f} GB")

    iscc = find_iscc()
    if iscc:
        step("Installer (Inno Setup)")
        run(iscc, f"/DAppVersion={ver}", ROOT / "packaging" / "windows" / "installer.iss")
    else:
        step("Zip (Inno Setup not found)")
        archive = DIST / f"YouTube-Downloader-{ver}-Windows-x64"
        shutil.make_archive(str(archive), "zip", DIST, APP_NAME)
    for artifact in sorted(DIST.glob("YouTube-Downloader-*")):
        print(f"Built: {artifact} ({artifact.stat().st_size / 1e9:.2f} GB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
