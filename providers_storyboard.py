"""Seedance first-frame generation. A saved submission is never posted twice."""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image

import providers_seedance as ark
from providers_jimeng import MotionSubmissionUncertainError, MotionTerminalError

OPTION = 'volc-seedance-2.5-first-frame'


def missing() -> list[str]:
    return [] if os.environ.get('ARK_API_KEY', '').strip() else ['请先在 API 设置中保存火山方舟 ARK_API_KEY，并开通 Seedance 2.5。']


def identity(resolution: str, seconds: int) -> dict:
    if resolution not in {'480p', '720p', '1080p'} or type(seconds) is not int or not 4 <= seconds <= 30:
        raise ValueError('图生视频需选择480p、720p或1080p，生成时长为4—30秒整数。')
    return {'provider': ark.PROVIDER, 'option': OPTION, 'model': ark.MODEL,
            'endpoint': ark.CREATE_URL, 'resolution': resolution, 'requested_seconds': seconds}


def task_record(task_id: str, resolution: str, seconds: int, generate_audio: bool = False) -> dict:
    if not ark._identifier(task_id):
        raise ValueError('原任务编号须为 cgt- 开头的方舟任务编号。')
    if type(generate_audio) is not bool:
        raise ValueError('生成声音开关必须为布尔值。')
    return {**identity(resolution, seconds), 'task_id': task_id, 'generate_audio': generate_audio}


def saved_task(destination: Path, resolution: str, seconds: int) -> dict:
    task = ark._read(ark._paths(destination)[0])
    expected = identity(resolution, seconds)
    if (not ark._identifier(task.get('task_id')) or any(task.get(k) != v for k, v in expected.items())):
        raise MotionSubmissionUncertainError('没有匹配的原任务编号，请在方舟核对后填写原编号续查，不要重复生成。')
    return task


def _result(destination: Path, task: dict, *, progress=None, stop_event=None) -> dict:
    if ark._valid_source(destination):
        result = ark._read(ark._paths(destination)[2])
    else:
        # Shared polling/download logic never submits a generation request.
        ark._poll(destination, task, progress=progress, stop_event=stop_event)
        result = ark._read(ark._paths(destination)[2])
    return {**task, 'operation': 'first_frame', 'generate_audio': task.get('generate_audio', False),
            'usage': ark._safe_usage(result.get('usage'))}


def resume(destination: Path, *, resolution: str, seconds: int, progress=None, stop_event=None) -> dict:
    destination = Path(destination)
    ark._cancelled(stop_event)
    task = saved_task(destination, resolution, seconds)
    return _result(destination, task, progress=progress, stop_event=stop_event)


def generate(frame: Path, prompt: str, destination: Path, *, resolution: str,
             seconds: int, generate_audio: bool = False, progress=None, stop_event=None) -> dict:
    destination = Path(destination)
    expected = identity(resolution, seconds)
    if type(generate_audio) is not bool:
        raise ValueError('生成声音开关必须为布尔值。')
    expected['generate_audio'] = generate_audio
    paths = ark._paths(destination)
    if any(p.exists() for p in (destination, *paths)):
        raise MotionSubmissionUncertainError('该次生成已有提交记录，请续查原任务；重新生成须另建一次记录。')
    if missing():
        raise ValueError(missing()[0])
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 12000:
        raise ValueError('视频提示词需为1—12000字。')
    if frame.stat().st_size >= ark.MAX_IMAGE_BYTES:
        raise ValueError('首帧必须小于30MB。')
    with Image.open(frame) as im:
        w, h = im.size
        if not 300 <= min(w, h) <= max(w, h) <= 6000 or not .4 <= w / h <= 2.5:
            raise ValueError('首帧宽高须在300—6000像素之间，宽高比为0.4—2.5。')
        im.verify()
    data_url = 'data:image/png;base64,' + base64.b64encode(frame.read_bytes()).decode('ascii')
    payload = {'model': ark.MODEL, 'content': [
        {'type': 'text', 'text': prompt.strip()},
        {'type': 'image_url', 'role': 'first_frame', 'image_url': {'url': data_url}}],
        'ratio': 'adaptive', 'duration': seconds, 'resolution': resolution,
        'output_format': 'mp4', 'generate_audio': generate_audio, 'watermark': True}
    destination.parent.mkdir(parents=True, exist_ok=True)
    ark._cancelled(stop_event)
    try:
        with paths[1].open('x', encoding='utf-8') as marker:
            json.dump({**expected, 'status': 'submitting',
                       'submitted_at_utc': datetime.now(timezone.utc).isoformat()}, marker)
            marker.flush()
            os.fsync(marker.fileno())
    except (OSError, ValueError):
        raise MotionSubmissionUncertainError('独占提交标记未能保存，已阻止提交。') from None
    ark._progress(progress, 'submitting', '正在提交本镜首帧生成请求')
    try:
        response = requests.post(ark.CREATE_URL, headers={'Authorization': 'Bearer ' + ark._key()},
                                 json=payload, timeout=(20, 120), allow_redirects=False)
    except Exception:
        raise MotionSubmissionUncertainError('提交响应未取回，可能已创建云端任务；请核对原编号，不会自动重提。') from None
    try:
        try:
            body = response.json()
        except (ValueError, RecursionError):
            body = None
        if not 200 <= response.status_code < 300:
            proof = ark._rejection_evidence(response.status_code, body)
            if proof:
                ark._atomic_record(paths[1], {**expected, **proof, 'status': 'rejected'})
                raise MotionTerminalError('方舟明确拒绝了首帧素材，请核对素材要求后手动另建生成记录。', task_status='rejected')
            raise MotionSubmissionUncertainError(
                f'提交未可靠确认（HTTP {response.status_code}），请核对方舟任务历史；不会自动重提。')
        identifier = body.get('id') if isinstance(body, dict) else None
        if not ark._identifier(identifier) or body.get('error') is not None:
            raise MotionSubmissionUncertainError('响应缺少可靠任务编号，请核对方舟任务历史；不会自动重提。')
    finally:
        response.close()
    task = task_record(identifier, resolution, seconds, generate_audio)
    ark._persist_accepted_task(paths[0], paths[1], task)
    return _result(destination, task, progress=progress, stop_event=stop_event)
