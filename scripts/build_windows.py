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
  * ONNX Runtime and Real-ESRGAN ONNX models in float16 for NVIDIA RTX GPUs (tensor cores)

Not bundled: the NVIDIA package (CUDA 13, cuDNN 9, TensorRT 10; well over 1 GB).
It goes into dist/nvidia-pack/ as a zip split into parts of < 2 GB, plus
nvidia_pack.json, and is published as the GitHub release "nvidia-<version>". Its
version is a hash of the DLLs, so it is only rebuilt and uploaded when they change;
if that release already exists, the build reuses its nvidia_pack.json. The
installer downloads the package on PCs with an NVIDIA GPU; the app can too.
"""

from __future__ import annotations

import hashlib
import json
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
# CUDA/TensorRT DLLs that ONNX Runtime's CUDA and TensorRT providers do not need.
SKIP_DLLS = re.compile(r"^(curand|nvblas|cufftw|nvinfer_lean|nvinfer_dispatch|nvinfer_vc_plugin)", re.I)
REQUIRED_DLLS = ["cublas64_13.dll", "cublasLt64_13.dll", "cudart64_13.dll", "cudnn64_9.dll",
                 "cufft64_12.dll", "nvrtc64_130_0.dll"]
TENSORRT_DLLS = ["nvinfer_10.dll", "nvonnxparser_10.dll"]
TENSORRT_PIP = ["tensorrt-cu13-libs==10.16.*", "--extra-index-url", "https://pypi.nvidia.com"]
RELEASES = "https://github.com/haasje33/youtubedownloader/releases/download"
PART_SIZE = 1_900_000_000  # GitHub release files must be < 2 GiB
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


def nvidia_dlls() -> list[Path]:
    """CUDA, cuDNN and TensorRT DLLs from NVIDIA's pip packages."""
    import onnxruntime

    site = Path(onnxruntime.__file__).resolve().parents[1]
    found: dict[str, Path] = {}
    for folder in (site / "nvidia", site / "tensorrt_libs"):
        for dll in sorted(folder.rglob("*.dll")) if folder.is_dir() else []:
            if not SKIP_DLLS.match(dll.name):
                found.setdefault(dll.name.lower(), dll)
    names = {p.name for p in found.values()}
    missing = [n for n in REQUIRED_DLLS if n not in names]
    if missing:
        raise SystemExit(f"NVIDIA DLLs missing: {', '.join(missing)}; is onnxruntime-gpu[cuda,cudnn] installed?")
    if all(n in names for n in TENSORRT_DLLS):
        print("  TensorRT: included")
    else:
        print("::warning::TensorRT libraries not found; the NVIDIA package will only have CUDA")
    return sorted(found.values(), key=lambda p: p.name.lower())


def pack_version(dlls: list[Path]) -> str:
    digest = hashlib.sha256()
    for dll in dlls:
        file_digest = hashlib.sha256()
        with open(dll, "rb") as fh:
            while chunk := fh.read(1 << 22):
                file_digest.update(chunk)
        digest.update(f"{dll.name.lower()} {file_digest.hexdigest()}\n".encode())
    trt = "trt-" if any(d.name == "nvinfer_10.dll" for d in dlls) else ""
    return f"cu13-{trt}{digest.hexdigest()[:10]}"


def published_pack_info(version: str) -> dict | None:
    url = f"{RELEASES}/nvidia-{version}/nvidia_pack.json"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return json.loads(response.read())
    except OSError:
        return None


def build_nvidia_pack(app_vendor: Path) -> None:
    """Make (or reuse) the NVIDIA package and put its nvidia_pack.json into the app."""
    dlls = nvidia_dlls()
    total = sum(d.stat().st_size for d in dlls)
    for dll in dlls:
        print(f"  {dll.name} ({dll.stat().st_size / 1e6:.0f} MB)")
    print(f"  {len(dlls)} DLLs, {total / 1e9:.2f} GB")
    version = pack_version(dlls)
    print(f"  version {version}")
    out = DIST / "nvidia-pack"
    shutil.rmtree(out, ignore_errors=True)
    info = published_pack_info(version)
    if info is not None:
        print(f"  already published as release nvidia-{version}; reusing it")
    else:
        out.mkdir(parents=True)
        archive = BUILD / f"YouTube-Downloader-NVIDIA-{version}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for dll in dlls:
                z.write(dll, dll.name)
        size = archive.stat().st_size
        print(f"  archive: {size / 1e9:.2f} GB")
        whole, parts = hashlib.sha256(), []
        with open(archive, "rb") as fh:
            index = 1
            while True:
                name = f"{archive.stem}.part{index}"
                part_digest, written = hashlib.sha256(), 0
                with open(out / name, "wb") as part:
                    while written < PART_SIZE and (chunk := fh.read(min(1 << 22, PART_SIZE - written))):
                        part.write(chunk)
                        part_digest.update(chunk)
                        whole.update(chunk)
                        written += len(chunk)
                if written == 0:
                    (out / name).unlink()
                    break
                parts.append({"url": f"{RELEASES}/nvidia-{version}/{name}",
                              "sha256": part_digest.hexdigest(), "size": written})
                index += 1
        archive.unlink()
        info = {"version": version, "sha256": whole.hexdigest(), "size": size, "parts": parts}
        (out / "nvidia_pack.json").write_text(json.dumps(info, indent=1))
        print(f"  {len(parts)} part(s) in {out}")
    (app_vendor / "nvidia_pack.json").write_text(json.dumps(info, indent=1))
    write_installer_include(info)


def write_installer_include(info: dict) -> None:
    """Pack details for installer.iss (#include'd from build/nvidia_pack.iss)."""
    base = info["parts"][0]["url"].rsplit("/", 1)[0] + "/"
    lines = [f'#define NvidiaPackVersion "{info["version"]}"',
             f'#define NvidiaPackSizeMB "{int(info["size"]) // 1_000_000}"',
             f'#define NvidiaPackBaseUrl "{base}"',
             f'#define NvidiaPackParts "{len(info["parts"])}"',
             "[Code]",
             "function PackPartName(I: Integer): String;",
             "begin",
             "  case I of"]
    lines += [f"    {i}: Result := '{p['url'].rsplit('/', 1)[1]}';" for i, p in enumerate(info["parts"])]
    lines += ["  end;", "end;", "",
              "function PackPartSha256(I: Integer): String;",
              "begin",
              "  case I of"]
    lines += [f"    {i}: Result := '{p['sha256']}';" for i, p in enumerate(info["parts"])]
    lines += ["  end;", "end;", "", "[Setup]"]  # back to the section the include sits in
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / "nvidia_pack.iss").write_text("\n".join(lines))


def copy_msvc_runtime(app_dir: Path) -> None:
    """App-local Visual C++ runtime, in case the PC doesn't have the redistributable."""
    capi = app_dir / "_internal" / "onnxruntime" / "capi"
    if not (capi / "onnxruntime_providers_cuda.dll").exists():
        raise SystemExit(f"onnxruntime_providers_cuda.dll is missing from {capi}")
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
    try:  # TensorRT is optional: without it the NVIDIA package has CUDA only
        run(sys.executable, "-m", "pip", "install", *TENSORRT_PIP)
    except subprocess.CalledProcessError:
        print("::warning::Installing TensorRT failed")

    fetch_tools()

    step("NVIDIA package (CUDA, cuDNN, TensorRT)")
    build_nvidia_pack(VENDOR)

    step("Icon")
    run(sys.executable, ROOT / "scripts" / "make_icon.py", BUILD / "icon")

    step("PyInstaller")
    app_dir = DIST / APP_NAME
    shutil.rmtree(app_dir, ignore_errors=True)
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", DIST,
        "--workpath", BUILD / "pyinstaller", ROOT / "packaging" / "windows" / "YouTubeDownloader.spec")

    step("Visual C++ runtime")
    copy_msvc_runtime(app_dir)
    # The NVIDIA libraries belong in the separate package, not in the app.
    bundled = [f for f in app_dir.rglob("*.dll") if re.match(r"(cublas|cudnn|cufft|cudart|nvrtc|nvinfer|nvonnx|nvJit)",
                                                             f.name, re.I)]
    for dll in bundled:
        print(f"  removing {dll.relative_to(app_dir)} (it is in the NVIDIA package)")
        dll.unlink()

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
    for artifact in sorted(DIST.glob("YouTube-Downloader-*")) + sorted(DIST.glob("nvidia-pack/*")):
        print(f"Built: {artifact} ({artifact.stat().st_size / 1e9:.2f} GB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
