"""Separate packaged resources from the user's private writable app data."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Mapping

SOURCE_ROOT = Path(__file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', SOURCE_ROOT)).resolve()


def data_root_for(*, frozen: bool, environ: Mapping[str, str] | None = None,
                  user_profile: Path | None = None, source_root: Path = SOURCE_ROOT) -> Path:
    environ = os.environ if environ is None else environ
    if environ.get('FASHION_DANCE_HOME', '').strip():
        return Path(environ['FASHION_DANCE_HOME']).expanduser().resolve()
    if frozen:
        profile = user_profile or Path(environ.get('USERPROFILE') or Path.home())
        return (profile / 'FashionDanceStudio').resolve()
    return source_root


DATA_ROOT = data_root_for(frozen=bool(getattr(sys, 'frozen', False)))
_INITIALIZE_LOCK = threading.Lock()
_MIGRATED_ROOTS: set[Path] = set()


def quiet_subprocess_kwargs() -> dict:
    """FFmpeg and FFprobe should not create console windows in the desktop app."""
    return {"creationflags": 0x08000000} if os.name == "nt" else {}


def initialize() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    (DATA_ROOT / 'jobs').mkdir(exist_ok=True)
    binaries = RESOURCE_ROOT / 'bin'
    if binaries.is_dir() and str(binaries) not in os.environ.get('PATH', '').split(os.pathsep):
        os.environ['PATH'] = str(binaries) + os.pathsep + os.environ.get('PATH', '')
    if getattr(sys, 'frozen', False) and not os.environ.get('FASHION_DANCE_HOME', '').strip():
        # The desktop entry holds the final profile's instance mutex before this
        # point. Migrate before app_settings.load_settings and job restoration.
        with _INITIALIZE_LOCK:
            if DATA_ROOT not in _MIGRATED_ROOTS:
                from profile_migration import migrate_legacy_profiles
                profile = Path(os.environ.get('USERPROFILE') or Path.home())
                migrate_legacy_profiles(DATA_ROOT, user_profile=profile)
                _MIGRATED_ROOTS.add(DATA_ROOT)
