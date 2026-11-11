"""Restore the pinned Windows Real-ESRGAN dependency without running it."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile


RESOURCE_DIRECTORY = Path(__file__).resolve().parent / "third_party" / "realesrgan"
RELEASE_PREFIX = "https://github.com/xinntao/Real-ESRGAN/releases/download/"
RELEASE_FILENAME = "realesrgan-ncnn-vulkan-20220424-windows.zip"
MEMBERS = (
    "realesrgan-ncnn-vulkan.exe",
    "vcomp140.dll",
    "README_windows.md",
    "models/realesr-animevideov3-x3.bin",
    "models/realesr-animevideov3-x3.param",
    "models/realesrgan-x4plus.bin",
    "models/realesrgan-x4plus.param",
)
MAX_BYTES = 100_000_000
CHUNK_BYTES = 1024 * 1024


def _digest(source, output=None) -> str:
    digest = hashlib.sha256()
    size = 0
    while block := source.read(CHUNK_BYTES):
        size += len(block)
        if size > MAX_BYTES:
            raise ValueError("Real-ESRGAN package or member exceeds the size limit.")
        digest.update(block)
        if output is not None:
            output.write(block)
    return digest.hexdigest()


def _matches(path: Path, expected: str) -> bool:
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        return False
    with path.open("rb") as source:
        return _digest(source) == expected


def _manifest(directory: Path) -> dict:
    value = json.loads((directory / "SOURCE.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("files"), dict):
        raise ValueError("Invalid Real-ESRGAN SOURCE.json.")
    files = value["files"]
    release = value.get("release")
    if (set(files) != set(MEMBERS)
            or not isinstance(release, str) or not re.fullmatch(r"v[\d.]+", release)
            or value.get("url") != RELEASE_PREFIX + release + "/" + RELEASE_FILENAME
            or not all(isinstance(s, str) and re.fullmatch(r"[a-f0-9]{64}", s)
                       for s in [value.get("archive_sha256"), *files.values()])):
        raise ValueError("Unexpected URL, member list or SHA-256 in Real-ESRGAN SOURCE.json.")
    return value


class _HTTPSOnlyRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise ValueError("The Real-ESRGAN download redirected away from HTTPS.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, archive: Path, expected: str) -> None:
    print("Downloading the pinned Real-ESRGAN release from GitHub...", flush=True)
    opener = build_opener(_HTTPSOnlyRedirect())
    request = Request(url, headers={"User-Agent": "FashionDanceStudio-Setup"})
    with opener.open(request, timeout=45) as response, archive.open("xb") as output:
        if _digest(response, output) != expected:
            raise ValueError("Real-ESRGAN download SHA-256 did not match SOURCE.json.")


def prepare(directory: Path = RESOURCE_DIRECTORY, *, archive: Path | None = None, check: bool = False) -> int:
    directory = directory.resolve()
    manifest = _manifest(directory)
    expected = manifest["files"]
    targets = {name: directory / name for name in MEMBERS}
    for name, target in targets.items():
        if target.resolve() != target or (target.exists() and not target.is_file()):
            raise ValueError(f"Unexpected Real-ESRGAN destination: {name}")
    missing = {name for name in MEMBERS if not _matches(targets[name], expected[name])}
    if check:
        if missing:
            raise ValueError("Missing or mismatched Real-ESRGAN files: " + ", ".join(sorted(missing)))
        return 0
    if not missing and archive is None:
        return 0

    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=directory) as temporary:
        staging = Path(temporary)
        if archive is None:
            archive = staging / "release.zip"
            _download(manifest["url"], archive, manifest["archive_sha256"])
        elif not _matches(archive, manifest["archive_sha256"]):
            raise ValueError("Local Real-ESRGAN ZIP SHA-256 did not match SOURCE.json.")

        with ZipFile(archive) as package:
            entries = {}
            for item in package.infolist():
                if item.filename not in expected:
                    continue
                if item.filename in entries or item.is_dir() or item.file_size > MAX_BYTES:
                    raise ValueError(f"Unexpected Real-ESRGAN ZIP member: {item.filename}")
                entries[item.filename] = item
            if set(entries) != set(expected):
                raise ValueError("The Real-ESRGAN ZIP is missing an expected member.")
            for name in MEMBERS:
                with package.open(entries[name]) as source:
                    if name in missing:
                        staged = staging / name
                        staged.parent.mkdir(parents=True, exist_ok=True)
                        with staged.open("xb") as output:
                            actual = _digest(source, output)
                    else:
                        actual = _digest(source)
                if actual != expected[name]:
                    raise ValueError(f"Real-ESRGAN member SHA-256 mismatch: {name}")

        # Publish only verified files, once the whole allowlist has been checked.
        restored = 0
        for name in MEMBERS:
            if name in missing and not _matches(targets[name], expected[name]):
                targets[name].parent.mkdir(parents=True, exist_ok=True)
                (staging / name).replace(targets[name])
                restored += 1
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--archive", type=Path, metavar="ZIP", help="Use the pinned upstream ZIP already downloaded.")
    mode.add_argument("--check", action="store_true", help="Verify installed files without downloading anything.")
    args = parser.parse_args()
    try:
        restored = prepare(archive=args.archive, check=args.check)
    except (OSError, ValueError, BadZipFile) as error:
        print(f"Real-ESRGAN setup failed: {error}", file=sys.stderr)
        return 1
    print(f"Real-ESRGAN files verified. Restored: {restored}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
