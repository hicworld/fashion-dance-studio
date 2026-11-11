"""Local fashion composition preview and explicitly confirmed dance generation."""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageOps, UnidentifiedImageError
from runtime_paths import DATA_ROOT, RESOURCE_ROOT, initialize, quiet_subprocess_kwargs


initialize()
import app_settings
ROOT = RESOURCE_ROOT
app_settings.load_settings()
import providers
import billing
import ui_preferences
import shortfilms as shortfilm_module
from provider_common import download_file, redact_error

ASSETS = ROOT / "assets"
JOBS = DATA_ROOT / "jobs"
BASE_MODEL = ASSETS / "base_model.png"
TRYON_MODEL = providers.FASHN_ENDPOINT
MOTION_MODEL = providers.KLING_ENDPOINTS[providers.DEFAULT_MOTION]
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
MAX_VIDEO_BYTES = 100 * 1024 * 1024
VALID_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".m4v"}

app = Flask(__name__, template_folder=str(RESOURCE_ROOT / "templates"), static_folder=str(RESOURCE_ROOT / "static"))
app.config["MAX_CONTENT_LENGTH"] = MAX_VIDEO_BYTES + 50 * 1024 * 1024
JOBS.mkdir(parents=True, exist_ok=True)
job_states: dict[str, dict] = {}
job_lock = threading.Lock()
worker_stop_event = threading.Event()
_claimed_jobs: set[str] = set()
_recovery_jobs: set[str] = set()
_worker_threads: dict[str, threading.Thread] = {}
shortfilm_studio = shortfilm_module.ShortFilmStudio(DATA_ROOT / 'shortfilms')
shortfilm_module.register(app, shortfilm_studio)


class _WorkerStopped(RuntimeError):
    pass


def _persist_state_locked(job_id: str) -> None:
    folder = JOBS / job_id
    if folder.is_dir():
        temporary = folder / "job_state.tmp"
        temporary.write_text(json.dumps(job_states[job_id], ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(folder / "job_state.json")


def set_state(job_id: str, **changes) -> None:
    with job_lock:
        job_states[job_id].update(changes)
        for field, alias in (("image", "imageURL"), ("video", "videoURL"), ("source_video", "sourceVideoURL"),
                             ("reference_video", "referenceVideoURL")):
            if field in changes:
                job_states[job_id][alias] = changes[field]
        _persist_state_locked(job_id)


def video_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, **quiet_subprocess_kwargs(),
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def trim_video(source: Path, destination: Path, start: float, seconds: float, *, crf: int = 23) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start),
         "-i", str(source), "-t", str(seconds), "-map", "0:v:0", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-movflags", "+faststart", str(destination)],
        check=True, capture_output=True, text=True, **quiet_subprocess_kwargs(),
    )


def extract_background_frame(clip: Path, destination: Path) -> None:
    """Atomically save the first visible frame of the already selected clip."""
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.png")
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip),
             "-map", "0:v:0", "-frames:v", "1", "-update", "1", str(temporary)],
            check=True, capture_output=True, text=True, **quiet_subprocess_kwargs(),
        )
        with Image.open(temporary) as image:
            if min(image.size) < 64 or image.width * image.height > 40_000_000:
                raise ValueError("首帧尺寸不符合图片输入要求")
            converted = ImageOps.exif_transpose(image).convert("RGB")
            converted.load()
        converted.save(temporary, format="PNG")
        temporary.replace(destination)
    except (OSError, ValueError, subprocess.SubprocessError, Image.DecompressionBombError):
        raise ValueError("无法读取所选舞蹈片段的首帧，请检查视频或调整截取范围；尚未提交图片生成。") from None
    finally:
        temporary.unlink(missing_ok=True)


def video_dimensions(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, **quiet_subprocess_kwargs(),
    )
    streams = json.loads(result.stdout).get("streams", [])
    if not streams or not streams[0].get("width") or not streams[0].get("height"):
        raise ValueError("生成文件没有可读取的视频画面")
    return int(streams[0]["width"]), int(streams[0]["height"])


def video_metrics(path: Path) -> dict:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=duration,avg_frame_rate,nb_frames:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, **quiet_subprocess_kwargs(),
    )
    data = json.loads(probe.stdout)
    stream = data["streams"][0]
    duration = stream.get("duration")
    if duration in (None, "N/A"):
        duration = data["format"]["duration"]
    rate = stream.get("avg_frame_rate", "0/1")
    return {"duration": float(duration), "fps": float(Fraction(rate)) if rate != "0/0" else 0}


def export_1080p(source: Path, destination: Path, reference_audio: Path | None = None,
                 target_duration: float | None = None, output_fps: int | None = None,
                 source_start_seconds: float = 0, *, preserve_dimensions: bool = False) -> dict:
    """Guarantee portrait delivery dimensions; preserve the unmodified API video."""
    width, height = video_dimensions(source)
    output_width, output_height = (width, height) if preserve_dimensions else (OUTPUT_WIDTH, OUTPUT_HEIGHT)
    changed = (width, height) != (output_width, output_height)
    scale = min(output_width / width, output_height / height)
    padded = width * output_height != height * output_width
    source_metrics = video_metrics(source)
    if not math.isfinite(source_start_seconds) or not 0 <= source_start_seconds < source_metrics["duration"]:
        raise ValueError("原片截取起点无效。")
    if target_duration is not None:
        if not math.isfinite(target_duration) or target_duration <= 0:
            raise ValueError("目标时长无效。")
        if source_metrics["duration"] + 0.001 < target_duration + source_start_seconds:
            raise ValueError(f"API原片只有{source_metrics['duration']:.3f}秒，短于参考的{target_duration:.3f}秒；请调整生成时长，不会循环或放慢补长。")
    if output_fps is not None and (not isinstance(output_fps, int) or output_fps < 1):
        raise ValueError("导出帧率须为正整数。")
    timing_changed = target_duration is not None or output_fps is not None or source_start_seconds > 0
    restore_audio = False
    if reference_audio is not None:
        audio_probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index",
             "-of", "json", str(reference_audio)], check=True, capture_output=True, text=True, **quiet_subprocess_kwargs(),
        )
        restore_audio = bool(json.loads(audio_probe.stdout).get("streams"))
    if changed or restore_audio or timing_changed:
        filters = (
            f"scale={output_width}:{output_height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if source_start_seconds > 0:
            command.extend(["-ss", str(source_start_seconds)])
        command.extend(["-i", str(source)])
        if restore_audio:
            command.extend(["-i", str(reference_audio)])
        command.extend(["-map", "0:v:0", "-map", "1:a:0" if restore_audio else "0:a?"])
        video_filters = [filters] if changed else []
        if output_fps is not None:
            video_filters.append(f"fps={output_fps}")
        if changed or timing_changed:
            if video_filters:
                command.extend(["-vf", ",".join(video_filters)])
            command.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p"])
        else:
            command.extend(["-c:v", "copy"])
        command.extend(["-c:a", "aac", "-b:a", "192k"])
        if restore_audio:
            command.extend(["-af", "apad", "-shortest"])
        if target_duration is not None:
            command.extend(["-t", str(target_duration)])
            if output_fps is not None:
                command.extend(["-frames:v", str(round(target_duration * output_fps))])
        command.extend(["-movflags", "+faststart", str(destination)])
        subprocess.run(command, check=True, capture_output=True, text=True, **quiet_subprocess_kwargs())
    else:
        shutil.copyfile(source, destination)
    actual = video_dimensions(destination)
    actual_metrics = video_metrics(destination)
    if actual != (output_width, output_height):
        raise ValueError(f"导出尺寸校验失败：{actual[0]}×{actual[1]}")
    if target_duration is not None and abs(actual_metrics["duration"] - target_duration) > 0.002:
        raise ValueError(f"导出时长校验失败：{actual_metrics['duration']:.3f}秒，目标{target_duration:.3f}秒。")
    upscaled = changed and scale > 1.000001
    if preserve_dimensions:
        note = f"视频角色替换沿用API原片的{width}×{height}尺寸及实际画幅，未放大或强制转为竖屏。"
    elif changed:
        note = f"API原片 {width}×{height}；已等比{'放大' if upscaled else '缩小'}"
        if padded:
            note += "并补边"
        note += "导出为1080×1920，保留完整画面。尺寸处理使用常规缩放，并非AI超分辨率。"
    else:
        note = "API返回尺寸已是1080×1920，导出保留原文件。"
        if restore_audio or timing_changed:
            note = "API返回尺寸已是1080×1920，画面保留原尺寸。"
    if restore_audio:
        note += "导出音轨使用所截取的参考视频。"
    if source_start_seconds > 0:
        note += f"已裁去原片开头{source_start_seconds:g}秒过渡画面。"
    if target_duration is not None:
        note += f"按参考视频裁切为{target_duration:g}秒。"
    if output_fps is not None and abs(source_metrics["fps"] - output_fps) > 0.001:
        note += f"帧率由{source_metrics['fps']:g}转换为{output_fps}fps，使用常规帧重采样。"
    return {
        "source_width": width, "source_height": height,
        "output_width": actual[0], "output_height": actual[1],
        "resized": changed, "upscaled": upscaled, "padded": padded,
        "reference_audio_restored": restore_audio,
        "source_duration_seconds": source_metrics["duration"], "source_fps": source_metrics["fps"],
        "output_duration_seconds": actual_metrics["duration"], "output_fps": actual_metrics["fps"],
        "target_duration_seconds": target_duration,
        "source_start_seconds": source_start_seconds,
        "note": note,
    }


def run_job(job_id: str, garment: Path, source_video: Path, start: float, seconds: float,
            tryon_id: str = providers.DEFAULT_TRYON, motion_id: str = providers.DEFAULT_MOTION) -> None:
    folder = JOBS / job_id
    try:
        selection = providers.preflight(tryon_id, motion_id)
        set_state(job_id, selection=selection)
        set_state(job_id, stage="处理舞蹈视频")
        clip = folder / "dance_clip.mp4"
        trim_video(source_video, clip, start, seconds)
        if motion_id == "volc-dreamactor-2":
            from providers_jimeng import prepare_motion_clip
            clip = prepare_motion_clip(clip)

        set_state(job_id, stage=f"生成试衣模特图 · {selection['tryon']['label']}")
        model_output = folder / "model_wearing_garment.png"
        tryon_info = providers.tryon(BASE_MODEL, garment, model_output, tryon_id)
        with Image.open(model_output) as image:
            converted = ImageOps.exif_transpose(image).convert("RGB")
        converted.save(model_output, format="PNG")
        set_state(job_id, image=f"/files/{job_id}/model_wearing_garment.png")

        set_state(job_id, stage=f"迁移舞蹈动作 · {selection['motion']['label']}")
        source_output = folder / "fashion_dance_source.mp4"
        motion_info = providers.motion(model_output, clip, source_output, motion_id)
        set_state(job_id, stage="检查原片尺寸并导出1080p", source_video=f"/files/{job_id}/fashion_dance_source.mp4")
        output_video = folder / "fashion_dance.mp4"
        exact_length = motion_info.get("duration_mapping_verified", False)
        export_info = export_1080p(source_output, output_video, clip if motion_info.get("restore_reference_audio") else None,
                                   target_duration=seconds if exact_length else None,
                                   output_fps=30 if exact_length else None)
        export_info.update({
            "selection": selection, "actual_models": {"tryon": tryon_info, "motion": motion_info},
            "motion_model": motion_info.get("model", selection["motion"]["id"]),
            "reference_start_seconds": start, "reference_duration_seconds": seconds,
            "input_sha256": {"garment": hashlib.sha256(garment.read_bytes()).hexdigest(),
                             "dance_clip": hashlib.sha256(clip.read_bytes()).hexdigest()},
        })
        (folder / "output_info.json").write_text(json.dumps(export_info, ensure_ascii=False, indent=2), encoding="utf-8")
        set_state(job_id, status="done", stage="完成 · 1080×1920", video=f"/files/{job_id}/fashion_dance.mp4",
                  source_dimensions=f"{export_info['source_width']}×{export_info['source_height']}",
                  output_dimensions="1080×1920", output_note=export_info["note"],
                  actual_models=export_info["actual_models"],
                  info=f"/files/{job_id}/output_info.json")
    except Exception as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        set_state(job_id, status="error", stage="失败", error=redact_error(detail))


@app.get("/")
def index():
    return render_template("index.html", catalog=providers.get_catalog(),
                           initial_theme=ui_preferences.load_preferences()["theme"])


@app.get("/api/catalog")
def catalog():
    return jsonify(providers.get_catalog())


def _metadata(value):
    """Keep cloud URLs, credentials and signed inputs out of public records."""
    if isinstance(value, dict):
        return {key: _metadata(item) for key, item in value.items()
                if not any(part in str(key).lower() for part in ("url", "base64", "secret", "api_key", "access_key"))}
    if isinstance(value, list):
        return [_metadata(item) for item in value]
    return redact_error(value) if isinstance(value, str) else value


def _public_job(state: dict) -> dict:
    """Add safe, local display details without changing private diagnostic state."""
    public = dict(state)
    job_id = str(state.get("id") or "")
    if re.fullmatch(r"[0-9a-f]{32}", job_id):
        public["billing"] = billing.summarize_job(JOBS / job_id, state)
        public["background_reference"] = _background_reference_url(job_id, state)
    detail = str(state.get("error") or "")
    if "Seedance提交未能可靠确认" in detail and "HTTP 400" in detail and any(
        code in detail for code in ("InputImageSensitiveContentDetected.PrivacyInformation", "InputVideoSensitiveContentDetected.PrivacyInformation")
    ):
        image_error = "InputImageSensitiveContentDetected.PrivacyInformation" in detail
        indexes = sorted(set(re.findall(r"content\[([1-3])\]", detail))) if image_error else []
        subject = "角色参考图" + "、".join(indexes) if indexes else "输入图片" if image_error else "输入视频"
        public["error"] = (
            f"方舟将{subject}识别为可能包含真人，拦截了本次素材提交。"
            "其他AI生成的写实人物也可能触发此审核；请使用方舟认可的原始产物或已审核可用的素材ID。"
            "本地没有取得任务编号，旧记录缺少完整拒绝证据，原提交标记已保留。"
            "请先核对方舟任务记录，处理素材后再新建预览并手动确认；此记录不会自动重提。"
        )
        public["resume_message"] = public["error"]
        return public
    if "MiniMax提交未确认成功" in detail and "HTTP 402" in detail:
        public["error"] = (
            "MiniMax返回HTTP 402，通常表示普通按量账户余额不足。请在MiniMax开放平台核对余额和任务记录。"
            "本次旧记录没有保存完整拒绝响应，也没有取得任务编号，原提交标记已保留。"
            "确认平台没有创建任务、余额已生效后，再新建预览并手动确认生成；此记录不会自动重提。"
        )
        public["resume_message"] = public["error"]
        return public
    normalized = " ".join(detail.casefold().split())
    known_message = "the request failed because the output image may be related to copyright restrictions."
    known_code = "outputimagesensitivecontentdetected.policyviolation"
    if "seedream" not in normalized or not (
        known_message in normalized or (known_code in normalized and "copyright" in normalized)
    ):
        return public
    message = "平台提示本次生成的图片可能涉及版权限制，已拦截输出。请更换有使用授权的人物或背景素材后，手动重新生成预览。"
    if state.get("mode") == "ready_image" and state.get("background_mode") in {"replace", "video_frame"}:
        message += "也可以选择“保留原图背景”。"
    message += "该任务不会自动重试。"
    job_id = str(state.get("id") or "")
    image_stage = state.get("status") == "error" and not state.get("image") and (
        (state.get("mode") == "ready_image" and state.get("background_mode") in {"replace", "video_frame"} and "seedream背景替换失败" in normalized)
        or (state.get("mode") == "compose" and "seedream图片合成失败" in normalized)
    )
    if image_stage and re.fullmatch(r"[0-9a-f]{32}", job_id) and not any(
        state.get(key) for key in ("task_id", "task_recorded", "source_video", "video")
    ) and not any((JOBS / job_id / filename).exists() for filename in (
        "fashion_dance_source_task.json", "fashion_dance_source_submission.json", "fashion_dance_source_result.json",
        "fashion_dance_source.mp4", "fashion_dance.mp4",
    )):
        message += "本次视频生成尚未提交。"
    request_id = re.search(r"\brequest\s*id\s*:\s*([A-Za-z0-9_-]{1,256})", detail, re.IGNORECASE)
    if request_id:
        message += "请求编号：" + request_id.group(1)
    public["error"] = message
    return public


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _job_error(job_id: str, error: Exception) -> None:
    detail = error.stderr if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
    changes = {"status": "error", "stage": "失败", "error": redact_error(detail)}
    task_path = JOBS / job_id / "fashion_dance_source_task.json"
    if task_path.is_file():
        changes["task_recorded"] = True
        try:
            saved = json.loads(task_path.read_text(encoding="utf-8"))
            if saved.get("task_id"):
                changes["task_id"] = redact_error(str(saved["task_id"]))
        except (OSError, ValueError, AttributeError):
            pass
    set_state(job_id, **changes)


def _output_fps(duration: float) -> int:
    for fps in (25, 30):
        if abs(round(duration * fps) / fps - duration) <= 0.002:
            return fps
    return 30


def _read_job_config(job_id: str) -> dict:
    return json.loads((JOBS / job_id / "job_config.json").read_text(encoding="utf-8"))


def _reference_clip_path(job_id: str) -> Path | None:
    """Resolve only fixed local reference filenames, never arbitrary config paths."""
    allowed = ("dance_clip_jimeng.mp4", "dance_clip.mp4")
    folder = JOBS / job_id
    try:
        if not folder.is_dir() or not folder.resolve().parent.samefile(JOBS):
            return None
        configured = None
        config_path = folder / "job_config.json"
        if config_path.is_file():
            try:
                configured = json.loads(config_path.read_text(encoding="utf-8")).get("clip_file")
            except (OSError, ValueError, AttributeError, RecursionError):
                pass
        if configured is not None and (not isinstance(configured, str) or configured not in allowed):
            return None
        candidates = ([configured] if configured else []) + list(allowed)
        for name in candidates:
            clip = folder / name
            if clip.is_file() and clip.resolve().parent.samefile(folder):
                return clip
    except OSError:
        return None
    return None


def _refresh_reference_state(job_id: str, state: dict) -> None:
    url = f"/files/{job_id}/reference_clip.mp4" if _reference_clip_path(job_id) else None
    state.update(reference_video=url, referenceVideoURL=url)


def _background_reference_path(job_id: str, state: dict) -> Path | None:
    """Only a video-frame job may expose this single validated local artifact."""
    if state.get("background_mode") != "video_frame":
        return None
    folder = JOBS / job_id
    frame = folder / "reference_background.png"
    try:
        if (folder.is_dir() and folder.resolve().parent.samefile(JOBS)
                and frame.is_file() and frame.resolve().parent.samefile(folder)):
            return frame
    except OSError:
        pass
    return None


def _background_reference_url(job_id: str, state: dict) -> str | None:
    return f"/files/{job_id}/reference_background.png" if _background_reference_path(job_id, state) else None


def run_compose_job(job_id: str) -> None:
    folder = JOBS / job_id
    try:
        config = _read_job_config(job_id)
        clip = folder / "dance_clip.mp4"
        set_state(job_id, stage="准备参考片段")
        trim_video(folder / config["video_file"], clip, config["start"], config["seconds"])
        background_mode = config.get("background_mode", "replace" if config["mode"] == "compose" else "original")
        background = folder / "background.png"
        if background_mode == "video_frame":
            set_state(job_id, stage="提取所选舞蹈片段首帧作为场景参考")
            background = folder / "reference_background.png"
            extract_background_frame(clip, background)
            set_state(job_id, background_reference=f"/files/{job_id}/reference_background.png")
        if config["motion_model"] == "volc-dreamactor-2":
            from providers_jimeng import prepare_motion_clip
            clip = prepare_motion_clip(clip)
        reference_duration = video_metrics(clip)["duration"]
        fps = _output_fps(reference_duration)
        export_duration = round(reference_duration * fps) / fps
        config.update(clip_file=clip.name, reference_duration=reference_duration,
                      export_duration=export_duration, output_fps=fps)
        _write_json(folder / "job_config.json", config)
        set_state(job_id, reference_video=f"/files/{job_id}/reference_clip.mp4")
        model_output = folder / "model_wearing_garment.png"
        if config["mode"] == "compose":
            set_state(job_id, stage="合成人物、服装与背景")
            garment_mode = config.get("garment_mode", "outfit")
            pants = folder / config["pants_file"] if garment_mode == "separate" and config.get("pants_file") else None
            image_info = providers.compose(
                folder / "model.png", folder / "garment.png", background,
                model_output, config["image_model"], prompt=config.get("prompt", ""),
                pants=pants, garment_mode=garment_mode,
            )
            with Image.open(model_output) as image:
                converted = ImageOps.exif_transpose(image).convert("RGB")
            converted.save(model_output, format="PNG")
        elif background_mode in {"replace", "video_frame"}:
            set_state(job_id, stage="保留现有穿搭，更换背景")
            image_info = providers.compose_background(
                folder / "model.png", background, model_output,
                config["image_model"], prompt=config.get("prompt", ""),
            )
            with Image.open(model_output) as image:
                converted = ImageOps.exif_transpose(image).convert("RGB")
            converted.save(model_output, format="PNG")
        else:
            image_info = {"provider": "uploaded", "model": "ready_image", "note": "保留上传的人物穿搭和原背景，未调用图像生成服务。"}
        if background_mode == "video_frame":
            background_note = (f"背景仅用所选舞蹈片段首帧作场景参考，不逐帧保留原视频动态背景；"
                               f"参考片段从原视频{config['start']:g}秒开始。")
            image_info = {**image_info, "background_mode": "video_frame",
                          "background_reference_start_seconds": config["start"],
                          "background_reference_note": background_note,
                          "note": str(image_info.get("note") or "") + background_note}
        image_info = _metadata(image_info)
        _write_json(folder / "image_info.json", image_info)
        set_state(job_id, status="awaiting_confirmation", stage="预览合成图，确认后生成视频",
                  image=f"/files/{job_id}/model_wearing_garment.png", seconds=reference_duration,
                  actual_models={"image": image_info, "tryon": image_info}, error=None)
    except Exception as exc:
        _job_error(job_id, exc)


def _role_paths(job_id: str, config: dict) -> list[Path]:
    folder = JOBS / job_id
    roles = config.get("roles")
    if not isinstance(roles, list) or not 1 <= len(roles) <= 3:
        raise ValueError("角色配置须包含1–3个连续编号的角色")
    result = []
    for index, role in enumerate(roles, 1):
        name = f"role_{index}.png"
        if (not isinstance(role, dict) or role.get("index") != index or role.get("file") != name
                or not isinstance(role.get("description"), str) or not role["description"].strip()
                or len(role["description"]) > 200):
            raise ValueError("角色配置或对应关系无效，请检查本地任务文件")
        path = folder / name
        if not path.is_file() or not path.resolve().parent.samefile(folder):
            raise ValueError("角色参考图缺失，请检查本地任务文件")
        result.append(path)
    return result


def _edit_kwargs(config: dict) -> dict:
    roles = config.get("roles") or []
    return providers.edit_parameters(config["motion_model"], resolution=config.get("video_resolution", ""),
                                     video_asset_id=config.get("video_asset_id", ""),
                                     role_asset_ids=[role.get("asset_id", "") for role in roles])


def run_video_replace_preview(job_id: str) -> None:
    """Prepare source-frame and role references locally; never submit generation."""
    folder = JOBS / job_id
    try:
        config = _read_job_config(job_id)
        clip = folder / "dance_clip.mp4"
        set_state(job_id, stage="本地准备角色替换片段")
        trim_video(folder / config["video_file"], clip, config["start"], config["seconds"], crf=18)
        providers.validate_edit_inputs(_role_paths(job_id, config), clip, config["motion_model"],
                                       role_descriptions=[role["description"] for role in config["roles"]],
                                       prompt=config.get("edit_prompt", ""), **_edit_kwargs(config))
        reference_duration = video_metrics(clip)["duration"]
        extract_background_frame(clip, folder / "model_wearing_garment.png")
        config.update(clip_file=clip.name, reference_duration=reference_duration,
                      export_duration=reference_duration, output_fps=None)
        _write_json(folder / "job_config.json", config)
        image_info = {"provider": "local", "model": "video_reference_frame", "operation": "video_replace",
                      "note": "仅本地提取原片首帧用于核对，不是角色替换效果；未调用图片生成接口。"}
        _write_json(folder / "image_info.json", image_info)
        set_state(job_id, status="awaiting_confirmation", stage="核对原片首帧与角色对应关系",
                  image=f"/files/{job_id}/model_wearing_garment.png", seconds=reference_duration,
                  reference_video=f"/files/{job_id}/reference_clip.mp4",
                  actual_models={"image": image_info, "tryon": image_info}, error=None)
    except Exception as exc:
        _job_error(job_id, exc)


def _read_dict(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


def _saved_task_id(job_id: str) -> str | None:
    saved = _read_dict(JOBS / job_id / "fashion_dance_source_task.json") or {}
    value = saved.get("task_id")
    task_id = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    config = _read_dict(JOBS / job_id / "job_config.json") or {}
    option = config.get("motion_model")
    if not isinstance(option, str):
        return None
    if option in providers.FAL_VIDEO_ENDPOINTS:
        if (saved.get("provider") != "fal" or saved.get("endpoint") != providers.FAL_VIDEO_ENDPOINTS[option]
                or saved.get("option") not in (None, option)
                or saved.get("request_id") != task_id):
            return None
    elif option in providers.OFFICIAL_EDIT_MODELS:
        spec = providers.OFFICIAL_EDIT_MODELS[option]
        if (saved.get("provider") != spec["provider"] or saved.get("endpoint") != spec["endpoint"]
                or saved.get("option") != option or saved.get("model") != spec["model"]
                or saved.get("resolution", spec["default_resolution"]) != config.get("video_resolution", spec["default_resolution"])):
            return None
    elif saved.get("provider") == "fal" or saved.get("req_key") not in (None, "", "jimeng_dreamactor_m20_gen_video"):
        return None
    return task_id if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", task_id) else None


def _valid_local_source(job_id: str) -> bool:
    source = JOBS / job_id / "fashion_dance_source.mp4"
    if not source.is_file():
        return False
    try:
        width, height = video_dimensions(source)
        duration = video_metrics(source)["duration"]
        return width > 0 and height > 0 and math.isfinite(duration) and duration > 0
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError):
        return False


def _recovery_plan(job_id: str, state: dict) -> dict:
    task_id = _saved_task_id(job_id)
    local = _valid_local_source(job_id)
    plan = {"task_id": task_id, "local": local, "kind": None,
            "safe_to_close": bool(task_id or local)}
    config = _read_dict(JOBS / job_id / "job_config.json")
    if config and config.get("motion_model") == "minimax-h3-edit":
        from providers_minimax import read_submission_rejection
        rejection = read_submission_rejection(JOBS / job_id / "fashion_dance_source.mp4")
        if rejection:
            status = rejection["http_status"]
            remedy = "请检查MiniMax普通按量账户余额。" if status == 402 else "请按平台提示处理账户、素材或额度问题。"
            plan.update(safe_to_close=True, rejection=rejection,
                        message=f"MiniMax明确拒绝本次提交（HTTP {status}），未创建视频任务。" + remedy
                        + "处理后可新建预览并手动确认生成；此记录不会自动重提。")
            return plan
    elif config and config.get("motion_model") == "volc-seedance-2.5-edit":
        from providers_seedance import read_submission_rejection
        rejection = read_submission_rejection(JOBS / job_id / "fashion_dance_source.mp4")
        if rejection:
            subject = "角色参考图" if rejection["error_code"].startswith("InputImage") else "原视频"
            plan.update(safe_to_close=True, rejection=rejection,
                        message=f"Seedance明确拒绝本次提交（HTTP 400），{subject}被识别为可能包含真人，未创建视频任务。"
                        "请使用方舟认可的原始产物或已审核可用的素材ID。处理后可新建预览并手动确认；此记录不会自动重提。")
            return plan
    if not config or _reference_clip_path(job_id) is None:
        plan["message"] = "本地参考片段或任务配置缺失；已保留任务记录，请先核对文件，未重新提交生成。"
        return plan
    if local:
        plan.update(kind="local", message="已找到本地原片，继续导出；无需重新调用生成接口。")
    elif task_id and config.get("motion_model") in providers.RECOVERABLE_VIDEO_MODELS:
        plan.update(kind="query", message="已找到保存的云端任务，继续查询并取回结果，不会重新提交生成。")
    else:
        plan["message"] = "没有可恢复的原片或有效任务ID；如曾提交生成，请先核对平台记录，勿重复提交。"
    return plan


def _apply_recovery_plan(job_id: str, state: dict, plan: dict) -> None:
    if plan.get("rejection"):
        state.update(status="error", stage="平台拒绝提交，未创建视频任务", can_resume=False,
                     is_recovering=False, safe_to_close=True, task_recorded=False,
                     recovery_status="failed", task_status="rejected", error=plan["message"],
                     resume_message=plan["message"])
        state.pop("task_id", None)
        return
    state.update(safe_to_close=plan["safe_to_close"], task_recorded=bool(
        (JOBS / job_id / "fashion_dance_source_task.json").is_file()))
    if plan["task_id"]:
        state["task_id"] = plan["task_id"]
    else:
        state.pop("task_id", None)
    state.update(can_resume=bool(plan["kind"]), resume_message=plan["message"],
                 error=None if plan["kind"] else state.get("error") or plan["message"],
                 is_recovering=bool(plan["kind"]), recovery_status="pending" if plan["kind"] else "needs_attention",
                 status="running" if plan["kind"] else "interrupted",
                 stage="等待恢复已有任务" if plan["kind"] else "任务已中断，请核对记录")


def _progress_callback(job_id: str):
    def progress(value: dict) -> None:
        if not isinstance(value, dict):
            return
        status = value.get("status")
        stages = {"querying": "继续查询云端任务", "processing": "云端排队或生成中",
                  "retrying": "网络暂不可用，继续查询同一任务", "downloading": "下载已生成原片",
                  "downloaded": "原片已保存到本地"}
        if status not in stages:
            return
        message = redact_error(str(value.get("message") or stages[status]))
        task_id = _saved_task_id(job_id)
        changes = {"stage": redact_error(str(value.get("stage") or stages[status])), "recovery_status": status,
                   "resume_message": message, "can_resume": False}
        if task_id:
            changes.update(task_id=task_id, task_recorded=True, safe_to_close=True)
        set_state(job_id, **changes)
    return progress


def _check_worker_stop() -> None:
    if worker_stop_event.is_set():
        raise _WorkerStopped("软件已停止等待，下次打开将继续恢复已有任务。")


def _finish_motion_job(job_id: str, motion_info: dict) -> None:
    folder = JOBS / job_id
    config = _read_job_config(job_id)
    clip = _reference_clip_path(job_id)
    if clip is None:
        raise ValueError("参考片段缺失，已保留生成原片；请恢复参考片段后继续本地导出。")
    source_output = folder / "fashion_dance_source.mp4"
    motion_info = _metadata(motion_info)
    _write_json(folder / "motion_info.json", motion_info)
    if not _valid_local_source(job_id):
        raise ValueError("原片尚未完整下载或无法读取；已保留任务记录，可继续取回同一任务。")
    _check_worker_stop()
    video_replace = config.get("mode") == "video_replace"
    set_state(job_id, stage="本地保存角色替换结果" if video_replace else "本地导出1080p", recovery_status="exporting", can_resume=False,
              safe_to_close=True, source_video=f"/files/{job_id}/fashion_dance_source.mp4",
              resume_message="原片已保存，正在本地导出；中断后可继续，无需重新生成。")
    target = config.get("export_duration", config["reference_duration"])
    source_length = video_metrics(source_output)["duration"]
    source_start = 0
    if config["motion_model"] == "volc-dreamactor-2":
        if source_length + 0.001 >= target + 1:
            source_start = 1
        elif abs(source_length - target) > 0.002:
            raise ValueError("即梦原片长度不足以裁去开头1秒过渡并保留完整参考动作；已保留原片，未循环或拉伸补长。")
    temporary = folder / f"fashion_dance_export_{uuid.uuid4().hex}.mp4"
    try:
        if video_replace:
            export_info = export_1080p(source_output, temporary, clip, preserve_dimensions=True)
        else:
            export_info = export_1080p(source_output, temporary, clip, target_duration=target,
                                      output_fps=config["output_fps"], source_start_seconds=source_start)
        _check_worker_stop()
        temporary.replace(folder / "fashion_dance.mp4")
    finally:
        temporary.unlink(missing_ok=True)
    saved_models = job_states[job_id].get("actual_models") or {}
    image_info = _read_dict(folder / "image_info.json") or (saved_models.get("image") if isinstance(saved_models, dict) else None) or {
        "provider": "local", "model": "saved_preview", "note": "使用已保存的人物预览图。"}
    export_info.update({
        "selection": job_states[job_id].get("selection"),
        "actual_models": {"image": image_info, "tryon": image_info, "motion": motion_info},
        "reference_start_seconds": config["start"], "reference_duration_seconds": config["reference_duration"],
        "duration_alignment_seconds": target - config["reference_duration"],
        "motion_model": motion_info.get("model", config["motion_model"]),
    })
    if video_replace:
        export_info.update(mode="video_replace", edit_prompt=config.get("edit_prompt", ""),
                           video_resolution=config.get("video_resolution", ""),
                           roles=[{"index": role["index"], "description": role["description"]}
                                  for role in config["roles"]])
    if abs(target - config["reference_duration"]) > 0.002:
        export_info["note"] += "末尾已按输出帧率对齐至最近一帧，未循环或变速补长。"
    _write_json(folder / "output_info.json", _metadata(export_info))
    dimensions = (f"{export_info['output_width']}×{export_info['output_height']}" if video_replace else "1080×1920")
    set_state(job_id, status="done", stage=f"完成 · {dimensions}", video=f"/files/{job_id}/fashion_dance.mp4",
              source_dimensions=f"{export_info['source_width']}×{export_info['source_height']}",
              output_dimensions=dimensions, output_note=export_info["note"],
              actual_models=export_info["actual_models"], info=f"/files/{job_id}/output_info.json", error=None,
              can_resume=False, is_recovering=False, recovery_status="complete", safe_to_close=True,
              resume_message="结果已保存到本地，重新打开仍可查看。")


def _motion_worker_error(job_id: str, error: Exception) -> None:
    import providers_jimeng
    plan = _recovery_plan(job_id, job_states[job_id])
    cancelled = isinstance(error, (_WorkerStopped, getattr(providers_jimeng, "MotionCancelledError", _WorkerStopped)))
    terminal_type = getattr(providers_jimeng, "MotionTerminalError", ())
    uncertain_type = getattr(providers_jimeng, "MotionSubmissionUncertainError", ())
    message = redact_error(str(error))
    if cancelled and plan["kind"]:
        set_state(job_id, status="running", stage="已暂停，下次打开继续", can_resume=True, is_recovering=True,
                  recovery_status="pending", resume_message="已保留已有任务，下次打开继续查询或本地导出。",
                  safe_to_close=plan["safe_to_close"], error=None)
    elif isinstance(error, terminal_type):
        rejected = getattr(error, "task_status", "") == "rejected" and bool(plan.get("rejection"))
        set_state(job_id, status="error", stage="平台拒绝提交，未创建视频任务" if rejected else "云端任务已结束，无法自动取回", can_resume=False, is_recovering=False,
                  recovery_status="failed", resume_message=message, safe_to_close=plan["safe_to_close"], error=message,
                  task_status=getattr(error, "task_status", "failed"))
    elif isinstance(error, uncertain_type) or not plan["kind"]:
        set_state(job_id, status="interrupted", stage="任务需人工核对", can_resume=False, is_recovering=False,
                  recovery_status="needs_attention", resume_message=message or plan["message"],
                  safe_to_close=plan["safe_to_close"], error=message or plan["message"])
    else:
        set_state(job_id, status="interrupted", stage="恢复暂未完成，可继续取回", can_resume=True, is_recovering=False,
                  recovery_status="retryable", resume_message=message, safe_to_close=plan["safe_to_close"], error=message)


def _release_worker(job_id: str) -> None:
    with job_lock:
        _claimed_jobs.discard(job_id)
        _recovery_jobs.discard(job_id)
        _worker_threads.pop(job_id, None)


def run_motion_job(job_id: str) -> None:
    folder = JOBS / job_id
    with job_lock:
        _claimed_jobs.add(job_id)
    try:
        _check_worker_stop()
        config = _read_job_config(job_id)
        clip = _reference_clip_path(job_id)
        if clip is None:
            raise ValueError("参考片段缺失或配置无效，尚未提交生成。")
        source_output = folder / "fashion_dance_source.mp4"
        set_state(job_id, stage="迁移舞蹈动作，等待云端生成")
        kwargs = {}
        if config["motion_model"] in providers.RECOVERABLE_VIDEO_MODELS:
            kwargs = {"progress": _progress_callback(job_id), "stop_event": worker_stop_event}
        if config.get("mode") == "video_replace":
            motion_info = providers.edit_video(_role_paths(job_id, config), clip, source_output, config["motion_model"],
                                               role_descriptions=[role["description"] for role in config["roles"]],
                                               prompt=config.get("edit_prompt", ""), **_edit_kwargs(config), **kwargs)
        else:
            motion_info = providers.motion(folder / "model_wearing_garment.png", clip, source_output, config["motion_model"], **kwargs)
        _finish_motion_job(job_id, motion_info)
    except Exception as exc:
        _motion_worker_error(job_id, exc)
    finally:
        _release_worker(job_id)


def run_recovery_job(job_id: str) -> None:
    try:
        _check_worker_stop()
        config = _read_job_config(job_id)
        plan = _recovery_plan(job_id, job_states[job_id])
        if not plan["kind"]:
            raise ValueError(plan["message"])
        local_info = _read_dict(JOBS / job_id / "motion_info.json") if plan["local"] else None
        if local_info is None:
            motion_info = providers.resume_motion(JOBS / job_id / "fashion_dance_source.mp4", config["motion_model"],
                                                  progress=_progress_callback(job_id), stop_event=worker_stop_event)
        else:
            motion_info = local_info
        _finish_motion_job(job_id, motion_info)
    except Exception as exc:
        _motion_worker_error(job_id, exc)
    finally:
        _release_worker(job_id)


def _launch_recovery(job_id: str) -> tuple[dict, int]:
    with job_lock:
        state = job_states.get(job_id)
        if not state:
            return {"error": "任务不存在"}, 404
        if worker_stop_event.is_set():
            return {"error": "软件正在关闭，请重新打开后恢复"}, 503
        if job_id in _claimed_jobs or state.get("status") == "done":
            return {"error": "任务已在处理或已完成，不会重复恢复", "state": state}, 409
        if state.get("status") == "running" and not state.get("is_recovering") and not state.get("can_resume"):
            return {"error": "当前任务仍在运行，不能另启恢复线程"}, 409
        if state.get("status") == "awaiting_confirmation" and not _saved_task_id(job_id) and not _valid_local_source(job_id):
            return {"error": "请先确认预览图；恢复接口不会提交新的生成任务"}, 409
        if state.get("recovery_status") == "failed":
            return {"error": "平台任务已结束，不能自动恢复；不会重新提交生成"}, 409
        plan = _recovery_plan(job_id, state)
        _apply_recovery_plan(job_id, state, plan)
        if not plan["kind"]:
            _persist_state_locked(job_id)
            return {"error": plan["message"], "state": state}, 400
        _claimed_jobs.add(job_id)
        _recovery_jobs.add(job_id)
        state.update(can_resume=False, is_recovering=True, recovery_status="querying" if plan["kind"] == "query" else "exporting")
        _persist_state_locked(job_id)
        thread = threading.Thread(target=run_recovery_job, args=(job_id,), daemon=True)
        _worker_threads[job_id] = thread
    try:
        thread.start()
    except Exception as error:
        _motion_worker_error(job_id, error)
        _release_worker(job_id)
        return {"error": "恢复线程未能启动，可稍后继续取回"}, 500
    return {"id": job_id, "state": state}, 202


def start_recovery_workers() -> int:
    """Explicit startup hook: imports and GET requests never start cloud calls."""
    worker_stop_event.clear()
    with job_lock:
        candidates = [job_id for job_id, state in job_states.items()
                      if state.get("can_resume") and state.get("recovery_status") in {"pending", "retryable"}]
    return sum(_launch_recovery(job_id)[1] == 202 for job_id in candidates)


def stop_recovery_workers() -> None:
    """Persist recoverable work and stop polling; never cancel a cloud task."""
    worker_stop_event.set()
    shortfilm_studio.shutdown()
    with job_lock:
        for job_id in list(_claimed_jobs):
            state = job_states.get(job_id)
            if state and state.get("status") == "running" and (_saved_task_id(job_id) or state.get("safe_to_close")):
                state.update(status="running", can_resume=True, is_recovering=True, recovery_status="pending",
                             resume_message="任务记录已保存，下次打开将继续查询或本地导出。", safe_to_close=True)
                _persist_state_locked(job_id)


def _has_active_job_locked() -> bool:
    return any(state.get("status") == "running" and not state.get("is_recovering") for state in job_states.values())


def _selection(image_id: str, motion_id: str) -> dict:
    selected = {}
    for stage, option_id in (("tryon", image_id), ("motion", motion_id)):
        option = providers.option_for(stage, option_id)
        selected[stage] = {key: option[key] for key in ("id", "label", "provider")}
    selected["image"] = dict(selected["tryon"])
    return selected


@app.before_request
def local_writes_only():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or not request.path.startswith("/api/"):
        return None
    local_names = {"localhost", "127.0.0.1", "::1"}
    if urlsplit(request.host_url).hostname not in local_names or request.remote_addr not in {None, "127.0.0.1", "::1"}:
        return jsonify(error="写入操作仅允许从本机页面发起"), 403
    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")
    expected = urlsplit(request.host_url)
    candidate = urlsplit(origin or referer or request.host_url)
    if (candidate.scheme, candidate.netloc) != (expected.scheme, expected.netloc) or request.headers.get("Sec-Fetch-Site") == "cross-site":
        return jsonify(error="已拒绝非同源写入请求，请从本机软件页面操作"), 403
    return None


@app.get("/api/settings")
def get_settings():
    return jsonify(app_settings.public_settings())


@app.get("/api/preferences")
def get_preferences():
    return jsonify(ui_preferences.load_preferences())


@app.put("/api/preferences")
def update_preferences():
    try:
        if request.content_length and request.content_length > 2048:
            raise ValueError("外观和创作偏好请求过大")
        return jsonify(ui_preferences.save_preferences(request.get_json(silent=True)))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except OSError:
        return jsonify(error="偏好保存失败，请检查本地数据目录的写入权限"), 500


@app.errorhandler(413)
def upload_too_large(error):
    return jsonify(error="上传内容过大：视频不能超过100MB，全部素材合计不能超过150MB"), 413


@app.put("/api/settings")
def update_settings():
    try:
        with job_lock:
            if _has_active_job_locked() or _claimed_jobs or shortfilm_studio.active():
                return jsonify(error="生成任务正在运行，请待任务结束后修改API配置"), 409
            settings = app_settings.save_settings(request.get_json(silent=True))
        return jsonify(settings)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except OSError:
        return jsonify(error="本地配置保存失败，请检查数据目录写入权限"), 500


def _save_uploaded_image(upload, destination: Path, label: str) -> None:
    try:
        with Image.open(upload.stream) as image:
            if image.width * image.height > 40_000_000 or min(image.size) < 64:
                raise ValueError("图片尺寸应至少64像素且不超过4000万像素")
            ImageOps.exif_transpose(image).convert("RGB").save(destination, format="PNG")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ValueError(f"{label}无法读取或尺寸不符合要求，请使用有效的JPG、PNG或WebP图片") from None


def create_video_replace_job():
    """Upload and preview locally. Confirmation is the only billable entry."""
    option_id = request.form.get("edit_model", "fal-kling-o3-pro-edit")
    try:
        option = providers.option_for("edit", option_id)
        spec = providers.OFFICIAL_EDIT_MODELS.get(option_id, {"min_seconds": 3, "max_seconds": 15})
        minimum, maximum = spec["min_seconds"], spec["max_seconds"]
        roles = []
        for index in range(1, 4):
            uploaded = request.files.get(f"role_{index}")
            if uploaded and uploaded.filename:
                if index != len(roles) + 1:
                    raise ValueError("请按顺序上传角色参考图，角色1为必需")
                description = request.form.get(f"role_description_{index}", "").strip()
                if not description or len(description) > 200 or "\0" in description:
                    raise ValueError(f"请填写角色{index}对应的原片人物描述（1–200字）")
                asset_id = request.form.get(f"role_asset_id_{index}", "").strip()
                if len(asset_id) > 300:
                    raise ValueError("可信素材ID过长")
                roles.append({"index": index, "description": description, "file": f"role_{index}.png", "asset_id": asset_id})
        if not roles:
            raise ValueError("请上传至少一张角色参考图，并描述要替换原片中的谁")
        if any(name.startswith("role_") and name not in {"role_1", "role_2", "role_3"} for name in request.files):
            raise ValueError("本模式最多支持3个角色参考图")
        video = request.files.get("video")
        if not video or not video.filename:
            raise ValueError("请上传用于角色替换的原视频")
        suffix = Path(video.filename).suffix.lower()
        if suffix not in VALID_VIDEO_SUFFIXES:
            raise ValueError("视频仅支持MP4、MOV、WEBM、M4V")
        start = float(request.form.get("start", "0"))
        seconds = float(request.form.get("seconds", "5"))
        duration_mode = request.form.get("duration_mode", "clip")
        if (duration_mode not in {"clip", "full"} or not math.isfinite(start) or start < 0
                or not math.isfinite(seconds) or (duration_mode == "clip" and not minimum <= seconds <= maximum)):
            raise ValueError(f"所选模型的角色替换片段须为{minimum}–{maximum}秒，开始时间须大于等于0")
        video_asset_id = request.form.get("video_asset_id", "").strip()
        if len(video_asset_id) > 300:
            raise ValueError("可信视频素材ID过长")
        edit_extra = providers.edit_parameters(option_id, resolution=request.form.get("video_resolution", ""),
                                              video_asset_id=video_asset_id,
                                              role_asset_ids=[role["asset_id"] for role in roles])
        if option_id == "volc-seedance-2.5-edit":
            from providers_seedance import normalize_asset_id
            video_asset_id = normalize_asset_id(video_asset_id)
            for role in roles:
                role["asset_id"] = normalize_asset_id(role["asset_id"])
        prompt = request.form.get("edit_prompt", "").strip()
        if len(prompt) > 1000 or "\0" in prompt:
            raise ValueError("角色替换补充要求不能超过1000字")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    with job_lock:
        if _has_active_job_locked():
            return jsonify(error="已有生成任务正在运行，请等待完成"), 409
    job_id = uuid.uuid4().hex
    folder = JOBS / job_id
    folder.mkdir(parents=True)
    try:
        for role in roles:
            _save_uploaded_image(request.files[f"role_{role['index']}"], folder / role["file"], f"角色{role['index']}参考图")
        video_path = folder / ("input" + suffix)
        video.save(video_path)
        if not 0 < video_path.stat().st_size <= MAX_VIDEO_BYTES:
            raise ValueError("原视频应为有效文件且不能超过100MB")
        try:
            total = video_duration(video_path)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            raise ValueError("原视频无法读取，请使用常见的视频格式") from None
        if not math.isfinite(total) or total <= 0 or start >= total:
            raise ValueError("开始位置超出视频时长")
        if duration_mode == "full":
            seconds = total - start
        tolerance = .05 if option_id == "fal-kling-o3-pro-edit" else .002
        if not minimum <= seconds <= maximum + tolerance:
            raise ValueError(f"所选模型每条支持{minimum}–{maximum}秒；请截取片段后生成")
        if option_id == "minimax-h3-edit" and abs(seconds - round(seconds)) > .002:
            raise ValueError("H3生成时长须为整秒，请选择5秒等整数片段后再试")
        if start + seconds > total + .01:
            raise ValueError(f"片段超出视频时长（约{total:.2f}秒）")
        selected = {key: option[key] for key in ("id", "label", "provider")}
        local_image = {"id": "local-reference-frame", "label": "原片首帧", "provider": "本地"}
        selection = {"motion": selected, "edit": dict(selected), "image": local_image, "tryon": dict(local_image)}
        now = datetime.now(timezone.utc)
        state = {
            "id": job_id, "status": "running", "stage": "本地准备角色配置预览", "mode": "video_replace",
            "created_at": now.isoformat(), "createdAt": int(now.timestamp() * 1000),
            "seconds": seconds, "start": start, "duration_mode": duration_mode, "background_mode": "original",
            "image": None, "imageURL": None, "video": None, "videoURL": None,
            "source_video": None, "sourceVideoURL": None, "reference_video": None, "referenceVideoURL": None,
            "info": None, "error": None, "selection": selection, "edit_prompt": prompt,
            "video_resolution": edit_extra.get("resolution", ""), "video_asset_id": video_asset_id,
            "roles": [{"index": role["index"], "description": role["description"],
                       "image": f"/files/{job_id}/{role['file']}", "asset_id": role["asset_id"]} for role in roles],
            "can_resume": False, "recovery_status": "none", "resume_message": "",
            "is_recovering": False, "safe_to_close": False,
        }
        _write_json(folder / "job_config.json", {
            "mode": "video_replace", "image_model": "local-reference-frame", "motion_model": option_id,
            "background_mode": "original", "roles": roles, "edit_prompt": prompt,
            "video_resolution": edit_extra.get("resolution", ""), "video_asset_id": video_asset_id,
            "start": start, "seconds": seconds, "video_file": video_path.name,
        })
        with job_lock:
            if _has_active_job_locked():
                raise ValueError("已有生成任务正在运行，请等待完成")
            job_states[job_id] = state
            _persist_state_locked(job_id)
    except (OSError, ValueError) as exc:
        if folder.resolve().parent == JOBS.resolve():
            shutil.rmtree(folder, ignore_errors=True)
        return jsonify(error=redact_error(str(exc))), 400
    threading.Thread(target=run_video_replace_preview, args=(job_id,), daemon=True).start()
    return jsonify(id=job_id, state=_public_job(state)), 202


@app.post("/api/jobs")
def create_job():
    mode = request.form.get("mode", "compose")
    if mode == "video_replace":
        return create_video_replace_job()
    image_id = request.form.get("image_model", request.form.get("tryon_model", "volc-seedream"))
    motion_id = request.form.get("motion_model", "volc-dreamactor-2")
    if mode not in {"compose", "ready_image"}:
        return jsonify(error="不支持的任务模式"), 400
    background_mode = request.form.get("background_mode", "replace" if mode == "compose" else "original")
    if background_mode not in {"original", "replace", "video_frame"}:
        return jsonify(error="背景模式须为保留原背景、上传背景图或使用舞蹈片段首帧"), 400
    if mode == "compose" and background_mode == "original":
        return jsonify(error="服装合成请选择上传背景图或使用舞蹈片段首帧"), 400
    if mode == "ready_image" and background_mode == "replace" and (
        not request.files.get("background") or not request.files["background"].filename
    ):
        return jsonify(error="更换背景时请上传背景图片"), 400
    garment_mode = request.form.get("garment_mode", "outfit") if mode == "compose" else "outfit"
    if garment_mode not in {"separate", "outfit"}:
        return jsonify(error="服装模式须为上衣与可选裤子，或整套参考图"), 400
    try:
        selection = _selection(image_id, motion_id)
        if mode == "compose" or background_mode in {"replace", "video_frame"}:
            if image_id != "volc-seedream":
                raise ValueError("人物、服装或背景合成目前使用Seedream")
            missing = providers.missing_for("tryon", image_id)
            if missing:
                raise ValueError("；".join(missing))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    required = (("model", "garment", "background", "video") if mode == "compose" and background_mode == "replace" else
                ("model", "garment", "video") if mode == "compose" else
                ("model", "background", "video") if background_mode == "replace" else ("model", "video"))
    if any(not request.files.get(name) or not request.files[name].filename for name in required):
        message = ("请上传模特图、服装图、背景图和舞蹈视频" if mode == "compose" and background_mode == "replace" else
                   "请上传模特图、服装图和舞蹈视频" if mode == "compose" else
                   "请上传人物穿搭图、背景图和舞蹈视频" if background_mode == "replace" else "请上传人物成图和舞蹈视频")
        return jsonify(error=message), 400
    video = request.files["video"]
    suffix = Path(video.filename).suffix.lower()
    if suffix not in VALID_VIDEO_SUFFIXES:
        return jsonify(error="视频仅支持MP4、MOV、WEBM、M4V"), 400
    try:
        start = float(request.form.get("start", "0"))
        seconds = float(request.form.get("seconds", "5"))
    except ValueError:
        return jsonify(error="截取时间须为数字"), 400
    duration_mode = request.form.get("duration_mode", "clip")
    if duration_mode not in {"clip", "full"} or not math.isfinite(start) or start < 0 or not math.isfinite(seconds):
        return jsonify(error="截取参数无效"), 400
    minimum_seconds = 3 if motion_id in providers.KLING_ENDPOINTS else 1
    if duration_mode == "clip" and not minimum_seconds <= seconds <= 30:
        return jsonify(error=f"所选动作模型的片段长度须为{minimum_seconds}–30秒"), 400
    prompt = request.form.get("prompt", "").strip()
    if len(prompt) > 3000:
        return jsonify(error="补充要求不能超过3000字"), 400
    with job_lock:
        if _has_active_job_locked():
            return jsonify(error="已有生成任务正在运行，请等待完成"), 409
    job_id = uuid.uuid4().hex
    folder = JOBS / job_id
    folder.mkdir(parents=True)
    pants_upload = request.files.get("pants") if mode == "compose" and garment_mode == "separate" else None
    pants_file = "pants.png" if pants_upload and pants_upload.filename else None
    try:
        for name, label in (("model", "模特图"), ("garment", "服装图"), ("background", "背景图")):
            if name in required:
                keep_uploaded = name == "model" and mode == "ready_image" and background_mode == "original"
                destination = folder / ("model_wearing_garment.png" if keep_uploaded else name + ".png")
                _save_uploaded_image(request.files[name], destination, label)
        if pants_file:
            _save_uploaded_image(pants_upload, folder / pants_file, "裤子图")
        video_path = folder / ("input" + suffix)
        video.save(video_path)
        if not 0 < video_path.stat().st_size <= MAX_VIDEO_BYTES:
            raise ValueError("舞蹈视频应为有效文件且不能超过100MB")
        try:
            total = video_duration(video_path)
        except (subprocess.CalledProcessError, ValueError, KeyError, OSError):
            raise ValueError("舞蹈视频无法读取，请使用常见的视频格式") from None
        if not math.isfinite(total) or total <= 0 or start >= total:
            raise ValueError("开始位置超出视频时长")
        if duration_mode == "full":
            seconds = total - start
        if not minimum_seconds <= seconds <= 30.05:
            raise ValueError(f"当前动作模型每条支持{minimum_seconds}–30秒；请截取片段后生成")
        if start + seconds > total + 0.01:
            raise ValueError(f"片段超出视频时长（约{total:.2f}秒）")
        now = datetime.now(timezone.utc)
        state = {
            "id": job_id, "status": "running", "stage": "准备素材", "mode": mode,
            "garment_mode": garment_mode, "background_mode": background_mode,
            "created_at": now.isoformat(), "createdAt": int(now.timestamp() * 1000),
            "seconds": seconds, "start": start, "duration_mode": duration_mode,
            "image": None, "imageURL": None, "video": None, "videoURL": None,
            "source_video": None, "sourceVideoURL": None, "info": None, "error": None, "selection": selection,
            "reference_video": None, "referenceVideoURL": None,
            "background_reference": None,
            "can_resume": False, "recovery_status": "none", "resume_message": "",
            "is_recovering": False, "safe_to_close": False,
        }
        _write_json(folder / "job_config.json", {
            "mode": mode, "image_model": image_id, "motion_model": motion_id,
            "garment_mode": garment_mode, "pants_file": pants_file, "background_mode": background_mode,
            "start": start, "seconds": seconds, "video_file": video_path.name, "prompt": prompt,
        })
        with job_lock:
            if _has_active_job_locked():
                raise ValueError("已有生成任务正在运行，请等待完成")
            job_states[job_id] = state
            _persist_state_locked(job_id)
    except (ValueError, OSError) as exc:
        if folder.resolve().parent == JOBS.resolve():
            shutil.rmtree(folder, ignore_errors=True)
        return jsonify(error=redact_error(str(exc))), 400
    threading.Thread(target=run_compose_job, args=(job_id,), daemon=True).start()
    return jsonify(id=job_id, state=_public_job(state)), 202


@app.post("/api/jobs/<job_id>/confirm")
def confirm_job(job_id: str):
    with job_lock:
        state = job_states.get(job_id)
        if not state:
            return jsonify(error="任务不存在"), 404
        if state.get("status") != "awaiting_confirmation":
            return jsonify(error="任务当前不能确认；已生成或已提交的任务不会重复提交", state=_public_job(state)), 409
        if _has_active_job_locked():
            return jsonify(error="已有生成任务正在运行，请等待完成"), 409
        try:
            config = _read_job_config(job_id)
            edit = config.get("mode") == "video_replace"
            missing = providers.edit_missing_for(config["motion_model"], video_asset_id=config.get("video_asset_id", ""),
                                                 role_asset_ids=[role.get("asset_id", "") for role in config.get("roles", [])]) if edit else providers.missing_for("motion", config["motion_model"])
            if missing:
                return jsonify(error="；".join(missing)), 400
        except (OSError, ValueError, KeyError):
            return jsonify(error="任务配置不可读取，请检查本地数据目录"), 400
        clip = _reference_clip_path(job_id)
        if not (JOBS / job_id / "model_wearing_garment.png").is_file() or clip is None:
            return jsonify(error="预览图片或参考片段已缺失，请检查本地任务文件"), 400
        if any((JOBS / job_id / name).exists() for name in (
            "fashion_dance_source_task.json", "fashion_dance_source_submission.json", "fashion_dance_source_result.json", "fashion_dance_source.mp4"
        )):
            return jsonify(error="已有云端任务记录或原片，请先取回已有结果；已阻止重复提交"), 409
        if config.get("mode") == "video_replace":
            try:
                providers.validate_edit_inputs(_role_paths(job_id, config), clip,
                                               config["motion_model"], role_descriptions=[role["description"] for role in config["roles"]],
                                               prompt=config.get("edit_prompt", ""), **_edit_kwargs(config))
            except (OSError, ValueError) as exc:
                return jsonify(error=redact_error(str(exc))), 400
        elif config["motion_model"] in providers.KLING_ENDPOINTS:
            try:
                from providers_fal import validate_motion_inputs
                validate_motion_inputs(JOBS / job_id / "model_wearing_garment.png", clip, config["motion_model"])
            except (OSError, ValueError) as exc:
                return jsonify(error=redact_error(str(exc))), 400
        state.update(status="running", stage="已确认，准备替换视频角色" if edit else "已确认，准备迁移舞蹈动作", error=None,
                     can_resume=False, is_recovering=False, recovery_status="submitting", safe_to_close=False,
                     resume_message="任务ID尚未保存，请暂时保持软件开启。")
        _claimed_jobs.add(job_id)
        _persist_state_locked(job_id)
        thread = threading.Thread(target=run_motion_job, args=(job_id,), daemon=True)
        _worker_threads[job_id] = thread
    try:
        thread.start()
    except Exception as error:
        _motion_worker_error(job_id, error)
        _release_worker(job_id)
        return jsonify(error="任务线程未能启动，尚未自动重提生成"), 500
    return jsonify(id=job_id, state=_public_job(state)), 202


@app.post("/api/jobs/<job_id>/resume")
def resume_job(job_id: str):
    result, code = _launch_recovery(job_id)
    if isinstance(result.get("state"), dict):
        result = {**result, "state": _public_job(result["state"])}
    return jsonify(result), code


@app.get("/api/jobs")
def list_jobs():
    with job_lock:
        for job_id, state in job_states.items():
            _refresh_reference_state(job_id, state)
        states = [_public_job(state) for state in sorted(job_states.values(), key=lambda state: state.get("createdAt", 0), reverse=True)]
        return jsonify(jobs=states)


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    with job_lock:
        state = job_states.get(job_id)
        if state:
            _refresh_reference_state(job_id, state)
        return (jsonify(_public_job(state)), 200) if state else (jsonify(error="任务不存在"), 404)


@app.get("/files/<job_id>/<name>")
def result_file(job_id: str, name: str):
    if job_id not in job_states:
        return jsonify(error="文件不存在"), 404
    if name == "reference_clip.mp4":
        clip = _reference_clip_path(job_id)
        if clip is None:
            return jsonify(error="参考舞蹈片段不存在"), 404
        return send_from_directory(clip.parent, clip.name, as_attachment=False, conditional=True, mimetype="video/mp4")
    if name == "reference_background.png":
        frame = _background_reference_path(job_id, job_states[job_id])
        if frame is None:
            return jsonify(error="首帧背景参考图不存在"), 404
        return send_from_directory(frame.parent, frame.name, as_attachment=False, conditional=True, mimetype="image/png")
    if name in {"role_1.png", "role_2.png", "role_3.png"}:
        config = _read_dict(JOBS / job_id / "job_config.json") or {}
        try:
            if config.get("mode") != "video_replace":
                raise ValueError("mode")
            paths = _role_paths(job_id, config)
            path = next((path for path in paths if path.name == name), None)
            if path is None:
                raise ValueError("role")
        except (OSError, ValueError):
            return jsonify(error="角色参考图不存在"), 404
        return send_from_directory(path.parent, path.name, as_attachment=False, conditional=True, mimetype="image/png")
    if name not in {"model_wearing_garment.png", "fashion_dance.mp4", "fashion_dance_source.mp4", "output_info.json"}:
        return jsonify(error="文件不存在"), 404
    return send_from_directory(JOBS / job_id, name, as_attachment=False, conditional=True)


def restore_job_history() -> None:
    for path in JOBS.glob("*/job_state.json"):
        if len(path.parent.name) != 32 or any(c not in "0123456789abcdef" for c in path.parent.name):
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                continue
            state = _metadata(state)
            state["id"] = path.parent.name
            created_ms = state.get("createdAt")
            if not isinstance(created_ms, (int, float)) or not math.isfinite(created_ms) or not 0 <= created_ms < 253402300800000:
                created_ms = int(path.stat().st_mtime * 1000)
            state["createdAt"] = int(created_ms)
            state.setdefault("created_at", datetime.fromtimestamp(created_ms / 1000, timezone.utc).isoformat())
            state.setdefault("mode", "legacy")
            state.setdefault("garment_mode", "outfit")
            config = _read_dict(path.parent / "job_config.json") or {}
            state.setdefault("background_mode", config.get("background_mode", "replace" if state["mode"] == "compose" else "original"))
            state["background_reference"] = _background_reference_url(path.parent.name, state)
            for field in ("seconds", "start", "image", "video", "source_video", "info", "error", "selection"):
                state.setdefault(field, None)
            if state["seconds"] is None and state.get("status") == "done":
                # Older successful samples predate the upload form's timing fields.
                # Recover their measured duration instead of displaying zero seconds.
                try:
                    output_info = json.loads((path.parent / "output_info.json").read_text(encoding="utf-8"))
                    for key in ("reference_duration_seconds", "target_duration_seconds", "output_duration_seconds"):
                        seconds = output_info.get(key)
                        if isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds > 0:
                            state["seconds"] = seconds
                            break
                except (OSError, ValueError, AttributeError):
                    pass
            for field, alias in (("image", "imageURL"), ("video", "videoURL"), ("source_video", "sourceVideoURL")):
                state[alias] = state[field]
            _refresh_reference_state(path.parent.name, state)
            for key, value in {"can_resume": False, "recovery_status": "none", "resume_message": "",
                               "safe_to_close": False, "is_recovering": False}.items():
                state.setdefault(key, value)
            if state.get("status") == "done":
                state.update(can_resume=False, is_recovering=False, safe_to_close=True, recovery_status="complete")
            elif state.get("recovery_status") != "failed" and (
                state.get("status") == "running"
                or (state.get("status") in {"awaiting_confirmation", "interrupted", "error"} and any(
                    (path.parent / name).exists() for name in (
                        "fashion_dance_source_task.json", "fashion_dance_source_submission.json", "fashion_dance_source.mp4")))
            ):
                plan = _recovery_plan(path.parent.name, state)
                _apply_recovery_plan(path.parent.name, state, plan)
            job_states[path.parent.name] = state
        except (OSError, ValueError, AttributeError):
            continue


restore_job_history()


if __name__ == "__main__":
    start_recovery_workers()
    try:
        app.run(host="127.0.0.1", port=7860, debug=False, threaded=True)
    finally:
        stop_recovery_workers()
