"""MiniMax H3 multimodal reference video generation, with durable query-only recovery.

Official CN V2 API: /docs/api-reference/video-generation-v2-create. Inputs use
documented data URIs, so this adapter needs no public bucket or upload service.
H3 reference generation may redraw the scene; it is not pixel-preserving editing.
"""
from __future__ import annotations

import base64
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
from urllib.parse import urlsplit

import requests
from PIL import Image

from providers_jimeng import (MotionCancelledError, MotionSubmissionUncertainError,
                              MotionTerminalError, MotionTransientError)
from runtime_paths import quiet_subprocess_kwargs

OPTION = "minimax-h3-edit"
MODEL = "MiniMax-H3"
CREATE_URL = "https://api.minimax.cn/v2/video_generation"
QUERY_BASE = "https://api.minimax.cn/v2/query/video_generation/"
MAX_IMAGE_BYTES = 30 * 1000 * 1000
MAX_VIDEO_BYTES = 50 * 1000 * 1000
MAX_REQUEST_BYTES = 64 * 1000 * 1000
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
USAGE_KEYS = {"total_seconds", "input_seconds", "output_seconds", "input_image_count",
              "input_audio_seconds", "total_tokens", "prompt_tokens", "completion_tokens"}
# Only documented V2 errors with a matching HTTP status are definitive refusal.
# In particular, a bare HTTP 402 or a proxy HTML page proves no task outcome.
REJECTION_TYPES = {400: "bad_request_error", 401: "authorized_error",
                   402: "insufficient_balance_error", 422: "unprocessable_entity_error",
                   429: "rate_limit_error"}
REJECTION_CODES = {400: "2013", 401: "1004", 402: "1008", 422: "1026", 429: "1002"}


def _acceptance_evidence(value) -> bool:
    """Any task/ID-like field makes a refusal ambiguous, including nested data."""
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


def _rejection_evidence(status, value) -> dict | None:
    """Return an allowlisted error proof, never the server's raw error message."""
    if (type(status) is not int or status not in REJECTION_TYPES or not isinstance(value, dict)
            or value.get("type") != "error" or _acceptance_evidence(value)):
        return None
    error = value.get("error")
    if (not isinstance(error, dict) or error.get("type") != REJECTION_TYPES[status]
            or error.get("http_code") != str(status)
            or not isinstance(error.get("message"), str) or not error["message"].strip()):
        return None
    # A contradictory documented numeric code is not a definite rejection proof.
    code_match = re.search(r"\(([0-9]{1,8})\)\s*$", error["message"])
    if code_match and code_match.group(1) != REJECTION_CODES[status]:
        return None
    result = {"http_status": status, "error_type": REJECTION_TYPES[status],
              "rejection_verified": True}
    if code_match:
        result["provider_code"] = code_match.group(1)
    request_id = value.get("request_id")
    # Official request tracing IDs are lowercase hex, not arbitrary text/URLs.
    if isinstance(request_id, str) and re.fullmatch(r"[0-9][a-f0-9]{15,63}", request_id):
        result["request_id"] = request_id
    return result


def submission_rejection(destination: Path, option: str = OPTION) -> dict | None:
    """Read verified refusal evidence only; legacy bare markers remain uncertain."""
    _option(option)
    destination = Path(destination)
    task_path, submission_path, result_path = _paths(destination)
    if any(path.exists() for path in (destination, task_path, result_path)):
        return None
    record = _read(submission_path)
    status = record.get("http_status")
    if (record.get("provider") != "minimax" or record.get("model") != MODEL
            or record.get("option") != option or record.get("endpoint") != CREATE_URL
            or record.get("status") != "rejected" or record.get("rejection_verified") is not True
            or type(status) is not int or status not in REJECTION_TYPES
            or record.get("error_type") != REJECTION_TYPES[status]
            or _acceptance_evidence(record)
            or not isinstance(record.get("resolution"), str) or record["resolution"] not in {"768P", "2K"}
            or type(record.get("duration")) is not int or not 4 <= record["duration"] <= 15):
        return None
    safe = {"http_status": status, "error_type": REJECTION_TYPES[status], "rejection_verified": True}
    if record.get("provider_code") == REJECTION_CODES[status]:
        safe["provider_code"] = record["provider_code"]
    request_id = record.get("request_id")
    if isinstance(request_id, str) and re.fullmatch(r"[0-9][a-f0-9]{15,63}", request_id):
        safe["request_id"] = request_id
    return safe


read_submission_rejection = submission_rejection


def _raise_rejected(proof: dict) -> None:
    status = proof["http_status"]
    advice = {
        400: "请修改素材或请求参数后再新建测试。",
        401: "请检查并保存普通按量API Key后再新建测试。",
        402: "请检查MiniMax普通按量账户余额，充值生效后再新建测试；海螺会员或M Plan不能代替该余额。",
        422: "素材或提示词未通过平台校验或审核，请按平台要求调整后再新建测试。",
        429: "账户频率或额度达到限制，请核对平台额度后再新建测试。",
    }[status]
    error = MotionTerminalError(
        f"MiniMax明确拒绝本次提交（HTTP {status}），未创建视频任务。{advice}本记录已保留拒绝证据，不会自动重新提交。",
        task_status="rejected")
    error.http_status = status
    error.error_type = proof["error_type"]
    error.request_id = proof.get("request_id", "")
    raise error


def available(stage: str = "edit") -> list[str]:
    if stage != "edit":
        return ["MiniMax H3仅用于视频角色替换"]
    return [] if os.environ.get("MINIMAX_API_KEY", "").strip() else [
        "请在API设置中保存 MINIMAX_API_KEY，并开通 MiniMax H3 按量调用权限。"]


def _option(option: str) -> None:
    if option != OPTION:
        raise ValueError("不支持的MiniMax视频模型选项")


def _key() -> str:
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not key:
        raise MotionTransientError("请先保存 MINIMAX_API_KEY；仅继续查询原任务，不会重新提交。")
    return key


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
        fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        raise MotionTransientError("MiniMax原任务记录保存失败；请检查数据目录并继续原任务，勿重新生成。") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _cancelled(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise MotionCancelledError("软件正在关闭，已暂停本地MiniMax查询；重开后可继续原任务。")


def _progress(progress, status: str, stage: str) -> None:
    if progress is not None:
        try:
            progress({"status": status, "stage": stage, "message": stage})
        except Exception:
            raise MotionTransientError("本地MiniMax进度保存失败；保留原任务后可稍后续查。") from None


def _wait(seconds: float, stop_event) -> None:
    _cancelled(stop_event)
    if stop_event is None:
        time.sleep(seconds)
    elif stop_event.wait(seconds):
        _cancelled(stop_event)


def _probe(path: Path) -> dict:
    process = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate:format=duration",
         "-of", "json", str(path)], check=True, capture_output=True, text=True,
        timeout=30, **quiet_subprocess_kwargs())
    data = json.loads(process.stdout)
    streams = data["streams"]
    video = next(stream for stream in streams if stream.get("codec_type") == "video")
    audio = [stream.get("codec_name") for stream in streams if stream.get("codec_type") == "audio"]
    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            value = float(Fraction(str(video.get(key, "0/0"))))
            if math.isfinite(value) and value > 0:
                fps = value
                break
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            pass
    duration = float(data["format"]["duration"])
    width, height = int(video["width"]), int(video["height"])
    if width <= 0 or height <= 0 or not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid metadata")
    return {"width": width, "height": height, "duration": duration, "fps": fps,
            "video_codec": video.get("codec_name"), "audio_codecs": audio}


def _valid_source(path: Path) -> bool:
    try:
        return path.is_file() and 0 < path.stat().st_size <= MAX_DOWNLOAD_BYTES and bool(_probe(path))
    except (OSError, ValueError, TypeError, KeyError, IndexError, StopIteration,
            RecursionError, subprocess.SubprocessError):
        return False


def _edit_prompt(role_descriptions: list[str], prompt: str) -> str:
    if not isinstance(prompt, str):
        raise ValueError("角色替换补充要求须为文字")
    mappings = []
    for index, description in enumerate(role_descriptions, 1):
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"请填写原视频角色{index}的辨认描述")
        mappings.append(f"原视频中的人物{index}（{description.strip()}），替换成参考图片{index}中的角色外观。")
    text = ("以参考视频1为原片，按照下面的对应关系生成同一段表演的视频。\n" + "\n".join(mappings)
            + "\n每个参考图片只用于对应角色的外观，不采用图片背景。保持角色对应关系、原片动作节奏、互动、站位、"
              "遮挡关系、场景布局和镜头运动；尽量保留原视频背景，不增加人物，不交换身份，不分屏。"
            + (("\n补充要求：" + prompt.strip()) if prompt.strip() else ""))
    if len(text) > 7000:
        raise ValueError("角色描述与补充要求合成后超过MiniMax H3的7000字符上限，请缩短。")
    return text


def validate_edit_inputs(references: list[Path], clip: Path, option: str = OPTION, *,
                         role_descriptions: list[str], prompt: str = "", resolution: str = "768P",
                         video_asset_id: str = "", role_asset_ids=None) -> dict:
    """Validate files and exact encoded request size locally, without credentials or HTTP."""
    _option(option)
    if video_asset_id or (role_asset_ids and any(role_asset_ids)):
        raise ValueError("MiniMax H3不使用方舟素材ID，请使用本地原视频与角色图片。")
    if resolution not in {"768P", "2K"}:
        raise ValueError("MiniMax H3输出分辨率请选择768P或2K")
    if not isinstance(references, (list, tuple)) or not 1 <= len(references) <= 3:
        raise ValueError("请提供1–3张MiniMax H3角色参考图")
    if not isinstance(role_descriptions, (list, tuple)) or len(role_descriptions) != len(references):
        raise ValueError("每张角色图都须填写对应的原视频人物描述")
    text = _edit_prompt(role_descriptions, prompt)
    dimensions, image_formats = [], []
    encoded_bytes = 0
    try:
        for index, reference in enumerate(references, 1):
            path = Path(reference)
            size = path.stat().st_size if path.is_file() else 0
            if not 0 < size <= MAX_IMAGE_BYTES:
                raise ValueError(f"角色图{index}须为有效图片且不能超过30MB")
            with Image.open(path) as image:
                width, height = image.size
                image_format = (image.format or "").lower()
                image.verify()
            if image_format not in {"jpeg", "png", "webp"}:
                raise ValueError(f"角色图{index}请使用JPG、PNG或WebP格式")
            if not (256 <= width <= 5760 and 256 <= height <= 5760 and .4 <= width / height <= 2.5):
                raise ValueError(f"角色图{index}宽高须在256–5760像素，宽高比须在0.4–2.5内")
            dimensions.append({"width": width, "height": height})
            image_formats.append(image_format)
            encoded_bytes += 4 * ((size + 2) // 3) + len(f"data:image/{image_format};base64,")
        clip = Path(clip)
        size = clip.stat().st_size if clip.is_file() else 0
        if not 0 < size <= MAX_VIDEO_BYTES or clip.suffix.lower() not in {".mp4", ".mov"}:
            raise ValueError("MiniMax H3原片须为MP4或MOV且不能超过50MB")
        video = _probe(clip)
        width, height = video["width"], video["height"]
        duration, fps = float(video["duration"]), float(video["fps"])
        if not (256 <= width <= 5760 and 256 <= height <= 5760 and .4 <= width / height <= 2.5):
            raise ValueError("MiniMax H3原片宽高须在256–5760像素，宽高比须在0.4–2.5内")
        if not math.isfinite(fps) or not 23.976 <= fps <= 60:
            raise ValueError("MiniMax H3原片帧率须在23.976–60fps内")
        if video.get("video_codec") not in {"h264", "hevc"} or any(
                codec not in {"aac", "mp3"} for codec in video.get("audio_codecs", [])):
            raise ValueError("MiniMax H3原片须使用H.264/H.265视频和AAC/MP3音频编码")
        if not math.isfinite(duration):
            raise ValueError("MiniMax H3原片时长无效，请重新选择片段")
        seconds = round(duration)
        if not 4 <= duration <= 15 or abs(duration - seconds) > .05:
            raise ValueError("MiniMax H3角色替换请选择4–15整秒片段；生成时长与所选片段对应。")
        # Official data URI video encoding is MP4. MOV is accepted by the API,
        # but this adapter's embedded transfer requires local normalization.
        if clip.suffix.lower() != ".mp4":
            raise ValueError("请先将原片整理为MP4片段，再提交MiniMax H3（软件预览会自动整理）。")
        skeleton = {"model": MODEL, "content": [{"type": "text", "text": text}]
                    + [{"type": "image_url", "image_url": {"url": ""}, "role": "reference_image"}
                       for _ in references]
                    + [{"type": "video_url", "video_url": {"url": ""}, "role": "reference_video"}],
                    "resolution": resolution, "duration": seconds, "ratio": "adaptive"}
        encoded_bytes += 4 * ((size + 2) // 3) + len("data:video/mp4;base64,")
        encoded_bytes += len(json.dumps(skeleton, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if encoded_bytes > MAX_REQUEST_BYTES:
            raise ValueError("素材编码后的请求超过MiniMax H3的64MB上限，请缩小角色图或降低原片码率后再预览。")
        return {"request_prompt": text, "resolution": resolution, "generation_duration_seconds": seconds,
                "reference_count": len(references), "reference_dimensions": dimensions,
                "image_formats": image_formats, "video_width": width, "video_height": height,
                "video_fps": fps, "reference_duration_seconds": duration,
                "request_bytes": encoded_bytes}
    except (OSError, KeyError, IndexError, TypeError, StopIteration, RecursionError,
            subprocess.SubprocessError, Image.DecompressionBombError):
        raise ValueError("MiniMax H3素材无法读取，请检查文件；尚未上传或提交生成。") from None


def _https(value) -> bool:
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        return bool(parsed and parsed.scheme == "https" and parsed.hostname
                    and not parsed.username and not parsed.password)
    except ValueError:
        return False


def _usage(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: number for key, number in value.items() if key in USAGE_KEYS
            and isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 10 ** 15}


def _metadata(task: dict, result: dict | None = None, *, reused: bool = False) -> dict:
    result = result or {}
    return {"provider": "minimax", "model": MODEL, "model_id": MODEL, "motion_model": MODEL,
            "option": OPTION, "task_id": task["task_id"], "operation": "video_edit",
            "resolution": result.get("resolution", task.get("resolution", "768P")),
            "requested_duration_seconds": task.get("duration"), "usage": _usage(result.get("usage")),
            "restore_reference_audio": True, "reused_local_video": reused,
            "note": "MiniMax H3使用多模态参考生成，按原视频与参考图片描述角色对应；可能重绘场景，不能保证逐帧保留原片或绝不串角色。实际画幅、尺寸和帧率由下载视频测量；费用包含输入参考视频和输出视频。"}


def _read_task(destination: Path, option: str) -> dict:
    _option(option)
    task = _read(_paths(destination)[0])
    identifier = task.get("task_id")
    if (task.get("provider") != "minimax" or task.get("option") != option or task.get("model") != MODEL
            or task.get("endpoint") != CREATE_URL or not isinstance(identifier, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier)
            or task.get("resolution") not in {"768P", "2K"}
            or type(task.get("duration")) is not int or not 4 <= task["duration"] <= 15):
        raise MotionSubmissionUncertainError("缺少匹配的MiniMax任务编号或模型记录；请核对官方任务历史，已阻止重新提交。")
    return {key: task[key] for key in ("provider", "option", "model", "endpoint", "task_id", "resolution", "duration")}


def _same_task(record: dict, task: dict) -> bool:
    return all(record.get(key) == task[key] for key in task)


class _URLUnavailable(MotionTransientError):
    pass


def _download(url: str, destination: Path, *, progress=None, stop_event=None) -> None:
    if not _https(url):
        raise MotionTransientError("MiniMax原任务完成但链接无效；可稍后继续查询原任务。")
    temporary = destination.with_name(destination.name + ".download")
    try:
        _cancelled(stop_event)
        _progress(progress, "downloading", "下载MiniMax H3生成原片")
        with requests.get(url, stream=True, timeout=(20, 60)) as response:
            if response.status_code in {401, 403, 404, 410}:
                raise _URLUnavailable("MiniMax临时下载链接已失效，将仅查询原任务。")
            response.raise_for_status()
            size = 0
            with temporary.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    _cancelled(stop_event)
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise ValueError("download too large")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        _cancelled(stop_event)
        if not _valid_source(temporary):
            raise ValueError("invalid downloaded video")
        _cancelled(stop_event)
        os.replace(temporary, destination)
    except (MotionCancelledError, _URLUnavailable):
        raise
    except Exception:
        raise MotionTransientError("MiniMax原片已生成，但下载或视频校验未完成；可继续原任务下载，不会重新生成。") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _terminal(destination: Path, task: dict, status: str) -> None:
    _atomic_record(_paths(destination)[2], {**task, "status": status})
    labels = {"failed": "生成失败", "cancelled": "已取消", "expired": "已过期", "not_found": "当前账户不可查"}
    raise MotionTerminalError(f"MiniMax原任务{labels.get(status, '无法继续')}；请核对官方任务历史，不会重新提交。", task_status=status)


def _query_json(task: dict) -> dict:
    try:
        response = requests.get(QUERY_BASE + task["task_id"],
                                headers={"Authorization": "Bearer " + _key()},
                                timeout=(15, 60), allow_redirects=False)
    except requests.RequestException:
        raise MotionTransientError("MiniMax查询网络暂不可用；任务编号已保存，稍后仅继续原任务。") from None
    try:
        if response.status_code in {404, 410}:
            raise MotionTerminalError("MiniMax原任务不可查；查询窗口为最近7天，请核对原账户。",
                                      task_status="expired" if response.status_code == 410 else "not_found")
        if not 200 <= response.status_code < 300:
            raise MotionTransientError(f"MiniMax查询暂不可用（HTTP {response.status_code}）；请核对密钥、余额或网络，仅续查原任务。")
        try:
            result = response.json()
        except (ValueError, RecursionError):
            raise MotionTransientError("MiniMax查询返回格式暂不可识别；原编号已保留。") from None
        value = result.get("task") if isinstance(result, dict) else None
        if (not isinstance(value, dict) or value.get("id") != task["task_id"]
                or value.get("model") != MODEL or value.get("task_type", "generation") != "generation"
                or value.get("modality", "video") != "video"
                or value.get("resolution", task["resolution"]) != task["resolution"]
                or value.get("duration", task["duration"]) != task["duration"]):
            raise MotionTransientError("MiniMax查询未返回匹配的视频任务；已保留原编号，不会重新提交。")
        return value
    finally:
        response.close()


def _poll(destination: Path, task: dict, *, progress=None, stop_event=None) -> dict:
    result_path = _paths(destination)[2]
    cached = _read(result_path)
    if not _same_task(cached, task):
        cached = {}
    if cached.get("status") in {"failed", "cancelled", "expired", "not_found"}:
        _terminal(destination, task, cached["status"])
    if _https(cached.get("video_url")):
        try:
            _download(cached["video_url"], destination, progress=progress, stop_event=stop_event)
            return _metadata(task, cached)
        except _URLUnavailable:
            pass
    failures = 0
    deadline = time.monotonic() + 45 * 60
    while time.monotonic() < deadline:
        _cancelled(stop_event)
        _progress(progress, "querying", "查询原有MiniMax H3任务")
        try:
            result = _query_json(task)
            phase = result.get("status")
            if phase in {"failed", "cancelled"}:
                _terminal(destination, task, phase)
            if phase == "succeeded":
                content = result.get("content")
                url = content.get("url") if isinstance(content, dict) else None
                if not _https(url):
                    raise MotionTransientError("MiniMax任务完成但暂未提供有效视频链接；稍后继续原任务。")
                saved = {**task, "status": "done", "video_url": url, "usage": _usage(result.get("usage")),
                         "updated_at_utc": datetime.now(timezone.utc).isoformat()}
                _atomic_record(result_path, saved)
                _download(url, destination, progress=progress, stop_event=stop_event)
                _progress(progress, "downloaded", "MiniMax H3原片已取回，准备本地保存")
                return _metadata(task, saved)
            if phase not in {"queued", "running"}:
                raise MotionTransientError("MiniMax任务状态暂不可识别；请稍后继续原任务。")
            failures = 0
            _progress(progress, "processing", "MiniMax H3原任务排队中" if phase == "queued" else "MiniMax H3原任务正在生成")
            _wait(3, stop_event)
        except MotionTerminalError as error:
            _terminal(destination, task, error.task_status)
        except MotionTransientError:
            failures += 1
            if failures >= 3:
                raise MotionTransientError("MiniMax原任务查询或下载暂未完成；编号已保留，稍后可继续，不会重新提交。") from None
            _progress(progress, "retrying", "稍后继续查询同一MiniMax H3任务")
            _wait(min(2 ** failures, 15), stop_event)
    raise MotionTransientError("本地等待已结束；MiniMax任务编号已保存，重开后可继续同一任务。")


def resume_motion(destination: Path, option: str = OPTION, *, progress=None, stop_event=None) -> dict:
    """GET and local filesystem only: never creates a generation or uploads inputs."""
    destination = Path(destination)
    _cancelled(stop_event)
    rejection = submission_rejection(destination, option)
    if rejection is not None:
        _raise_rejected(rejection)
    task = _read_task(destination, option)
    if _valid_source(destination):
        saved = _read(_paths(destination)[2])
        _progress(progress, "downloaded", "复用已完整下载的MiniMax原片，继续本地保存")
        return _metadata(task, saved if _same_task(saved, task) else None, reused=True)
    return _poll(destination, task, progress=progress, stop_event=stop_event)


def video_edit(references: list[Path], clip: Path, destination: Path, option: str = OPTION, *,
               role_descriptions: list[str], prompt: str = "", resolution: str = "768P",
               video_asset_id: str = "", role_asset_ids=None, progress=None, stop_event=None) -> dict:
    destination = Path(destination)
    if any(path.exists() for path in (destination, *_paths(destination))):
        rejection = submission_rejection(destination, option)
        if rejection is not None:
            _raise_rejected(rejection)
        raise MotionSubmissionUncertainError("已有MiniMax结果或提交记录，请继续原任务，已阻止重复生成。")
    checked = validate_edit_inputs(references, clip, option, role_descriptions=role_descriptions, prompt=prompt,
                                   resolution=resolution, video_asset_id=video_asset_id, role_asset_ids=role_asset_ids)
    missing = available("edit")
    if missing:
        raise ValueError("；".join(missing))
    _cancelled(stop_event)
    content = [{"type": "text", "text": checked["request_prompt"]}]
    try:
        for path, image_format in zip(references, checked["image_formats"]):
            _cancelled(stop_event)
            url = f"data:image/{image_format};base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": url}, "role": "reference_image"})
        _cancelled(stop_event)
        content.append({"type": "video_url", "video_url": {"url": "data:video/mp4;base64," +
                        base64.b64encode(Path(clip).read_bytes()).decode("ascii")}, "role": "reference_video"})
        encoded = json.dumps({"model": MODEL, "content": content, "resolution": resolution,
                              "duration": checked["generation_duration_seconds"], "ratio": "adaptive"},
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except OSError:
        raise ValueError("MiniMax素材读取失败；尚未提交视频生成。") from None
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("素材编码后超过MiniMax H3的64MB上限，尚未提交生成。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return _submit_payload(destination, option, encoded, resolution=resolution,
                           duration=checked["generation_duration_seconds"], progress=progress, stop_event=stop_event)


def _submit_payload(destination: Path, option: str, encoded: bytes, *, resolution: str, duration: int,
                    progress=None, stop_event=None) -> dict:
    _option(option)
    task_path, submission_path, result_path = _paths(destination)
    if any(path.exists() for path in (destination, task_path, submission_path, result_path)):
        rejection = submission_rejection(destination, option)
        if rejection is not None:
            _raise_rejected(rejection)
        raise MotionSubmissionUncertainError("已有MiniMax结果或提交记录，请继续原任务，已阻止重复生成。")
    _cancelled(stop_event)
    key = _key()
    identity = {"provider": "minimax", "option": option, "model": MODEL, "endpoint": CREATE_URL,
                "resolution": resolution, "duration": duration}
    try:
        with submission_path.open("x", encoding="utf-8") as output:
            json.dump({**identity, "status": "submitting", "submitted_at_utc": datetime.now(timezone.utc).isoformat()}, output)
            output.flush()
            os.fsync(output.fileno())
    except (OSError, ValueError, TypeError):
        raise MotionSubmissionUncertainError("无法保存独占MiniMax提交标记；已阻止提交，请核对数据目录及官方任务历史。") from None
    try:
        # Deliberately one billable POST: no redirects, retry adapters or SDK retries.
        response = requests.post(CREATE_URL, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                                 data=encoded, timeout=(20, 180), allow_redirects=False)
    except Exception:
        raise MotionSubmissionUncertainError("MiniMax提交响应未可靠取回；标记已保留，请核对官方任务历史，勿重复提交。") from None
    try:
        status = response.status_code
        if not isinstance(status, int) or not 200 <= status < 300:
            # A bare status is insufficient. Only the documented error envelope
            # with matching status/type and no task identity proves a refusal.
            proof = None
            if type(status) is int and status in REJECTION_TYPES:
                try:
                    proof = _rejection_evidence(status, response.json())
                except (ValueError, TypeError, RecursionError):
                    pass
            if proof is not None:
                marker = _read(submission_path)
                if (any(path.exists() for path in (destination, task_path, result_path))
                        or marker.get("status") != "submitting"
                        or any(marker.get(field) != value for field, value in identity.items())):
                    raise MotionSubmissionUncertainError("MiniMax返回拒绝，但本地提交记录出现冲突；请核对官方历史，已阻止重新提交。")
                try:
                    _atomic_record(submission_path, {**identity, **proof, "status": "rejected",
                                   "submitted_at_utc": marker.get("submitted_at_utc"),
                                   "rejected_at_utc": datetime.now(timezone.utc).isoformat()})
                except MotionTransientError:
                    raise MotionSubmissionUncertainError("MiniMax返回拒绝，但拒绝证据未可靠保存；请核对官方历史，勿重复提交。") from None
                _raise_rejected(proof)
            advice = {401: "请检查普通API Key是否有效。", 402: "请检查按量账户余额。",
                      403: "请开通MiniMax H3调用权限。", 422: "素材或提示词未通过平台审核。",
                      429: "账户调用频率或额度达到限制。"}.get(status, "请核对账户权限、素材和官方任务历史。")
            hint = f"（HTTP {status}）" if isinstance(status, int) else ""
            raise MotionSubmissionUncertainError(f"MiniMax提交未确认成功{hint}：{advice}标记已保留，勿重复提交。")
        try:
            result = response.json()
            identifier = result.get("task_id") if isinstance(result, dict) else None
            if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier)
                    or result.get("error") or result.get("type") == "error"):
                raise ValueError("missing task ID")
        except Exception:
            raise MotionSubmissionUncertainError("MiniMax提交响应未返回可靠任务编号；请核对官方任务历史，勿重复提交。") from None
    finally:
        response.close()
    task = {**identity, "task_id": identifier}
    try:
        _atomic_record(task_path, task)
    except MotionTransientError:
        raise MotionSubmissionUncertainError(f"MiniMax已接受任务但本地编号保存失败，任务编号：{identifier}；请人工取回，勿重复提交。") from None
    return _poll(destination, task, progress=progress, stop_event=stop_event)
