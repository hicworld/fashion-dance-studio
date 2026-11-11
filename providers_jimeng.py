"""Volcengine Seedream image editing and Jimeng DreamActor motion transfer.

Seedream is a general image editor, not a garment-specific try-on model.
DreamActor 2.0 produces 720p/25 fps, with 1080p delivery handled separately.
Official API references:
https://api.volcengine.com/api-docs/view?action=ImageGenerations&serviceCode=ark&version=2024-01-01
https://docs.volcengine.com/docs/JimengAI/ActionImitation20-InterfaceDocumentation?lang=zh
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageOps

from billing import safe_usage
import storage_tos
from provider_common import download_file
from runtime_paths import quiet_subprocess_kwargs


SEEDREAM_DEFAULT = "doubao-seedream-5-0-pro-260628"
ARK_GENERATIONS = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
DREAMACTOR_MODEL = "jimeng_dreamactor_m20_gen_video"
_VISUAL_LOCK = threading.Lock()  # VisualService is a mutable SDK singleton.


class MotionRecoveryError(RuntimeError):
    """A persisted motion task could not be recovered safely."""


class MotionTransientError(MotionRecoveryError):
    """Keep the original task and retry query/download later, never submit again."""


class MotionTerminalError(MotionRecoveryError):
    def __init__(self, message: str, *, task_status: str):
        super().__init__(message)
        self.task_status = task_status


class MotionSubmissionUncertainError(MotionRecoveryError):
    """No reliable task identifier: a human must check the platform first."""


class MotionCancelledError(MotionRecoveryError):
    """Local shutdown interrupted recovery; the cloud task remains unchanged."""


def _value(name: str) -> str:
    return os.environ.get(name, "").strip()


def available(stage: str) -> list[str]:
    """Return setup problems, using lazy SDK imports and no remote calls."""
    if stage == "tryon":
        return [] if _value("ARK_API_KEY") else ["请配置火山方舟 ARK_API_KEY，并开通所选 Seedream 模型"]
    if stage == "motion":
        missing = [f"请配置 {name}" for name in ("VOLC_ACCESS_KEY_ID", "VOLC_SECRET_ACCESS_KEY") if not _value(name)]
        if importlib.util.find_spec("volcengine") is None:
            missing.append("尚未安装官方火山 SDK，请运行 pip install volcengine")
        missing.extend(storage_tos.available())
        return missing
    return ["未知的生成阶段"]


def _image_base64(path: Path, *, data_uri: bool) -> str:
    """Send genuine image bytes with the correct MIME type."""
    path = Path(path)
    if not path.is_file():
        raise ValueError("图片文件不存在")
    with Image.open(path) as image:
        image.verify()
        image_format = (image.format or "").lower()
    if image_format not in {"jpeg", "png", "webp"}:
        raise ValueError("参考图请使用 JPG、PNG 或 WebP 格式")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/{image_format};base64,{encoded}" if data_uri else encoded


def _motion_image_base64(path: Path) -> str:
    """Fit DreamActor image limits before a chargeable motion request."""
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    if min(image.size) < 480:
        raise ValueError("即梦动作模仿的图片宽、高均需至少480像素；请重新生成更清晰的试衣图")
    bounds = (1920, 1080) if image.width >= image.height else (1080, 1920)
    image.thumbnail(bounds, Image.Resampling.LANCZOS)
    if min(image.size) < 480:
        raise ValueError("试衣图画幅过窄，缩小后短边不足480像素；请使用较接近竖屏或横屏的构图")
    for quality in (95, 90, 85, 75):
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality)
        data = buffer.getvalue()
        if len(data) < 4_700_000:
            return base64.b64encode(data).decode("ascii")
    raise ValueError("无法将即梦参考图压到4.7MB以内，请使用更简单的背景")


def prepare_motion_clip(clip: Path) -> Path:
    """Validate/resize locally; call this before try-on to catch bad inputs early."""
    clip = Path(clip)
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(clip)],
        check=True, capture_output=True, text=True, **quiet_subprocess_kwargs(),
    )
    info = json.loads(probed.stdout)
    streams = info.get("streams", [])
    if not streams:
        raise ValueError("舞蹈素材没有可读取的视频画面")
    width, height = int(streams[0]["width"]), int(streams[0]["height"])
    duration = float(info.get("format", {}).get("duration", 0))
    if not 0 < duration <= 30.05:
        raise ValueError("即梦动作模仿要求参考视频时长不超过30秒")
    if min(width, height) < 200:
        raise ValueError("即梦动作模仿要求视频宽、高均至少200像素")
    if max(width, height) <= 1920 and min(width, height) <= 1080:
        return clip
    bounds = (1920, 1080) if width >= height else (1080, 1920)
    factor = min(bounds[0] / width, bounds[1] / height)
    if min(width, height) * factor < 200:
        raise ValueError("舞蹈视频画幅过窄，缩小到即梦限制后短边不足200像素")
    prepared = clip.with_name(clip.stem + "_jimeng.mp4")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip),
         "-map", "0:v:0", "-map", "0:a?", "-vf",
         f"scale={bounds[0]}:{bounds[1]}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
         "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-movflags", "+faststart", str(prepared)],
        check=True, capture_output=True, text=True, **quiet_subprocess_kwargs(),
    )
    return prepared


def _safe_message(message: object) -> str:
    text = str(message)
    for name in ("ARK_API_KEY", "VOLC_ACCESS_KEY_ID", "VOLC_SECRET_ACCESS_KEY", "TOS_ACCESS_KEY_ID", "TOS_SECRET_ACCESS_KEY"):
        secret = _value(name)
        if secret:
            text = text.replace(secret, "[redacted]")
    return re.sub(r"https?://[^\s\"<>]+", "[链接已隐藏]", text)[:500]


def _check_visual(result: object, stage: str) -> dict:
    if not isinstance(result, dict):
        raise RuntimeError(f"即梦{stage}返回了无法解析的结果")
    if result.get("code") != 10000:
        code = result.get("code", "未知")
        request_id = result.get("request_id", "")
        message = _safe_message(result.get("message", "请检查能力是否开通、账户余额及素材要求"))
        raise RuntimeError(f"即梦{stage}失败，错误码 {code}：{message}；请求 ID：{request_id}")
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"即梦{stage}响应缺少 data")
    return data


def _compose_input(path: Path) -> str:
    path = Path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= 30 * 1024 * 1024:
        raise ValueError("每张参考图片须非空且不超过30MB")
    with Image.open(path) as image:
        width, height = image.size
        if min(width, height) <= 14 or width * height > 36_000_000 or not 1 / 16 <= width / height <= 16:
            raise ValueError("参考图尺寸或画幅超出图片编辑模型的范围")
    return _image_base64(path, data_uri=True)


def compose(model: Path, garment: Path, background: Path, destination: Path,
            option: str = "volc-seedream", prompt: str = "", *,
            pants: Path | None = None, garment_mode: str = "outfit") -> dict:
    """Identity, clothing references and background produce a preview only."""
    if option != "volc-seedream":
        raise ValueError("穿搭合成请选择Seedream图片编辑模型")
    if garment_mode not in {"outfit", "separate"}:
        raise ValueError("服装模式须为分开上传或完整穿搭图")
    if pants is not None and garment_mode != "separate":
        raise ValueError("完整穿搭图模式只使用一张服装参考图，请切换为分开上传后添加裤子")
    problems = available("tryon")
    if problems:
        raise ValueError("；".join(problems))
    model_id = _value("SEEDREAM_MODEL") or SEEDREAM_DEFAULT
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", model_id):
        raise ValueError("图片模型名称无效，请填写模型ID或推理接入点ID")
    if len(prompt) > 3000:
        raise ValueError("补充要求请控制在3000字以内")
    references = [Path(model), Path(garment)]
    reference_roles = ["model", "garment"]
    if pants is not None:
        references.append(Path(pants))
        reference_roles.append("pants")
    references.append(Path(background))
    reference_roles.append("background")
    encoded = [_compose_input(path) for path in references]
    input_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in references]
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = destination.with_name("seedream_request.private.json")
    clothing_prompt = (
        "图2是上衣参考，只参考其上衣的服装细节，让图1人物穿上该上衣。"
        + ("图3是裤子参考，只参考其裤子/下装细节，让同一人物同时穿上图2上衣和图3裤子，不能遗漏其中一件。"
           "上衣与裤子分别对应各自参考，不要交换两件衣物的颜色、材质和图案。"
           if pants is not None else "未提供裤子参考，保留图1原有的裤子或下装。")
        if garment_mode == "separate" else
        "图2是完整穿搭参考，参考其中可见的上衣、裤子/裙装、套装、鞋履与搭配，让图1人物穿上同一套服装。"
        "若图2只展示单件商品，只更换这一件，未展示的裤子、鞋子等搭配沿用图1，不擅自更换。"
    )
    base_prompt = (
        f"使用{len(references)}张参考图合成一张真实自然、9:16竖版的全身人物照片。"
        "图1是唯一的人物身份参考：保持其脸型、五官、发型、肤色、体型与年龄特征。"
        + clothing_prompt +
        "准确保留所选服装的颜色、材质、图案、文字、领口、袖口、纽扣、裤型、剪裁与衣摆。"
        "服装图中若有人物，只提取服装，不采用其脸、发型、体型、姿势或背景；人物身份始终以图1为准。"
        "服装参考若包含正反面拼图，正面细节用于正面，背面细节只用于背面；不要把拼图标签、商品说明或分栏放入成图。"
        + ("鞋子和未指定的配饰沿用图1。" if garment_mode == "separate" else "") +
        f"图{len(references)}是背景参考：保留场景布局、主要物体、色调和光照，将模特自然放进该场景，匹配透视、接地阴影与光照。"
        "画面中只出现这一个模特，不复制背景图中的人物。"
        "人物面向镜头，双手自然舒展，头顶、手指和鞋底完整可见；全身居中，四周留出跳舞空间，避免裁掉手脚。"
        "只输出一张完整照片，不要输出拼图、分屏或前后对比图。"
    )
    full_prompt = base_prompt + ((" 补充要求：" + prompt.strip()) if prompt.strip() else "")
    payload = {"model": model_id, "prompt": full_prompt, "image": encoded,
               "size": "1440x2560", "response_format": "url", "watermark": True}
    # Current Pro/Flash only generate a single image; the older optional group
    # and stream fields are deliberately absent from this request.
    record = None
    if checkpoint.exists():
        record = json.loads(checkpoint.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("operation", "fashion_compose") != "fashion_compose":
            raise RuntimeError("本地图片请求属于其他编辑操作，不能作为本次穿搭合成复用，已阻止重复提交")
        if (record.get("input_sha256") != input_hashes
                or record.get("garment_mode", "outfit") != garment_mode):
            raise RuntimeError("本地记录对应其他服装素材或输入模式，请先取回原请求结果，不能复用为本次穿搭")
        if not record.get("image_url"):
            raise RuntimeError("该合成请求已有提交记录；请先查看方舟请求记录，避免重复生成计费")
    elif destination.exists():
        raise RuntimeError("目标已存在合成图，已阻止重复提交")
    else:
        record = {"status": "submitting", "operation": "fashion_compose", "model": model_id, "prompt": full_prompt,
                  "submitted_at_utc": datetime.now(timezone.utc).isoformat(),
                  "input_sha256": input_hashes, "reference_roles": reference_roles,
                  "garment_mode": garment_mode}
        with checkpoint.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            response = requests.post(ARK_GENERATIONS,
                                     headers={"Authorization": f"Bearer {_value('ARK_API_KEY')}", "Content-Type": "application/json"},
                                     json=payload, timeout=(20, 300))
        except requests.RequestException:
            raise RuntimeError("图片合成请求连接失败或超时，提交结果不确定；请先查看方舟请求记录，避免重复计费") from None
        try:
            result = response.json()
        except ValueError:
            raise RuntimeError(f"图片合成返回非JSON响应（HTTP {response.status_code}），请先核对方舟请求记录") from None
        if not isinstance(result, dict):
            raise RuntimeError("图片合成响应无效，请先查看方舟请求记录")
        record["request_id"] = response.headers.get("x-request-id") or response.headers.get("x-tt-logid") or result.get("id")
        if not response.ok or result.get("error"):
            error = result.get("error") or {}
            message = error.get("message", "请检查模型开通状态、API Key和余额") if isinstance(error, dict) else error
            record.update(status="rejected", error=_safe_message(message))
            checkpoint.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            raise RuntimeError(f"Seedream图片合成失败（HTTP {response.status_code}）：{_safe_message(message)}")
        images = result.get("data") or []
        if not images or not isinstance(images[0], dict) or not images[0].get("url"):
            raise RuntimeError("Seedream没有返回图片，请查看方舟请求记录")
        record.update(status="generated", image_url=images[0]["url"], usage=result.get("usage"))
        checkpoint.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    if not destination.exists():
        temporary = destination.with_name(destination.name + ".download")
        try:
            download_file(record["image_url"], temporary)
            with Image.open(temporary) as opened:
                ImageOps.exif_transpose(opened).convert("RGB").save(destination, format="PNG")
        except Exception:
            raise RuntimeError("合成图已生成但下载失败，已保存原请求；请取回该图片，避免重复生成计费") from None
        finally:
            temporary.unlink(missing_ok=True)
    return {"provider": "volcengine", "model": model_id, "model_id": model_id,
            "request_id": record.get("request_id"), "reference_count": len(references),
            "usage": safe_usage(record.get("usage")),
            "reference_roles": reference_roles, "garment_mode": garment_mode,
            "note": "Seedream使用指定模特、服装和背景合成全身图；人物与整套服装细节请预览确认后再生成视频。"}


def _save_background_record(checkpoint: Path, record: dict) -> None:
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=checkpoint.name + ".", suffix=".tmp", dir=checkpoint.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, checkpoint)
    except (OSError, ValueError, TypeError):
        raise RuntimeError("背景图片请求已留存，但结果记录暂未保存；请先核对方舟原请求，勿重复生成") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def compose_background(model: Path, background: Path, destination: Path,
                       option: str = "volc-seedream", prompt: str = "") -> dict:
    """Edit a dressed model's scene only; never run motion or change garments."""
    if option != "volc-seedream":
        raise ValueError("背景替换请选择Seedream图片编辑模型")
    if not isinstance(prompt, str) or len(prompt) > 3000:
        raise ValueError("补充要求须为文本且不超过3000字")
    model_id = _value("SEEDREAM_MODEL") or SEEDREAM_DEFAULT
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", model_id):
        raise ValueError("图片模型名称无效，请填写模型ID或推理接入点ID")
    references = [Path(model), Path(background)]
    roles = ["dressed_model", "background"]
    encoded = [_compose_input(path) for path in references]
    input_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in references]
    base_prompt = (
        "使用两张参考图完成一次背景替换，输出一张真实自然、9:16竖版的完整人物照片。"
        "图1是已经完成穿搭的唯一人物与服装参考：保留其人物身份、脸型、五官、表情、发型、肤色、体型和年龄特征。"
        "完整保留图1现有上衣、裤子或裙装、鞋履和全部配饰，不换衣、不增减衣物，不重新设计服装。"
        "服装的颜色、材质、印花、文字、图案、剪裁、领口、袖口、纽扣、衣摆和裤型均以图1为准。"
        "保持图1人物的姿态、肢体动作、身体比例、朝向、站位、主体大小与原有全身构图；不要重新摆姿势或裁掉原有可见手脚与衣物。"
        "图2只作为场景背景参考：使用其中的环境布局、主要场景物体、色调和空间透视替换图1原背景。"
        "不要提取图2中的人物、脸、发型、身体、姿势、衣物、鞋子或配饰，不把图2人物或其服装移入结果。"
        "只调整与新场景衔接所必需的自然光影、色温、接地阴影和边缘融合，使图1穿搭模特融入图2场景。"
        "画面中只出现图1这一个模特，不额外生成多人、人物复制或倒影中的另一个人。"
        "只输出一张完整照片，不要拼图、分屏、文字说明或前后对比图。"
    )
    full_prompt = base_prompt + ((" 补充背景要求（不得改变上述人物、服装与姿态）：" + prompt.strip()) if prompt.strip() else "")
    operation = "background_replace"
    intent = {"operation": operation, "model": model_id, "prompt": full_prompt,
              "inputs": [{"role": role, "sha256": digest} for role, digest in zip(roles, input_hashes)]}
    operation_hash = hashlib.sha256(json.dumps(intent, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Share the exclusive request slot with full composition. An existing paid
    # composition in this job must never be silently replaced by another edit.
    checkpoint = destination.with_name("seedream_request.private.json")
    if checkpoint.exists():
        try:
            record = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise RuntimeError("本地已有无法读取的图片请求记录；请先核对原请求，已阻止重复提交") from None
        if (not isinstance(record, dict) or record.get("operation") != operation
                or record.get("reference_roles") != roles or record.get("input_sha256") != input_hashes
                or record.get("operation_sha256") != operation_hash):
            raise RuntimeError("本地图片请求属于其他操作、素材或要求；不能作为本次背景替换复用，已阻止重复提交")
        if not isinstance(record.get("image_url"), str) or not record["image_url"].startswith("https://"):
            raise RuntimeError("该背景图片请求已有提交记录但结果尚未可靠保存；请先核对方舟原请求，避免重复计费")
    elif destination.exists():
        raise RuntimeError("目标已存在预览图，已阻止重复提交背景替换")
    else:
        problems = available("tryon")
        if problems:
            raise ValueError("；".join(problems))
        record = {"status": "submitting", "operation": operation, "operation_sha256": operation_hash,
                  "model": model_id, "prompt": full_prompt, "input_sha256": input_hashes, "reference_roles": roles,
                  "submitted_at_utc": datetime.now(timezone.utc).isoformat()}
        try:
            with checkpoint.open("x", encoding="utf-8") as output:
                json.dump(record, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            raise RuntimeError("无法独占保存背景请求记录，已阻止重复提交；请检查本地原请求") from None
        payload = {"model": model_id, "prompt": full_prompt, "image": encoded,
                   "size": "1440x2560", "response_format": "url", "watermark": True}
        try:
            response = requests.post(ARK_GENERATIONS,
                                     headers={"Authorization": f"Bearer {_value('ARK_API_KEY')}", "Content-Type": "application/json"},
                                     json=payload, timeout=(20, 300))
        except requests.RequestException:
            raise RuntimeError("背景替换请求连接失败或超时，提交结果不确定；请先核对方舟原请求，勿重复生成") from None
        try:
            result = response.json()
        except ValueError:
            raise RuntimeError("背景替换未返回有效JSON，已保留提交记录；请先核对方舟原请求") from None
        if not isinstance(result, dict):
            raise RuntimeError("背景替换响应结构无效，已保留提交记录；请先核对方舟原请求")
        request_id = response.headers.get("x-request-id") or response.headers.get("x-tt-logid") or result.get("id")
        record["request_id"] = request_id if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,256}", request_id) else None
        if not response.ok or result.get("error"):
            error = result.get("error") or {}
            message = error.get("message", "请检查模型开通状态、API Key和余额") if isinstance(error, dict) else error
            record.update(status="rejected", error=_safe_message(message))
            _save_background_record(checkpoint, record)
            raise RuntimeError("Seedream背景替换失败：" + _safe_message(message))
        images = result.get("data")
        if (not isinstance(images, list) or not images or not isinstance(images[0], dict)
                or not isinstance(images[0].get("url"), str) or not images[0]["url"].startswith("https://")):
            raise RuntimeError("Seedream没有返回有效图片地址；已保留提交记录，请先核对方舟原请求")
        record.update(status="generated", image_url=images[0]["url"], usage=result.get("usage"))
        _save_background_record(checkpoint, record)
    valid_existing = False
    if destination.is_file():
        try:
            with Image.open(destination) as opened:
                opened.verify()
            valid_existing = True
        except (OSError, ValueError, SyntaxError):
            pass
    if not valid_existing:
        temporary = destination.with_name(destination.name + ".download")
        normalized = destination.with_name(destination.name + ".normalized")
        try:
            download_file(record["image_url"], temporary)
            with Image.open(temporary) as opened:
                ImageOps.exif_transpose(opened).convert("RGB").save(normalized, format="PNG")
            with normalized.open("r+b") as output:
                output.flush()
                os.fsync(output.fileno())
            os.replace(normalized, destination)
        except Exception:
            raise RuntimeError("背景预览图已生成但下载或校验失败；已保存原图片地址，可取回原结果，避免重复生成计费") from None
        finally:
            temporary.unlink(missing_ok=True)
            normalized.unlink(missing_ok=True)
    return {"provider": "volcengine", "model": record["model"], "model_id": record["model"],
            "request_id": record.get("request_id"), "operation": operation,
            "usage": safe_usage(record.get("usage")),
            "reference_count": 2, "reference_roles": roles,
            "note": "Seedream仅替换已有穿搭图的背景；请确认人物、服装与姿态保持后再生成视频。"}


def tryon(model: Path, garment: Path, destination: Path, option: str) -> dict:
    if option not in {"volc-seedream", "seedream", ""}:
        raise ValueError("不支持的火山试衣模型选项")
    problems = available("tryon")
    if problems:
        raise ValueError("；".join(problems))
    model_id = _value("SEEDREAM_MODEL") or SEEDREAM_DEFAULT
    payload = {
        "model": model_id,
        "prompt": (
            "以图1中的成年人物为唯一模特，让其穿上图2展示的服装。"
            "保持图1人物的脸、发型、身材比例、姿势、背景与全身构图。"
            "准确保留图2服装的颜色、印花、文字、剪裁、领口、袖长和衣摆。"
            "服装自然贴合人物，呈现合理褶皱与光影。输出一张完整清晰的全身穿搭照片。"
        ),
        "image": [_image_base64(model, data_uri=True), _image_base64(garment, data_uri=True)],
        "size": "2K",
        "response_format": "url",
        "watermark": True,
    }
    # Generation requests are not retried automatically: a retry can be billable.
    response = requests.post(
        ARK_GENERATIONS,
        headers={"Authorization": f"Bearer {_value('ARK_API_KEY')}", "Content-Type": "application/json"},
        json=payload, timeout=(20, 300),
    )
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Seedream 返回了非 JSON 响应（HTTP {response.status_code}）") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Seedream 返回了无法解析的响应结构")
    if not response.ok or result.get("error"):
        error = result.get("error", {})
        message = error.get("message", "请检查模型开通状态、密钥与账户余额") if isinstance(error, dict) else error
        raise RuntimeError(f"Seedream 请求失败（HTTP {response.status_code}）：{_safe_message(message)}")
    images = result.get("data", [])
    if not images or not isinstance(images[0], dict) or not images[0].get("url"):
        raise RuntimeError("Seedream 没有返回图片链接，请查看方舟请求记录")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".download")
    try:
        try:
            download_file(images[0]["url"], temporary)
        except Exception:
            raise RuntimeError("Seedream 图片已生成但下载失败，请先从方舟请求记录取回，避免重复生成计费") from None
        with Image.open(temporary) as image:
            image.convert("RGB").save(destination, format="PNG")
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "provider": "volcengine", "model": model_id, "model_id": model_id,
        "tryon_model": model_id,
        "note": "使用 Seedream 多图编辑生成穿搭图；通用编辑模型的服装文字和细节需要人工检查。",
    }


def _visual_call(service: object, method: str, payload: dict) -> dict:
    """Serialize calls because the official VisualService is a singleton."""
    with _VISUAL_LOCK:
        service.set_ak(_value("VOLC_ACCESS_KEY_ID"))
        service.set_sk(_value("VOLC_SECRET_ACCESS_KEY"))
        service.set_scheme("https")
        service.set_connection_timeout(20)
        service.set_socket_timeout(180)
        try:
            return getattr(service, method)(payload)
        except Exception:
            # SDK errors can echo request payloads and URLs. Do not expose those.
            if method == "cv_sync2async_get_result":
                raise MotionTransientError("即梦查询连接暂时失败，将继续查询原任务，不会重新提交") from None
            raise RuntimeError("即梦接口连接失败，请查看控制台请求记录；如提交后超时，先核对记录再重试，避免重复计费") from None


def _verify_clip_url(url: str, clip: Path) -> None:
    """Check the actual signed GET download before submitting a paid task."""
    try:
        with Path(clip).open("rb") as local_file:
            expected = local_file.read(32)
        if len(expected) != 32:
            raise ValueError("参考视频过短，无法校验")
        with requests.get(
            url, stream=True, timeout=(20, 45), headers={"Range": "bytes=0-31"},
        ) as response:
            if response.status_code not in {200, 206}:
                raise ValueError("签名下载未返回成功状态")
            prefix = bytearray()
            for chunk in response.iter_content(chunk_size=32):
                prefix.extend(chunk[:32 - len(prefix)])
                if len(prefix) == 32:
                    break
            if bytes(prefix) != expected:
                raise ValueError("签名下载与本地视频内容不一致")
    except Exception:
        # Requests errors may contain the signed URL. Keep them out of UI/logs.
        raise RuntimeError("TOS 视频签名下载校验失败，已阻止提交生成任务；请检查桶权限、签名和网络后重试") from None


def _save_motion_task(task_path: Path, task_id: object, request_id: object) -> str:
    """Persist only safe recovery identifiers immediately after acceptance."""
    identifier = str(task_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier):
        raise MotionSubmissionUncertainError("即梦已接受任务，但任务 ID 格式异常；请先检查控制台记录，勿重新提交")
    request_identifier = str(request_id) if request_id is not None else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", request_identifier):
        request_identifier = ""
    try:
        with task_path.open("x", encoding="utf-8") as task_file:
            json.dump({
                "task_id": identifier, "req_key": DREAMACTOR_MODEL,
                "request_id": request_identifier,
            }, task_file, ensure_ascii=False, indent=2)
            task_file.flush()
            os.fsync(task_file.fileno())
    except Exception:
        raise MotionSubmissionUncertainError(f"即梦任务已提交，但本地任务记录保存失败；任务 ID：{identifier}。请先从控制台取回，勿重新提交") from None
    return identifier


def _check_motion_option(option: str) -> None:
    if option not in {"volc-dreamactor-2", "jimeng_motion_2", ""}:
        raise ValueError("不支持的即梦动作模型选项")


def _motion_paths(destination: Path) -> tuple[Path, Path, Path]:
    return tuple(destination.with_name(destination.stem + suffix)
                 for suffix in ("_task.json", "_submission.json", "_result.json"))


def _motion_metadata(task_id: str = "", *, reused: bool = False) -> dict:
    return {
        "provider": "volcengine", "model": DREAMACTOR_MODEL,
        "model_id": DREAMACTOR_MODEL, "motion_model": DREAMACTOR_MODEL,
        "task_id": task_id, "native_resolution": "720p", "native_fps": 25,
        "cut_result_first_second_switch": False, "restore_reference_audio": True,
        "reused_local_video": reused,
        "note": "即梦动作模仿 2.0 原生输出为 720p、25fps；1080×1920 交付需后续等比缩放补边。参考音轨由本地后处理保留。",
    }


def _motion_terminal_message(status: str) -> str:
    label = {"failed": "已失败", "expired": "已过期", "not_found": "不存在或已过期"}[status]
    return f"即梦原任务{label}（{status}）；不会重新生成，请查看平台记录"


def _motion_progress(progress, status: str, stage: str, message: str = "") -> None:
    if progress is not None:
        try:
            progress({"status": status, "stage": stage, "message": message or stage})
        except Exception:
            raise MotionTransientError("本地恢复进度保存失败；原任务已保留，可稍后继续查询") from None


def _motion_cancelled(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise MotionCancelledError("软件正在关闭，已暂停本地查询；下次启动可继续原任务，不会重新提交")


def _motion_wait(seconds: float, stop_event) -> None:
    _motion_cancelled(stop_event)
    if stop_event is not None:
        if stop_event.wait(seconds):
            _motion_cancelled(stop_event)
    else:
        time.sleep(seconds)


def _atomic_motion_record(path: Path, record: dict) -> None:
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError):
        raise MotionTransientError("原任务的本地恢复记录暂时无法保存，请检查数据目录后续查，勿重新生成") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _read_motion_task(task_path: Path) -> dict:
    try:
        record = json.loads(task_path.read_text(encoding="utf-8-sig"))
        if not isinstance(record, dict):
            raise ValueError
        identifier = record.get("task_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier):
            raise ValueError
        if record.get("req_key") not in {None, "", DREAMACTOR_MODEL}:
            raise ValueError
        return {**record, "req_key": DREAMACTOR_MODEL}
    except (OSError, ValueError, TypeError):
        raise MotionSubmissionUncertainError("缺少有效的即梦任务 ID 或模型记录；请人工核对控制台任务，已阻止重新提交") from None


def _valid_motion_video(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 32:
            return False
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=30, **quiet_subprocess_kwargs(),
        )
        info = json.loads(result.stdout)
        streams = info.get("streams", [])
        duration = float(info.get("format", {}).get("duration", 0))
        return bool(streams and int(streams[0]["width"]) > 0 and int(streams[0]["height"]) > 0
                    and math.isfinite(duration) and duration > 0)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return False


class _MotionURLUnavailable(MotionTransientError):
    pass


def _download_motion_video(url: str, destination: Path, *, progress=None, stop_event=None) -> None:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise MotionTransientError("任务已完成但未提供有效的 HTTPS 视频地址，请稍后续查原任务")
    temporary = destination.with_name(destination.name + ".download")
    for attempt in range(3):
        _motion_cancelled(stop_event)
        _motion_progress(progress, "downloading", "下载已生成的原片")
        try:
            download_file(url, temporary)
            _motion_cancelled(stop_event)
            if not _valid_motion_video(temporary):
                raise ValueError("invalid downloaded video")
            with temporary.open("r+b") as output:
                output.flush()
                os.fsync(output.fileno())
            _motion_cancelled(stop_event)
            os.replace(temporary, destination)
            return
        except MotionCancelledError:
            raise
        except Exception as error:
            response = getattr(error, "response", None)
            if getattr(response, "status_code", None) in {401, 403, 404, 410}:
                raise _MotionURLUnavailable("原视频地址暂不可用，将仅查询原任务尝试取回，不会重新生成") from None
            if attempt == 2:
                raise MotionTransientError("原视频已生成但下载或视频校验暂未成功；任务和链接已保留，可稍后继续下载，勿重新提交") from None
            _motion_progress(progress, "retrying", "下载暂未完成，稍后继续原片下载")
            _motion_wait(2 ** (attempt + 1), stop_event)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _new_motion_service(*, validate_credentials: bool = True):
    if validate_credentials and any(not _value(name) for name in ("VOLC_ACCESS_KEY_ID", "VOLC_SECRET_ACCESS_KEY")):
        raise MotionTransientError("继续查询原任务需要已保存的火山引擎 AK/SK；不需要重新上传素材或配置 TOS")
    try:
        from volcengine.visual.VisualService import VisualService
        with _VISUAL_LOCK:
            return VisualService()
    except (ImportError, OSError):
        raise MotionTransientError("缺少可用的官方火山 SDK，暂不能查询原任务；请修复本地依赖，勿重新生成") from None


def _poll_motion(destination: Path, task: dict, *, service=None, progress=None, stop_event=None) -> dict:
    """Query/download a known ID. This function has no preparation or submission path."""
    _, _, result_path = _motion_paths(destination)
    task_id = task["task_id"]
    try:
        cached = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(cached, dict) or cached.get("task_id") != task_id or cached.get("req_key") != DREAMACTOR_MODEL:
            cached = {}
    except (OSError, ValueError, TypeError):
        cached = {}
    if isinstance(cached.get("status"), str) and cached["status"] in {"not_found", "expired", "failed"}:
        status = cached["status"]
        raise MotionTerminalError(_motion_terminal_message(status), task_status=status)
    if isinstance(cached.get("video_url"), str) and cached["video_url"].startswith("https://"):
        try:
            _download_motion_video(cached["video_url"], destination, progress=progress, stop_event=stop_event)
            _motion_progress(progress, "downloaded", "原片已取回，准备继续本地导出")
            return _motion_metadata(task_id)
        except _MotionURLUnavailable:
            pass  # A fresh query may return a usable link; it never generates a new task.
    if service is None:
        service = _new_motion_service()
    deadline = time.monotonic() + 45 * 60
    failures = 0
    while time.monotonic() < deadline:
        _motion_cancelled(stop_event)
        _motion_progress(progress, "querying", "查询原有即梦任务")
        try:
            result = _visual_call(service, "cv_sync2async_get_result", {"req_key": DREAMACTOR_MODEL, "task_id": task_id})
            if not isinstance(result, dict) or result.get("code") != 10000 or not isinstance(result.get("data"), dict):
                raise MotionTransientError("即梦查询暂未返回有效结果，可稍后继续查询原任务")
            state = result["data"]
            status = state.get("status")
            if not isinstance(status, str):
                raise MotionTransientError("即梦返回暂不可识别的任务状态；保留原任务，稍后继续查询")
            if status == "done":
                url = state.get("video_url")
                if not isinstance(url, str) or not url.startswith("https://"):
                    raise MotionTransientError("任务标记完成但未提供有效视频地址，请稍后续查原任务")
                _atomic_motion_record(result_path, {"task_id": task_id, "req_key": DREAMACTOR_MODEL,
                                                    "status": "done", "video_url": url,
                                                    "updated_at_utc": datetime.now(timezone.utc).isoformat()})
                _download_motion_video(url, destination, progress=progress, stop_event=stop_event)
                _motion_progress(progress, "downloaded", "原片已取回，准备继续本地导出")
                return _motion_metadata(task_id)
            if status in {"not_found", "expired", "failed"}:
                _atomic_motion_record(result_path, {"task_id": task_id, "req_key": DREAMACTOR_MODEL, "status": status})
                raise MotionTerminalError(_motion_terminal_message(status), task_status=status)
            if status not in {"processing", "in_queue", "generating"}:
                raise MotionTransientError("即梦返回暂不可识别的任务状态；保留原任务，稍后继续查询")
            failures = 0
            _motion_progress(progress, "processing", "原任务排队中" if status == "in_queue" else "原任务仍在生成")
            _motion_wait(5, stop_event)
        except MotionTerminalError:
            raise
        except (MotionTransientError, requests.RequestException, TimeoutError, ConnectionError, OSError):
            failures += 1
            if failures >= 6:
                raise MotionTransientError("查询或下载连续暂未成功；原任务 ID 已保留，下次可继续，绝不会自动重新提交") from None
            _motion_progress(progress, "retrying", "连接暂未恢复，正在等待续查原任务")
            _motion_wait(min(60, 5 * 2 ** (failures - 1)), stop_event)
    raise MotionTransientError("已查询45分钟，原任务可能仍在执行；保留任务 ID，稍后继续查询，不会重新生成")


def resume_motion(destination: Path, option: str = "volc-dreamactor-2", *, progress=None, stop_event=None) -> dict:
    """Recover an existing cloud task only: never prepare, upload or submit."""
    _check_motion_option(option)
    destination = Path(destination)
    _motion_cancelled(stop_event)
    task_path, _, _ = _motion_paths(destination)
    if _valid_motion_video(destination):
        _motion_cancelled(stop_event)
        try:
            task_id = _read_motion_task(task_path)["task_id"]
        except MotionSubmissionUncertainError:
            task_id = ""
        _motion_progress(progress, "downloaded", "复用已完整下载的原片，继续本地导出")
        return _motion_metadata(task_id, reused=True)
    task = _read_motion_task(task_path)
    return _poll_motion(destination, task, progress=progress, stop_event=stop_event)


def motion(image: Path, clip: Path, destination: Path, option: str, *, progress=None, stop_event=None) -> dict:
    _check_motion_option(option)
    destination = Path(destination)
    task_path, submission_path, _ = _motion_paths(destination)
    if destination.exists() or task_path.exists() or submission_path.exists():
        raise RuntimeError("目标已存在生成结果或任务记录；请先取回已有任务，已阻止重复提交")
    problems = available("motion")
    if problems:
        raise ValueError("；".join(problems))
    destination.parent.mkdir(parents=True, exist_ok=True)
    _motion_cancelled(stop_event)
    image_data = _motion_image_base64(image)
    clip = prepare_motion_clip(Path(clip))
    video_url = storage_tos.upload_file(clip)
    _verify_clip_url(video_url, clip)
    service = _new_motion_service(validate_credentials=False)
    try:
        with submission_path.open("x", encoding="utf-8") as marker:
            json.dump({"req_key": DREAMACTOR_MODEL, "status": "submitting",
                       "submitted_at_utc": datetime.now(timezone.utc).isoformat()}, marker)
            marker.flush()
            os.fsync(marker.fileno())
    except FileExistsError:
        raise MotionSubmissionUncertainError("已存在提交标记，已阻止重复提交；请先核对平台任务记录") from None
    except OSError:
        raise MotionSubmissionUncertainError("无法可靠保存提交标记，已阻止生成；请检查数据目录并人工核对") from None
    _motion_cancelled(stop_event)
    try:
        submitted = _visual_call(service, "cv_sync2async_submit_task", {
            "req_key": DREAMACTOR_MODEL, "binary_data_base64": [image_data],
            "video_url": video_url, "cut_result_first_second_switch": False,
        })
    except Exception:
        raise MotionSubmissionUncertainError("即梦提交响应未能可靠取回；已保留独占提交标记，请人工核对平台任务，勿重新提交") from None
    try:
        data = _check_visual(submitted, "提交")
    except RuntimeError:
        raise MotionSubmissionUncertainError("即梦提交未返回可恢复的成功响应；请人工核对平台记录，已阻止重新提交") from None
    task_id = data.get("task_id")
    if not task_id:
        raise MotionSubmissionUncertainError("即梦没有返回任务 ID；请人工核对控制台，已阻止重新提交")
    task_id = _save_motion_task(task_path, task_id, submitted.get("request_id"))
    return _poll_motion(destination, {"task_id": task_id, "req_key": DREAMACTOR_MODEL},
                        service=service, progress=progress, stop_event=stop_event)
