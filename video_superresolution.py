"""Offline Real-ESRGAN video enhancement, with resumable frame checkpoints.

The cloud-generated source is immutable. Only a fully encoded and verified clip
may replace the editor's output. No downloads, cloud requests or silent fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image

from providers_jimeng import MotionCancelledError
from runtime_paths import RESOURCE_ROOT, quiet_subprocess_kwargs

ENGINE_ROOT = RESOURCE_ROOT / 'third_party' / 'realesrgan'
MODES = {
    'ai-light': {'model': 'realesr-animevideov3', 'scale': 3, 'label': '动画轻量 · 较快'},
    'ai-general': {'model': 'realesrgan-x4plus', 'scale': 4, 'label': '通用画面 / 三维 · 较慢'},
}
GPU_LOCK = threading.Lock()
PROCESS_LOCK = threading.Lock()
PROCESSES = set()


def shutdown():
    """Stop only subprocesses launched by this module in this app instance."""
    with PROCESS_LOCK:
        for process in tuple(PROCESSES):
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass


def remove_cache(path, root):
    target, parent = Path(path).resolve(), Path(root).resolve()
    if target == parent or not target.is_relative_to(parent) or Path(path).is_symlink():
        raise ValueError('超分缓存路径无效，停止清理。')
    shutil.rmtree(path)


def configuration(mode):
    if mode not in MODES:
        raise ValueError('请选择有效的 AI 超分模型。')
    return MODES[mode]


def availability():
    modes = {}
    for mode, config in MODES.items():
        stem = config['model'] + (f'-x{config["scale"]}' if mode == 'ai-light' else '')
        files = ['realesrgan-ncnn-vulkan.exe', 'vcomp140.dll',
                 f'models/{stem}.param', f'models/{stem}.bin']
        modes[mode] = {**config, 'available': all((ENGINE_ROOT / f).is_file() for f in files)}
    return {'engine': 'Real-ESRGAN ncnn Vulkan', 'modes': modes,
            'note': '本机运算，无超分 API 费用。需要可用的 Vulkan 显卡驱动；核显可能较慢。'}


def require(mode):
    config = configuration(mode)
    if not availability()['modes'][mode]['available']:
        raise ValueError('本机缺少 AI 超分引擎或模型，请使用包含超分组件的新版软件。')
    return config


def cancelled(stop):
    if stop is not None and stop.is_set():
        raise MotionCancelledError('本地超分已暂停。原片和已完成帧已保留，可继续本地超分，不重新生成。')


def run_process(command, *, stop, log, progress=None, timeout=600, cwd=None):
    """Drain output to disk and reap children on every exit, including shutdown."""
    cancelled(stop)
    with Path(log).open('wb') as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                   cwd=cwd, **quiet_subprocess_kwargs())
        with PROCESS_LOCK:
            PROCESSES.add(process)
        began = time.monotonic()
        try:
            while process.poll() is None:
                cancelled(stop)
                if time.monotonic() - began > timeout:
                    raise RuntimeError('本地超分处理超时。已保留原片与完成帧，可稍后继续或换用轻量模型。')
                if progress:
                    progress()
                if stop is not None:
                    stop.wait(.35)
                else:
                    time.sleep(.35)
            cancelled(stop)
            if process.returncode:
                raise RuntimeError('本地超分引擎处理失败。请检查 Vulkan 显卡驱动及可用内存；可换轻量模型重试。原片已保留。')
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=15)
            finally:
                with PROCESS_LOCK:
                    PROCESSES.discard(process)


def valid_frame(path, size=None):
    try:
        with Image.open(path) as im:
            if im.format != 'PNG' or (size and im.size != size):
                return False
            im.verify()
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def _identity(source, mode, start, frames, fps, width, height):
    config = configuration(mode)
    stem = config['model'] + (f'-x{config["scale"]}' if mode == 'ai-light' else '')
    return {'version': 1, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'mode': mode, 'start': start, 'frames': frames, 'fps': fps, 'width': width, 'height': height,
            'model_sha256': hashlib.sha256((ENGINE_ROOT / 'models' / (stem + '.bin')).read_bytes()).hexdigest()}


def enhance(source, output, *, cache_root, mode, start, frames, fps, width, height,
            stop=None, progress=None):
    """Enhance the used interval only. Return evidence; output is a caller-owned temp."""
    config = require(mode)
    source, output, cache_root = Path(source), Path(output), Path(cache_root)
    cancelled(stop)
    if not source.is_file() or not 1 <= frames <= 1800 or not 1 <= fps <= 120:
        raise ValueError('超分原片或帧数无效。')
    if width % 2 or height % 2 or not 300 <= min(width, height) <= max(width, height) <= 1920:
        raise ValueError('超分输出画幅无效。')
    identity = _identity(source, mode, start, frames, fps, width, height)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    # Keep only one resumable variant per immutable source attempt. Changes to
    # model or trim range start a fresh cache, never reuse incompatible frames.
    cache_root.mkdir(parents=True, exist_ok=True)
    work = cache_root / key
    for old in cache_root.iterdir():
        if old.is_dir() and old.name != key and len(old.name) == 24 and all(c in '0123456789abcdef' for c in old.name):
            remove_cache(old, cache_root)
    work.mkdir(exist_ok=True)
    (work / 'checkpoint.json').write_text(json.dumps(identity, indent=2), encoding='utf-8')
    raw, result, pending = [work / s for s in ('raw', 'enhanced', 'pending')]
    raw.mkdir(exist_ok=True); result.mkdir(exist_ok=True)
    if pending.exists():
        remove_cache(pending, cache_root)
    pending.mkdir()
    names = [f'{i:08d}.png' for i in range(1, frames + 1)]
    notify = progress or (lambda _: None)
    notify('等待本机超分引擎；原始视频已保留。')
    while not GPU_LOCK.acquire(timeout=.35):
        cancelled(stop)
    try:
        cancelled(stop)
        # Conservative free-space bound for RGB PNGs, their enhanced copies and
        # encoding. Fail before filling the system drive; no paid job is retried.
        from shortfilms import probe
        metrics = probe(source)
        required = int(metrics['width'] * metrics['height'] * 3 * frames * (1 + config['scale']**2) * 1.15)
        occupied = sum(f.stat().st_size for d in (raw, result) for f in d.glob('*.png'))
        if shutil.disk_usage(work).free < max(256 * 1024**2, required - occupied):
            raise ValueError('本地磁盘空间不足，无法安全保存超分帧。请释放空间后继续，本次不重新调用生成 API。')
        if not all(valid_frame(raw / name) for name in names):
            notify('正在提取分镜取用范围的画面…')
            run_process(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',str(start),'-i',str(source),
                         '-vf',f'fps={fps}','-frames:v',str(frames),'-start_number','1',str(raw/'%08d.png')],
                        stop=stop, log=work/'extract.log')
        if not all(valid_frame(raw / name) for name in names):
            raise ValueError('原片不足所需超分帧数，请检查裁切起点。')
        with Image.open(raw / names[0]) as image:
            expected = tuple(v * config['scale'] for v in image.size)
        reused = 0
        for name in names:
            cancelled(stop)
            if valid_frame(result / name, expected):
                reused += 1
                continue
            (result / name).unlink(missing_ok=True)
            try:
                os.link(raw / name, pending / name)
            except OSError:
                shutil.copyfile(raw / name, pending / name)
        last = [-1, 0]
        def report():
            count = min(frames, len(list(result.glob('*.png'))))
            elapsed = time.monotonic()
            if count != last[0] or elapsed - last[1] > 10:
                notify(f'AI 超分：{count}/{frames} 帧（{round(count/frames*100)}%） · {config["label"]}'
                       + (f' · 已复用 {reused} 帧' if reused else ''))
                last[:] = [count, elapsed]
        if reused < frames:
            run_process([str(ENGINE_ROOT/'realesrgan-ncnn-vulkan.exe'),'-i',str(pending),'-o',str(result),
                         '-n',config['model'],'-s',str(config['scale']),'-m',str(ENGINE_ROOT/'models'),
                         '-t','128','-j','1:1:1','-g','0','-f','png'],
                        stop=stop, log=work/'engine.log', progress=report,
                        timeout=max(600, (frames-reused)*180))
        if not all(valid_frame(result / name, expected) for name in names):
            raise RuntimeError('AI 超分未完成全部有效帧。原片和完成帧已保留，请继续本地超分。')
        cancelled(stop)
        notify('AI 超分完成，正在编码成片尺寸…')
        vf = (f'scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,'
              f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1')
        run_process(['ffmpeg','-hide_banner','-loglevel','error','-y','-framerate',str(fps),'-start_number','1',
                     '-i',str(result/'%08d.png'),'-vf',vf,'-frames:v',str(frames),'-an','-c:v','libx264',
                     '-preset','fast','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',str(output)],
                    stop=stop, log=work/'encode.log', timeout=600)
        final = probe(output)
        if (final['width'], final['height']) != (width, height) or abs(final['duration']-frames/fps) > 1/fps+.005:
            raise RuntimeError('超分输出的尺寸或时长校验失败，原有剪辑片段保持原样。')
        evidence = {'engine': 'Real-ESRGAN ncnn Vulkan', 'mode': mode, **config,
                    'input_width': metrics['width'], 'input_height': metrics['height'],
                    'output_width': width, 'output_height': height, 'frames': frames,
                    'source_sha256': identity['source_sha256'], 'model_sha256': identity['model_sha256']}
        remove_cache(work, cache_root)
        return evidence
    finally:
        try:
            if pending.exists():
                remove_cache(pending, cache_root)
        finally:
            GPU_LOCK.release()
