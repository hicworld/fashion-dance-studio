"""Install the pinned Windows FFmpeg tools from the upstream ZIP, without executing them."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import struct
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile

MANIFEST = Path(__file__).resolve().with_name("ffmpeg-win64.json")
UPSTREAM = "https://github.com/GyanD/codexffmpeg/releases/download/"
TOOL_NAMES = ("ffmpeg.exe", "ffprobe.exe")
CHUNK = 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 192 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_DOWNLOAD_SECONDS = 600


class MediaInstallError(RuntimeError):
    """The selected FFmpeg archive or its installation could not be verified."""


def _load_manifest() -> dict:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid FFmpeg manifest.")
    version = value.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", version):
        raise ValueError("Invalid pinned FFmpeg version.")
    prefix = f"ffmpeg-{version}-essentials_build"
    if value.get("url") != f"{UPSTREAM}{version}/{prefix}.zip":
        raise ValueError("Unexpected FFmpeg upstream URL.")

    def validate_size_hash(obj: dict, maximum: int) -> bool:
        size, sha = obj.get("size"), obj.get("sha256")
        return type(size) is int and 0 < size <= maximum and isinstance(sha, str) and bool(
            re.fullmatch(r"[a-f0-9]{64}", sha)
        )

    archive, members = value.get("archive"), value.get("members")
    if not isinstance(archive, dict) or not validate_size_hash(archive, MAX_ARCHIVE_BYTES):
        raise ValueError("Invalid FFmpeg archive lock.")
    if not isinstance(members, dict) or set(members) != set(TOOL_NAMES):
        raise ValueError("Invalid FFmpeg member allowlist.")
    for name, spec in members.items():
        if (not isinstance(spec, dict) or not validate_size_hash(spec, MAX_MEMBER_BYTES)
                or spec.get("path") != f"{prefix}/bin/{name}"):
            raise ValueError(f"Invalid FFmpeg lock for {name}.")
    return value


def _copy_verified(source, spec: dict, output=None, *, deadline: float | None = None) -> None:
    expected = spec["size"]
    digest = hashlib.sha256()
    count = 0
    while block := source.read(CHUNK):
        count += len(block)
        if count > expected:
            raise ValueError("FFmpeg data exceeds the pinned byte count.")
        if deadline is not None and time.monotonic() >= deadline:
            raise ValueError("FFmpeg download timed out.")
        digest.update(block)
        if output is not None:
            output.write(block)
    if count != expected or digest.hexdigest() != spec["sha256"]:
        raise ValueError("FFmpeg SHA-256 or size does not match the pinned release.")


def _safe_path(path: Path) -> Path:
    path = path.absolute()
    if path.resolve() != path or path.is_symlink():
        raise ValueError("FFmpeg cache paths cannot use symbolic links or junctions.")
    return path


def _verified_file(path: Path, spec: dict) -> bool:
    try:
        _safe_path(path)
        if not path.is_file() or path.stat().st_size != spec["size"]:
            return False
        with path.open("rb") as source:
            _copy_verified(source, spec)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def ffmpeg_ready(destination: Path) -> bool:
    """Check both cache files without accessing the network."""
    try:
        directory = _safe_path(Path(destination))
        return all(_verified_file(directory / name, spec)
                   for name, spec in _load_manifest()["members"].items())
    except (OSError, RuntimeError, ValueError):
        return False


class _HTTPSRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or not target.hostname or target.username is not None:
            raise ValueError("The FFmpeg download redirected away from a valid HTTPS URL.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, archive: Path, spec: dict) -> None:
    request = Request(url, headers={"User-Agent": "FashionDanceStudio-MediaRuntime/1"})
    deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
    with build_opener(_HTTPSRedirect()).open(request, timeout=45) as response, archive.open("xb") as out:
        _copy_verified(response, spec, out, deadline=deadline)


def _archive_entries(package: ZipFile, members: dict) -> dict:
    entries, seen = {}, set()
    inventory = package.infolist()
    if len(inventory) > MAX_MEMBERS:
        raise ValueError("Unexpected number of files in FFmpeg ZIP.")
    wanted = {spec["path"]: name for name, spec in members.items()}
    for item in inventory:
        path = item.filename
        clean = path[:-1] if item.is_dir() else path
        parts = clean.split("/")
        kind = stat.S_IFMT(item.external_attr >> 16)
        if (path != item.orig_filename or "\\" in path or ":" in path
                or any(part in ("", ".", "..") for part in parts)
                or clean.casefold() in seen
                or kind not in (0, stat.S_IFDIR if item.is_dir() else stat.S_IFREG)):
            raise ValueError("Unexpected path or link in FFmpeg ZIP.")
        seen.add(clean.casefold())
        if path in wanted:
            name = wanted[path]
            if item.is_dir() or item.file_size != members[name]["size"] or item.flag_bits & 1:
                raise ValueError(f"Unexpected FFmpeg ZIP member: {name}.")
            entries[name] = item
    if set(entries) != set(TOOL_NAMES):
        raise ValueError("The FFmpeg ZIP is missing a required executable.")
    return entries


def install_ffmpeg(destination: Path, *, archive: Path | None = None) -> Path:
    """Install both verified tools and return their directory.

    `archive` optionally supplies the exact upstream ZIP without networking.
    Existing verified files are reused. Downloaded code is never executed here.
    """
    try:
        directory = _safe_path(Path(destination))
        manifest = _load_manifest()
        members = manifest["members"]
        targets = {name: _safe_path(directory / name) for name in TOOL_NAMES}
        if any(path.exists() and not path.is_file() for path in targets.values()):
            raise ValueError("An FFmpeg executable path is not a regular file.")
        missing = {name for name in TOOL_NAMES if not _verified_file(targets[name], members[name])}
        if not missing:
            return directory
        if archive is None and (os.name != "nt" or struct.calcsize("P") != 8
                                or platform.machine().lower() not in {"amd64", "x86_64"}):
            raise ValueError("The pinned FFmpeg download requires Windows x64.")
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".ffmpeg-", dir=directory) as temporary:
            staging = Path(temporary)
            if archive is None:
                archive = staging / "upstream.zip"
                _download(manifest["url"], archive, manifest["archive"])
            with Path(archive).open("rb") as source:
                _copy_verified(source, manifest["archive"])
                source.seek(0)
                with ZipFile(source) as package:
                    entries = _archive_entries(package, members)
                    for name in TOOL_NAMES:
                        with package.open(entries[name]) as executable:
                            if name not in missing:
                                _copy_verified(executable, members[name])
                                continue
                            with (staging / name).open("xb") as output:
                                _copy_verified(executable, members[name], output)
                                output.flush()
                                os.fsync(output.fileno())
            for name in TOOL_NAMES:
                target = _safe_path(targets[name])
                if name in missing and not _verified_file(target, members[name]):
                    (staging / name).replace(target)
        return directory
    except URLError:
        raise MediaInstallError("Unable to download FFmpeg from the official upstream release.") from None
    except (BadZipFile, OSError, ValueError, RuntimeError) as error:
        # Do not surface potentially signed redirect URLs in errors or logs.
        if isinstance(error, OSError):
            raise MediaInstallError("Unable to read or write the FFmpeg cache or upstream ZIP.") from None
        raise MediaInstallError(str(error)) from None
