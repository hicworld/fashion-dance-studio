"""Seedance 2.5 source-video editing through the official Volcengine Ark API.

One chargeable POST is protected by an exclusive, durable submission marker.
Recovery only reads a saved task, queries it, and downloads its original result.
Human portrait inputs must follow Ark's trusted/authorized asset rules; an
asset identifier is a reference to approved material, not an approval bypass.

Official references checked 2026-10-21:
https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh
https://docs.volcengine.com/docs/ark/get-video-generation-task-api?lang=zh
https://docs.volcengine.com/docs/ark/seedance-2-5?lang=zh
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from PIL import Image

import storage_tos
from provider_common import redact_error
from providers_jimeng import (MotionCancelledError, MotionSubmissionUncertainError,
                              MotionTerminalError, MotionTransientError)
from runtime_paths import quiet_subprocess_kwargs

OPTION = "volc-seedance-2.5-edit"
MODEL = "doubao-seedance-2-5-260628"
CREATE_URL = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
PROVIDER = "volcengine-ark"
MAX_IMAGE_BYTES = 30_000_000  # Official limit is strictly less than 30 MB.
MAX_VIDEO_BYTES = 200_000_000
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
TERMINAL_STATUSES = {"failed", "expired", "cancelled", "not_found"}
# A synchronous official input-privacy rejection is different from a lost
# submission response. Keep this allowlist deliberately narrow; a bare 4xx,
# proxy response, unknown error code, or any task identity remains uncertain.
# https://docs.volcengine.com/docs/ark/error-codes?lang=en
SUBMISSION_REJECTION_CODES = {
    "InputImageSensitiveContentDetected.PrivacyInformation": "image",
    "InputVideoSensitiveContentDetected.PrivacyInformation": "video",
}


def _acceptance_evidence(value) -> bool:
    """Any task/ID field, even empty or nested, contradicts a pure refusal."""
    pending, visited = [value], 0
    while pending:
        current = pending.pop()
        visited += 1
        if visited > 1000:
            return True
        if isinstance(current, dict):
            for key, item in current.items():
                if isinstance(key, str) and key.lower() in {"id", "task", "task_id", "taskid"}:
                    return True
                if isinstance(item, (dict, list)):
                    pending.append(item)
        elif isinstance(current, list):
            pending.extend(item for item in current if isinstance(item, (dict, list)))
    return False


def _safe_request_id(value) -> str:
    # Ark request tracing IDs use a numeric prefix and hexadecimal suffix.
    # Do not persist arbitrary error text, URLs, account identifiers or tokens.
    return value if isinstance(value, str) and re.fullmatch(r"[0-9][a-fA-F0-9]{15,127}", value) else ""


def _rejection_evidence(status, value) -> dict | None:
    """Recognize only the documented Ark 400 input-privacy error envelope."""
    if (type(status) is not int or status != 400 or not isinstance(value, dict)
            or set(value) - {"error", "request_id"} or _acceptance_evidence(value)):
        return None
    error = value.get("error")
    if (not isinstance(error, dict) or set(error) - {"code", "message", "param", "type"}
            or not isinstance(error.get("code"), str) or error["code"] not in SUBMISSION_REJECTION_CODES
            or not isinstance(error.get("message"), str) or not error["message"].strip()
            or ("type" in error and error["type"] != "BadRequest")
            or (error.get("param") is not None and not isinstance(error["param"], str))):
        return None
    proof = {"http_status": 400, "error_type": "BadRequest", "provider_code": error["code"],
             "error_code": error["code"],
             "rejection_verified": True}
    request_id = _safe_request_id(value.get("request_id"))
    if not request_id:
        match = re.search(r"\bRequest\s*ID\s*:\s*([0-9][a-fA-F0-9]{15,127})(?![A-Za-z0-9_-])",
                          error["message"], flags=re.IGNORECASE)
        request_id = _safe_request_id(match.group(1)) if match else ""
    if request_id:
        proof["request_id"] = request_id
    return proof


def read_submission_rejection(destination: Path, option: str = OPTION) -> dict | None:
    """Return safe verified proof only; never reinterpret legacy bare markers."""
    _option(option)
    destination = Path(destination)
    task_path, submission_path, result_path = _paths(destination)
    if any(path.exists() for path in (destination, task_path, result_path)):
        return None
    record = _read(submission_path)
    if (record.get("provider") != PROVIDER or record.get("option") != option
            or record.get("model") != MODEL or record.get("endpoint") != CREATE_URL
            or not isinstance(record.get("resolution"), str) or record["resolution"] not in {"720p", "1080p"}
            or record.get("status") != "rejected" or record.get("rejection_verified") is not True
            or type(record.get("http_status")) is not int or record["http_status"] != 400
            or record.get("error_type") != "BadRequest"
            or not isinstance(record.get("provider_code"), str)
            or record["provider_code"] not in SUBMISSION_REJECTION_CODES
            or record.get("error_code") != record["provider_code"]
            or _acceptance_evidence(record)):
        return None
    proof = {"http_status": 400, "error_type": "BadRequest", "provider_code": record["provider_code"],
             "error_code": record["provider_code"],
             "rejection_verified": True}
    request_id = _safe_request_id(record.get("request_id"))
    if request_id:
        proof["request_id"] = request_id
    return proof


def _raise_rejected(proof: dict) -> None:
    media = "角色参考图片" if SUBMISSION_REJECTION_CODES[proof["provider_code"]] == "image" else "原视频"
    error = MotionTerminalError(
        f"Seedance明确拒绝本次提交（HTTP 400）：{media}可能包含真人人脸，"
        "请使用平台允许的素材，或按官方可信素材／真人授权流程入库后再新建测试。"
        "本次未创建视频任务；拒绝证据已保留，本记录不会自动重新提交。",
        task_status="rejected")
    error.http_status = proof["http_status"]
    error.error_type = proof["error_type"]
    error.provider_code = proof["provider_code"]
    error.error_code = proof["error_code"]
    error.request_id = proof.get("request_id", "")
    raise error


def available(stage: str = "edit") -> list[str]:
    if stage != "edit":
        return ["Seedance 2.5仅用于视频角色替换"]
    return [] if os.environ.get("ARK_API_KEY", "").strip() else [
        "请在API设置中保存火山方舟 ARK_API_KEY，并开通 Seedance 2.5 模型。"]


def upload_missing() -> list[str]:
    """Only ordinary local-media uploads require TOS; approved assets do not."""
    return storage_tos.available()


def _option(option: str) -> None:
    if option != OPTION:
        raise ValueError("视频角色替换请选择Seedance 2.5编辑模型")


def normalize_asset_id(value: str | None, *, label: str = "素材") -> str:
    """Accept an official ID or asset://ID, never an arbitrary URL or path."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label}ID须为asset-开头的素材编号或asset://链接")
    value = value.strip()
    if not value:
        return ""
    identifier = value[8:] if value.startswith("asset://") else value
    if not re.fullmatch(r"asset-[A-Za-z0-9][A-Za-z0-9_-]{1,190}", identifier):
        raise ValueError(f"{label}ID无效，请从方舟素材库复制asset-开头的编号；不接受网页链接或本地路径")
    return "asset://" + identifier


def _assets(video_asset_id: str, role_asset_ids, count: int) -> tuple[str, list[str]]:
    video = normalize_asset_id(video_asset_id, label="原视频素材")
    if role_asset_ids is None:
        roles = [""] * count
    elif (not isinstance(role_asset_ids, (list, tuple)) or len(role_asset_ids) != count):
        raise ValueError("角色素材ID须按角色顺序提供，每张角色图对应一个ID或留空")
    else:
        roles = [normalize_asset_id(value, label=f"角色{index}素材")
                 for index, value in enumerate(role_asset_ids, 1)]
    return video, roles


def _paths(destination: Path) -> tuple[Path, Path, Path]:
    return tuple(destination.with_name(destination.stem + suffix)
                 for suffix in ("_task.json", "_submission.json", "_result.json"))


def _read(path: Path) -> dict:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def _atomic_record(path: Path, value: dict) -> None:
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError):
        raise MotionTransientError("Seedance原任务记录保存失败，请检查数据目录后续查原任务，勿重新生成。") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _cancelled(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise MotionCancelledError("软件正在关闭，已暂停本地Seedance查询；重开可继续原任务，不会重新提交。")


def _progress(progress, status: str, stage: str, message: str = "") -> None:
    if progress is not None:
        try:
            progress({"status": status, "stage": stage, "message": message or stage})
        except Exception:
            raise MotionTransientError("Seedance本地进度记录未保存，可稍后继续原任务；不会重新提交。") from None


def _wait(seconds: float, stop_event) -> None:
    _cancelled(stop_event)
    if stop_event is None:
        time.sleep(seconds)
    elif stop_event.wait(seconds):
        _cancelled(stop_event)


def _probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height,duration,avg_frame_rate,r_frame_rate:format=duration",
         "-of", "json", str(path)], check=True, capture_output=True, text=True,
        timeout=30, **quiet_subprocess_kwargs())
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    stream = next(item for item in streams if item.get("codec_type") == "video")
    duration = 0.0
    for value in (stream.get("duration"), data.get("format", {}).get("duration")):
        try:
            candidate = float(value)
            if math.isfinite(candidate) and candidate > 0:
                duration = candidate
                break
        except (ValueError, TypeError, OverflowError):
            pass
    width, height = int(stream["width"]), int(stream["height"])
    fps = 0.0
    for field in ("avg_frame_rate", "r_frame_rate"):
        try:
            value = float(Fraction(str(stream.get(field, "0/0"))))
            if math.isfinite(value) and value > 0:
                fps = value
                break
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            pass
    if min(width, height) <= 0 or not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid video metadata")
    return {"width": width, "height": height, "duration": duration, "fps": fps,
            "video_codec": stream.get("codec_name", ""),
            "audio_codecs": [item.get("codec_name", "") for item in streams if item.get("codec_type") == "audio"]}


def _prompt(role_descriptions, prompt: str) -> str:
    if not isinstance(prompt, str) or len(prompt) > 1000:
        raise ValueError("Seedance角色替换补充要求须为1000字以内的文字")
    lines = []
    for index, description in enumerate(role_descriptions, 1):
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 200:
            raise ValueError(f"请用1–200字描述原视频中的角色{index}")
        lines.append(f"将@视频1中“{description.strip()}”替换为@图片{index}中的角色，仅采用该图的角色外观，不采用图中背景。")
    return ("编辑@视频1，按以下对应关系分别替换角色，并使每个角色在整段视频中保持同一身份。\n"
            + "\n".join(lines)
            + "\n保持原视频的动作、节奏、人物互动、遮挡关系、初始站位、背景、物体和镜头运动。"
              "不要交换角色身份，不添加额外人物，不生成分屏或拼图。保留原片声音。"
            + (("\n补充要求：" + prompt.strip()) if prompt.strip() else ""))


def validate_edit_inputs(references: list[Path], clip: Path, option: str = OPTION, *,
                         role_descriptions: list[str], prompt: str = "", resolution: str = "1080p",
                         video_asset_id: str = "", role_asset_ids=None) -> dict:
    """Offline validation only. Local preview media must match any supplied asset.

    An approved original-video asset must correspond to the selected *trimmed
    clip*, not to the untrimmed full video. This function cannot inspect the
    remote asset, so the UI must require the user to confirm that correspondence.
    """
    _option(option)
    if not isinstance(references, (list, tuple)) or not 1 <= len(references) <= 3:
        raise ValueError("Seedance视频角色替换须提供1–3张角色参考图")
    if not isinstance(role_descriptions, (list, tuple)) or len(role_descriptions) != len(references):
        raise ValueError("每张角色图须对应一条原视频人物描述")
    if resolution not in {"720p", "1080p"}:
        raise ValueError("Seedance角色替换请选择720p或1080p输出")
    request_prompt = _prompt(role_descriptions, prompt)
    video_asset, role_assets = _assets(video_asset_id, role_asset_ids, len(references))
    dimensions = []
    try:
        for index, reference in enumerate(references, 1):
            path = Path(reference)
            if not path.is_file() or not 0 < path.stat().st_size < MAX_IMAGE_BYTES:
                raise ValueError(f"角色图{index}须为非空图片且小于30MB")
            with Image.open(path) as image:
                width, height = image.size
                image_format = image.format
                image.verify()
            if image_format not in {"JPEG", "PNG", "WEBP", "BMP", "TIFF", "GIF", "HEIF", "HEIC"}:
                raise ValueError(f"角色图{index}格式不受Seedance支持")
            if not (300 <= width <= 6000 and 300 <= height <= 6000 and 0.4 <= width / height <= 2.5):
                raise ValueError(f"角色图{index}宽、高须在300–6000像素内，宽高比须在0.4–2.5内")
            dimensions.append({"width": width, "height": height})
        clip = Path(clip)
        if clip.suffix.lower() not in {".mp4", ".mov"}:
            raise ValueError("Seedance视频编辑仅支持MP4或MOV片段")
        if not clip.is_file() or not 0 < clip.stat().st_size <= MAX_VIDEO_BYTES:
            raise ValueError("Seedance原视频须为非空文件且不超过200MB")
        try:
            video = _probe(clip)
            width, height = int(video["width"]), int(video["height"])
            duration, fps = float(video["duration"]), float(video["fps"])
        except (OSError, ValueError, TypeError, KeyError, StopIteration, IndexError, RecursionError, subprocess.SubprocessError):
            raise ValueError("Seedance原视频无法读取，尚未上传或提交生成") from None
        if not (300 <= width <= 6000 and 300 <= height <= 6000 and 0.4 <= width / height <= 2.5
                and 407696 <= width * height <= 8295044):
            raise ValueError("Seedance原视频宽、高须在300–6000像素内，宽高比0.4–2.5，总像素407696–8295044；不会自动放大或裁切")
        if not math.isfinite(duration) or not 4 <= duration <= 30:
            raise ValueError("Seedance视频编辑片段须为4–30秒，请先选取对应片段")
        if not math.isfinite(fps) or not 24 <= fps <= 60:
            raise ValueError("Seedance原视频帧率须在24–60fps内")
        if video.get("video_codec") not in {"h264", "hevc"}:
            raise ValueError("Seedance原视频须使用H.264或H.265编码，请先转为标准MP4")
        accepted_audio = {"aac", "mp3"}
        if clip.suffix.lower() == ".mov":
            invalid_audio = [codec for codec in video.get("audio_codecs", [])
                             if codec not in accepted_audio and not codec.startswith("pcm_")]
        else:
            invalid_audio = [codec for codec in video.get("audio_codecs", []) if codec not in accepted_audio]
        if invalid_audio:
            raise ValueError("Seedance原视频音频须为AAC/MP3；MOV还支持PCM，请先转换音频编码")
        return {"reference_count": len(references), "reference_dimensions": dimensions,
                "video_width": width, "video_height": height, "video_fps": fps,
                "reference_duration_seconds": duration, "request_prompt": request_prompt,
                "resolution": resolution, "video_asset_id": video_asset, "role_asset_ids": role_assets}
    except (OSError, TypeError, Image.DecompressionBombError):
        raise ValueError("Seedance角色图或原视频无法读取，请检查素材；尚未上传或提交生成") from None


def _valid_source(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= 32 and bool(_probe(path))
    except (OSError, ValueError, TypeError, KeyError, StopIteration, IndexError, RecursionError, subprocess.SubprocessError):
        return False


def _https(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if (parsed.scheme != "https" or not hostname or parsed.username or parsed.password
                or parsed.port not in {None, 443} or parsed.fragment
                or hostname.lower() == "localhost" or hostname.lower().endswith((".localhost", ".local"))):
            return False
        try:
            if not ipaddress.ip_address(hostname).is_global:
                return False
        except ValueError:
            pass
        return True
    except ValueError:
        return False


def _key() -> str:
    key = os.environ.get("ARK_API_KEY", "").strip()
    if not key:
        raise MotionTransientError("继续查询Seedance原任务需要保存ARK_API_KEY；不会重新上传或提交生成。")
    return key


def _safe_usage(value) -> dict:
    """Only official task-usage counters, never provider URLs or arbitrary data."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for field in ("completion_tokens", "total_tokens"):
        number = value.get(field)
        if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 10 ** 15:
            result[field] = number
    tools = value.get("tool_usage")
    searches = tools.get("web_search") if isinstance(tools, dict) else None
    if isinstance(searches, int) and not isinstance(searches, bool) and 0 <= searches <= 10 ** 9:
        result["tool_usage"] = {"web_search": searches}
    return result


def _metadata(task: dict | None = None, result: dict | None = None, *, reused: bool = False) -> dict:
    task, result = task or {}, result or {}
    requested_resolution = task.get("resolution", "1080p")
    if requested_resolution not in {"720p", "1080p"}:
        requested_resolution = "1080p"
    metadata = {"provider": PROVIDER, "model": MODEL, "model_id": MODEL, "motion_model": MODEL,
                "option": OPTION, "task_id": task.get("task_id", ""), "operation": "video_edit",
                "requested_resolution": requested_resolution, "restore_reference_audio": True,
                "reused_local_video": reused, "usage": _safe_usage(result.get("usage")),
                "note": f"Seedance 2.5按角色描述编辑原视频，已请求{requested_resolution}；实际尺寸、帧率和时长由下载文件实测。输出可能比输入短约0.4秒，不强制补帧、拉伸或改为竖屏。"}
    for field in ("duration", "framespersecond"):
        value = result.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
            metadata["api_" + field] = value
    resolution = result.get("api_resolution")
    if resolution in {"480p", "720p", "1080p", "4k"}:
        metadata["api_resolution"] = resolution
    return metadata


def _task_record(identifier: str, resolution: str = "1080p") -> dict:
    return {"provider": PROVIDER, "option": OPTION, "model": MODEL,
            "endpoint": CREATE_URL, "task_id": identifier, "resolution": resolution}


def _identifier(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"cgt-[A-Za-z0-9_-]{1,250}", value))


def _read_task(destination: Path, option: str, resolution: str | None = None) -> dict:
    _option(option)
    task = _read(_paths(destination)[0])
    saved_resolution = task.get("resolution", "1080p")
    if (task.get("provider") != PROVIDER or task.get("option") != option or task.get("model") != MODEL
            or task.get("endpoint") != CREATE_URL or not _identifier(task.get("task_id"))
            or saved_resolution not in {"720p", "1080p"}
            or (resolution is not None and resolution != saved_resolution)):
        raise MotionSubmissionUncertainError("缺少有效Seedance原任务编号或精确模型记录；请核对方舟任务历史，已阻止重新提交。")
    return _task_record(task["task_id"], saved_resolution)


def _matching_result(task: dict, result: dict) -> bool:
    return (all(result.get(field) == task.get(field) for field in ("provider", "option", "model", "endpoint", "task_id"))
            and result.get("resolution", "1080p") == task.get("resolution", "1080p"))


def _safe_error(body: dict, default: str = "请核对账户权限、模型开通、余额和素材要求") -> str:
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return default
    code, message = error.get("code"), error.get("message")
    detail = "；".join(str(value) for value in (code, message) if isinstance(value, str))
    return redact_error(detail)[:500] or default


def _query(task: dict) -> dict:
    try:
        response = requests.get(CREATE_URL + "/" + task["task_id"],
                                headers={"Authorization": "Bearer " + _key()},
                                timeout=(15, 60), allow_redirects=False)
    except requests.RequestException:
        raise MotionTransientError("Seedance查询网络暂不可用，原编号已保留；稍后仅续查原任务。") from None
    try:
        if response.status_code in {404, 410}:
            raise MotionTerminalError("Seedance原任务不存在、已过期或当前项目不可查。", task_status="not_found" if response.status_code == 404 else "expired")
        if not 200 <= response.status_code < 300:
            raise MotionTransientError(f"Seedance查询暂不可用（HTTP {response.status_code}）；请检查密钥、项目和网络，原任务不会重新提交。")
        try:
            body = response.json()
        except (ValueError, RecursionError):
            raise MotionTransientError("Seedance查询响应暂不可解析，可稍后继续原任务。") from None
        if (not isinstance(body, dict) or body.get("id") != task["task_id"]
                or body.get("model", MODEL) != MODEL):
            raise MotionTransientError("Seedance未返回匹配的原任务及模型记录，已保留编号，勿重新提交。")
        return body
    finally:
        response.close()


class _URLUnavailable(MotionTransientError):
    pass


def _download(url: str, destination: Path, *, progress=None, stop_event=None) -> None:
    if not _https(url):
        raise MotionTransientError("Seedance已完成，但视频地址不是有效HTTPS链接；请续查原任务。")
    temporary = destination.with_name(destination.name + ".download")
    try:
        _progress(progress, "downloading", "下载已生成的Seedance原片")
        response = None
        for _ in range(4):
            _cancelled(stop_event)
            response = requests.get(url, stream=True, timeout=(20, 60), allow_redirects=False)
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            following = urljoin(url, response.headers.get("Location", ""))
            response.close()
            response = None
            if not _https(following):
                raise ValueError("unsafe download redirect")
            url = following
        if response is None or response.status_code in {301, 302, 303, 307, 308}:
            raise ValueError("too many download redirects")
        try:
            if response.status_code in {401, 403, 404, 410}:
                raise _URLUnavailable("Seedance原片链接暂不可用，将仅续查原任务更新链接。")
            if not 200 <= response.status_code < 300:
                raise ValueError("download not successful")
            size = 0
            with temporary.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    _cancelled(stop_event)
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise ValueError("video exceeds download limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        finally:
            response.close()
        _cancelled(stop_event)
        if not _valid_source(temporary):
            raise ValueError("invalid generated video")
        os.replace(temporary, destination)
    except (MotionCancelledError, _URLUnavailable):
        raise
    except Exception:
        raise MotionTransientError("Seedance原片已生成，但下载或视频校验未完成；可继续原任务下载，不会重新生成。") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _terminal(destination: Path, task: dict, status: str, detail: str = "") -> None:
    _atomic_record(_paths(destination)[2], {**task, "status": status, "error": detail,
                                           "updated_at_utc": datetime.now(timezone.utc).isoformat()})
    label = {"failed": "生成失败", "expired": "已超时或过期", "cancelled": "已被取消",
             "not_found": "不存在或当前账户不可查"}.get(status, "无法继续")
    suffix = f"：{detail}" if detail else ""
    raise MotionTerminalError(f"Seedance原任务{label}{suffix}；请核对方舟任务历史，不会重新提交。", task_status=status)


def _poll(destination: Path, task: dict, *, progress=None, stop_event=None) -> dict:
    result_path = _paths(destination)[2]
    cached = _read(result_path)
    if not _matching_result(task, cached):
        cached = {}
    if cached.get("status") in TERMINAL_STATUSES:
        _terminal(destination, task, cached["status"], cached.get("error", ""))
    if _https(cached.get("video_url")):
        try:
            _download(cached["video_url"], destination, progress=progress, stop_event=stop_event)
            _progress(progress, "downloaded", "Seedance原片已取回，准备本地保存")
            return _metadata(task, cached)
        except _URLUnavailable:
            pass
    failures = 0
    deadline = time.monotonic() + 45 * 60
    while time.monotonic() < deadline:
        _cancelled(stop_event)
        _progress(progress, "querying", "查询原有Seedance 2.5任务")
        try:
            body = _query(task)
            phase = body.get("status")
            if phase in TERMINAL_STATUSES:
                _terminal(destination, task, phase, _safe_error(body, ""))
            if phase == "succeeded":
                content = body.get("content")
                url = content.get("video_url") if isinstance(content, dict) else None
                if not _https(url):
                    raise MotionTransientError("Seedance任务成功但未返回可用视频链接，稍后仅续查原任务。")
                record = {**task, "status": "done", "video_url": url, "usage": _safe_usage(body.get("usage")),
                          "duration": body.get("duration"), "framespersecond": body.get("framespersecond"),
                          "api_resolution": body.get("resolution"), "raw_result": body,
                          "updated_at_utc": datetime.now(timezone.utc).isoformat()}
                _atomic_record(result_path, record)
                _download(url, destination, progress=progress, stop_event=stop_event)
                _progress(progress, "downloaded", "Seedance原片已取回，准备本地保存")
                return _metadata(task, record)
            if phase not in {"queued", "running"}:
                raise MotionTransientError("Seedance任务状态暂不可识别，已保留原编号。")
            failures = 0
            _progress(progress, "processing", "Seedance原任务排队中" if phase == "queued" else "Seedance原任务正在生成")
            _wait(5, stop_event)
        except MotionTerminalError as error:
            # _terminal already persisted a failed API result; a query HTTP
            # not-found/expired response still needs a durable terminal record.
            existing = _read(result_path)
            if not (_matching_result(task, existing) and existing.get("status") == error.task_status):
                _terminal(destination, task, error.task_status)
            raise
        except MotionTransientError:
            failures += 1
            if failures >= 3:
                raise MotionTransientError("Seedance原任务查询或下载暂未完成；编号已保留，可稍后续查，不会重新提交。") from None
            _progress(progress, "retrying", "查询暂未完成，稍后续查同一Seedance任务")
            _wait(min(2 ** failures, 15), stop_event)
    raise MotionTransientError("本地等待已结束，Seedance原编号已保留；重开后继续查询同一任务，不会重新生成。")


def resume_motion(destination: Path, option: str = OPTION, *, resolution: str | None = None,
                  progress=None, stop_event=None) -> dict:
    """No input upload and no chargeable POST, including corrupt checkpoints."""
    _option(option)
    if resolution is not None and resolution not in {"720p", "1080p"}:
        raise ValueError("Seedance角色替换请选择720p或1080p输出")
    destination = Path(destination)
    _cancelled(stop_event)
    rejection = read_submission_rejection(destination, option)
    if rejection is not None:
        _raise_rejected(rejection)
    if _valid_source(destination):
        try:
            task = _read_task(destination, option, resolution)
        except MotionSubmissionUncertainError:
            if _paths(destination)[0].exists():
                raise
            task = {}
            if resolution is not None:
                task["resolution"] = resolution
        cached = _read(_paths(destination)[2])
        if not task or not _matching_result(task, cached):
            cached = {}
        _progress(progress, "downloaded", "复用已完整下载的Seedance原片，继续本地保存")
        return _metadata(task, cached, reused=True)
    return _poll(destination, _read_task(destination, option, resolution), progress=progress, stop_event=stop_event)


def _upload_inputs(references: list[Path], clip: Path, validated: dict, *, stop_event=None) -> tuple[list[str], str]:
    requires_upload = not validated["video_asset_id"] or not all(validated["role_asset_ids"])
    if requires_upload:
        problems = upload_missing()
        if problems:
            raise ValueError("Seedance普通素材上传需要TOS：" + "；".join(problems))
    try:
        images = []
        for index, path in enumerate(references):
            _cancelled(stop_event)
            asset = validated["role_asset_ids"][index]
            url = asset or storage_tos.upload_file(Path(path))
            if not asset and not _https(url):
                raise ValueError("invalid uploaded reference")
            images.append(url)
        _cancelled(stop_event)
        video = validated["video_asset_id"] or storage_tos.upload_file(Path(clip))
        if not validated["video_asset_id"] and not _https(video):
            raise ValueError("invalid uploaded video")
        _cancelled(stop_event)
        return images, video
    except MotionCancelledError:
        raise
    except Exception:
        raise MotionTransientError("Seedance素材上传未完成，尚未提交视频生成；请检查TOS及网络配置。") from None


def _persist_accepted_task(task_path: Path, submission_path: Path, task: dict) -> None:
    try:
        _atomic_record(task_path, task)
    except MotionTransientError:
        # Preserve an accepted ID even if fsync/atomic replace is unavailable.
        # This best-effort fallback never causes another submission. If the
        # disk is completely unwritable, the displayed error also carries ID.
        for path, value in ((task_path, task), (submission_path, {**task, "status": "accepted_record_uncertain"})):
            try:
                with path.open("w", encoding="utf-8") as output:
                    json.dump(value, output, ensure_ascii=False, indent=2)
                    output.flush()
                    os.fsync(output.fileno())
            except OSError:
                pass
        raise MotionSubmissionUncertainError(
            f"Seedance已接受任务，但本地编号同步未能确认。原任务编号：{task['task_id']}；请保留该编号并核对方舟历史，勿重新提交。") from None


def video_edit(references: list[Path], clip: Path, destination: Path, option: str = OPTION, *,
               role_descriptions: list[str], prompt: str = "", resolution: str = "1080p",
               video_asset_id: str = "", role_asset_ids=None, progress=None, stop_event=None) -> dict:
    destination = Path(destination)
    task_path, submission_path, result_path = _paths(destination)
    if any(path.exists() for path in (destination, task_path, submission_path, result_path)):
        rejection = read_submission_rejection(destination, option)
        if rejection is not None:
            _raise_rejected(rejection)
        raise MotionSubmissionUncertainError("已有Seedance结果或提交记录，请继续原任务，已阻止重复生成。")
    validated = validate_edit_inputs(references, clip, option, role_descriptions=role_descriptions, prompt=prompt,
                                     resolution=resolution, video_asset_id=video_asset_id, role_asset_ids=role_asset_ids)
    problems = available("edit")
    if problems:
        raise ValueError("；".join(problems))
    destination.parent.mkdir(parents=True, exist_ok=True)
    _cancelled(stop_event)
    _progress(progress, "uploading", "准备Seedance角色图与原视频素材")
    image_urls, video_url = _upload_inputs(references, clip, validated, stop_event=stop_event)
    payload = {"model": MODEL, "content": [{"type": "text", "text": validated["request_prompt"]}],
               "omni_reference_task_type": "edit", "ratio": "adaptive", "duration": -1,
               "resolution": resolution, "output_format": "mp4", "generate_audio": True, "watermark": True}
    payload["content"].extend({"type": "image_url", "role": "reference_image", "image_url": {"url": url}}
                              for url in image_urls)
    payload["content"].append({"type": "video_url", "role": "reference_video", "video_url": {"url": video_url}})
    _cancelled(stop_event)
    # A concurrent local invocation may have submitted while uploads ran.
    if any(path.exists() for path in (destination, task_path, submission_path, result_path)):
        rejection = read_submission_rejection(destination, option)
        if rejection is not None:
            _raise_rejected(rejection)
        raise MotionSubmissionUncertainError("已有Seedance结果或提交记录，请继续原任务，已阻止重复生成。")
    identity = {"provider": PROVIDER, "option": option, "model": MODEL,
                "endpoint": CREATE_URL, "resolution": resolution}
    try:
        with submission_path.open("x", encoding="utf-8") as marker:
            json.dump({**identity,
                       "status": "submitting", "submitted_at_utc": datetime.now(timezone.utc).isoformat()}, marker)
            marker.flush()
            os.fsync(marker.fileno())
    except (OSError, ValueError, TypeError):
        raise MotionSubmissionUncertainError("无法保存Seedance独占提交标记，已阻止提交；请核对数据目录和方舟历史。") from None
    try:
        # Deliberately no SDK retry, Session retry, redirect or retry loop.
        response = requests.post(CREATE_URL, headers={"Authorization": "Bearer " + _key()}, json=payload,
                                 timeout=(20, 120), allow_redirects=False)
    except Exception:
        raise MotionSubmissionUncertainError("Seedance提交响应未可靠取回，独占标记已保留；请核对方舟历史，勿重复提交。") from None
    try:
        code = response.status_code
        if not isinstance(code, int) or not 200 <= code < 300:
            detail = ""
            proof = None
            if isinstance(code, int) and 400 <= code < 500:
                try:
                    body = response.json()
                    detail = _safe_error(body) if isinstance(body, dict) else ""
                    proof = _rejection_evidence(code, body)
                except (ValueError, TypeError, RecursionError):
                    pass
            if proof is not None:
                marker = _read(submission_path)
                if (any(path.exists() for path in (destination, task_path, result_path))
                        or marker.get("status") != "submitting" or _acceptance_evidence(marker)
                        or any(marker.get(field) != value for field, value in identity.items())):
                    raise MotionSubmissionUncertainError(
                        "Seedance返回素材拒绝，但本地提交记录存在冲突；请核对方舟历史，勿重新提交。")
                try:
                    _atomic_record(submission_path, {**identity, **proof, "status": "rejected",
                                   "submitted_at_utc": marker.get("submitted_at_utc"),
                                   "rejected_at_utc": datetime.now(timezone.utc).isoformat()})
                except MotionTransientError:
                    raise MotionSubmissionUncertainError(
                        "Seedance返回素材拒绝，但拒绝证据未可靠保存；请核对方舟历史，勿重新提交。") from None
                _raise_rejected(proof)
            hint = f"（HTTP {code}）" if isinstance(code, int) else ""
            suffix = f"：{detail}" if detail else ""
            raise MotionSubmissionUncertainError(
                f"Seedance提交未能可靠确认{hint}{suffix}。提交标记已保留，请核对方舟账户与任务历史，勿重复提交。")
        try:
            body = response.json()
            identifier = body.get("id") if isinstance(body, dict) else None
            if not _identifier(identifier) or body.get("error") is not None:
                raise ValueError("missing accepted task ID")
        except (ValueError, TypeError, RecursionError):
            raise MotionSubmissionUncertainError("Seedance响应缺少可靠任务编号；独占标记已保留，请核对方舟历史，勿重新提交。") from None
    finally:
        response.close()
    task = _task_record(identifier, resolution)
    _persist_accepted_task(task_path, submission_path, task)
    return _poll(destination, task, progress=progress, stop_event=stop_event)
