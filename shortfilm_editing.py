"""Local, versioned cut lists and snapshot-based preview rendering. No AI API."""
import copy
import json
import math
from pathlib import Path
import re
import shutil
import threading
import uuid

from flask import Blueprint, jsonify, request, send_from_directory

import providers_seedance as records
import shortfilm_audio as audio
import video_superresolution as processes
from provider_common import redact_error

HEX = re.compile(r'[0-9a-f]{32}')


class ShortFilmEditing:
    def __init__(self, films):
        self.films, self.lock = films, films.lock
        self.plans, self.workers, self.stops = {}, {}, {}

    def folder(self, pid):
        return self.films.folder(pid)/'editing'

    def load(self, pid):
        self.films.load(pid)
        if pid not in self.plans:
            path = self.folder(pid)/'plan.json'
            plan = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(
                revision=0, items=[], status='idle', stage='把已完成的片段加入下方时间线。', error='')
            if not isinstance(plan, dict) or not isinstance(plan.get('revision'), int):
                raise ValueError('时间线记录损坏。')
            plan['items'] = self.validate(pid, plan.get('items'))
            if pid not in self.workers and plan.get('status') == 'rendering':
                plan.update(status='idle', stage='上次连续预览被中断；草稿和上一版预览已保留，可重新本地拼接。')
            self.plans[pid] = plan
        return self.plans[pid]

    def persist(self, pid, plan):
        self.folder(pid).mkdir(parents=True, exist_ok=True)
        records._atomic_record(self.folder(pid)/'plan.json', plan)
        self.plans[pid] = plan

    def validate(self, pid, items):
        project = self.films.load(pid)
        if not isinstance(items, list) or len(items) > 200:
            raise ValueError('时间线最多放入200个片段。')
        fps = project['fps']
        used, frames, result = set(), 0, []
        for item in items:
            if not isinstance(item, dict) or set(item)-{'id', 'sid', 'in_seconds', 'out_seconds', 'muted'}:
                raise ValueError('时间线条目字段无效。')
            identifier = item.get('id')
            if not isinstance(identifier, str) or not HEX.fullmatch(identifier) or identifier in used:
                raise ValueError('时间线条目编号必须唯一。')
            used.add(identifier)
            shot = self.films.shot(project, item.get('sid'))
            start, end, muted = item.get('in_seconds'), item.get('out_seconds'), item.get('muted', False)
            if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
                   for n in (start, end)) or type(muted) is not bool:
                raise ValueError('片段入点、出点和静音格式不正确。')
            if start < 0 or end > shot['duration_seconds']+.001 or end <= start:
                raise ValueError(shot['id']+'取用范围超出成片片段时长。')
            left, right = round(start*fps), round(end*fps)
            if right <= left:
                raise ValueError('每个片段至少保留一帧。')
            frames += right-left
            result.append(dict(id=identifier, sid=shot['id'], in_seconds=round(left/fps, 9),
                               out_seconds=round(right/fps, 9), muted=muted))
        if frames/fps > 1200:
            raise ValueError('时间线总时长不能超过20分钟。')
        return result

    def _revision(self, plan, body):
        revision = body.get('expected_revision')
        if type(revision) is not int or revision != plan['revision']:
            raise ValueError('时间线已在别处修改，请读取最新版本后再保存；当前草稿不会自动覆盖。')

    def public(self, pid):
        with self.lock:
            plan = self.load(pid)
            project = self.films.load(pid)
            result = copy.deepcopy(plan)
            result.setdefault('render', None)
            result['busy'] = pid in self.workers
            result['duration_seconds'] = sum(i['out_seconds']-i['in_seconds'] for i in plan['items'])
            render = result.get('render')
            if render:
                render['preview_url'] = f'/api/shortfilms/{pid}/editing/renders/{render["id"]}/preview.mp4'
                render['stale'] = render['revision'] != plan['revision'] or not self._same_clips(project, render['clip_versions'])
                render['published'] = bool(render.get('published') and project.get('roughcut_version') == render.get('roughcut_version'))
            return result

    @staticmethod
    def _same_clips(project, versions):
        shots = {s['id']: s for s in project['shots']}
        return all(sid in shots and shots[sid]['status']=='done' and shots[sid].get('clip_version', '')==version
                   for sid, version in versions.items())

    def save(self, pid, body):
        with self.lock:
            plan = self.load(pid)
            self._revision(plan, body)
            items = self.validate(pid, body.get('items'))
            if items != plan['items']:
                updated = copy.deepcopy(plan)
                updated.update(items=items, revision=plan['revision']+1, error='')
                if pid not in self.workers:
                    updated['stage'] = '时间线草稿已保存在本机。'
                self.persist(pid, updated)
                project = self.films.load(pid)
                if project.get('roughcut_edit_revision') is not None:
                    project['roughcut_stale'] = True
                    self.films.save(project)
            return self.public(pid)

    def render(self, pid, body):
        with self.lock:
            plan = self.load(pid)
            self._revision(plan, body)
            if pid in self.workers:
                raise ValueError('连续预览正在生成，请等待完成或先暂停。')
            if pid in self.films.workers and pid not in self.films.pipelines:
                raise ValueError('项目正在导出或合成声音，请等待该操作结束。')
            if not plan['items']:
                raise ValueError('请先把片段加入时间线。')
            project = self.films.load(pid)
            rid = uuid.uuid4().hex
            work = self.folder(pid)/'renders'/rid
            snapshot = dict(revision=plan['revision'], items=copy.deepcopy(plan['items']), clip_versions={},
                            fps=project['fps'], width=project['width'], height=project['height'])
            snapshot['frames_total'] = sum(round((i['out_seconds']-i['in_seconds'])*project['fps']) for i in snapshot['items'])
            try:
                for item in snapshot['items']:
                    shot = self.films.shot(project, item['sid'])
                    source = self.films.folder(pid)/'shots'/shot['id']/'clip.mp4'
                    if shot['status'] != 'done' or not source.is_file():
                        raise ValueError(shot['id']+'还没有可用的完成片段，请先完成本地处理。')
                    if shot['id'] in snapshot['clip_versions']:
                        continue
                    work.mkdir(parents=True, exist_ok=True)
                    destination = work/('snapshot-'+shot['id']+'.mp4')
                    try:
                        destination.hardlink_to(source)
                    except OSError:
                        shutil.copyfile(source, destination)
                    snapshot['clip_versions'][shot['id']] = shot.get('clip_version', '')
                stop = threading.Event()
                worker = threading.Thread(target=self._run_render, args=(pid, snapshot, work, rid, stop),
                                          daemon=True, name='shortfilm-editor-'+pid[:8])
                plan.update(status='rendering', stage='正在本地拼接时间线，原素材保持不变。', error='')
                self.persist(pid, plan)
                self.stops[pid], self.workers[pid] = stop, worker
                worker.start()
            except Exception:
                self.stops.pop(pid, None)
                self.workers.pop(pid, None)
                plan['status'] = 'idle'
                self._cleanup(work)
                raise
            return self.public(pid)

    def _run_command(self, command, work, stop, name):
        processes.run_process(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *command],
                              stop=stop, log=work/(name+'.log'), cwd=work, timeout=600)

    def _compile(self, snapshot, work, stop, progress):
        fps = snapshot['fps']
        items = snapshot['items']
        length = snapshot['frames_total']/fps
        source_bytes = sum(p.stat().st_size for p in work.glob('snapshot-*.mp4'))
        if shutil.disk_usage(work).free < max(128*1024**2, source_bytes*3+int(length*48000*4*2)):
            raise ValueError('磁盘空间不足，暂时无法安全生成连续预览。')
        info = {sid: audio.inspect(work/('snapshot-'+sid+'.mp4')) for sid in snapshot['clip_versions']}
        has_audio = any(not i['muted'] and info[i['sid']]['has_audio'] is True for i in items)
        for index, item in enumerate(items):
            records._cancelled(stop)
            progress(f'正在拼接第{index+1}/{len(items)}段：{item["sid"]}。生成与超分队列可继续运行。')
            source = 'snapshot-'+item['sid']+'.mp4'
            first, last = round(item['in_seconds']*fps), round(item['out_seconds']*fps)
            count = last-first
            filters = (f'fps={fps},trim=start_frame={first}:end_frame={last},setpts=PTS-STARTPTS,'
                       f'scale={snapshot["width"]}:{snapshot["height"]}:force_original_aspect_ratio=decrease:flags=lanczos,'
                       f'pad={snapshot["width"]}:{snapshot["height"]}:(ow-iw)/2:(oh-ih)/2,setsar=1')
            self._run_command(['-threads','1','-i',source,'-map','0:v:0','-an','-vf',filters,
                               '-frames:v',str(count),'-r',str(fps),'-fps_mode','cfr',
                               '-c:v','libx264','-bf','0','-preset','fast','-crf','18',
                               '-pix_fmt','yuv420p',f'segment-{index:04}.mp4'],work,stop,f'video-{index}')
            if has_audio:
                if item['muted'] or info[item['sid']]['has_audio'] is not True:
                    inputs = ['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
                else:
                    inputs = ['-ss',str(first/fps),'-i',source]
                self._run_command([*inputs,'-vn','-af',f'aresample=48000,apad,atrim=end_sample={round(count/fps*48000)},asetpts=PTS-STARTPTS',
                                   '-ar','48000','-ac','2','-c:a','pcm_s16le',f'audio-{index:04}.wav'],work,stop,f'audio-{index}')
        (work/'video.txt').write_text(''.join(f"file 'segment-{n:04}.mp4'\n" for n in range(len(items))),encoding='utf-8')
        self._run_command(['-f','concat','-safe','1','-i','video.txt','-map','0:v:0','-an',
                           '-c:v','copy','-movflags','+faststart','joined.mp4'],work,stop,'join-video')
        if has_audio:
            (work/'audio.txt').write_text(''.join(f"file 'audio-{n:04}.wav'\n" for n in range(len(items))),encoding='utf-8')
            self._run_command(['-i','joined.mp4','-f','concat','-safe','1','-i','audio.txt',
                               '-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k',
                               '-ar','48000','-ac','2','-t',str(length),'-movflags','+faststart','preview.mp4'],work,stop,'join-audio')
        else:
            (work/'joined.mp4').replace(work/'preview.mp4')
        from shortfilms import probe
        metrics = probe(work/'preview.mp4')
        if abs(metrics['duration']-length) > 1/fps+.005:
            raise ValueError('拼接时长校验未通过，上一版预览已保留。')
        if has_audio and audio.inspect(work/'preview.mp4')['has_audio'] is not True:
            raise ValueError('拼接音轨校验未通过，上一版预览已保留。')
        return dict(has_audio=has_audio)

    def _publish(self, pid, snapshot, work, rid, commit=None):
        project = self.films.load(pid)
        folder = self.films.folder(pid)
        destination = folder/'roughcut.mp4'
        temp = folder/('roughcut-editor-'+rid+'.tmp.mp4')
        backup = work/'previous-roughcut.mp4'
        keys = ('roughcut_shots','roughcut_segments','roughcut_stale','roughcut_version','roughcut_duration',
                'roughcut_has_audio','roughcut_edit_revision')
        previous = {k:copy.deepcopy(project[k]) for k in keys if k in project}
        had_previous = destination.exists()
        replaced = False
        rollback_complete = True
        try:
            if had_previous:
                try:backup.hardlink_to(destination)
                except OSError:shutil.copyfile(destination,backup)
            try:temp.hardlink_to(work/'preview.mp4')
            except OSError:shutil.copyfile(work/'preview.mp4',temp)
            temp.replace(destination)
            replaced = True
            segments, cursor = [], 0
            for item in snapshot['items']:
                shot = self.films.shot(project,item['sid'])
                length = item['out_seconds']-item['in_seconds']
                segments.append(dict(shot_id=shot['id'],source_start_seconds=item['in_seconds'],
                                     start_seconds=shot['start_seconds']+item['in_seconds'],
                                     duration_seconds=length,out_start_seconds=cursor,muted=item['muted']))
                cursor += length
            version = uuid.uuid4().hex
            project.update(roughcut_shots=[i['sid'] for i in snapshot['items']],roughcut_segments=segments,
                           roughcut_stale=False,roughcut_version=version,roughcut_duration=cursor,
                           roughcut_has_audio=audio.inspect(destination)['has_audio'],roughcut_edit_revision=snapshot['revision'])
            self.films.save(project)
            # The editor record and published rough cut form one logical
            # commit. Keep the old video until the new plan is durable too.
            if commit is not None:
                commit(version)
            return version
        except Exception:
            try:
                if replaced:
                    if had_previous:
                        backup.replace(destination)
                    else:
                        destination.unlink(missing_ok=True)
            except Exception:
                # If the OS also refuses rollback, keep the old file in the
                # work directory as recovery evidence instead of deleting it.
                rollback_complete = False
                raise
            finally:
                for key in keys:project.pop(key,None)
                project.update(previous)
                try:self.films.save(project)
                except Exception:pass
            raise
        finally:
            try:temp.unlink(missing_ok=True)
            except OSError:pass
            if rollback_complete:
                try:backup.unlink(missing_ok=True)
                except OSError:pass

    def _run_render(self, pid, snapshot, work, rid, stop):
        success = False
        def progress(stage):
            with self.lock:
                plan=self.load(pid);plan['stage']=stage;self.persist(pid,plan)
        try:
            info = self._compile(snapshot,work,stop,progress)
            records._cancelled(stop)
            with self.lock:
                # Pause may have acquired the lock after the previous check.
                # Serialize its acknowledgement with publication as well.
                records._cancelled(stop)
                plan=self.load(pid);project=self.films.load(pid)
                published=plan['revision']==snapshot['revision'] and self._same_clips(project,snapshot['clip_versions'])
                def commit(version):
                    updated=copy.deepcopy(plan)
                    updated['render']=dict(id=rid,revision=snapshot['revision'],items_snapshot=snapshot['items'],
                                           clip_versions=snapshot['clip_versions'],duration_seconds=snapshot['frames_total']/snapshot['fps'],
                                           published=published,roughcut_version=version,has_audio=bool(info.get('has_audio')))
                    updated.update(status='idle', stage='连续预览已完成，可播放、下载，并用于全片声音合成。' if published else
                                   '上一版时间线的预览已完成；草稿或素材已更新，请再次生成连续预览。',error='')
                    self.persist(pid,updated)
                if published:
                    self._publish(pid,snapshot,work,rid,commit)
                else:
                    commit(None)
                success=True
        except Exception as error:
            with self.lock:
                plan=self.load(pid);plan.update(stage='本地拼接未完成，原素材和上一版预览已保留。',error=redact_error(str(error))[:800])
        finally:
            with self.lock:
                try:
                    if not success:
                        plan=self.load(pid);plan['status']='idle';self.persist(pid,plan)
                except Exception:
                    # Durable recovery can retry after a disk problem. Runtime
                    # locks must be released even when the error cannot be saved.
                    plan=self.plans.get(pid)
                    if plan is not None:
                        plan.update(status='idle',stage='本地拼接记录未能保存，原素材和上一版预览已保留。')
                finally:
                    self.workers.pop(pid,None);self.stops.pop(pid,None)
            if success:
                for path in list(work.iterdir()):
                    if path.name!='preview.mp4':
                        try:path.unlink(missing_ok=True)
                        except OSError:pass
            elif not (work/'previous-roughcut.mp4').exists():
                self._cleanup(work)

    def _cleanup(self, work):
        root = self.films.root.resolve()
        if work.exists() and work.resolve().is_relative_to(root) and HEX.fullmatch(work.name):
            shutil.rmtree(work)

    def pause(self, pid, body=None):
        with self.lock:
            self.load(pid)
            if pid in self.stops:self.stops[pid].set()
            return self.public(pid)

    def shutdown(self):
        with self.lock:
            for stop in self.stops.values():stop.set()
            workers=tuple(self.workers.values())
        for worker in workers:
            if worker is not threading.current_thread():worker.join(timeout=1)


def register(app, films):
    studio=films.editing
    bp=Blueprint('shortfilm_editing',__name__)
    def respond(action,status=200):
        try:return jsonify(action()),status
        except (ValueError,KeyError,TypeError) as error:
            return jsonify(error=redact_error(str(error))[:800] if isinstance(error,ValueError) else '时间线请求格式不正确。'),400
        except Exception:
            app.logger.exception('Local editor operation failed')
            return jsonify(error='本地拼接操作未完成，请检查磁盘空间和文件状态。'),500
    @bp.get('/api/shortfilms/<pid>/editing')
    def state(pid):return respond(lambda:studio.public(pid))
    @bp.post('/api/shortfilms/<pid>/editing/<action>')
    def action(pid,action):
        handlers={'save':studio.save,'render':studio.render,'pause':studio.pause}
        if action not in handlers:return jsonify(error='未知操作。'),404
        body=request.get_json(silent=True)
        if not isinstance(body,dict):return jsonify(error='请提供JSON对象。'),400
        return respond(lambda:handlers[action](pid,body),200 if action=='save' else 202)
    @bp.get('/api/shortfilms/<pid>/editing/renders/<rid>/<name>')
    def output(pid,rid,name):
        if not HEX.fullmatch(rid) or name!='preview.mp4':return jsonify(error='文件不存在。'),404
        return respond_file(pid,rid)
    def respond_file(pid,rid):
        try:
            films.load(pid)
            return send_from_directory(studio.folder(pid)/'renders'/rid,'preview.mp4',conditional=True)
        except ValueError:return jsonify(error='文件不存在。'),404
    app.register_blueprint(bp)
