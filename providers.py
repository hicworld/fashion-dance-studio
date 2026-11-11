"""An allowlisted catalog and two independently selectable generation stages."""

import importlib
import os
from pathlib import Path

from provider_common import download_file

DEFAULT_TRYON = "fal-fashn-v1.6"
DEFAULT_MOTION = "fal-kling-v3-pro"
DEFAULT_EDIT = "fal-kling-o3-pro-edit"
FASHN_ENDPOINT = "fal-ai/fashn/tryon/v1.6"
KLING_ENDPOINTS = {
    DEFAULT_MOTION: "fal-ai/kling-video/v3/pro/motion-control",
    "fal-kling-v3-standard": "fal-ai/kling-video/v3/standard/motion-control",
}
EDIT_ENDPOINTS = {DEFAULT_EDIT: "fal-ai/kling-video/o3/pro/video-to-video/edit"}
FAL_VIDEO_ENDPOINTS = {**KLING_ENDPOINTS, **EDIT_ENDPOINTS}
OFFICIAL_EDIT_MODELS = {
    "minimax-h3-edit": {"provider": "minimax", "model": "MiniMax-H3",
                        "endpoint": "https://api.minimax.cn/v2/video_generation",
                        "module": "providers_minimax", "min_seconds": 4, "max_seconds": 15,
                        "resolutions": ("768P", "2K"), "default_resolution": "768P"},
    "volc-seedance-2.5-edit": {"provider": "volcengine-ark", "model": "doubao-seedance-2-5-260628",
                               "endpoint": "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks",
                               "module": "providers_seedance", "min_seconds": 4, "max_seconds": 30,
                               "resolutions": ("720p", "1080p"), "default_resolution": "1080p"},
}
RECOVERABLE_VIDEO_MODELS = {"volc-dreamactor-2", *FAL_VIDEO_ENDPOINTS, *OFFICIAL_EDIT_MODELS}

CATALOG = {
    "tryon": [
        {"id": DEFAULT_TRYON, "label": "FASHN v1.6", "provider": "fal",
         "note": "专用虚拟试衣；人物图＋服装图。", "price": {"currency": "USD", "per_run": 0.075, "per_second": 0}},
        {"id": "volc-seedream", "label": "Seedream 多图换装", "provider": "火山引擎",
         "note": "通过多图编辑换装；模型版本由本地 SEEDREAM_MODEL 配置。", "price": None},
        {"id": "liblib-tryon", "label": "LiblibAI · 自选试衣工作流", "provider": "LiblibAI",
         "note": "使用你配置的云端工作流；需先确认能接收人物图＋服装图。", "price": None},
    ],
    "motion": [
        {"id": DEFAULT_MOTION, "label": "Kling 3.0 Pro · 动作控制", "provider": "fal",
         "note": "人物图＋参考舞蹈；3–30秒。原片尺寸由接口决定。", "price": {"currency": "USD", "per_run": 0, "per_second": 0.168}},
        {"id": "fal-kling-v3-standard", "label": "Kling 3.0 Standard · 动作控制", "provider": "fal",
         "note": "用于比较 Standard 与 Pro；3–30秒。原片尺寸由接口决定。", "price": None},
        {"id": "volc-dreamactor-2", "label": "即梦动作模仿 2.0", "provider": "火山引擎",
         "note": "官方输出720p、25fps，最长30秒；1080p交付会放大。需要TOS存储参考视频。", "price": {"currency": "CNY", "per_run": 0, "per_second": 0.4}},
        {"id": "liblib-motion", "label": "LiblibAI · 自选动作迁移工作流", "provider": "LiblibAI",
         "note": "例如已开放API的Wan Animate；原片尺寸、时长限制取决于你选定的工作流。", "price": None},
    ],
    "edit": [
        {"id": "minimax-h3-edit", "label": "MiniMax H3 · 双人/多角色参考", "provider": "MiniMax官方",
         "note": "原视频＋1–3张角色图，4–15秒；768P或2K。参考生成可能重绘场景，请先做5秒测试。", "price": None},
        {"id": "volc-seedance-2.5-edit", "label": "Seedance 2.5 · 视频角色编辑", "provider": "火山方舟",
         "note": "原视频＋1–3张角色图，4–30秒，支持1080p。真人参考素材须符合官方可信素材规则。", "price": None},
        {"id": DEFAULT_EDIT, "label": "Kling O3 Pro · 视频角色替换", "provider": "fal",
         "note": "原视频＋1–3张角色图；逐一描述对应人物，3–15秒。画面可能重绘，请预览确认结果。",
         "price": {"currency": "USD", "per_run": 0, "per_second": 0.168}},
    ],
}


def option_for(stage: str, option_id: str) -> dict:
    for option in CATALOG.get(stage, []):
        if option["id"] == option_id:
            return dict(option)
    raise ValueError("请选择列表中的图片、动作或视频编辑模型。")


def missing_for(stage: str, option_id: str) -> list[str]:
    option = option_for(stage, option_id)
    module_name = (OFFICIAL_EDIT_MODELS[option_id]["module"] if option_id in OFFICIAL_EDIT_MODELS else
                   "providers_fal" if option_id.startswith("fal-") else
                   "providers_jimeng" if option_id.startswith("volc-") else "providers_liblib")
    try:
        module = importlib.import_module(module_name)
        return module.available(stage)
    except (ImportError, OSError, ValueError) as exc:
        # Import failures do not make the other providers unavailable.
        return [f"{option['provider']}适配器尚未准备完成，请检查依赖和配置。"]


def edit_parameters(option: str, *, resolution: str = "", video_asset_id: str = "", role_asset_ids=None) -> dict:
    """Validate requested settings without accepting arbitrary model endpoints."""
    option_for("edit", option)
    if option not in OFFICIAL_EDIT_MODELS:
        if resolution or video_asset_id or any(role_asset_ids or []):
            raise ValueError("当前可灵编辑接口不支持分辨率或方舟可信素材ID参数")
        return {}
    spec = OFFICIAL_EDIT_MODELS[option]
    resolution = resolution or spec["default_resolution"]
    if resolution not in spec["resolutions"]:
        raise ValueError("请选择该视频模型支持的输出分辨率")
    if option == "minimax-h3-edit" and (video_asset_id or any(role_asset_ids or [])):
        raise ValueError("方舟可信素材ID仅用于Seedance模型")
    return {"resolution": resolution, "video_asset_id": video_asset_id,
            "role_asset_ids": role_asset_ids}


def edit_missing_for(option: str, *, video_asset_id: str = "", role_asset_ids=None) -> list[str]:
    missing = missing_for("edit", option)
    if option == "volc-seedance-2.5-edit" and (not video_asset_id or not role_asset_ids or not all(role_asset_ids)):
        from storage_tos import available
        missing.extend(available())
    return list(dict.fromkeys(missing))


def validate_edit_inputs(references: list[Path], clip: Path, option: str, *, role_descriptions: list[str],
                         prompt: str = "", resolution: str = "", video_asset_id: str = "", role_asset_ids=None) -> dict:
    extra = edit_parameters(option, resolution=resolution, video_asset_id=video_asset_id, role_asset_ids=role_asset_ids)
    module = importlib.import_module(OFFICIAL_EDIT_MODELS[option]["module"] if option in OFFICIAL_EDIT_MODELS else "providers_fal")
    return module.validate_edit_inputs(references, clip, option, role_descriptions=role_descriptions, prompt=prompt, **extra)


def get_catalog() -> dict:
    result = {}
    for stage, options in CATALOG.items():
        result[stage] = []
        for option in options:
            missing = missing_for(stage, option["id"])
            item = {**option, "ready": not missing, "missing": missing}
            if option["id"] == "liblib-tryon":
                try:
                    liblib = importlib.import_module("providers_liblib")
                    mode = liblib.input_mode(stage)
                    item["input_mode"] = mode
                    if mode == "garment_only":
                        item["note"] = "此模板只接收服装图，人物由平台工作流决定，未使用内置模特底图。全身构图与人物来源需检查试衣样片。"
                except (ImportError, OSError, ValueError):
                    pass
            result[stage].append(item)
    return result


def preflight(tryon_id: str, motion_id: str) -> dict:
    selected = {"tryon": option_for("tryon", tryon_id), "motion": option_for("motion", motion_id)}
    errors = []
    for stage, option in selected.items():
        errors.extend(f"{option['label']}：{message}" for message in missing_for(stage, option["id"]))
    if errors:
        raise ValueError("\n".join(errors))
    return {stage: {key: option[key] for key in ("id", "label", "provider")} for stage, option in selected.items()}


def tryon(model: Path, garment: Path, destination: Path, option: str) -> dict:
    option_for("tryon", option)
    if option != DEFAULT_TRYON:
        module = importlib.import_module("providers_jimeng" if option.startswith("volc-") else "providers_liblib")
        return module.tryon(model, garment, destination, option)
    import fal_client

    result = fal_client.subscribe(FASHN_ENDPOINT, arguments={
        "model_image": fal_client.upload_file(str(model)),
        "garment_image": fal_client.upload_file(str(garment)),
        "category": "auto", "mode": "quality", "garment_photo_type": "auto",
        "num_samples": 1, "output_format": "png",
    })
    download_file(result["images"][0]["url"], destination)
    return {"provider": "fal", "model": FASHN_ENDPOINT}


def motion(image: Path, clip: Path, destination: Path, option: str, *, progress=None, stop_event=None) -> dict:
    option_for("motion", option)
    if option not in KLING_ENDPOINTS:
        module = importlib.import_module("providers_jimeng" if option.startswith("volc-") else "providers_liblib")
        if option.startswith("volc-"):
            return module.motion(image, clip, destination, option, progress=progress, stop_event=stop_event)
        return module.motion(image, clip, destination, option)
    module = importlib.import_module("providers_fal")
    return module.motion(image, clip, destination, option, progress=progress, stop_event=stop_event)


def resume_motion(destination: Path, option: str = "volc-dreamactor-2", *, progress=None, stop_event=None) -> dict:
    """Query/download a persisted task; never dispatch a generation endpoint."""
    if option in OFFICIAL_EDIT_MODELS:
        module = importlib.import_module(OFFICIAL_EDIT_MODELS[option]["module"])
    elif option in FAL_VIDEO_ENDPOINTS:
        module = importlib.import_module("providers_fal")
    elif option == "volc-dreamactor-2":
        module = importlib.import_module("providers_jimeng")
    else:
        raise ValueError("此模型没有可恢复的已知任务通道，已阻止重新提交")
    return module.resume_motion(destination, option, progress=progress, stop_event=stop_event)


def edit_video(references: list[Path], clip: Path, destination: Path, option: str = DEFAULT_EDIT, *,
               role_descriptions: list[str], prompt: str = "", progress=None, stop_event=None,
               resolution: str = "", video_asset_id: str = "", role_asset_ids=None) -> dict:
    option_for("edit", option)
    extra = edit_parameters(option, resolution=resolution, video_asset_id=video_asset_id, role_asset_ids=role_asset_ids)
    module = importlib.import_module(OFFICIAL_EDIT_MODELS[option]["module"] if option in OFFICIAL_EDIT_MODELS else "providers_fal")
    return module.video_edit(references, clip, destination, option, role_descriptions=role_descriptions,
                             prompt=prompt, progress=progress, stop_event=stop_event, **extra)


def __getattr__(name: str):
    if name in {"MotionRecoveryError", "MotionTransientError", "MotionTerminalError",
                "MotionSubmissionUncertainError", "MotionCancelledError"}:
        return getattr(importlib.import_module("providers_jimeng"), name)
    raise AttributeError(name)


def compose(model: Path, garment: Path, background: Path, destination: Path,
            option: str = "volc-seedream", prompt: str = "", *,
            pants: Path | None = None, garment_mode: str = "outfit") -> dict:
    if option != "volc-seedream":
        raise ValueError("模特、服装和背景合成目前使用Seedream")
    module = importlib.import_module("providers_jimeng")
    return module.compose(model, garment, background, destination, option, prompt=prompt,
                          pants=pants, garment_mode=garment_mode)


def compose_background(model: Path, background: Path, destination: Path,
                       option: str = "volc-seedream", prompt: str = "") -> dict:
    if option != "volc-seedream":
        raise ValueError("已有穿搭图的背景替换目前使用Seedream")
    module = importlib.import_module("providers_jimeng")
    return module.compose_background(model, background, destination, option, prompt=prompt)
