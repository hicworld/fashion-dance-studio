# -*- mode: python ; coding: utf-8 -*-
"""Explicit resource allowlist: never include private .env, jobs, or user media."""
from pathlib import Path
import os
import shutil
import webview

root = Path(SPECPATH).resolve()
portable = os.environ.get("FASHION_STUDIO_PORTABLE_RELEASE") == "1"
media_binaries = []
if not portable:
    ffmpeg = Path(os.environ.get("FASHION_STUDIO_FFMPEG") or shutil.which("ffmpeg") or "")
    ffprobe = Path(os.environ.get("FASHION_STUDIO_FFPROBE") or shutil.which("ffprobe") or "")
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise RuntimeError("FFmpeg and FFprobe are required to build with bundled media")
    media_binaries = [(str(ffmpeg), "bin"), (str(ffprobe), "bin")]
webview_root = Path(webview.__file__).resolve().parent
datas = [(str(root / "templates"), "templates"),
         (str(root / "desktop_assets"), "desktop_assets"),
         (str(root / "assets" / "base_model.png"), "assets"),
         (str(root / "ffmpeg-win64.json"), "."),
         (str(root / ".env.example"), ".")]
for directory in ("static",):
    if (root / directory).is_dir():
        datas.append((str(root / directory), directory))
sr_root = root / 'third_party' / 'realesrgan'
sr_files = ['README_windows.md', 'SOURCE.json', 'LICENSE-Real-ESRGAN.txt', 'LICENSE-ncnn-vulkan.txt', 'LICENSE-ncnn.txt']
if not portable:
    sr_files += ['realesrgan-ncnn-vulkan.exe', 'vcomp140.dll',
                 'models/realesr-animevideov3-x3.bin', 'models/realesr-animevideov3-x3.param',
                 'models/realesrgan-x4plus.bin', 'models/realesrgan-x4plus.param']
for name in sr_files:
    resource = sr_root / name
    if not resource.is_file():
        raise RuntimeError('AI super resolution resource missing: ' + name)
    datas.append((str(resource), 'third_party/realesrgan/' + str(Path(name).parent).replace('\\', '/')))
for directory in ("lib", "js"):
    if (webview_root / directory).is_dir():
        datas.append((str(webview_root / directory), "webview/" + directory))

analysis = Analysis([str(root / "desktop.py")], pathex=[str(root)],
    binaries=media_binaries, datas=datas,
    hiddenimports=["webview.platforms.winforms", "webview.platforms.edgechromium", "tkinter", "tkinter.ttk", "volcengine.visual.VisualService", "providers_fal", "providers_minimax", "providers_seedance"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "cefpython3", "gi", "torch", "tensorflow", "IPython", "matplotlib", "pandas"],
    noarchive=False)
if portable:
    # Windows 10/11 provides UCRT. Use Microsoft's redistributable for VC.
    def system_runtime(name):
        leaf = Path(name).name.lower()
        return leaf.endswith(".dll") and (
            leaf.startswith(("api-ms-win-", "ext-ms-win-", "vcruntime", "msvcp140", "concrt140", "vcomp140"))
            or leaf == "ucrtbase.dll"
        )
    analysis.binaries = [entry for entry in analysis.binaries if not system_runtime(entry[0])]
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="穿搭舞蹈工作室",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=True,
    icon=str(root / "desktop_assets" / "studio.ico"))
collection = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name="穿搭舞蹈工作室")
