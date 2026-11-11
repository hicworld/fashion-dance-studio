"""Kling motion control with one submission and query-only durable recovery.

The SDK's submit method retries POSTs internally. Uploads use a fresh official
SDK client, while the billable queue POST below is deliberately issued once.
"""
from __future__ import annotations

import importlib
import importlib.util
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

ENDPOINTS = {
    "fal-kling-v3-pro": "fal-ai/kling-video/v3/pro/motion-control",
    "fal-kling-v3-standard": "fal-ai/kling-video/v3/standard/motion-control",
}
EDIT_ENDPOINTS = {
    "fal-kling-o3-pro-edit": "fal-ai/kling-video/o3/pro/video-to-video/edit",
}
FAL_VIDEO_ENDPOINTS = {**ENDPOINTS, **EDIT_ENDPOINTS}
QUEUE_BASE = "https://queue.fal.run/"
QUEUE_REQUESTS = QUEUE_BASE + "fal-ai/kling-video/requests/"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_EDIT_IMAGE_BYTES = 50 * 1024 * 1024
MAX_EDIT_VIDEO_BYTES = 200 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024


def available(stage: str = "motion") -> list[str]:
    missing = []
    if not os.environ.get("FAL_KEY", "").strip():
        missing.append("请在API设置中填写并保存 FAL_KEY。")
    try:
        installed = importlib.util.find_spec("fal_client") is not None
    except (ImportError, ValueError):
        installed = False
    if not installed:
        missing.append("缺少官方 fal-client 上传依赖，请修复软件依赖。")
    return missing


def _endpoint(option: str) -> str:
    if option not in FAL_VIDEO_ENDPOINTS:
        raise ValueError("不支持的Kling视频模型选项")
    return FAL_VIDEO_ENDPOINTS[option]


def _key() -> str:
    value = os.environ.get("FAL_KEY", "").strip()
    if not value:
        raise MotionTransientError("继续查询Kling原任务需要保存 FAL_KEY；不会重新上传或提交生成。")
    return value


def _paths(destination: Path) -> tuple[Path, Path, Path]:
    return tuple(destination.with_name(destination.stem + suffix)
                 for suffix in ("_task.json", "_submission.json", "_result.json"))


def _read(path: Path) -> dict:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return {}
        result = json.loads(path.read_text(encoding="utf-8-sig"))
        return result if isinstance(result, dict) else {}
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
        raise MotionTransientError("Kling原任务恢复记录保存失败；请检查数据目录后继续原任务，勿重新生成。") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _cancelled(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise MotionCancelledError("软件正在关闭，已暂停本地Kling查询；下次可继续原任务，不会重新提交。")


def _progress(progress, status: str, stage: str, message: str = "") -> None:
    if progress is not None:
        try:
            progress({"status": status, "stage": stage, "message": message or stage})
        except Exception:
            raise MotionTransientError("本地Kling进度保存失败；已保留原任务，可稍后继续查询。") from None


def _wait(seconds: float, stop_event) -> None:
    _cancelled(stop_event)
    if stop_event is None:
        time.sleep(seconds)
    elif stop_event.wait(seconds):
        _cancelled(stop_event)


def _probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,avg_frame_rate,r_frame_rate:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=30, **quiet_subprocess_kwargs(),
    )
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    duration = float(data["format"]["duration"])
    width, height = int(stream["width"]), int(stream["height"])
    if width <= 0 or height <= 0 or not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid video metadata")
    fps = 0.0
    for field in ("avg_frame_rate", "r_frame_rate"):
        try:
            candidate = float(Fraction(str(stream.get(field, "0/0"))))
            if math.isfinite(candidate) and candidate > 0:
                fps = candidate
                break
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            pass
    return {"width": width, "height": height, "duration": duration, "fps": fps}


def validate_motion_inputs(image: Path, clip: Path, option: str = "fal-kling-v3-pro") -> dict:
    """Check official OpenAPI limits locally before uploading or charging."""
    if option not in ENDPOINTS:
        raise ValueError("动作控制请选择Kling动作模型")
    image, clip = Path(image), Path(clip)
    try:
        if not image.is_file() or not 0 < image.stat().st_size <= MAX_IMAGE_BYTES:
            raise ValueError("Kling人物参考图不能超过10MiB，请另选合规图片。")
        with Image.open(image) as opened:
            width, height = opened.size
            opened.verify()
        if not (340 <= width <= 3850 and 340 <= height <= 3850 and 0.4 <= width / height <= 2.5):
            raise ValueError("Kling人物图宽高须在340–3850像素内，宽高比须在0.4–2.5内；请调整后再确认。")
        if not clip.is_file() or not 0 < clip.stat().st_size <= MAX_VIDEO_BYTES:
            raise ValueError("Kling参考视频不能超过100MiB。")
        try:
            video = _probe(clip)
        except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError, subprocess.SubprocessError):
            raise ValueError("Kling参考视频无法读取；尚未上传或提交生成。") from None
        if not (340 <= video["width"] <= 3850 and 340 <= video["height"] <= 3850):
            raise ValueError("Kling参考视频宽高须在340–3850像素内，请另选合规视频。")
        if not 3 <= video["duration"] <= 30.05:
            raise ValueError("Kling动作控制参考片段须为3–30秒（接口允许末帧对齐至30.05秒）。")
        return {"image_width": width, "image_height": height,
                "video_width": video["width"], "video_height": video["height"],
                "reference_duration_seconds": video["duration"]}
    except (OSError, subprocess.SubprocessError, KeyError, IndexError, TypeError, Image.DecompressionBombError):
        raise ValueError("Kling人物图或参考视频无法读取，请检查素材；尚未上传或提交生成。") from None


def _edit_prompt(role_descriptions: list[str], prompt: str) -> str:
    if not isinstance(prompt, str):
        raise ValueError("角色替换补充要求须为文字")
    mappings = []
    for index, description in enumerate(role_descriptions, 1):
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"请填写原视频角色{index}的辨认描述")
        mappings.append(f"Source character {index}: {description.strip()}. Replace only this character with the appearance of @Image{index}.")
    combined = (
        "Edit @Video1 by replacing only the following source characters. Keep each replacement bound to its described source character throughout the video.\n"
        + "\n".join(mappings)
        + "\nPreserve the source actions, timing, interactions, occlusions, spatial ordering, scene layout and camera motion. "
          "Keep the background and all unlisted people and objects as close to @Video1 as possible. "
          "Do not swap identities, add characters, or create split screens. Use each image only for its character's appearance, not its background."
        + (("\nAdditional instructions: " + prompt.strip()) if prompt.strip() else "")
    )
    if len(combined) > 2500:
        raise ValueError(f"角色描述与补充要求合成后共{len(combined)}字，超过接口2500字上限；请缩短后确认。")
    return combined


def validate_edit_inputs(references: list[Path], clip: Path, option: str = "fal-kling-o3-pro-edit", *,
                         role_descriptions: list[str], prompt: str = "") -> dict:
    """Local O3 edit validation; no resizing, upload, SDK or generation call."""
    if option not in EDIT_ENDPOINTS:
        raise ValueError("视频角色替换请选择列表中的Kling O3编辑模型")
    if not isinstance(references, (list, tuple)) or not 1 <= len(references) <= 3:
        raise ValueError("视频角色替换须提供1–3张角色参考图")
    if not isinstance(role_descriptions, (list, tuple)) or len(role_descriptions) != len(references):
        raise ValueError("每张角色图须对应一条原视频人物描述，请逐一填写")
    request_prompt = _edit_prompt(role_descriptions, prompt)
    dimensions = []
    try:
        for index, reference in enumerate(references, 1):
            path = Path(reference)
            if not path.is_file() or not 0 < path.stat().st_size <= MAX_EDIT_IMAGE_BYTES:
                raise ValueError(f"角色图{index}须为有效图片且不能超过50MiB")
            with Image.open(path) as image:
                width, height = image.size
                image.verify()
            if min(width, height) < 300 or not 0.4 <= width / height <= 2.5:
                raise ValueError(f"角色图{index}宽高须至少300像素，宽高比须在0.4–2.5内")
            dimensions.append({"width": width, "height": height})
        clip = Path(clip)
        if clip.suffix.lower() not in {".mp4", ".mov"}:
            raise ValueError("视频角色替换仅支持MP4或MOV片段")
        if not clip.is_file() or not 0 < clip.stat().st_size <= MAX_EDIT_VIDEO_BYTES:
            raise ValueError("视频角色替换片段须为有效文件且不能超过200MiB")
        try:
            video = _probe(clip)
            fps = float(video.get("fps", 0))
            duration = float(video["duration"])
        except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError, subprocess.SubprocessError):
            raise ValueError("角色替换原视频无法读取，请检查文件；尚未上传或提交生成。") from None
        if not 720 <= video["width"] <= 3840 or not 720 <= video["height"] <= 3840:
            raise ValueError("角色替换原视频宽高须在720–3840像素内；不会自动放大或裁切素材。")
        if not math.isfinite(duration) or not 3 <= duration <= 15.05:
            raise ValueError("角色替换视频片段须为3–15秒（接口允许末帧对齐至15.05秒）。")
        if not math.isfinite(fps) or not 23 <= fps <= 60:
            raise ValueError("角色替换原视频帧率须在23–60fps内，请转换后再确认。")
        return {"reference_count": len(references), "reference_dimensions": dimensions,
                "video_width": video["width"], "video_height": video["height"], "video_fps": fps,
                "reference_duration_seconds": duration, "request_prompt": request_prompt}
    except (OSError, TypeError, Image.DecompressionBombError):
        raise ValueError("角色图或原视频无法读取，请检查素材；尚未上传或提交生成。") from None


def _valid_source(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= 32 and bool(_probe(path))
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError, subprocess.SubprocessError):
        return False


def _https(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def _metadata(option: str, request_id: str = "", *, reused: bool = False) -> dict:
    endpoint = _endpoint(option)
    if option in EDIT_ENDPOINTS:
        return {"provider": "fal", "model": endpoint, "model_id": endpoint, "motion_model": endpoint,
                "option": option, "task_id": request_id, "request_id": request_id,
                "operation": "video_edit", "keep_audio": True,
                "restore_reference_audio": True, "reused_local_video": reused,
                "note": "Kling O3对原视频做角色替换，按描述和参考图保持角色对应；动作、互动、场景和镜头保留程度以生成结果为准，画面可能重绘。原片尺寸和帧率由下载文件实测。"}
    return {"provider": "fal", "model": endpoint, "model_id": endpoint, "motion_model": endpoint,
            "option": option, "task_id": request_id, "request_id": request_id,
            "character_orientation": "video", "keep_original_sound": True,
            "restore_reference_audio": True, "reused_local_video": reused,
            "note": "Kling动作控制原片尺寸和帧率以下载文件实测为准；1080×1920交付由本地导出保证，不宣称原片固定1080p。"}


def _read_task(destination: Path, option: str) -> dict:
    task_path, _, _ = _paths(destination)
    task = _read(task_path)
    identifier = task.get("request_id")
    if (task.get("provider") != "fal" or task.get("endpoint") != _endpoint(option)
            or task.get("option", option) != option or not isinstance(identifier, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier) or task.get("task_id") != identifier):
        raise MotionSubmissionUncertainError("缺少有效Kling请求编号或模型记录；请人工核对fal历史，已阻止重新提交。")
    return {"provider": "fal", "task_id": identifier, "request_id": identifier,
            "endpoint": _endpoint(option), "option": option}


def _new_upload_client():
    try:
        module = importlib.import_module("fal_client")
        # The module-level SDK singleton caches credentials. A fresh instance
        # makes settings changes take effect without restarting the program.
        return module.SyncClient(key=_key(), default_timeout=120)
    except (ImportError, AttributeError, OSError):
        raise MotionTransientError("缺少可用的官方fal上传SDK，请修复本地依赖后再提交。") from None


def _upload_inputs(image: Path, clip: Path, *, stop_event=None) -> tuple[str, str]:
    urls = _upload_files([image, clip], stop_event=stop_event)
    return urls[0], urls[1]


def _upload_files(paths: list[Path], *, stop_event=None) -> list[str]:
    client = _new_upload_client()
    try:
        urls = []
        for path in paths:
            _cancelled(stop_event)
            url = client.upload_file(path)
            _cancelled(stop_event)
            if not _https(url):
                raise ValueError("invalid upload URL")
            urls.append(url)
        return urls
    except MotionCancelledError:
        raise
    except Exception:
        raise MotionTransientError("fal素材上传未完成；尚未提交视频生成，请检查网络后再操作。") from None
    finally:
        connection = getattr(client, "__dict__", {}).get("_client")
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


class _URLUnavailable(MotionTransientError):
    pass


def _download(url: str, destination: Path, *, progress=None, stop_event=None) -> None:
    if not _https(url):
        raise MotionTransientError("Kling任务完成但视频链接无效，请稍后仅查询原任务。")
    temporary = destination.with_name(destination.name + ".download")
    try:
        _cancelled(stop_event)
        _progress(progress, "downloading", "下载已生成的Kling原片")
        with requests.get(url, stream=True, timeout=(20, 60)) as response:
            if response.status_code in {401, 403, 404, 410}:
                raise _URLUnavailable("原片地址暂不可用，将仅查询原任务取回链接。")
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
        raise MotionTransientError("Kling原片已生成，但下载或视频校验未完成；可继续原任务下载，不会重新生成。") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _terminal(destination: Path, task: dict, status: str) -> None:
    _, _, result_path = _paths(destination)
    _atomic_record(result_path, {**task, "status": status, "updated_at_utc": datetime.now(timezone.utc).isoformat()})
    label = {"failed": "已失败", "expired": "已过期", "not_found": "不存在或当前账户不可查"}.get(status, "无法继续")
    raise MotionTerminalError(f"Kling原任务{label}（{status}）；请核对fal生成历史，不会重新提交。", task_status=status)


def _query_json(task: dict, *, result: bool = False) -> dict:
    url = QUEUE_REQUESTS + task["request_id"] + ("" if result else "/status")
    try:
        response = requests.get(url, headers={"Authorization": "Key " + _key()}, timeout=(15, 60), allow_redirects=False)
    except requests.RequestException:
        raise MotionTransientError("fal查询网络暂不可用，原请求编号已保留；稍后继续原任务。") from None
    if response.status_code in {404, 410}:
        error = MotionTerminalError("Kling原任务暂不可查，请核对fal历史和当前密钥对应账户。", task_status="not_found" if response.status_code == 404 else "expired")
        raise error
    if result and response.status_code in {400, 422}:
        raise MotionTerminalError("Kling原任务返回生成错误，请核对fal历史。", task_status="failed")
    if not 200 <= response.status_code < 300:
        raise MotionTransientError(f"fal查询暂不可用（HTTP {response.status_code}）；请核对密钥、余额或网络，仅续查原任务。")
    try:
        value = response.json()
    except (ValueError, RecursionError):
        raise MotionTransientError("fal查询返回格式暂不可识别，可稍后继续原任务。") from None
    if not isinstance(value, dict) or value.get("request_id", task["request_id"]) != task["request_id"]:
        raise MotionTransientError("fal查询未返回匹配的任务记录；已保留原编号，不会重新提交。")
    return value


def _poll(destination: Path, task: dict, *, progress=None, stop_event=None) -> dict:
    _, _, result_path = _paths(destination)
    option = task["option"]
    cached = _read(result_path)
    if (cached.get("request_id") != task["request_id"] or cached.get("endpoint") != task["endpoint"]
            or cached.get("provider") != "fal"):
        cached = {}
    if isinstance(cached.get("status"), str) and cached["status"] in {"failed", "expired", "not_found"}:
        _terminal(destination, task, cached["status"])
    if _https(cached.get("video_url")):
        try:
            _download(cached["video_url"], destination, progress=progress, stop_event=stop_event)
            _progress(progress, "downloaded", "Kling原片已取回，准备本地导出")
            return _metadata(option, task["request_id"])
        except _URLUnavailable:
            pass
    failures = 0
    deadline = time.monotonic() + 45 * 60
    while time.monotonic() < deadline:
        _cancelled(stop_event)
        _progress(progress, "querying", "查询原有Kling任务")
        try:
            status = _query_json(task)
            phase = status.get("status")
            if phase == "COMPLETED":
                if status.get("error") or status.get("error_type"):
                    raise MotionTerminalError("Kling原任务生成失败。", task_status="failed")
                result = _query_json(task, result=True)
                if result.get("error") or result.get("error_type"):
                    raise MotionTerminalError("Kling原任务生成失败。", task_status="failed")
                video = result.get("video")
                url = video.get("url") if isinstance(video, dict) else None
                if not _https(url):
                    raise MotionTransientError("Kling任务已完成但未提供有效视频链接，稍后仅查询原任务。")
                _atomic_record(result_path, {**task, "status": "done", "video_url": url,
                                             "updated_at_utc": datetime.now(timezone.utc).isoformat()})
                _download(url, destination, progress=progress, stop_event=stop_event)
                _progress(progress, "downloaded", "Kling原片已取回，准备本地导出")
                return _metadata(option, task["request_id"])
            if not isinstance(phase, str) or phase not in {"IN_QUEUE", "IN_PROGRESS"}:
                raise MotionTransientError("fal任务状态暂不可识别；保留原任务稍后续查。")
            failures = 0
            _progress(progress, "processing", "Kling原任务排队中" if phase == "IN_QUEUE" else "Kling原任务正在生成")
            _wait(3, stop_event)
        except MotionTerminalError as error:
            _terminal(destination, task, error.task_status)
        except MotionTransientError:
            failures += 1
            if failures >= 3:
                raise MotionTransientError("Kling原任务查询或下载暂未完成；编号已保留，可稍后继续，不会重新提交。") from None
            _progress(progress, "retrying", "查询暂未完成，稍后继续同一Kling任务")
            _wait(min(2 ** failures, 15), stop_event)
    raise MotionTransientError("本地等待已结束，Kling原请求编号已保留；下次继续查询同一任务，不会重新生成。")


def resume_motion(destination: Path, option: str = "fal-kling-v3-pro", *, progress=None, stop_event=None) -> dict:
    """Never calls upload or queue POST, including missing/corrupt task records."""
    _endpoint(option)
    destination = Path(destination)
    _cancelled(stop_event)
    if _valid_source(destination):
        try:
            identifier = _read_task(destination, option)["request_id"]
        except MotionSubmissionUncertainError:
            identifier = ""
        _progress(progress, "downloaded", "复用已完整下载的Kling原片，继续本地导出")
        return _metadata(option, identifier, reused=True)
    return _poll(destination, _read_task(destination, option), progress=progress, stop_event=stop_event)


def motion(image: Path, clip: Path, destination: Path, option: str = "fal-kling-v3-pro", *, progress=None, stop_event=None) -> dict:
    if option not in ENDPOINTS:
        raise ValueError("动作控制请选择Kling动作模型")
    destination = Path(destination)
    task_path, submission_path, result_path = _paths(destination)
    if any(path.exists() for path in (destination, task_path, submission_path, result_path)):
        raise MotionSubmissionUncertainError("已存在Kling结果或提交记录；请继续原任务，已阻止重复生成。")
    problems = available()
    if problems:
        raise ValueError("；".join(problems))
    validate_motion_inputs(image, clip, option)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _cancelled(stop_event)
    image_url, video_url = _upload_inputs(Path(image), Path(clip), stop_event=stop_event)
    return _submit_payload(destination, option, {
        "image_url": image_url, "video_url": video_url, "character_orientation": "video", "keep_original_sound": True,
        "prompt": "One adult fashion model wears exactly the clothing shown in the reference image and follows the dance motion in the driving video. Preserve garment color, pattern, silhouette and face consistently. Full body visible.",
    }, progress=progress, stop_event=stop_event)


def video_edit(references: list[Path], clip: Path, destination: Path, option: str = "fal-kling-o3-pro-edit", *,
               role_descriptions: list[str], prompt: str = "", progress=None, stop_event=None) -> dict:
    destination = Path(destination)
    if any(path.exists() for path in (destination, *_paths(destination))):
        raise MotionSubmissionUncertainError("已有角色替换结果或提交记录；请继续原任务，已阻止重复生成。")
    validated = validate_edit_inputs(references, clip, option, role_descriptions=role_descriptions, prompt=prompt)
    problems = available("edit")
    if problems:
        raise ValueError("；".join(problems))
    destination.parent.mkdir(parents=True, exist_ok=True)
    paths = [Path(reference) for reference in references] + [Path(clip)]
    urls = _upload_files(paths, stop_event=stop_event)
    return _submit_payload(destination, option, {"prompt": validated["request_prompt"], "video_url": urls[-1],
        "image_urls": urls[:-1], "keep_audio": True, "enable_safety_checker": True},
        progress=progress, stop_event=stop_event)


def _submit_payload(destination: Path, option: str, payload: dict, *, progress=None, stop_event=None) -> dict:
    """The one billable POST shared by motion control and source-video edits."""
    endpoint = _endpoint(option)
    task_path, submission_path, result_path = _paths(destination)
    if any(path.exists() for path in (destination, task_path, submission_path, result_path)):
        raise MotionSubmissionUncertainError("已有Kling结果或提交记录；请继续原任务，已阻止重复生成。")
    _cancelled(stop_event)
    try:
        with submission_path.open("x", encoding="utf-8") as marker:
            json.dump({"provider": "fal", "endpoint": endpoint, "option": option, "status": "submitting",
                       "submitted_at_utc": datetime.now(timezone.utc).isoformat()}, marker)
            marker.flush()
            os.fsync(marker.fileno())
    except (OSError, ValueError, TypeError):
        raise MotionSubmissionUncertainError("无法保存独占Kling提交标记，已阻止提交；请核对本地和fal历史后操作。") from None
    try:
        # No Session retry adapter, SDK retry helper, redirect, or retry loop.
        response = requests.post(QUEUE_BASE + endpoint, headers={"Authorization": "Key " + _key()},
                                 json=payload,
                                 timeout=(20, 120), allow_redirects=False)
    except Exception:
        raise MotionSubmissionUncertainError("Kling提交响应未能可靠取回；独占标记已保留，请人工核对fal历史，勿重新提交。") from None
    status_code = response.status_code
    if isinstance(status_code, int) and 400 <= status_code < 500:
        advice = {
            401: "FAL_KEY无效或已失效，请检查并重新保存密钥。",
            402: "fal账户余额不足或付款状态异常，请检查余额并充值。",
            403: "FAL_KEY权限不足或模型访问被拒绝，请检查密钥权限与账户模型权限。",
            422: "素材或请求参数未通过平台校验，请核对图片、视频和模型要求。",
        }.get(status_code, "平台拒绝本次请求，请核对账户权限、余额及素材参数。")
        raise MotionSubmissionUncertainError(
            f"Kling提交被拒绝（HTTP {status_code}）：{advice}提交标记已保留；请先核对fal生成历史，勿直接重复提交。"
        )
    if not isinstance(status_code, int) or not 200 <= status_code < 300:
        status_hint = f"（HTTP {status_code}）" if isinstance(status_code, int) else ""
        raise MotionSubmissionUncertainError(
            f"Kling提交结果尚未可靠确认{status_hint}；独占标记已保留，请人工核对fal历史，勿重新提交。"
        )
    try:
        submitted = response.json()
        identifier = submitted.get("request_id") if isinstance(submitted, dict) else None
        if not (isinstance(identifier, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier)):
            raise ValueError("no reliable acceptance ID")
    except Exception:
        raise MotionSubmissionUncertainError("Kling提交响应未能可靠取回；独占标记已保留，请人工核对fal历史，勿重新提交。") from None
    task = {"provider": "fal", "task_id": identifier, "request_id": identifier, "endpoint": endpoint, "option": option}
    try:
        _atomic_record(task_path, task)
    except MotionTransientError:
        raise MotionSubmissionUncertainError(f"Kling已接受任务但本地编号保存失败；请求编号：{identifier}。请人工取回，勿重新提交。") from None
    return _poll(destination, task, progress=progress, stop_event=stop_event)
