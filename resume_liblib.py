"""Retrieve an existing Liblib task without uploading or creating a new task.

Usage: python resume_liblib.py path/to/source_task.json path/to/recovered.mp4
Importing this module does not read .env or make requests.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import providers_liblib as liblib
from provider_common import redact_error


MAX_WAIT_SECONDS = 60 * 60
MAX_TASK_FILE_BYTES = 64 * 1024
_TASK_UUID = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_task(task_file: Path) -> dict:
    if not task_file.name.endswith("_task.json"):
        raise ValueError("请指定已保存的 *_task.json 任务记录")
    if not task_file.is_file() or not 0 < task_file.stat().st_size <= MAX_TASK_FILE_BYTES:
        raise ValueError("任务记录不存在、为空或超过64KB")
    try:
        task = json.loads(task_file.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("任务记录须为有效的UTF-8 JSON") from None
    if not isinstance(task, dict):
        raise ValueError("任务记录须为JSON对象")
    task_uuid = task.get("generate_uuid")
    if (not isinstance(task_uuid, str) or not _TASK_UUID.fullmatch(task_uuid)
            or not task_uuid.replace("-", "").strip("0")):
        raise ValueError("任务记录缺少有效的 generate_uuid；不能通过此工具创建新任务")
    if task.get("stage") not in {"tryon", "motion"}:
        raise ValueError("任务记录的 stage 必须为 tryon 或 motion")
    return task


def _save_metadata(path: Path, metadata: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def resume_task(
    task_file: Path,
    destination: Path,
    *,
    timeout_seconds: float = MAX_WAIT_SECONDS,
    poll_interval_seconds: float = 5,
) -> dict:
    """Poll only the saved UUID; return done/failed/timeout/error metadata.

    A failed or timed-out recovery can be run again against the same task file.
    It never invokes a generation or upload endpoint.
    """
    task_file = Path(task_file).resolve()
    destination = Path(destination).resolve()
    if task_file == destination:
        raise ValueError("输出文件不能与输入任务JSON相同")
    if destination.suffix.lower() == ".json":
        raise ValueError("输出应为图片或视频文件，不能为JSON")
    if destination.exists():
        raise ValueError("输出文件已存在，请选择另一个文件名以保留已有结果")
    if (not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= MAX_WAIT_SECONDS
            or not math.isfinite(poll_interval_seconds) or poll_interval_seconds < 1):
        raise ValueError("恢复等待时间须在1小时以内，查询间隔须至少1秒")
    task = _read_task(task_file)
    metadata_path = destination.with_name(destination.stem + "_resume.json")
    if metadata_path == task_file:
        raise ValueError("恢复元数据路径不能覆盖输入任务JSON")
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "provider": "liblib", "mode": "resume_existing_task",
        "generate_uuid": task["generate_uuid"], "stage": task["stage"],
        "status": "waiting", "resumed_at_utc": _utc_now(),
        "metadata_file": str(metadata_path),
    }
    # Copy only these known fields, never arbitrary credentials or asset URLs.
    for key, pattern in (("workflow_uuid", r"[0-9a-fA-F]{32}"),
                         ("workflow_config_sha256", r"[0-9a-fA-F]{64}")):
        value = task.get(key)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            metadata[key] = value
    _save_metadata(metadata_path, metadata)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            # This literal endpoint is the only remote API operation permitted.
            data = liblib._signed_post(liblib.STATUS_URI, {"generateUuid": task["generate_uuid"]})
            status = int(data.get("generateStatus"))
            platform_timeout = status == 7 and bool(re.search(r"timeout|超时", str(data.get("generateMsg") or ""), re.I))
            if status not in {1, 2, 3, 4, 5, 6} and not platform_timeout:
                raise ValueError("平台返回未知任务状态，请在控制台核对")
            metadata.update({"generate_status": status, "checked_at_utc": _utc_now()})
            for source, target in (("percentCompleted", "percent_completed"),
                                   ("pointsCost", "points_cost"),
                                   ("accountBalance", "account_balance")):
                value = data.get(source)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    metadata[target] = value
            if platform_timeout:
                reason = redact_error(str(data["generateMsg"]))
                metadata.update(status="failed", failure_reason="platform_timeout",
                                error="平台生成超时：" + reason + "；已保留原任务记录，不会重新提交")
                _save_metadata(metadata_path, metadata)
                return metadata
            if status == 6:
                metadata.update(status="failed", error=redact_error(str(data.get("generateMsg") or "平台未提供错误说明")))
                _save_metadata(metadata_path, metadata)
                return metadata
            if status == 5:
                url = liblib._output_url(data, task["stage"])
                temporary = destination.with_name(destination.name + ".resume-part")
                if temporary.exists():
                    raise ValueError("恢复下载临时文件已存在，请选择另一个输出文件名")
                try:
                    liblib.download_file(url, temporary)
                    if not temporary.is_file() or temporary.stat().st_size == 0:
                        raise ValueError("平台结果下载为空，请从控制台取回已有结果")
                    # Do not overwrite a result written by another process.
                    if destination.exists():
                        raise ValueError("输出文件已被其他进程创建，已保留该文件")
                    temporary.rename(destination)
                finally:
                    temporary.unlink(missing_ok=True)
                metadata.update(status="done", destination=str(destination), completed_at_utc=_utc_now())
                _save_metadata(metadata_path, metadata)
                return metadata
            _save_metadata(metadata_path, metadata)
        except Exception as exc:
            metadata.update(status="error", error=redact_error(str(exc)))
            _save_metadata(metadata_path, metadata)
            return metadata
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(poll_interval_seconds, remaining))
    metadata.update(status="timeout", checked_at_utc=_utc_now(),
                    error="等待已达本次上限；云端任务可能仍在执行，可用同一任务JSON继续恢复")
    _save_metadata(metadata_path, metadata)
    return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="仅查询并取回已保存的Liblib任务，不会提交新生成任务")
    parser.add_argument("task_file", type=Path, help="已有的 *_task.json")
    parser.add_argument("destination", type=Path, help="取回结果保存路径（不能为任务JSON）")
    args = parser.parse_args(argv)
    # Reading credentials is confined to explicit CLI invocation, never import.
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False, encoding="utf-8-sig")
    try:
        metadata = resume_task(args.task_file, args.destination)
    except Exception as exc:
        print("恢复未开始：" + redact_error(str(exc)))
        return 1
    if metadata["status"] == "done":
        print("已取回原任务结果：" + str(args.destination.resolve()))
        print("恢复记录：" + metadata["metadata_file"])
        return 0
    print("恢复状态：" + metadata["status"] + "；" + metadata.get("error", ""))
    print("恢复记录：" + metadata["metadata_file"])
    return 2 if metadata["status"] == "timeout" else 1


if __name__ == "__main__":
    raise SystemExit(main())
