"""Verify and archive a Windows portable build and write its SHA-256 checksum."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat
from zipfile import ZIP_DEFLATED, ZipFile


EXE = "穿搭舞蹈工作室.exe"
TOP_LEVEL_FILES = {EXE, "README.md", "启动说明.txt", "安装AI超分.cmd",
                   "创建桌面快捷方式.cmd", "创建桌面快捷方式.ps1"}
TOP_LEVEL_DIRS = {"_internal", "docs", "licenses"}
REQUIRED = {
    EXE, "启动说明.txt", "安装AI超分.cmd",
    "_internal/.env.example", "_internal/ffmpeg-win64.json",
    "_internal/templates/index.html", "_internal/static/studio.js",
    "_internal/third_party/realesrgan/SOURCE.json",
    "docs/third-party.md",
}
MEDIA_SUFFIXES = {".mp4", ".mov", ".webm", ".wav", ".mp3", ".zip", ".bin", ".param"}
FORBIDDEN_DIRS = {".git", ".venv", "jobs", "shortfilms", "research", "webview-data"}


def files_for_package(root: Path) -> list[Path]:
    for entry in root.iterdir():
        if entry.name not in (TOP_LEVEL_DIRS if entry.is_dir() else TOP_LEVEL_FILES):
            raise ValueError(f"Unexpected top-level release entry: {entry.name}")
    files = []
    for folder, dirs, names in os.walk(root):
        for name in sorted(dirs + names):
            path = Path(folder) / name
            info = path.lstat()
            relative = path.relative_to(root)
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"Reparse point in release: {relative}")
            if path.is_dir():
                if name.lower() in FORBIDDEN_DIRS:
                    raise ValueError(f"Private directory in release: {relative}")
                continue
            leaf = name.lower()
            if ((leaf.startswith(".env") and relative.as_posix() != "_internal/.env.example")
                    or leaf in {"ffmpeg.exe", "ffprobe.exe", "realesrgan-ncnn-vulkan.exe",
                                "ucrtbase.dll", "ui_preferences.json", "job_state.json"}
                    or (leaf.endswith(".dll") and leaf.startswith(
                        ("api-ms-win-", "ext-ms-win-", "vcruntime", "msvcp140", "concrt140", "vcomp140")))
                    or (path.suffix.lower() in MEDIA_SUFFIXES
                        and relative.as_posix() != "_internal/base_library.zip")):
                raise ValueError(f"Non-portable or private file in release: {relative}")
            files.append(path)
    names = {path.relative_to(root).as_posix() for path in files}
    if REQUIRED - names or not any(name.startswith("licenses/") for name in names):
        raise ValueError("Release is missing the executable, manifests, interface, or notices.")
    sample = (root / "_internal" / ".env.example").read_text(encoding="utf-8")
    for line in sample.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line != "LIBLIB_VIDEO_UPLOAD=native":
            if not line.endswith("="):
                raise ValueError("The packaged .env.example has an unexpected non-empty value.")
    return sorted(files)


def package(root: Path, output: Path) -> tuple[int, str]:
    root, output = root.resolve(strict=True), output.resolve()
    if output.is_relative_to(root) or output.suffix.lower() != ".zip":
        raise ValueError("Choose a ZIP path outside the application folder.")
    checksum = output.with_suffix(".zip.sha256")
    if output.exists() or checksum.exists():
        raise FileExistsError("The ZIP or checksum already exists.")
    files = files_for_package(root)
    with ZipFile(output, "x", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            archive.write(path, "FashionDanceStudio/" + path.relative_to(root).as_posix())
    with ZipFile(output) as archive:
        if archive.testzip() is not None or len(archive.infolist()) != len(files):
            raise ValueError("The release ZIP did not pass read-back verification.")
    with output.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    with checksum.open("x", encoding="ascii", newline="\n") as target:
        target.write(f"{digest}  {output.name}\n")
    return len(files), digest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_folder", type=Path)
    parser.add_argument("output_zip", type=Path)
    args = parser.parse_args()
    count, digest = package(args.release_folder, args.output_zip)
    print(f"Verified portable ZIP: {count} files; SHA-256 {digest}")
