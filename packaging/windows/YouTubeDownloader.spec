# PyInstaller spec for the Windows app. Build with scripts/build_windows.py.
# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
APP_NAME = "YouTube Downloader"
VENDOR = os.path.join(ROOT, "vendor")
ICON = os.path.join(ROOT, "build", "icon", "AppIcon.ico")

binaries = []
for name in os.listdir(os.path.join(VENDOR, "bin")):
    binaries.append((os.path.join(VENDOR, "bin", name), "bin"))

datas = collect_data_files("yt_dlp_ejs")
datas += [
    (os.path.join(VENDOR, "realesrgan-models"), "realesrgan-models"),
    (os.path.join(VENDOR, "onnx-models"), "onnx-models"),
    (ICON, "."),
]

a = Analysis(
    [os.path.join(ROOT, "packaging", "app_main.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=["yt_dlp_ejs", "onnxruntime", "numpy"],
    excludes=["pytest", "PIL", "onnx"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# The app itself (no console window) ...
gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=APP_NAME,
    icon=ICON,
    console=False,
)
# ... and a console twin for the command line and for CI self-tests.
cli = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="youtube-downloader-cli",
    icon=ICON,
    console=True,
)

coll = COLLECT(gui, cli, a.binaries, a.datas, name=APP_NAME)
