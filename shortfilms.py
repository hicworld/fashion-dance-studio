"""Local storyboard packages, explicit generation batches, and silent rough cuts."""
from __future__ import annotations

import copy
import io
import json
import math
import re
import shutil
import stat
import subprocess
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from flask import Blueprint, jsonify, request, send_from_directory
from PIL import Image, ImageOps

import providers_seedance as ark
import providers_storyboard as provider
import video_superresolution as superresolution
import shortfilm_audio as audio
from shortfilm_pipeline import FilmPipeline, idle_state
from shortfilm_editing import ShortFilmEditing
from provider_common import redact_error
from providers_jimeng import MotionCancelledError, MotionSubmissionUncertainError, MotionTerminalError
from runtime_paths import quiet_subprocess_kwargs

ID = re.compile(r'[0-9a-f]{32}')
SHOT = re.compile(r'S[0-9]{2,3}')


def now():
    return datetime.now(timezone.utc).isoformat()


def number(value, minimum, maximum, label):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(label + '超出允许范围。')
    return value


def text(value, maximum=12000):
    return value[:maximum] if isinstance(value, str) else ''


def probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                             '-show_entries', 'stream=width,height,nb_frames,duration:format=duration',
                             '-of', 'json', str(path)], capture_output=True, check=True, timeout=45,
                            **quiet_subprocess_kwargs())
    data = json.loads(result.stdout)
    video = data['streams'][0]
    return {'duration': float(video.get('duration') or data['format']['duration']),
            'width': int(video['width']), 'height': int(video['height'])}


class ShortFilmStudio:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.projects = {}
        self.workers = {}
        self.stops = {}
        self.pipelines = {}
        self.editing = ShortFilmEditing(self)

    def folder(self, pid):
        if not isinstance(pid, str) or not ID.fullmatch(pid):
            raise ValueError('短片项目编号无效。')
        return self.root / pid

    def load(self, pid):
        with self.lock:
            folder = self.folder(pid)
            if pid not in self.projects:
                try:
                    p = json.loads((folder / 'project.json').read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    raise ValueError('短片项目不存在。') from None
                if p.get('id') != pid or not isinstance(p.get('shots'), list):
                    raise ValueError('短片项目记录损坏。')
                for s in p['shots']:
                    if not SHOT.fullmatch(str(s.get('id', ''))):
                        raise ValueError('镜头编号记录损坏。')
                    if s.get('status') == 'dubbing':
                        s['status'] = 'done' if (folder/'shots'/s['id']/'clip.mp4').is_file() else 'needs_trim'
                        s['stage'] = '声音处理被中断，原有片段已保留；可重新导入声音。'
                    if s.get('status') == 'queued':
                        s.update(status=s.pop('queued_from_status', 'pending'),
                                 stage='软件已重开，排队请求未自动提交；请重新选择并确认。')
                        s.pop('queued_from_stage', None)
                    if s.get('status') == 'waiting_local' and s.get('queued_from_status') == 'done':
                        # A manual local re-trim was queued but never started;
                        # the old clip and its settings are still the usable take.
                        if (folder/'shots'/s['id']/'clip.mp4').is_file():
                            s.update(status='done', stage='软件已重开，排队的本地重做未开始，已保留上一版成品。')
                            s.pop('queued_from_status', None)
                            s.pop('queued_from_stage', None)
                    if s.get('status') in {'running', 'waiting_local', 'trimming', 'enhancing'}:
                        a = self.attempt(s)
                        destination = self.destination(pid, s)
                        evidence = a and any(path.exists() for path in (destination, *ark._paths(destination)))
                        s['status'] = 'needs_trim' if destination.is_file() else 'awaiting_resume' if evidence else 'pending'
                        s['stage'] = '软件已重开。已有原片可继续本地裁切/超分；云端任务请续查原编号。'
                        s.pop('queued_from_status', None)
                        s.pop('queued_from_stage', None)
                if p.get('status') in {'running', 'exporting', 'trimming', 'enhancing', 'dubbing'}:
                    p['status'] = 'paused'
                self.projects[pid] = p
                self.save(p)
            return self.projects[pid]

    def save(self, p):
        p['updated_at'] = now()
        ark._atomic_record(self.folder(p['id']) / 'project.json', p)

    @staticmethod
    def attempt(s):
        return next((a for a in s.get('attempts', []) if a['id'] == s.get('active_attempt')), None)

    def destination(self, pid, s):
        aid = s.get('active_attempt', '')
        if not ID.fullmatch(aid):
            return self.folder(pid) / 'shots' / s['id'] / 'unused.mp4'
        return self.folder(pid) / 'shots' / s['id'] / 'attempts' / aid / 'source.mp4'

    def audio_override(self, pid, s):
        record=s.get('audio_override') or {}
        name=record.get('file','')
        if (not re.fullmatch(r'[0-9a-f]{32}\.(wav|mp3|m4a|aac|flac|ogg)',name)
                or record.get('attempt')!=s.get('active_attempt')
                or record.get('trim_start')!=s.get('trim_start',0)):
            return None
        path=self.folder(pid)/'shots'/s['id']/'audio'/name
        return path if path.is_file() else None

    @staticmethod
    def shot(p, sid):
        s = next((s for s in p['shots'] if s['id'] == sid), None)
        if s is None:
            raise ValueError('镜头不存在。')
        return s

    def active(self):
        with self.lock:
            return bool(self.workers or self.editing.workers)

    def public(self, p):
        with self.lock:
            result = copy.deepcopy(p)
            pid = p['id']
            result['busy'] = pid in self.workers or pid in self.editing.workers
            result['editing_busy'] = pid in self.editing.workers
            result['pipeline'] = self.pipelines[pid].public(p) if pid in self.pipelines else idle_state(pid not in self.workers)
            result['model'] = provider.ark.MODEL
            result['missing'] = provider.missing()
            result['superresolution'] = superresolution.availability()
            result['ready_count'] = sum(s['status'] == 'done' for s in p['shots'])
            result['generation_seconds'] = sum(s['generation_seconds'] for s in p['shots'])
            result['animatic_url'] = f'/api/shortfilms/{pid}/files/animatic' if (self.folder(pid)/'animatic.mp4').is_file() else None
            result['roughcut_url'] = f'/api/shortfilms/{pid}/files/roughcut' if (self.folder(pid)/'roughcut.mp4').is_file() else None
            result['roughcut_stale'] = bool(result.get('roughcut_url') and result.get('roughcut_stale'))
            result['roughcut_has_audio'] = audio.inspect(self.folder(pid)/'roughcut.mp4')['has_audio']
            for s in result['shots']:
                base = f'/api/shortfilms/{pid}/shots/{s["id"]}/files/'
                s['frame_url'] = base + 'frame'
                s['source_url'] = base + 'source?v=' + s.get('active_attempt','') if self.destination(pid, s).is_file() else None
                s['clip_url'] = base + 'clip?v=' + s.get('clip_version','') if (self.folder(pid)/'shots'/s['id']/'clip.mp4').is_file() else None
                s['audio_status']={'source_has_audio':audio.inspect(self.destination(pid,s))['has_audio'],
                    'clip_has_audio':audio.inspect(self.folder(pid)/'shots'/s['id']/'clip.mp4')['has_audio'],
                    'imported':bool(self.audio_override(pid,s))}
                a = self.attempt(s)
                if a and 'task_id' not in a:
                    recorded = ark._read(ark._paths(self.destination(pid,s))[0])
                    if ark._identifier(recorded.get('task_id')):
                        a['task_id'] = recorded['task_id']
            if result['roughcut_url']:
                result['roughcut_url'] += '?v=' + p.get('roughcut_version','')
            return result

    def list(self):
        result = []
        for f in self.root.glob('*/project.json'):
            try:
                p = self.load(f.parent.name)
                public = self.public(p)
                result.append({k: public[k] for k in ('id', 'title', 'status', 'shot_count', 'duration_seconds', 'ready_count', 'updated_at', 'busy')})
            except (ValueError, OSError, KeyError):
                continue
        return sorted(result, key=lambda p: p['updated_at'], reverse=True)

    def import_zip(self, stream):
        pid = uuid.uuid4().hex
        folder = self.folder(pid)
        try:
            with zipfile.ZipFile(stream) as archive:
                infos = archive.infolist()
                if len(infos) > 500 or sum(i.file_size for i in infos) > 400 * 1024**2:
                    raise ValueError('分镜包内容过大，最多500项、解压总量400MB。')
                names = {}
                for info in infos:
                    path = PurePosixPath(info.filename.replace('\\', '/'))
                    if path.is_absolute() or '..' in path.parts or ':' in info.filename or stat.S_ISLNK(info.external_attr >> 16):
                        raise ValueError('分镜包包含不安全的文件路径。')
                    if info.is_dir():
                        continue
                    if info.file_size > 100 * 1024**2:
                        raise ValueError('分镜包单文件不能超过100MB。')
                    key = path.name.casefold()
                    if key in names:
                        raise ValueError('分镜包包含重名文件，请只保留一套选定首帧。')
                    names[key] = info
                data = None
                for name, info in names.items():
                    if name.endswith('.json') and info.file_size <= 5 * 1024**2:
                        obj = json.loads(archive.read(info).decode('utf-8-sig'))
                        if isinstance(obj, dict) and isinstance(obj.get('shots'), list):
                            if data is not None:
                                raise ValueError('发现多份分镜数据，请每个包只保留一个项目。')
                            data = obj
                if data is None or not 1 <= len(data['shots']) <= 200:
                    raise ValueError('未找到含shots列表的分镜JSON，镜头数量须为1—200。')
                meta = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}
                fps = int(number(meta.get('fps', 24), 12, 60, '成片帧率'))
                resolution = str(meta.get('delivery_resolution', '1920x1080'))
                dimensions = re.fullmatch(r'(\d{3,4})[x×](\d{3,4})', resolution)
                if not dimensions:
                    raise ValueError('成片分辨率格式应为1920x1080。')
                width, height = map(int, dimensions.groups())
                if min(width, height) < 300 or max(width, height) > 1920 or width % 2 or height % 2:
                    raise ValueError('导出宽高需为300—1920之间的偶数。')
                p = {'id': pid, 'title': text(meta.get('title'), 100) or '未命名短片',
                     'created_at': now(), 'status': 'ready', 'stage': '已导入，尚未调用生成API。',
                     'fps': fps, 'width': width, 'height': height, 'shots': [], 'roughcut_stale': False}
                elapsed, seen = 0., set()
                for index, raw in enumerate(data['shots'], 1):
                    if not isinstance(raw, dict):
                        raise ValueError('分镜条目必须为对象。')
                    sid = raw.get('id', f'S{index:02}')
                    if not isinstance(sid, str) or not SHOT.fullmatch(sid) or sid in seen:
                        raise ValueError('镜号需为不重复的S01、S02等编号。')
                    seen.add(sid)
                    duration = float(number(raw.get('duration_seconds'), .1, 30, sid+'剪辑时长'))
                    # The authored timeline must be representable at the delivery frame rate.
                    if abs(duration*fps-round(duration*fps)) > .001:
                        raise ValueError(sid+'时长不对应整数帧，请调整分镜时长。')
                    prompt = raw.get('video_prompt')
                    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 11000:
                        raise ValueError(sid+'缺少有效video_prompt视频提示词。')
                    frame_name = raw.get('generated_start_frame_file')
                    if not isinstance(frame_name, str) or not frame_name:
                        raise ValueError(sid+'缺少generated_start_frame_file首帧文件名。')
                    info = names.get(PurePosixPath(frame_name.replace('\\', '/')).name.casefold())
                    if info is None or info.file_size >= 30_000_000:
                        raise ValueError(sid+'首帧缺失或不小于30MB。')
                    target = folder / 'shots' / sid
                    target.mkdir(parents=True)
                    with Image.open(io.BytesIO(archive.read(info))) as im:
                        w, h = im.size
                        if not 300 <= min(w,h) <= max(w,h) <= 6000 or not .4 <= w/h <= 2.5:
                            raise ValueError(sid+'首帧尺寸不符合Seedance要求。')
                        ImageOps.exif_transpose(im).convert('RGB').save(target/'frame.png')
                    p['shots'].append({'id': sid, 'title': text(raw.get('title'), 120) or sid,
                        'duration_seconds': duration, 'generation_seconds': max(4, math.ceil(duration)),
                        'start_seconds': round(elapsed, 4), 'video_prompt': prompt.strip(),
                        'action': text(raw.get('action')), 'camera': text(raw.get('camera')),
                        'sound': text(raw.get('sound')), 'post': text(raw.get('post')),
                        'status': 'pending', 'stage': '未生成', 'attempts': [], 'trim_start': 0})
                    elapsed += duration
                p.update(shot_count=len(p['shots']), duration_seconds=round(elapsed, 4))
                for name, info in names.items():
                    if name.endswith('.mp4') and '预演' in name:
                        with archive.open(info) as source, (folder/'animatic.mp4').open('wb') as dest:
                            shutil.copyfileobj(source, dest)
                        break
                with self.lock:
                    self.projects[pid] = p
                    self.save(p)
                return self.public(p)
        except Exception:
            # Only this newly generated folder, never paths contained in the ZIP.
            if folder.parent == self.root and folder.exists():
                shutil.rmtree(folder)
            self.projects.pop(pid, None)
            raise

    def _idle(self, pid):
        if pid in self.workers or pid in self.editing.workers:
            raise ValueError('该短片正在处理，请先暂停并等待当前操作结束。')

    def _queueable(self, pid):
        pipeline = self.pipelines.get(pid)
        if pipeline:
            if not pipeline.accepts():
                raise ValueError('队列正在暂停或已有异常，请等待当前操作结束后再提交。')
        else:
            # Editor previews render immutable snapshots in their own folder.
            # Cloud/local video work can start without waiting for that render.
            if pid in self.workers:
                self._idle(pid)
        return pipeline

    def _enqueue(self, pid, jobs, *, local=False):
        pipeline = self._queueable(pid)
        created = pipeline is None
        if created:
            pipeline = FilmPipeline(self, pid)
        pipeline.queue(jobs, local=local)
        if created:
            self.pipelines[pid] = pipeline
            try:
                self._thread(pid, pipeline.run, (pid,))
            except Exception:
                p = self.load(pid)
                pipeline.cancel_cloud_queue(p)
                pipeline.defer_local_queue(p)
                p.update(status='paused', stage='队列未能启动，尚未提交新生成请求。')
                self.save(p)
                self.pipelines.pop(pid, None)
                raise
        return self.public(self.load(pid))

    def edit(self, pid, sid, body):
        with self.lock:
            self._idle(pid)
            p = self.load(pid)
            s = self.shot(p, sid)
            prompt = body.get('video_prompt')
            if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 11000:
                raise ValueError('提示词需为1—11000字。')
            if 'sound' in body:
                if not isinstance(body['sound'],str) or len(body['sound'])>2000:
                    raise ValueError('声音说明不能超过2000字。')
                s['sound']=body['sound'].strip()
            s['video_prompt'] = prompt.strip()
            self.save(p)
            return self.public(p)

    def _thread(self, pid, target, args):
        stop = threading.Event()
        self.stops[pid] = stop
        thread = threading.Thread(target=target, args=(*args, stop), daemon=True, name='shortfilm-'+pid[:8])
        self.workers[pid] = thread
        try:
            thread.start()
        except Exception:
            self.workers.pop(pid, None)
            self.stops.pop(pid, None)
            raise

    def start(self, pid, body):
        with self.lock:
            pipeline = self._queueable(pid)
            p = self.load(pid)
            if body.get('confirmed') is not True:
                raise ValueError('请勾选确认本批会产生API费用。')
            if provider.missing():
                raise ValueError(provider.missing()[0])
            resolution = body.get('resolution', '720p')
            provider.identity(resolution, 4)
            generate_audio=body.get('generate_audio',False)
            if type(generate_audio) is not bool:
                raise ValueError('生成声音开关必须为布尔值。')
            mode = body.get('upscale_mode', 'none')
            if mode != 'none':
                superresolution.require(mode)
            ids = body.get('shot_ids')
            if not isinstance(ids, list) or not ids or len(ids) != len(set(map(str, ids))):
                raise ValueError('请选择不重复的镜头。')
            selected = [self.shot(p, sid) for sid in ids]
            limit = number(body.get('max_generation_seconds'), 4, 6000, '本批生成秒数上限')
            total = sum(s['generation_seconds'] for s in selected)
            if total > limit:
                raise ValueError(f'本批需要生成{total}秒，超过你设置的{limit:g}秒上限。')
            regenerate = body.get('regenerate') is True
            for s in selected:
                if pipeline and pipeline.contains(s['id']):
                    raise ValueError(s['id']+'已在生成或本地处理队列中，请勿重复加入。')
                if len(self.generation_prompt(s,generate_audio))>12000:
                    raise ValueError(s['id']+'画面提示词与声音说明合计过长，请缩短后提交。')
                if s['status'] in {'awaiting_resume', 'uncertain'}:
                    raise ValueError(s['id']+'已有待核对任务，请先续查原任务；不允许直接再次扣费生成。')
                if s.get('attempts') and not regenerate:
                    raise ValueError(s['id']+'已有生成记录，若要另付费重做，请勾选“重新生成”。')
            jobs = [dict(kind='generate', sid=s['id'], resolution=resolution, mode=mode,
                         generate_audio=generate_audio, seconds=s['generation_seconds'],
                         prompt=self.generation_prompt(s,generate_audio)) for s in selected]
            return self._enqueue(pid, jobs)

    @staticmethod
    def generation_prompt(s, generate_audio=False):
        n, used = s['generation_seconds'], s['duration_seconds']
        prompt=s['video_prompt']
        if generate_audio:
            # The imported package's standard instruction explicitly disabled
            # sound. Replace that known sentence only; preserve authored action.
            prompt=prompt.replace('不生成字幕、可读文字、配乐或旁白，声音后期制作。','不生成字幕、可读文字。')
            prompt+='\n生成与画面同步的声音。声音说明：'+(s.get('sound') or '自然的环境声和动作音效，不添加台词或旁白。')
            prompt+='\n按声音说明决定是否有台词；有中文台词时清晰说出，不擅自加旁白。背景配乐统一后期制作。'
        return (prompt + f'\n实际生成{n}秒，剪辑取用{used:g}秒。'
                f'主要动作在前{used:g}秒内完成，剩余时间自然停留，不重演动作。保持首帧画幅，单一连续镜头。')

    def _progress(self, pid, sid):
        def update(value):
            with self.lock:
                p = self.load(pid)
                s = self.shot(p, sid)
                s['stage'] = text(value.get('stage') or value.get('message'), 500) if isinstance(value, dict) else text(str(value), 500)
                self.save(p)
        return update

    def _clip(self, p, s):
        source = self.destination(p['id'], s)
        start = float(number(s.get('trim_start', 0), 0, 30, '裁切起点'))
        metrics = probe(source)
        length = s['duration_seconds']
        if start+length > metrics['duration']+.025:
            raise ValueError('原片不够所需裁切时长，请调整取用起点；没有自动补帧或变速。')
        folder = self.folder(p['id'])/'shots'/s['id']
        temporary = folder / ('clip-'+uuid.uuid4().hex+'.mp4')
        with_audio = temporary.with_name(temporary.stem+'-audio.mp4')
        mode = s.get('processing_mode', self.attempt(s).get('upscale_mode', 'none') if self.attempt(s) else 'none')
        vf = (f'scale={p["width"]}:{p["height"]}:force_original_aspect_ratio=decrease:flags=lanczos,'
              f'pad={p["width"]}:{p["height"]}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={p["fps"]}')
        try:
            if mode != 'none':
                with self.lock:
                    s.update(status='enhancing', stage='准备本地 AI 超分；不调用生成 API。')
                    self.save(p)
                processing = superresolution.enhance(source, temporary, cache_root=source.parent/'superresolution',
                    mode=mode, start=start, frames=round(length*p['fps']), fps=p['fps'], width=p['width'], height=p['height'],
                    stop=self.stops.get(p['id']), progress=self._progress(p['id'],s['id']))
            else:
                subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',str(start),'-i',str(source),
                    '-vf',vf,'-frames:v',str(round(length*p['fps'])),'-an','-c:v','libx264','-preset','fast',
                    '-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',str(temporary)],
                    check=True,capture_output=True,timeout=600,**quiet_subprocess_kwargs())
                processing = {'mode':'none','engine':'Lanczos','output_width':p['width'],'output_height':p['height']}
            override=self.audio_override(p['id'],s)
            sound_source=override or source
            has_audio=audio.inspect(sound_source)['has_audio'] is True
            if has_audio:
                self._progress(p['id'],s['id'])('正在保留本镜声音，不重新生成画面…')
                audio.mux(temporary,sound_source,with_audio,start=0 if override else start,length=length,
                          stop=self.stops.get(p['id']))
                with_audio.replace(temporary)
            ark._cancelled(self.stops.get(p['id']))
            if abs(probe(temporary)['duration']-length) > 1/p['fps']+.005:
                raise ValueError('裁切片段长度不符合分镜，请调整取用范围。')
            temporary.replace(folder/'clip.mp4')
            with self.lock:
                s.update(status='done', stage='AI 超分完成，已按分镜时长裁切。' if mode != 'none' else '已按分镜时长裁切（普通缩放）。',
                         source_metrics=metrics, error='', clip_version=uuid.uuid4().hex, clip_processing=processing)
                s['audio_info']={'has_audio':has_audio,'mode':'imported' if override else 'generated' if has_audio else 'silent'}
                p['roughcut_stale'] = True
        finally:
            temporary.unlink(missing_ok=True)
            with_audio.unlink(missing_ok=True)

    def _failure(self, pid, s, error):
        dest = self.destination(pid, s)
        a = self.attempt(s)
        if isinstance(error, MotionTerminalError):
            status = 'failed'
        elif dest.is_file():
            status = 'needs_trim'
        elif ark._paths(dest)[0].exists():
            status = 'awaiting_resume'
        elif ark._paths(dest)[1].exists():
            status = 'uncertain'
        else:
            status = 'pending'
        s.update(status=status, stage='原片已保留，可继续本地裁切 / AI 超分，不重新扣费生成。' if status=='needs_trim' else '本镜未完成，已暂停本批', error=redact_error(str(error))[:800])
        if a:
            a['status'] = status
        return status

    def pause(self, pid):
        with self.lock:
            p = self.load(pid)
            if pid in self.stops:
                self.stops[pid].set()
                if pid in self.pipelines:
                    pipeline = self.pipelines[pid]
                    # Persist cancellation now, including an unstarted re-do's
                    # previous usable state, while an in-flight cloud call may
                    # still be blocked in its HTTP timeout.
                    pipeline.cancel_cloud_queue(p)
                    pipeline.defer_local_queue(p)
                    pipeline.condition.notify_all()
                p['stage'] = '正在暂停；本地超分保留已完成帧。已提交的云端任务可能仍在运行，可稍后续查。'
                self.save(p)
            return self.public(p)

    def resume(self, pid, sid, body):
        with self.lock:
            pipeline = self._queueable(pid)
            p = self.load(pid)
            s = self.shot(p, sid)
            if pipeline and pipeline.contains(sid):
                raise ValueError('本镜已在队列中，请勿重复续查。')
            a = self.attempt(s)
            if not a:
                raise ValueError('本镜尚无原任务可续查。')
            dest = self.destination(pid, s)
            supplied = body.get('task_id')
            if supplied:
                if ark._paths(dest)[0].exists():
                    raise ValueError('已有原任务编号，请直接续查，不要替换编号。')
                if s['status'] not in {'uncertain','awaiting_resume'}:
                    raise ValueError('只有待核对的提交记录可以补充原编号。')
                record = provider.task_record(supplied, a['resolution'], a['seconds'],a.get('generate_audio',False))
                ark._atomic_record(ark._paths(dest)[0], record)
            provider.saved_task(dest, a['resolution'], a['seconds'])
            return self._enqueue(pid, [dict(kind='resume', sid=sid)])

    def trim(self, pid, sid, body):
        with self.lock:
            pipeline = self._queueable(pid)
            p = self.load(pid)
            s = self.shot(p,sid)
            if pipeline and pipeline.contains(sid):
                raise ValueError('本镜已在队列中，请勿重复处理。')
            if not self.destination(pid,s).is_file():
                raise ValueError('本镜尚无生成原片。')
            mode = body.get('upscale_mode', s.get('processing_mode','none'))
            if mode != 'none':
                superresolution.require(mode)
            start = number(body.get('trim_start',0),0,30,'取用起点')
            if pipeline and len(pipeline.local) + bool(pipeline.cloud_active) >= 2:
                raise ValueError('本地处理队列已满，请等一镜完成后再加入。')
            return self._enqueue(pid, [dict(kind='local', sid=sid, mode=mode, trim_start=start)], local=True)

    def import_audio(self,pid,sid,stream=None,filename=None):
        with self.lock:
            self._idle(pid)
            p=self.load(pid);s=self.shot(p,sid)
            clip=self.folder(pid)/'shots'/sid/'clip.mp4'
            if s['status']!='done' or not clip.is_file():
                raise ValueError('请先完成本镜片段，再导入配音或音效。')
            record=None
            if stream is not None:
                suffix=Path(filename or '').suffix.lower()
                if suffix not in {'.wav','.mp3','.m4a','.aac','.flac','.ogg'}:
                    raise ValueError('请选择WAV、MP3、M4A、AAC、FLAC或OGG音频。')
                data=stream.read(30*1024**2+1)
                if not data or len(data)>30*1024**2:
                    raise ValueError('音频文件须为非空文件，最大30MB。')
                folder=clip.parent/'audio';folder.mkdir(exist_ok=True)
                destination=folder/(uuid.uuid4().hex+suffix)
                destination.write_bytes(data)
                info=audio.inspect(destination)
                if info['has_audio'] is not True or not .05<=info['duration']<=600:
                    destination.unlink(missing_ok=True)
                    raise ValueError('音频未通过检查，须含有效音轨且长度为0.05—600秒。')
                record={'file':destination.name,'name':text(str(filename).replace('\\','/').split('/')[-1],160),
                    'attempt':s.get('active_attempt'),'trim_start':s.get('trim_start',0),'duration':info['duration']}
            s.update(status='dubbing',stage='正在本机合成声音，保留当前画面…',error='')
            p.update(status='dubbing',stage='正在本机合成声音，不调用生成 API。')
            self.save(p)
            self._thread(pid,self._run_audio,(pid,sid,record))
            return self.public(p)

    def _run_audio(self,pid,sid,record,stop):
        p=self.load(pid);s=self.shot(p,sid)
        folder=self.folder(pid)/'shots'/sid
        temporary=folder/('sound-'+uuid.uuid4().hex+'.mp4')
        try:
            source=folder/'audio'/record['file'] if record else self.destination(pid,s)
            has_audio=audio.inspect(source)['has_audio'] is True
            if has_audio:
                audio.mux(folder/'clip.mp4',source,temporary,start=0 if record else s.get('trim_start',0),
                          length=s['duration_seconds'],stop=stop)
            else:
                audio.strip(folder/'clip.mp4',temporary,length=s['duration_seconds'],stop=stop)
            if abs(probe(temporary)['duration']-s['duration_seconds'])>1/p['fps']+.025:
                raise ValueError('声音合成后的片段时长不匹配，原有片段已保留。')
            ark._cancelled(stop)
            temporary.replace(folder/'clip.mp4')
            with self.lock:
                s.update(audio_override=record,audio_info={'has_audio':has_audio,
                    'mode':'imported' if record else 'generated' if has_audio else 'silent'},clip_version=uuid.uuid4().hex,
                    stage='已补入本地声音，当前画面保持不变。' if record else '已恢复生成原片的声音状态。')
                p['roughcut_stale']=True
        except Exception as error:
            with self.lock:
                s.update(stage='本地声音处理未完成，原有片段已保留。',error=redact_error(str(error))[:800])
        finally:
            temporary.unlink(missing_ok=True)
            with self.lock:
                s['status']='done';p.update(status='ready',stage='本地声音处理结束，未调用生成 API。')
                self.save(p);self.workers.pop(pid,None);self.stops.pop(pid,None)

    def export(self,pid,body):
        with self.lock:
            self._idle(pid)
            p=self.load(pid)
            ids=body.get('shot_ids')
            if ids is None:
                selected=p['shots']
            elif not isinstance(ids,list) or not ids:
                raise ValueError('请选择要试剪的镜头。')
            else:
                for sid in ids:self.shot(p,sid)
                selected=[s for s in p['shots'] if s['id'] in ids]
            if any(s['status']!='done' for s in selected):
                raise ValueError('要导出的镜头必须全部生成并裁切完成，不会用静帧替代未完成镜头。')
            p.update(status='exporting',stage='正在本地导出试剪，保留各镜声音…')
            self.save(p)
            self._thread(pid,self._run_export,(pid,[s['id'] for s in selected]))
            return self.public(p)

    def _run_export(self,pid,ids,stop):
        p=self.load(pid);folder=self.folder(pid);temporary=folder/'roughcut.tmp.mp4'
        work=folder/('export-'+uuid.uuid4().hex)
        try:
            listing='\n'.join(f"file 'shots/{sid}/clip.mp4'" for sid in ids)+'\n'
            (folder/'concat.txt').write_text(listing,encoding='utf-8')
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','1',
                '-i',str(folder/'concat.txt'),'-map','0:v:0','-an','-c','copy','-movflags','+faststart',str(temporary)],
                check=True,capture_output=True,timeout=600,**quiet_subprocess_kwargs())
            clips=[folder/'shots'/sid/'clip.mp4' for sid in ids]
            has_audio=any(audio.inspect(clip)['has_audio'] is True for clip in clips)
            total=sum(self.shot(p,s)['duration_seconds'] for s in ids)
            if has_audio:
                work.mkdir()
                for sid,clip in zip(ids,clips):
                    ark._cancelled(stop)
                    audio.wav(clip,work/(sid+'.wav'),length=self.shot(p,sid)['duration_seconds'],stop=stop)
                (work/'audio.txt').write_text('\n'.join(f"file '{sid}.wav'" for sid in ids)+'\n',encoding='utf-8')
                audio._run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(temporary),
                    '-f','concat','-safe','1','-i',str(work/'audio.txt'),'-map','0:v:0','-map','1:a:0',
                    '-c:v','copy','-c:a','aac','-b:a','192k','-ar','48000','-ac','2','-t',str(total),
                    '-movflags','+faststart',str(work/'final.mp4')],work/'final.mp4',stop)
                if audio.inspect(work/'final.mp4')['has_audio'] is not True:
                    raise ValueError('试剪声音校验未通过，上一版试剪已保留。')
                (work/'final.mp4').replace(temporary)
            ark._cancelled(stop)
            temporary.replace(folder/'roughcut.mp4')
            p.update(roughcut_shots=ids,roughcut_segments=None,roughcut_edit_revision=None,
                     roughcut_stale=False,roughcut_version=uuid.uuid4().hex,roughcut_duration=sum(self.shot(p,s)['duration_seconds'] for s in ids),
                     roughcut_has_audio=has_audio,
                     stage='已导出试剪并保留声音；无声镜头保持安静。' if has_audio else '已导出无声试剪；当前片段均没有音轨，可本地补声音。',error='')
        except Exception as error:
            p.update(stage='本地导出未完成。',error=redact_error(str(error))[:800])
        finally:
            temporary.unlink(missing_ok=True)
            if work.exists() and work.resolve().is_relative_to(folder.resolve()):
                shutil.rmtree(work)
            with self.lock:
                p['status']='ready';self.save(p);self.workers.pop(pid,None);self.stops.pop(pid,None)

    def shutdown(self):
        with self.lock:
            for stop in self.stops.values():stop.set()
            workers = tuple(self.workers.values())
        self.editing.shutdown()
        superresolution.shutdown()
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=1)


def register(app, studio):
    bp=Blueprint('shortfilms',__name__)

    @bp.get('/api/shortfilms')
    def list_projects():
        return jsonify(projects=studio.list(),missing=provider.missing())

    def respond(action, status=200):
        try:
            return jsonify(action()),status
        except (ValueError,KeyError,TypeError,zipfile.BadZipFile,Image.DecompressionBombError,RecursionError):
            # Give validation details, but never echo an uploaded JSON document or cloud URL.
            import sys
            error=sys.exc_info()[1]
            detail=str(error) if isinstance(error,ValueError) else '分镜包或请求格式不正确，请检查文件。'
            return jsonify(error=redact_error(detail)[:800]),400
        except Exception:
            app.logger.exception('Short film local operation failed')
            return jsonify(error='短片操作未完成，请检查数据目录、FFmpeg和本地日志。'),500

    @bp.post('/api/shortfilms/import')
    def import_project():
        file=request.files.get('package')
        if not file or not file.filename.lower().endswith('.zip'):
            return jsonify(error='请选择含分镜JSON和首帧图片的ZIP。'),400
        return respond(lambda:studio.import_zip(file.stream),201)

    @bp.get('/api/shortfilms/<pid>')
    def project(pid):
        return respond(lambda:studio.public(studio.load(pid)))

    @bp.get('/api/shortfilms/<pid>/manifest')
    def manifest(pid):
        result=respond(lambda:studio.public(studio.load(pid)))
        result[0].headers['Content-Disposition']='attachment; filename="shortfilm_manifest.json"'
        return result

    @bp.post('/api/shortfilms/<pid>/<action>')
    def project_action(pid,action):
        handlers={'start':studio.start,'pause':lambda p,b:studio.pause(p),'export':studio.export}
        if action not in handlers:return jsonify(error='未知操作。'),404
        body=request.get_json(silent=True)
        if not isinstance(body,dict):return jsonify(error='请提供JSON对象。'),400
        return respond(lambda:handlers[action](pid,body),202)

    @bp.route('/api/shortfilms/<pid>/shots/<sid>/<action>',methods=['POST','PUT'])
    def shot_action(pid,sid,action):
        if action=='audio' and request.method=='POST':
            file=request.files.get('audio')
            if not file:return jsonify(error='请选择本地音频文件。'),400
            return respond(lambda:studio.import_audio(pid,sid,file.stream,file.filename),202)
        handlers={'edit':studio.edit,'resume':studio.resume,'trim':studio.trim}
        if action not in handlers:return jsonify(error='未知操作。'),404
        body=request.get_json(silent=True)
        if not isinstance(body,dict):return jsonify(error='请提供JSON对象。'),400
        return respond(lambda:handlers[action](pid,sid,body),200 if action=='edit' else 202)

    @bp.delete('/api/shortfilms/<pid>/shots/<sid>/audio')
    def reset_audio(pid,sid):
        return respond(lambda:studio.import_audio(pid,sid),202)

    @bp.get('/api/shortfilms/<pid>/shots/<sid>/files/<kind>')
    def shot_file(pid,sid,kind):
        try:
            p=studio.load(pid);s=studio.shot(p,sid)
            dest={'frame':studio.folder(pid)/'shots'/sid/'frame.png',
                  'source':studio.destination(pid,s),'clip':studio.folder(pid)/'shots'/sid/'clip.mp4'}.get(kind)
            if dest is None or not dest.is_file():raise ValueError()
            return send_from_directory(dest.parent,dest.name,conditional=True)
        except (ValueError,KeyError):return jsonify(error='文件不存在。'),404

    @bp.get('/api/shortfilms/<pid>/files/<kind>')
    def project_file(pid,kind):
        try:
            studio.load(pid)
            name={'animatic':'animatic.mp4','roughcut':'roughcut.mp4'}.get(kind)
            if name is None:raise ValueError()
            return send_from_directory(studio.folder(pid),name,conditional=True)
        except ValueError:return jsonify(error='文件不存在。'),404

    app.register_blueprint(bp)
    import shortfilm_editing
    shortfilm_editing.register(app,studio)
    import soundtrack_studio
    soundtrack_studio.register(app,studio)
