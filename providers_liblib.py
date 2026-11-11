"""Run user-selected LiblibAI ComfyUI API request templates.

The model and node inputs are supplied by the actual workflow's "查看API参数"
JSON. No workflow UUIDs or node numbers are invented by this adapter.

Official references:
  https://www.liblib.art/apis/workflow
  https://liblibai.feishu.cn/wiki/UAMVw67NcifQHukf8fpccgS5n6d
  https://liblibai.feishu.cn/wiki/A9M2whHxsiKtu8kpIn3cZp0PnVw

The official upload document lists images. An authenticated, non-generation
upload-signature probe also accepted mp4 and returned an OSS policy explicitly
allowing video/mp4 up to 20 MiB. Videos use that uploader by default; optional
LIBLIB_VIDEO_UPLOAD=tos uses the project's private TOS uploader instead.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import mimetypes
import math
import os
import re
import time
import subprocess
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from provider_common import download_file, redact_error


ROOT = Path(__file__).resolve().parent
API_BASE = "https://openapi.liblibai.cloud"
GENERATE_URI = "/api/generate/comfyui/app"
STATUS_URI = "/api/generate/comfy/status"
UPLOAD_SIGNATURE_URI = "/api/generate/upload/signature"
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 3600
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 20 * 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024

MODEL_IMAGE = "{{MODEL_IMAGE_URL}}"
GARMENT_IMAGE = "{{GARMENT_IMAGE_URL}}"
REFERENCE_VIDEO = "{{REFERENCE_VIDEO_URL}}"

_STAGES = {
    "tryon": ("LIBLIB_TRYON_CONFIG", {GARMENT_IMAGE}),
    "motion": ("LIBLIB_MOTION_CONFIG", {MODEL_IMAGE, REFERENCE_VIDEO}),
}
_ALLOWED_INPUTS = {"tryon": {MODEL_IMAGE, GARMENT_IMAGE}, "motion": {MODEL_IMAGE, REFERENCE_VIDEO}}
_UUID = re.compile(r"[0-9a-fA-F]{32}\Z")
_PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")


def _strings(value):
    """Traverse values without changing request types or dictionary keys."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _validate_template(payload: object, stage: str) -> list[str]:
    env_name, required = _STAGES[stage]
    prefix = f"{env_name}："
    if not isinstance(payload, dict):
        return [prefix + "JSON顶层须为官方请求对象。"]

    problems = []
    # The example is intentionally not a ready-to-charge workflow.
    if any(str(key).startswith("_") for key in payload):
        problems.append(prefix + "请用工作流“查看API参数”的完整请求JSON替换示例说明。")
    template_uuid = payload.get("templateUuid")
    if not isinstance(template_uuid, str) or not _UUID.fullmatch(template_uuid) or set(template_uuid) == {"0"}:
        problems.append(prefix + "缺少有效的官方templateUuid，不能使用示例或占位值。")
    params = payload.get("generateParams")
    if not isinstance(params, dict):
        return problems + [prefix + "缺少官方generateParams对象。"]
    workflow_uuid = params.get("workflowUuid")
    if not isinstance(workflow_uuid, str) or not _UUID.fullmatch(workflow_uuid) or set(workflow_uuid) == {"0"}:
        problems.append(prefix + "缺少有效的工作流workflowUuid，请从平台复制。")
    if not any(isinstance(v, dict) for k, v in params.items() if k != "workflowUuid"):
        problems.append(prefix + "缺少真实工作流节点参数，请复制完整官方请求体。")

    values = list(_strings(payload))
    for placeholder in sorted(required):
        if placeholder not in values:
            problems.append(prefix + f"须将相应素材URL完整替换为 {placeholder}。")
    found = {match.group() for item in values for match in _PLACEHOLDER.finditer(item)}
    if found - _ALLOWED_INPUTS[stage]:
        problems.append(prefix + "存在此阶段不支持或未替换的模板占位符。")
    if any(match.group() != item for item in values for match in _PLACEHOLDER.finditer(item)):
        problems.append(prefix + "素材占位符必须单独作为JSON字符串值，不能拼接在其他文字中。")
    if any("{{" in item.replace(MODEL_IMAGE, "").replace(GARMENT_IMAGE, "").replace(REFERENCE_VIDEO, "")
           or "}}" in item.replace(MODEL_IMAGE, "").replace(GARMENT_IMAGE, "").replace(REFERENCE_VIDEO, "")
           for item in values):
        problems.append(prefix + "存在不完整或未知占位符。")
    # Never accept placeholder node names from a hand-written example.
    def has_placeholder_key(value):
        if isinstance(value, dict):
            return any("{{" in str(k) or "}}" in str(k) or has_placeholder_key(v) for k, v in value.items())
        if isinstance(value, list):
            return any(has_placeholder_key(item) for item in value)
        return False
    if has_placeholder_key(payload):
        problems.append(prefix + "节点键名存在占位符，请保留平台提供的真实节点编号。")
    return problems


def _load_template(stage: str) -> tuple[dict, str]:
    env_name, _ = _STAGES[stage]
    configured = os.getenv(env_name, "").strip()
    if not configured:
        raise ValueError(f"未设置 {env_name}：请指定已配置的官方工作流请求JSON文件。")
    path = Path(os.path.expandvars(configured)).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    try:
        if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
            raise OSError
        raw = path.read_bytes()
    except OSError:
        raise ValueError(f"{env_name}：文件不存在、不可读取或超过1MB。") from None
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError(f"{env_name}：文件须为有效的UTF-8 JSON，不能包含注释。") from None
    problems = _validate_template(payload, stage)
    if problems:
        raise ValueError("；".join(problems))
    return payload, hashlib.sha256(raw).hexdigest()


def available(stage: str) -> list[str]:
    """Return all configuration blockers before any generation is submitted."""
    if stage not in _STAGES:
        return ["Liblib阶段无效，仅支持tryon或motion。"]
    problems = []
    for env_name in ("LIBLIB_ACCESS_KEY", "LIBLIB_SECRET_KEY"):
        if not os.getenv(env_name, "").strip():
            problems.append(f"未设置 {env_name}。")
    try:
        _load_template(stage)
    except ValueError as exc:
        problems.append(str(exc))
    if stage == "motion" and os.getenv("LIBLIB_VIDEO_UPLOAD", "native").strip().lower() == "tos":
        try:
            storage = importlib.import_module("storage_tos")
        except ImportError:
            problems.append("缺少TOS上传组件，Liblib参考视频尚不能上传。")
        else:
            problems.extend(storage.available())
    elif stage == "motion" and os.getenv("LIBLIB_VIDEO_UPLOAD", "native").strip().lower() not in {"", "native"}:
        problems.append("LIBLIB_VIDEO_UPLOAD 只能填写 native 或 tos。")
    return problems


def input_mode(stage: str) -> str:
    payload, _ = _load_template(stage)
    if stage == "tryon":
        return "person_and_garment" if MODEL_IMAGE in set(_strings(payload)) else "garment_only"
    return "person_and_video"


def _signed_params(uri: str) -> dict[str, str]:
    access_key = os.getenv("LIBLIB_ACCESS_KEY", "").strip()
    secret_key = os.getenv("LIBLIB_SECRET_KEY", "").strip()
    if not access_key or not secret_key:
        raise ValueError("请先配置LIBLIB_ACCESS_KEY和LIBLIB_SECRET_KEY。")
    timestamp = str(int(time.time() * 1000))
    nonce = uuid.uuid4().hex
    content = "&".join((uri, timestamp, nonce)).encode("utf-8")
    digest = hmac.new(secret_key.encode("utf-8"), content, hashlib.sha1).digest()
    return {
        "AccessKey": access_key,
        "Signature": base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii"),
        "Timestamp": timestamp,
        "SignatureNonce": nonce,
    }


def _signed_post(uri: str, body: dict) -> dict:
    """One attempt only: retrying an ambiguous generation can charge twice."""
    try:
        response = requests.post(
            API_BASE + uri, params=_signed_params(uri), json=body, timeout=(20, 120),
        )
    except requests.RequestException:
        suffix = "提交结果不确定，请先到Liblib任务记录核对，再决定是否重试。" if uri == GENERATE_URI else "请检查网络连接后重试。"
        raise RuntimeError("Liblib请求连接失败或超时。" + suffix) from None
    if not response.ok:
        # requests exceptions include the signed URL; never propagate those.
        raise RuntimeError(f"Liblib API返回HTTP {response.status_code}，请检查API权限、余额及平台状态。")
    try:
        result = response.json()
    except ValueError:
        raise RuntimeError("Liblib API未返回有效JSON，请到平台核对任务状态。") from None
    if not isinstance(result, dict):
        raise RuntimeError("Liblib API响应结构异常。")
    code = result.get("code")
    if code not in (0, "0"):
        safe_code = re.sub(r"[^a-zA-Z0-9_-]", "", str(code))[:40] or "未知"
        message = redact_error(str(result.get("msg") or result.get("message") or "平台未提供错误说明"))
        stage = {UPLOAD_SIGNATURE_URI: "获取上传凭证", GENERATE_URI: "提交生成", STATUS_URI: "查询任务"}.get(uri, "API")
        raise RuntimeError(f"Liblib{stage}拒绝请求（错误码 {safe_code}）：{message}")
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Liblib API响应缺少data对象。")
    return data


def _check_image(path: Path) -> None:
    if not path.is_file():
        raise ValueError("Liblib输入图片不存在。")
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        raise ValueError("Liblib原生上传仅使用JPG、JPEG或PNG图片，请先转换格式。")
    if path.stat().st_size <= 0 or path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("Liblib输入图片须非空且不超过10MB。")


def _upload_asset(path: Path, *, video: bool = False) -> str:
    if video:
        if not path.is_file() or path.suffix.lower() != ".mp4" or not 0 < path.stat().st_size <= MAX_VIDEO_BYTES:
            raise ValueError("Liblib原生视频上传使用非空MP4文件，最大20MB。")
    else:
        _check_image(path)
    suffix = path.suffix.lower()
    name = uuid.uuid4().hex + suffix
    data = _signed_post(UPLOAD_SIGNATURE_URI, {"name": name, "extension": suffix[1:]})
    required = ("postUrl", "key", "policy", "xOssDate", "xOssExpires", "xOssSignature", "xOssCredential", "xOssSignatureVersion")
    if any(data.get(key) in (None, "") for key in required):
        raise RuntimeError("Liblib上传签名响应不完整，未提交生成任务。")
    post_url = data["postUrl"]
    if not isinstance(post_url, str) or urlsplit(post_url).scheme != "https" or not urlsplit(post_url).netloc:
        raise RuntimeError("Liblib上传地址无效。")
    mime = mimetypes.guess_type(name)[0] or ("video/mp4" if video else "image/png")
    form = {
        "key": data["key"], "policy": data["policy"],
        "x-oss-date": data["xOssDate"], "x-oss-expires": str(data["xOssExpires"]),
        "x-oss-signature": data["xOssSignature"], "x-oss-credential": data["xOssCredential"],
        "x-oss-signature-version": data["xOssSignatureVersion"],
    }
    try:
        with path.open("rb") as handle:
            # requests emits data fields before files, as required by OSS.
            response = requests.post(post_url, data=form, files={"file": (name, handle, mime)}, timeout=(20, 300))
    except requests.RequestException:
        raise RuntimeError("上传素材到Liblib失败或超时，未提交生成任务。") from None
    if not response.ok:
        raise RuntimeError(f"Liblib素材上传失败（HTTP {response.status_code}），未提交生成任务。")
    return post_url.rstrip("/") + "/" + quote(str(data["key"]), safe="/")


def _upload_image(path: Path) -> str:
    return _upload_asset(path)


def _upload_video(path: Path) -> str:
    if os.getenv("LIBLIB_VIDEO_UPLOAD", "native").strip().lower() == "tos":
        storage = importlib.import_module("storage_tos")
        return storage.upload_file(path)
    if path.stat().st_size > MAX_VIDEO_BYTES:
        # Bound upload size locally before creating a generation task.
        import subprocess
        prepared = path.with_name(path.stem + "_liblib.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
             "-map", "0:v:0", "-map", "0:a?", "-vf",
             "scale=1280:1280:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
             "-c:v", "libx264", "-preset", "fast", "-crf", "24", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(prepared)],
            check=True, capture_output=True, text=True,
        )
        path = prepared
    return _upload_asset(path, video=True)


def _replace(value, substitutions: dict[str, str]):
    if isinstance(value, str):
        return substitutions.get(value, value)
    if isinstance(value, dict):
        return {key: _replace(item, substitutions) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, substitutions) for item in value]
    return value


def _output_url(data: dict, stage: str) -> str:
    items = data.get("images" if stage == "tryon" else "videos")
    key = "imageUrl" if stage == "tryon" else "videoUrl"
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            # Official status 3 means asset review has passed. Some responses
            # omit per-asset review status after task-level success.
            if item.get("auditStatus") not in (None, 3, "3"):
                continue
            url = item.get(key)
            if isinstance(url, str) and urlsplit(url).scheme in {"http", "https"} and urlsplit(url).netloc:
                return url
    raise RuntimeError("Liblib任务完成但没有对应且通过审核的输出文件，请核对所选工作流输出类型。")


def _generate(payload: dict, stage: str, destination: Path, fingerprint: str, option: str) -> dict:
    if any(_PLACEHOLDER.search(item) or "{{" in item or "}}" in item for item in _strings(payload)):
        raise ValueError("Liblib素材占位符尚未全部替换，已阻止提交生成任务。")
    initial = _signed_post(GENERATE_URI, payload)
    generate_uuid = initial.get("generateUuid")
    if not isinstance(generate_uuid, str) or not generate_uuid:
        raise RuntimeError("Liblib提交响应缺少任务ID，请先到平台核对，避免重复付费。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.with_name(destination.stem + "_task.json").write_text(json.dumps({
        "generate_uuid": generate_uuid, "stage": stage,
        "workflow_uuid": payload["generateParams"]["workflowUuid"],
        "workflow_config_sha256": fingerprint,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        data = _signed_post(STATUS_URI, {"generateUuid": generate_uuid})
        try:
            status = int(data.get("generateStatus"))
        except (ValueError, TypeError):
            raise RuntimeError("Liblib任务状态无效，请到API控制台核对；程序不会重新提交任务。") from None
        # Observed in a real response: 7 + "generate task timeout". Do not
        # assign a meaning to other undocumented statuses or messages.
        if status == 7 and re.search(r"timeout|超时", str(data.get("generateMsg") or ""), re.I):
            reason = redact_error(str(data["generateMsg"]))
            raise RuntimeError(f"Liblib平台生成超时：{reason}；任务ID：{generate_uuid}。已保留任务记录，程序不会自动重新提交。")
        if status == 6:
            reason = redact_error(str(data.get("generateMsg") or "平台未提供错误说明"))
            raise RuntimeError(f"Liblib生成任务失败：{reason}；任务ID：{generate_uuid}")
        if status == 5:
            url = _output_url(data, stage)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                download_file(url, destination)
            except requests.RequestException:
                raise RuntimeError("Liblib已有输出文件下载失败，请到平台取回结果；程序不会重新提交任务。") from None
            if not destination.is_file() or destination.stat().st_size == 0:
                raise RuntimeError("Liblib输出文件下载失败或为空，请在平台取回已有结果。")
            metadata = {
                "provider": "liblib", "model": "LiblibAI ComfyUI工作流",
                "option": option, "generate_uuid": generate_uuid,
                "workflow_config_sha256": fingerprint,
                "template_uuid": payload["templateUuid"],
                "workflow_uuid": payload["generateParams"]["workflowUuid"],
            }
            # These numeric fields are useful when comparing providers.
            points = data.get("pointsCost")
            if isinstance(points, (int, float)) and not isinstance(points, bool):
                metadata["points_cost"] = points
            return metadata
        if status not in (1, 2, 3, 4):
            raise RuntimeError("Liblib返回未知任务状态，请在平台核对；程序不会重新提交任务。")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError("Liblib任务等待超时，任务可能仍在平台运行；请先核对已有任务，避免重复付费。")


def tryon(model: Path, garment: Path, destination: Path, option: str) -> dict:
    problems = available("tryon")
    if problems:
        raise ValueError("；".join(problems))
    payload, fingerprint = _load_template("tryon")
    model, garment, destination = Path(model), Path(garment), Path(destination)
    _check_image(garment)
    uses_person = MODEL_IMAGE in set(_strings(payload))
    if uses_person:
        _check_image(model)
    substitutions = {GARMENT_IMAGE: _upload_image(garment)}
    if uses_person:
        substitutions[MODEL_IMAGE] = _upload_image(model)
    result = _generate(_replace(payload, substitutions), "tryon", destination, fingerprint, option)
    result.update({"input_mode": "person_and_garment" if uses_person else "garment_only",
                   "reference_model_used": uses_person})
    if not uses_person:
        result["note"] = "此模板只开放服装图输入；人物由所选Liblib工作流决定，人物来源未确认。未上传内置模特照片。全身构图与服装细节需检查样片。"
    return result


def motion(image: Path, clip: Path, destination: Path, option: str) -> dict:
    problems = available("motion")
    if problems:
        raise ValueError("；".join(problems))
    payload, fingerprint = _load_template("motion")
    image, clip, destination = Path(image), Path(clip), Path(destination)
    _check_image(image)
    if not clip.is_file() or clip.stat().st_size == 0:
        raise ValueError("Liblib参考视频不存在或为空。")
    payload, duration_info = motion_duration_payload(payload, clip)
    substitutions = {MODEL_IMAGE: _upload_image(image), REFERENCE_VIDEO: _upload_video(clip)}
    result = _generate(_replace(payload, substitutions), "motion", destination, fingerprint, option)
    result["restore_reference_audio"] = True
    result.update(duration_info)
    return result


def motion_duration_payload(payload: dict, clip: Path) -> tuple[dict, dict]:
    """Only this verified workflow binds node79=seconds and node80=FPS."""
    import copy
    payload = copy.deepcopy(payload)
    if payload.get("generateParams", {}).get("workflowUuid") != "db9317116a5641c98c4a340d9a4de80e":
        return payload, {}
    params = payload["generateParams"]
    if params.get("79", {}).get("class_type") != "ImpactInt" or params.get("80", {}).get("class_type") != "ImpactInt":
        raise ValueError("该动作工作流的时长/帧率节点已变化，请重新核对平台参数。")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(clip)],
                           check=True, capture_output=True, text=True)
    duration = float(json.loads(probe.stdout)["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("参考视频时长无效。")
    seconds = math.ceil(duration - 1e-6)
    params["79"]["inputs"]["value"] = seconds
    return payload, {"reference_duration_seconds": duration, "requested_duration_seconds": seconds,
                     "requested_fps": params["80"]["inputs"]["value"], "duration_mapping_verified": True}
