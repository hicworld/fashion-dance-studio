"""Local allowlisted settings; secrets are never included in API responses."""

from __future__ import annotations

import io
import os
import tempfile
import threading
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from dotenv.parser import parse_stream

from runtime_paths import DATA_ROOT


ENV_PATH = DATA_ROOT / ".env"
SECRET_KEYS = {"ARK_API_KEY", "VOLC_ACCESS_KEY_ID", "VOLC_SECRET_ACCESS_KEY", "FAL_KEY", "MINIMAX_API_KEY", "ELEVENLABS_API_KEY"}
VALUE_KEYS = {"TOS_BUCKET", "TOS_ENDPOINT", "TOS_REGION", "SEEDREAM_MODEL"}
ALLOWED_KEYS = SECRET_KEYS | VALUE_KEYS
_SETTINGS_LOCK = threading.Lock()


def load_settings() -> None:
    with _SETTINGS_LOCK:
        # Saved desktop settings must survive a launcher's blank or stale environment.
        # Other dotenv keys retain their legacy environment-first behavior.
        saved = dotenv_values(ENV_PATH, encoding="utf-8-sig", interpolate=False)
        os.environ.update({key: value for key, value in saved.items()
                           if key in ALLOWED_KEYS and value is not None})
        load_dotenv(ENV_PATH, override=False, encoding="utf-8-sig")


def public_settings() -> dict:
    import providers
    from providers_jimeng import SEEDREAM_DEFAULT

    image_missing = providers.missing_for("tryon", "volc-seedream")
    motion_missing = providers.missing_for("motion", "volc-dreamactor-2")
    motion_models = {}
    for option_id in ("volc-dreamactor-2", "fal-kling-v3-pro"):
        option = providers.option_for("motion", option_id)
        missing = providers.missing_for("motion", option_id)
        motion_models[option_id] = {"ready": not missing, "missing": missing,
                                    "label": option["label"], "provider": option["provider"]}
    edit_models = {}
    for option_id in ("fal-kling-o3-pro-edit", *providers.OFFICIAL_EDIT_MODELS):
        option = providers.option_for("edit", option_id)
        missing = providers.missing_for("edit", option_id)
        edit_models[option_id] = {"ready": not missing, "missing": missing,
                                  "label": option["label"], "provider": option["provider"]}
    return {
        "data_directory": str(DATA_ROOT),
        "configured": {key: bool(os.environ.get(key, "").strip()) for key in sorted(ALLOWED_KEYS)},
        "values": {key: os.environ.get(key, "").strip() or (SEEDREAM_DEFAULT if key == "SEEDREAM_MODEL" else "")
                   for key in sorted(VALUE_KEYS)},
        "image_ready": not image_missing,
        "motion_ready": not motion_missing,
        "motion_models": motion_models,
        "edit_models": edit_models,
        "missing": {"image": image_missing, "motion": motion_missing},
    }


def _updated_dotenv_text(existing: str, changes: dict[str, str]) -> str:
    """Update the batch while preserving comments and non-UI legacy settings."""
    lines = []
    written = set()

    def assignment(key: str) -> str:
        value = changes[key].replace("\\", "\\\\").replace("'", "\\'")
        return f"{key}='{value}'\n"

    for binding in parse_stream(io.StringIO(existing)):
        if binding.key in changes:
            if binding.key not in written:
                lines.append(assignment(binding.key))
                written.add(binding.key)
        else:
            lines.append(binding.original.string)
    for key in sorted(changes.keys() - written):
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines.append("\n")
        lines.append(assignment(key))
    return "".join(lines)


def _save_dotenv_batch(changes: dict[str, str]) -> None:
    """Commit one complete file; failures before replace leave the old file intact."""
    existing = ENV_PATH.read_text(encoding="utf-8-sig") if ENV_PATH.exists() else ""
    content = _updated_dotenv_text(existing, changes)
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(ENV_PATH.parent))
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, ENV_PATH)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def save_settings(values: object) -> dict:
    if not isinstance(values, dict):
        raise ValueError("配置须为 JSON 对象")
    if set(values) - ALLOWED_KEYS:
        raise ValueError("包含不支持的配置项")
    changes = {}
    for key, value in values.items():
        if not isinstance(value, str):
            raise ValueError("配置值须为文本")
        value = value.strip()
        if "\n" in value or "\r" in value or "\0" in value or len(value) > 4096:
            raise ValueError("配置值包含不支持的字符或过长")
        if key in SECRET_KEYS and not value:
            continue
        changes[key] = value
    with _SETTINGS_LOCK:
        if changes:
            _save_dotenv_batch(changes)
            # Publish values to the running process only after the disk commit.
            os.environ.update(changes)
        return public_settings()
