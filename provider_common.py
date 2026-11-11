"""Shared file handling for cloud providers; credentials stay on the server."""

import os
import re
from pathlib import Path

import requests

MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024


def download_file(url: str, destination: Path) -> None:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("生成接口返回的文件地址不是 HTTPS")
    size = 0
    with requests.get(url, stream=True, timeout=(20, 180)) as response:
        response.raise_for_status()
        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise ValueError("生成文件超过下载上限")
                output.write(chunk)


def redact_error(detail: str) -> str:
    # Exception text may include a signed storage URL or a provider's credential.
    for name, value in os.environ.items():
        if value and any(token in name.upper() for token in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
            detail = detail.replace(value, "[redacted]")
    return re.sub(r"https?://[^\s\"'<>]+", "[远程地址已隐藏]", detail)[-500:]
