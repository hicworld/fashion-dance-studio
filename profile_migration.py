"""Copy legacy desktop data into the stable profile without deleting originals.

Migration diagnostics contain counts, setting names and exception types only.
Configuration values and user media paths are never logged or returned.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values, set_key


PROFILE_NAME = "FashionDanceStudio"
MIGRATION_VERSION = 1
CONFIG_KEYS = {
    "ARK_API_KEY", "SEEDREAM_MODEL", "VOLC_ACCESS_KEY_ID", "VOLC_SECRET_ACCESS_KEY",
    "TOS_ACCESS_KEY_ID", "TOS_SECRET_ACCESS_KEY", "TOS_BUCKET", "TOS_ENDPOINT", "TOS_REGION",
    "TOS_PREFIX", "TOS_URL_TTL_SECONDS", "FAL_KEY", "LIBLIB_ACCESS_KEY", "LIBLIB_SECRET_KEY",
}
CREDENTIAL_PAIRS = (
    ("VOLC_ACCESS_KEY_ID", "VOLC_SECRET_ACCESS_KEY"),
    ("TOS_ACCESS_KEY_ID", "TOS_SECRET_ACCESS_KEY"),
    ("LIBLIB_ACCESS_KEY", "LIBLIB_SECRET_KEY"),
)
_JOB_ID = re.compile(r"[0-9a-f]{32}\Z")


def _linked(path: Path) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    except FileNotFoundError:
        return False
    except OSError:
        return True


def discover_legacy_profiles(target: Path, user_profile: Path,
                             environ: Mapping[str, str] | None = None, report: dict | None = None) -> list[Path]:
    """Inspect only known app-data layouts; never scan a source project."""
    environ = os.environ if environ is None else environ
    local = user_profile / "AppData" / "Local"
    roaming = user_profile / "AppData" / "Roaming"
    candidates = [local / PROFILE_NAME, roaming / PROFILE_NAME]
    for variable in ("LOCALAPPDATA", "APPDATA"):
        if environ.get(variable, "").strip():
            candidates.append(Path(environ[variable]) / PROFILE_NAME)
    packages = local / "Packages"
    if packages.is_dir():
        for package in packages.glob("OpenAI.Codex_*"):
            if not package.is_dir() or _linked(package):
                continue
            for suffix in (
                ("LocalCache", "Local"), ("LocalCache", "Roaming"),
                ("LocalCache", "Local", "AppData", "Local"),
                ("LocalCache", "Local", "AppData", "Roaming"),
                ("LocalCache", "AppData", "Local"), ("LocalCache", "AppData", "Roaming"),
                ("AppData", "Local"), ("AppData", "Roaming"), ("LocalState",),
            ):
                candidates.append(package.joinpath(*suffix) / PROFILE_NAME)
    result = []
    for candidate in candidates:
        try:
            if not candidate.is_dir() or _linked(candidate):
                continue
            if target.exists() and candidate.samefile(target):
                continue
            if any(candidate.samefile(existing) for existing in result):
                continue
            result.append(candidate)
        except OSError as error:
            if report is not None:
                _error(report, "inspect_profile", error)
            continue
    return result


def _error(report: dict, operation: str, error: Exception) -> None:
    if len(report["errors"]) < 50:
        report["errors"].append({"operation": operation, "type": type(error).__name__})


def _read_config(path: Path, report: dict) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        values = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
        return {key: value for key, value in values.items()
                if key in CONFIG_KEYS and isinstance(value, str) and value.strip()}
    except (OSError, UnicodeError, ValueError) as error:
        _error(report, "read_configuration", error)
        return {}


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _merge_configuration(target: Path, sources: list[Path], report: dict) -> None:
    current = _read_config(target / ".env", report)
    source_configs = [_read_config(source / ".env", report) for source in
                      sorted(sources, key=lambda source: _mtime(source / ".env"), reverse=True)]
    changes = {}
    paired = {key for pair in CREDENTIAL_PAIRS for key in pair}
    for first, second in CREDENTIAL_PAIRS:
        if current.get(first) and current.get(second):
            continue
        compatible = next((values for values in source_configs
                           if values.get(first) and values.get(second)
                           and (not current.get(first) or current[first] == values[first])
                           and (not current.get(second) or current[second] == values[second])), None)
        if compatible is not None:
            for key in (first, second):
                if not current.get(key):
                    changes[key] = compatible[key]
        elif any(values.get(first) or values.get(second) for values in source_configs) or current.get(first) or current.get(second):
            report["credential_pairs_needing_configuration"].append(f"{first}/{second}")
    for key in sorted(CONFIG_KEYS - paired):
        if not current.get(key):
            value = next((values[key] for values in source_configs if values.get(key)), None)
            if value is not None:
                changes[key] = value
    if not changes:
        return
    temporary = None
    try:
        destination = target / ".env"
        with tempfile.NamedTemporaryFile(dir=target, prefix=".env-migration-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
        if destination.is_file():
            shutil.copyfile(destination, temporary)
        for key, value in sorted(changes.items()):
            set_key(str(temporary), key, value, quote_mode="always", encoding="utf-8")
        with temporary.open("r+b") as output:
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        report["config_keys_merged"].extend(sorted(changes))
    except (OSError, UnicodeError, ValueError) as error:
        _error(report, "write_configuration", error)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _copy_missing(source: Path, destination: Path, target_root: Path) -> bool:
    if destination.exists() or not _inside(destination, target_root):
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.migration-tmp")
    try:
        shutil.copy2(source, temporary)
        if os.name == "nt":
            # Windows rename refuses an existing destination; never replace data.
            os.rename(temporary, destination)
        else:
            # POSIX rename would overwrite, so use an exclusive hard-link publish.
            os.link(temporary, destination)
        return True
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _merge_jobs(target: Path, sources: list[Path], report: dict) -> None:
    candidates = []
    for source in sources:
        jobs = source / "jobs"
        if not jobs.is_dir() or _linked(jobs):
            continue
        try:
            for folder in jobs.iterdir():
                if folder.is_dir() and _JOB_ID.fullmatch(folder.name) and not _linked(folder):
                    candidates.append(folder)
        except OSError as error:
            _error(report, "list_jobs", error)
    candidates.sort(key=lambda folder: _mtime(folder / "job_state.json") or _mtime(folder), reverse=True)
    report["source_job_ids"] = len({folder.name for folder in candidates})
    for source in candidates:
        destination = target / "jobs" / source.name
        try:
            if destination.exists() and (_linked(destination) or not destination.is_dir()):
                continue
            if not destination.exists():
                destination.mkdir()
                report["jobs_created"] += 1
            for parent, directories, filenames in os.walk(source, followlinks=False):
                parent = Path(parent)
                directories[:] = [name for name in directories if not _linked(parent / name)]
                for name in filenames:
                    original = parent / name
                    if _linked(original) or name.endswith(".migration-tmp"):
                        continue
                    relative = original.relative_to(source)
                    if _copy_missing(original, destination / relative, destination):
                        report["files_copied"] += 1
        except OSError as error:
            _error(report, "copy_job", error)


def _save_report(target: Path, report: dict) -> None:
    path = target / "profile_migration.json"
    totals = {"jobs_created": 0, "files_copied": 0, "config_keys_merged": []}
    try:
        previous = json.loads(path.read_text(encoding="utf-8")).get("totals", {})
        for key in ("jobs_created", "files_copied"):
            if isinstance(previous.get(key), int) and previous[key] >= 0:
                totals[key] = previous[key]
        totals["config_keys_merged"] = [key for key in previous.get("config_keys_merged", []) if key in CONFIG_KEYS]
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    for key in ("jobs_created", "files_copied"):
        totals[key] += report[key]
    totals["config_keys_merged"] = sorted(set(totals["config_keys_merged"]) | set(report["config_keys_merged"]))
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"version": MIGRATION_VERSION, "completed": not report["errors"],
                                    "last_run": report, "totals": totals},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def migrate_legacy_profiles(target: Path, *, user_profile: Path,
                            environ: Mapping[str, str] | None = None) -> dict:
    """Copy old data, fill missing settings and return only safe diagnostics."""
    target, user_profile = Path(target), Path(user_profile)
    report = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source_profiles": 0, "source_job_ids": 0, "jobs_created": 0, "files_copied": 0,
        "config_keys_merged": [], "credential_pairs_needing_configuration": [], "errors": [], "skipped": False,
    }
    try:
        saved = json.loads((target / "profile_migration.json").read_text(encoding="utf-8"))
        if saved.get("version") == MIGRATION_VERSION and saved.get("completed") is True:
            report["skipped"] = True
            return report
    except (OSError, ValueError, AttributeError):
        pass
    target.mkdir(parents=True, exist_ok=True)
    (target / "jobs").mkdir(exist_ok=True)
    try:
        sources = discover_legacy_profiles(target, user_profile, environ, report)
        report["source_profiles"] = len(sources)
        _merge_jobs(target, sources, report)
        _merge_configuration(target, sources, report)
    except OSError as error:
        _error(report, "discover_profiles", error)
    try:
        _save_report(target, report)
    except (OSError, ValueError, TypeError) as error:
        _error(report, "write_report", error)
    return report
