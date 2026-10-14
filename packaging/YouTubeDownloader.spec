# PyInstaller spec for the macOS app bundle. Build with scripts/build_macos.sh.
# -*- mode: python ; coding: utf-8 -*-
import os
import re

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
APP_NAME = "YouTube Downloader"

with open(os.path.join(ROOT, "src", "ytdownloader", "__init__.py")) as fh:
    VERSION = re.search(r'__version__ = "([^"]+)"', fh.read()).group(1)

binaries = []
for tool in ("ffmpeg", "ffprobe", "deno", "realesrgan-ncnn-vulkan"):
    path = os.path.join(ROOT, "vendor", "bin", tool)
    if not os.path.exists(path):
        raise SystemExit(f"Missing {path}. Run scripts/fetch_tools.sh first.")
    binaries.append((path, "bin"))

models = os.path.join(ROOT, "vendor", "realesrgan-models")
if not os.path.isdir(models):
    raise SystemExit(f"Missing {models}. Run scripts/fetch_tools.sh first.")

icon = os.path.join(ROOT, "build", "icon", "AppIcon.icns")

a = Analysis(
    [os.path.join(ROOT, "packaging", "app_main.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=binaries,
    datas=collect_data_files("yt_dlp_ejs") + [(models, "realesrgan-models")],
    hiddenimports=["yt_dlp_ejs"],
    excludes=["pytest", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    argv_emulation=False,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)

app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon=icon if os.path.exists(icon) else None,
    bundle_identifier="io.github.haasje33.youtubedownloader",
    version=VERSION,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "LSApplicationCategoryType": "public.app-category.video",
        "NSHumanReadableCopyright": "Uses yt-dlp (Unlicense), ffmpeg (GPL) and deno (MIT).",
    },
)
