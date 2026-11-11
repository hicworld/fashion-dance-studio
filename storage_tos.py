"""Private TOS uploads with expiring download URLs, using the official SDK.

Source: https://github.com/volcengine/ve-tos-python-sdk
The URL expiry does not delete the object: configure a bucket lifecycle rule for
the configured prefix if temporary inputs should be removed automatically.
"""

from __future__ import annotations

import importlib.util
import mimetypes
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def _value(name: str) -> str:
    return os.environ.get(name, "").strip()


def _credentials() -> tuple[str, str]:
    """Use a dedicated pair together; never accidentally mix two identities."""
    own_ak, own_sk = _value("TOS_ACCESS_KEY_ID"), _value("TOS_SECRET_ACCESS_KEY")
    if own_ak or own_sk:
        return own_ak, own_sk
    return _value("VOLC_ACCESS_KEY_ID"), _value("VOLC_SECRET_ACCESS_KEY")


def _endpoint() -> str:
    raw = _value("TOS_ENDPOINT").rstrip("/")
    return raw if "://" in raw else "https://" + raw


def _ttl() -> int:
    try:
        ttl = int(_value("TOS_URL_TTL_SECONDS") or "21600")
    except ValueError as exc:
        raise ValueError("TOS_URL_TTL_SECONDS 必须是整数秒数") from exc
    if not 300 <= ttl <= 604800:
        raise ValueError("TOS_URL_TTL_SECONDS 必须在 300–604800 秒之间")
    return ttl


def available() -> list[str]:
    """Return setup problems without contacting TOS or reading an .env file."""
    missing = []
    for name in ("TOS_BUCKET", "TOS_ENDPOINT", "TOS_REGION"):
        if not _value(name):
            missing.append(f"请配置 {name}")
    ak, sk = _credentials()
    if not ak or not sk:
        missing.append("请配置完整的 TOS_ACCESS_KEY_ID / TOS_SECRET_ACCESS_KEY，或复用 VOLC_ACCESS_KEY_ID / VOLC_SECRET_ACCESS_KEY")
    if _value("TOS_ENDPOINT"):
        endpoint = urlsplit(_endpoint())
        if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.path or endpoint.query or endpoint.fragment):
            missing.append("TOS_ENDPOINT 应为 HTTPS 公网服务端域名，不包含桶名、路径或登录信息")
        elif "-internal" in endpoint.hostname:
            missing.append("TOS_ENDPOINT 请使用公网端点，生成服务需要下载上传的素材")
    try:
        _ttl()
    except ValueError as exc:
        missing.append(str(exc))
    if importlib.util.find_spec("tos") is None:
        missing.append("尚未安装官方 TOS SDK，请运行 pip install tos")
    return missing


def upload_file(path: Path) -> str:
    """Upload an input without making it public; return an expiring GET URL."""
    problems = available()
    if problems:
        raise ValueError("；".join(problems))
    path = Path(path)
    if not path.is_file():
        raise ValueError("待上传的素材文件不存在")

    import tos
    from tos.enum import HttpMethodType

    ak, sk = _credentials()
    client = tos.TosClientV2(
        ak=ak, sk=sk, endpoint=_endpoint(), region=_value("TOS_REGION"),
        connection_time=20, socket_timeout=180,
    )
    prefix = (_value("TOS_PREFIX") or "fashion-dance").strip("/")
    date = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    # Random names avoid collisions and avoid disclosing local filenames.
    key = f"{prefix}/{date}/{uuid.uuid4().hex}{path.suffix.lower()}"
    try:
        client.put_object_from_file(
            bucket=_value("TOS_BUCKET"), key=key, file_path=str(path),
            content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )
        signed = client.pre_signed_url(
            http_method=HttpMethodType.Http_Method_Get,
            bucket=_value("TOS_BUCKET"), key=key, expires=_ttl(),
        )
        url = signed.signed_url
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("TOS 未返回可供下载的 HTTPS 签名链接")
        return url
    except Exception as exc:
        # SDK exception strings can include signed URLs; surface only safe codes.
        status = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
        suffix = f"（状态 {status or '未知'}，错误码 {code or type(exc).__name__}）"
        raise RuntimeError("TOS 上传或签名失败，请核对桶地域、端点和读写权限" + suffix) from None
    finally:
        client.close()
