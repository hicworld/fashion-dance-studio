"""Read-only, local usage summaries and list-price estimates, never billing claims.

No cloud client is imported here. Provider responses record usage, not settled
charges; the public summary therefore always leaves actual_amount unset.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from pathlib import Path

from PIL import Image


CHECKED_AT = "2026-10-21"
ARK_PRICING = "https://docs.volcengine.com/docs/ark/model-pricing?lang=en&redirect=1"
ARK_USAGE = "https://docs.volcengine.com/docs/ark/image-generation-api?lang=en"
MOTION_PRICING = "https://docs.volcengine.com/docs/JimengAI/ActionImitation20-ProductIntroduction?lang=zh"
MOTION_RATE = Decimal("0.4")
FAL_MOTION_MODEL = "fal-ai/kling-video/v3/pro/motion-control"
FAL_MOTION_PRICING = "https://fal.ai/models/" + FAL_MOTION_MODEL
FAL_MOTION_RATE = Decimal("0.168")
FAL_EDIT_MODEL = "fal-ai/kling-video/o3/pro/video-to-video/edit"
FAL_EDIT_PRICING = "https://fal.ai/models/" + FAL_EDIT_MODEL
FAL_EDIT_RATE = Decimal("0.168")
MINIMAX_PRICING = "https://platform.minimax.cn/docs/pricing/overview"
MINIMAX_RATES = {"768P": Decimal("0.50"), "2K": Decimal("0.80")}
SEEDANCE_RATES = {"720p": Decimal("42"), "1080p": Decimal("46")}
DISCLAIMER = (
    "按公开单价和本地记录估算，平台未返回本任务实际账单。"
    "未计入账户折扣、免费额度、结算取整规则及存储/流量等费用；实际扣费以所用平台账单为准。"
)


def safe_usage(value) -> dict:
    """Only documented numeric image-usage counters may enter public state."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for name in ("generated_images", "input_images", "output_tokens", "total_tokens"):
        number = _count(value.get(name))
        if number is not None:
            result[name] = number
    tools = value.get("tool_usage")
    searches = _count(tools.get("web_search")) if isinstance(tools, dict) else None
    if searches is not None:
        result["tool_usage"] = {"web_search": searches}
    return result


def _count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return int(value) if math.isfinite(value) and value >= 0 and int(value) == value else None
    except (ValueError, OverflowError):
        return None


def _positive(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) and value > 0 else None
    except (ValueError, OverflowError):
        return None


def _number(*values) -> float | None:
    return next((number for value in values if (number := _positive(value)) is not None), None)


def _read(folder: Path, filename: str) -> dict:
    path = folder / filename
    try:
        # Fixed filenames only, and no links out of this task's directory.
        if not path.is_file() or not path.resolve().parent.samefile(folder) or path.stat().st_size > 2 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _model(*values) -> str | None:
    for value in values:
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,200}", value):
            return value
        # Model endpoints are identifiers, never URLs. Only the namespace used
        # by this integration is accepted; queries, hosts and dot paths fail.
        if isinstance(value, str) and len(value) <= 200 and value.startswith("fal-ai/"):
            parts = value.split("/")
            if all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) and part not in {".", ".."}
                   for part in parts):
                return value
    return None


def _currency(provider, model: str | None) -> str | None:
    provider = provider if isinstance(provider, str) else None
    if provider == "fal" or (model and (model.startswith("fal-ai/") or model.startswith("fal-"))):
        return "USD"
    if provider in {"volcengine", "volcengine-ark", "minimax"} or (model and (model.startswith(("doubao-seedream-", "doubao-seedance-"))
            or model in {"MiniMax-H3", "minimax-h3-edit", "volc-seedance-2.5-edit"}
            or model in {"volc-seedream", "volc-dreamactor-2", "jimeng_dreamactor_m20_gen_video"})):
        return "CNY"
    return None


def _image_size(folder: Path) -> tuple[int | None, int | None]:
    path = folder / "model_wearing_garment.png"
    try:
        if path.is_file() and path.resolve().parent.samefile(folder):
            with Image.open(path) as image:
                return image.size
    except (OSError, ValueError, Image.DecompressionBombError):
        pass
    return None, None


def _image_summary(folder: Path, state: dict, config: dict, output: dict) -> dict:
    actual = _dict(state.get("actual_models"))
    output_models = _dict(output.get("actual_models"))
    info = (_read(folder, "image_info.json") or _dict(actual.get("image")) or _dict(actual.get("tryon"))
            or _dict(output_models.get("image")) or _dict(output_models.get("tryon")))
    private = _read(folder, "seedream_request.private.json")
    model = _model(info.get("model_id"), info.get("model"), private.get("model"))
    usage = safe_usage(private.get("usage"))
    usage.update(safe_usage(info.get("usage")))
    width, height = _image_size(folder)
    result = {"status": "unknown", "model": model, "currency": _currency(info.get("provider"), model),
              "estimated_amount": None,
              "usage": usage, "usage_source": "api_response" if usage else None,
              "reference_count": None, "output_width": width, "output_height": height,
              "pricing_basis": "图片模型或用量记录不足，无法估算。"}
    local_replace_preview = config.get("mode", state.get("mode")) == "video_replace" or (
        info.get("provider") == "local" and info.get("operation") == "video_replace"
    )
    if local_replace_preview:
        # The PNG is an extracted source frame, never evidence of a billed
        # image generation, regardless of its size or stale image metadata.
        result.update(status="not_used", model=None, currency=None, estimated_amount=0.0,
                      usage={}, usage_source=None,
                      pricing_basis="本地提取原视频首帧并确认角色映射，未调用图片生成服务；预览不是角色替换效果。")
        return result
    uploaded = info.get("provider") == "uploaded" or (
        config.get("mode", state.get("mode")) == "ready_image"
        and config.get("background_mode", state.get("background_mode", "original")) == "original"
        and not private and not info.get("provider")
    )
    if uploaded:
        result.update(status="not_used", currency=None, estimated_amount=0.0, usage={}, usage_source=None,
                      pricing_basis="直接使用已上传穿搭图，未调用图片生成服务。")
        return result
    if result["currency"] is None and (private or config.get("mode", state.get("mode")) in {"compose", "ready_image"}):
        # Current composition/background editing is direct Ark. An explicit
        # historical provider/model currency above always takes precedence.
        result["currency"] = "CNY"
    if private.get("status") == "rejected" and not width:
        result.update(status="not_generated", estimated_amount=0.0,
                      pricing_basis="图片请求被拒绝且未输出图片；官方规则为审核等原因未成功输出的图片不计费。")
        return result
    generated = usage.get("generated_images")
    if generated is None and width and (info or private.get("status") == "generated"):
        generated = 1
        result["generation_count_basis"] = "本地单张生成结果，API未返回生成张数"
    elif generated is not None:
        result["generation_count_basis"] = "API usage.generated_images"
    if generated is None:
        if (not private or private.get("status") == "submitting") and state.get("status") in {"running", "awaiting_confirmation"}:
            result.update(status="pending", pricing_basis="图片结果尚未就绪，生成费用暂不可确定。")
        return result
    result["generated_images"] = generated
    if generated == 0:
        result.update(status="not_generated", estimated_amount=0.0,
                      pricing_basis="API记录成功输出图片0张；未成功输出的图片不计费。")
        return result
    if not model:
        return result
    if re.fullmatch(r"doubao-seedream-5-0-pro(?:-[0-9]+)?", model):
        # This workflow requests one output. A lone local image cannot establish
        # every output's pixel tier for a historical multi-image request.
        if generated != 1 or not width or not height:
            result["pricing_basis"] = "Seedream 5.0 Pro按每张实际像素档计费；现有记录不足以确定全部图片档位。"
            return result
        count = usage.get("input_images")
        if count is not None:
            result["input_count_basis"] = "API usage.input_images"
        else:
            count = _count(info.get("reference_count"))
            roles = private.get("reference_roles")
            if count is None and isinstance(roles, list):
                count = len(roles)
            result["input_count_basis"] = "本地请求引用图片数（API未返回输入张数）"
        result["reference_count"] = count
        if count is None:
            result["pricing_basis"] = "缺少输入图片张数，无法完整估算Seedream 5.0 Pro输入图费用。"
            return result
        output_rate = Decimal("0.30") if width * height <= 2_610_000 else Decimal("0.60")
        input_amount = Decimal(max(count - 1, 0)) * Decimal("0.02")
        result.update(status="estimated", estimated_amount=float(output_rate + input_amount),
                      output_rate_per_image=float(output_rate), input_rate_per_extra_image=0.02,
                      output_estimated_amount=float(output_rate), input_estimated_amount=float(input_amount),
                      pricing_basis=f"方舟Seedream 5.0 Pro单图生成：{width}×{height}，输出{output_rate}元；输入首张免费，其后0.02元/张。")
        return result
    flat_rates = {"5-0-flash": "0.12", "5-0-lite": "0.22", "4-5": "0.25", "4-0": "0.20"}
    for version, rate in flat_rates.items():
        if re.fullmatch(r"doubao-seedream-" + version + r"(?:-[0-9]+)?", model):
            result.update(status="estimated", estimated_amount=float(Decimal(rate) * generated),
                          output_rate_per_image=float(rate), input_estimated_amount=0.0,
                          pricing_basis=f"方舟公开单价{rate}元/成功输出图片×{generated}张，输入图免费。")
            return result
    return result


def _motion_summary(folder: Path, state: dict, config: dict, output: dict) -> dict:
    actual = _dict(state.get("actual_models"))
    output_models = _dict(output.get("actual_models"))
    info = _read(folder, "motion_info.json") or _dict(actual.get("motion")) or _dict(output_models.get("motion"))
    model = _model(info.get("model_id"), info.get("model"), config.get("motion_model"), output.get("motion_model"))
    reference = _number(output.get("reference_duration_seconds"), config.get("reference_duration"), state.get("seconds"))
    native = _number(output.get("source_duration_seconds"), info.get("source_duration_seconds"))
    exported = _number(output.get("output_duration_seconds"))
    evidence = any((folder / name).is_file() for name in (
        "fashion_dance_source_task.json", "fashion_dance_source_submission.json",
        "fashion_dance_source_result.json", "fashion_dance_source.mp4", "fashion_dance.mp4"))
    evidence = evidence or bool(info or state.get("task_id") or state.get("task_recorded")
                                or state.get("video") or state.get("source_video"))
    result = {"status": "unknown", "model": model, "currency": _currency(info.get("provider"), model),
              "estimated_amount": None,
              "usage": {}, "usage_source": None,
              "reference_duration_seconds": reference, "source_duration_seconds": native,
              "output_duration_seconds": exported, "rate_per_second": None,
              "source_width": _count(output.get("source_width")), "source_height": _count(output.get("source_height")),
              "source_fps": _positive(output.get("source_fps")),
              "output_width": _count(output.get("output_width")), "output_height": _count(output.get("output_height")),
              "output_fps": _positive(output.get("output_fps")),
              "pricing_basis": "当前接口未返回可核对的任务账单或计费用量。"}
    if not evidence and native is None:
        image_rejected = _read(folder, "seedream_request.private.json").get("status") == "rejected"
        before_motion = state.get("status") == "awaiting_confirmation" or (
            state.get("status") == "error" and image_rejected and not state.get("image")
        ) or (state.get("status") == "running" and state.get("stage") in {
            "准备参考片段", "合成人物、服装与背景", "保留现有穿搭，更换背景"
        })
        if before_motion:
            result.update(status="not_submitted", estimated_amount=0.0, pricing_basis="当前停在图片预览或图片阶段，尚无视频生成提交记录，视频生成费用未计入。")
        else:
            result["pricing_basis"] = "缺少视频提交和原片时长记录，无法确认视频消耗；不据此视为免费。"
        return result
    if model in {"volc-dreamactor-2", "jimeng_dreamactor_m20_gen_video"}:
        result["rate_per_second"] = float(MOTION_RATE)
        if native is not None:
            result.update(status="estimated", estimated_amount=float(Decimal(str(native)) * MOTION_RATE),
                          duration_basis="local_source_metadata",
                          pricing_basis="按生成原片时长×0.4元/秒估算，包含原片中过渡画面；不按裁切后的导出时长计。接口未返回实扣金额。")
        else:
            result.update(status="pending" if state.get("status") in {"running", "interrupted"} else "unknown",
                          pricing_basis="官方按生成视频时长0.4元/秒计费；原片实际时长尚未记录，不以参考时长代替。")
    elif model in {"fal-kling-v3-pro", FAL_MOTION_MODEL, "fal-kling-o3-pro-edit", FAL_EDIT_MODEL}:
        editing = model in {"fal-kling-o3-pro-edit", FAL_EDIT_MODEL}
        rate = FAL_EDIT_RATE if editing else FAL_MOTION_RATE
        model_label = "Kling O3 Pro Video Edit" if editing else "Kling 3.0 Pro Motion Control"
        result["currency"] = "USD"
        result["rate_per_second"] = float(rate)
        if native is not None:
            result.update(status="estimated", estimated_amount=float(Decimal(str(native)) * rate),
                          duration_basis="local_source_metadata",
                          pricing_basis=f"按{model_label}生成原片时长×{rate}美元/秒估算，不按参考或裁切后的导出时长计；未返回实际账单。")
        else:
            result.update(status="pending" if state.get("status") in {"running", "interrupted"} else "unknown",
                          pricing_basis="fal公开单价为0.168美元/生成秒；原片实际时长尚未记录，不以参考时长代替，也不据失败状态认定免费。")
    elif model in {"minimax-h3-edit", "MiniMax-H3"}:
        result["currency"] = "CNY"
        counters = {key: value for key in ("total_seconds", "input_seconds", "output_seconds", "input_image_count")
                    if (value := _count(_dict(info.get("usage")).get(key))) is not None}
        result.update(usage=counters, usage_source="api_response" if counters else None)
        resolution = next((value for value in (info.get("resolution"), config.get("video_resolution"))
                           if isinstance(value, str) and value in MINIMAX_RATES), None)
        result["requested_resolution"] = resolution
        rate = MINIMAX_RATES.get(resolution)
        input_seconds = counters.get("input_seconds")
        output_seconds = counters.get("output_seconds")
        images = counters.get("input_image_count")
        # Local measurements are estimates only after a usable source exists.
        if input_seconds is None and native is not None:
            input_seconds = reference
        if output_seconds is None and native is not None:
            output_seconds = native
        if images is None and isinstance(config.get("roles"), list):
            images = len(config["roles"])
        result.update(input_duration_seconds=input_seconds, billable_output_seconds=output_seconds,
                      input_rate_per_second=float(rate) if rate else None,
                      output_rate_per_second=float(rate) if rate else None)
        if rate is not None and input_seconds is not None and output_seconds is not None and images is not None and images <= 5:
            amount = (Decimal(str(input_seconds)) + Decimal(str(output_seconds))) * rate
            result.update(status="estimated", estimated_amount=float(amount),
                          pricing_basis=f"MiniMax H3 {resolution}：参考视频输入{input_seconds:g}秒和输出{output_seconds:g}秒分别按{rate}元/秒估算，前5张参考图免费；API用量或本地原片测量不是实际账单。")
        else:
            result.update(status="pending" if state.get("status") in {"running", "interrupted"} else "unknown",
                          pricing_basis="MiniMax H3按参考视频输入和生成输出分别计费：768P各0.50元/秒，2K各0.80元/秒；输入/输出记录不足，费用待确认。")
    elif model in {"volc-seedance-2.5-edit", "doubao-seedance-2-5-260628"}:
        result["currency"] = "CNY"
        usage = _dict(info.get("usage"))
        counters = {key: value for key in ("completion_tokens", "total_tokens")
                    if (value := _count(usage.get(key))) is not None}
        result.update(usage=counters, usage_source="api_response" if counters else None)
        resolution = next((value for value in (info.get("requested_resolution"), config.get("video_resolution"))
                           if isinstance(value, str) and value in SEEDANCE_RATES), None)
        rate = SEEDANCE_RATES.get(resolution)
        tokens = counters.get("completion_tokens")
        result.update(requested_resolution=resolution, rate_per_million_tokens=float(rate) if rate else None)
        if rate is not None and tokens is not None:
            result.update(status="estimated", estimated_amount=float(Decimal(tokens) * rate / Decimal(1_000_000)),
                          pricing_basis=f"Seedance 2.5 {resolution}含输入视频，按API返回usage.completion_tokens={tokens}×{rate}元/百万token估算；非实际账单。")
        else:
            result.update(status="pending" if state.get("status") in {"running", "interrupted"} else "unknown",
                          pricing_basis="Seedance 2.5含输入视频：720p按42元/百万token、1080p按46元/百万token；等待API返回completion_tokens，不以秒数或total_tokens冒充计费用量。")
    return result


def summarize_job(folder: Path, state: dict) -> dict:
    """Enrich old and current tasks without changing any file or querying APIs."""
    folder = Path(folder)
    config = _read(folder, "job_config.json")
    output = _read(folder, "output_info.json")
    image = _image_summary(folder, state, config, output)
    motion = _motion_summary(folder, state, config, output)
    searches = _dict(image["usage"].get("tool_usage")).get("web_search", 0)
    if searches:
        image["unestimated_usage"] = ["web_search"]
        image["pricing_basis"] += " 联网搜索工具费用尚未估算，已知金额未包含该项。"
    stages = (image, motion)
    active = [item for item in stages if item["status"] != "not_used"]
    currencies = sorted({item["currency"] for item in active if item["currency"] in {"CNY", "USD"}})
    known = {}
    incomplete = set()
    for item in active:
        currency, amount = item["currency"], item["estimated_amount"]
        if currency not in currencies:
            continue
        if amount is not None:
            known[currency] = known.get(currency, Decimal(0)) + Decimal(str(amount))
        if amount is None or item.get("unestimated_usage"):
            incomplete.add(currency)
    known_totals = {currency: float(known[currency]) for currency in currencies if currency in known}
    estimated_totals = {currency: amount for currency, amount in known_totals.items() if currency not in incomplete}
    complete = all(item["estimated_amount"] is not None and not item.get("unestimated_usage")
                   and (item["currency"] in currencies or item["status"] in {"not_used", "not_submitted"})
                   for item in stages)
    mixed = len(currencies) > 1
    currency = currencies[0] if len(currencies) == 1 else None
    status = "estimated" if complete else ("partial" if known_totals else
             "pending" if any(item["status"] == "pending" for item in stages) else "unknown")
    amounts = " + ".join(f"{code} {'¥' if code == 'CNY' else '$'}{amount:.4f}".rstrip("0").rstrip(".")
                         for code, amount in known_totals.items())
    label = (f"预估生成费用 {amounts}" if complete and amounts else
             f"已知部分预估 {amounts}" if known_totals else "费用暂不可估算")
    note = DISCLAIMER + ("人民币与美元分别列示，未换汇或合并为单一金额。" if mixed else "")
    return {"currency": currency, "actual_amount": None, "actual_available": False,
            "estimated_amount": estimated_totals.get(currency) if complete and not mixed else None,
            "known_estimated_amount": known_totals.get(currency, 0.0) if not mixed else None,
            "estimated_totals": estimated_totals, "known_estimated_totals": known_totals,
            "mixed_currencies": mixed, "estimate_complete": complete, "status": status,
            "display": label + "（非实际账单）",
            "image": image, "motion": motion, "note": note,
            "sources": [{"label": "方舟模型价格", "url": ARK_PRICING},
                        {"label": "图片API用量字段", "url": ARK_USAGE},
                        {"label": "动作模仿2.0计费规则", "url": MOTION_PRICING},
                        {"label": "fal Kling 3.0 Pro Motion Control价格", "url": FAL_MOTION_PRICING},
                        {"label": "fal Kling O3 Pro Video Edit价格", "url": FAL_EDIT_PRICING},
                        {"label": "MiniMax H3输入与输出价格", "url": MINIMAX_PRICING}],
            "checked_at": CHECKED_AT}
