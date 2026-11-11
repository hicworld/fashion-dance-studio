"""Non-secret appearance and creation choices in the stable desktop profile."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from runtime_paths import DATA_ROOT

PREFERENCES_PATH = DATA_ROOT / "ui_preferences.json"
DEFAULTS = {"theme": "light", "motion_model": "volc-dreamactor-2", "creation_mode": "compose",
            "edit_model": "minimax-h3-edit"}
CHOICES = {
    "theme": {"light", "dark"},
    "motion_model": {"volc-dreamactor-2", "fal-kling-v3-pro"},
    "creation_mode": {"compose", "ready_image", "video_replace"},
    "edit_model": {"minimax-h3-edit", "volc-seedance-2.5-edit", "fal-kling-o3-pro-edit"},
}
_LOCK = threading.Lock()


def _read() -> dict[str, str]:
    result = dict(DEFAULTS)
    try:
        if PREFERENCES_PATH.stat().st_size > 4096:
            return result
        saved = json.loads(PREFERENCES_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return result
    if isinstance(saved, dict):
        result.update({key: value for key, value in saved.items()
                       if key in CHOICES and isinstance(value, str) and value in CHOICES[key]})
    return result


def load_preferences() -> dict[str, str]:
    with _LOCK:
        return _read()


def save_preferences(changes: object) -> dict[str, str]:
    if not isinstance(changes, dict) or set(changes) - CHOICES.keys():
        raise ValueError("外观和创作偏好包含不支持的配置项")
    for key, value in changes.items():
        if not isinstance(value, str) or value not in CHOICES[key]:
            raise ValueError("外观和创作偏好值无效")
    with _LOCK:
        result = _read()
        result.update(changes)
        if not changes:
            return result
        PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".ui_preferences.", suffix=".tmp",
                                                dir=str(PREFERENCES_PATH.parent))
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, PREFERENCES_PATH)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return result
