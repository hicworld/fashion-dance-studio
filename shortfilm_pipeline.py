"""One cloud lane and one local lane; only explicitly queued jobs may submit."""
from collections import deque
import threading
import uuid

import providers_storyboard as provider
import providers_seedance as ark
from providers_jimeng import MotionCancelledError


LOCAL_BACKLOG = 2


def idle_state(can_enqueue=True):
    return dict(active=False, can_enqueue=can_enqueue, cloud_shot_id=None,
                local_shot_id=None, cloud_stage='', local_stage='',
                cloud_queued=[], local_queued=[], stopping=False,
                cloud_halted=False, local_halted=False)


class FilmPipeline:
    def __init__(self, studio, pid):
        self.studio, self.pid = studio, pid
        self.condition = threading.Condition(studio.lock)
        self.cloud, self.local = deque(), deque()
        self.cloud_active = self.local_active = None
        self.cloud_halted = self.local_halted = self.closing = False
        self.stop = None
        self.failures = []

    def accepts(self):
        return not (self.closing or self.cloud_halted or self.local_halted
                    or (self.stop and self.stop.is_set())
                    or (self.studio.stops.get(self.pid) and self.studio.stops[self.pid].is_set()))

    def contains(self, sid):
        return (sid in (self.cloud_active, self.local_active)
                or any(j['sid'] == sid for j in self.cloud)
                or any(j['sid'] == sid for j in self.local))

    def public(self, project):
        def stage(sid):
            return self.studio.shot(project, sid).get('stage', '') if sid else ''
        stopping = bool(self.studio.stops.get(self.pid) and self.studio.stops[self.pid].is_set())
        return dict(active=True, can_enqueue=self.accepts(),
                    cloud_shot_id=self.cloud_active, local_shot_id=self.local_active,
                    cloud_stage=stage(self.cloud_active), local_stage=stage(self.local_active),
                    cloud_queued=[j['sid'] for j in self.cloud],
                    local_queued=[j['sid'] for j in self.local], stopping=stopping,
                    cloud_halted=self.cloud_halted, local_halted=self.local_halted)

    def queue(self, jobs, *, local=False):
        # Caller holds studio.lock. Queue publication and paid attempt creation
        # share that lock, so pause / duplicate checks cannot race submission.
        if not self.accepts():
            raise ValueError('队列正在暂停或已有异常，请等待当前操作结束后再提交。')
        if any(self.contains(j['sid']) for j in jobs):
            raise ValueError('所选镜头已在生成或本地处理队列中，请勿重复加入。')
        if local and len(self.local) + bool(self.cloud_active) >= LOCAL_BACKLOG:
            raise ValueError('本地处理队列已满，请等一镜完成后再加入。')
        project = self.studio.load(self.pid)
        for job in jobs:
            shot = self.studio.shot(project, job['sid'])
            job['previous_status'] = shot['status']
            job['previous_stage'] = shot.get('stage', '')
            shot['queued_from_status'] = shot['status']
            shot['queued_from_stage'] = shot.get('stage', '')
            shot.update(status='waiting_local' if local else 'queued',
                        stage='原片已就绪，等待本地裁切 / AI 超分。' if local else '已确认，等待云端生成队列。', error='')
            (self.local if local else self.cloud).append(job)
        project.update(status='running', stage='云端生成与本地处理分队列运行，可追加其他镜头。')
        self.studio.save(project)
        self.condition.notify_all()

    @staticmethod
    def clear_queued_fields(shot):
        shot.pop('queued_from_status', None)
        shot.pop('queued_from_stage', None)

    def cancel_cloud_queue(self, project):
        while self.cloud:
            job = self.cloud.popleft()
            shot = self.studio.shot(project, job['sid'])
            shot.update(status=job['previous_status'],
                        stage='尚未提交新生成请求；'+job['previous_stage'])
            self.clear_queued_fields(shot)

    def defer_local_queue(self, project):
        while self.local:
            job = self.local.popleft()
            shot = self.studio.shot(project, job['sid'])
            if 'previous_status' in job:
                # A manually queued re-trim has not touched the previous clip
                # or its settings. Cancelling it keeps that finished take usable.
                shot.update(status=job['previous_status'],
                            stage='尚未开始本地重做；'+job['previous_stage'])
            else:
                # A freshly downloaded raw has no finished clip for this take.
                shot.update(status='needs_trim', stage='原片已保留，等待继续本地裁切 / AI 超分；不会重新生成。')
            self.clear_queued_fields(shot)

    def _local_loop(self):
        try:
            self._local_jobs()
        except Exception:
            # Queue setup and persistence can fail before the per-clip handler.
            # Always wake the coordinator, otherwise a dead local worker can
            # leave a nonempty queue / local_active waiting forever.
            with self.condition:
                project = self.studio.projects.get(self.pid)
                self.failures.append((self.local_active or '', 'local-worker'))
                self.local_halted = self.cloud_halted = True
                if project is not None:
                    # The inner finally may have cleared local_active while
                    # unwinding a secondary error in its own error handler.
                    interrupted = [s for s in project['shots']
                                   if s['id'] == self.local_active or s['status'] in {'enhancing', 'trimming'}]
                    for shot in interrupted:
                        shot.update(status='needs_trim' if self.studio.destination(self.pid, shot).is_file() else 'pending',
                                    stage='本地处理记录未能完成，原片和已有成品已保留。')
                    self.cancel_cloud_queue(project)
                    self.defer_local_queue(project)
                    try:
                        self.studio.save(project)
                    except Exception:
                        # A disk failure must not strand the runtime lock. The
                        # last durable attempt / raw remains recoverable on open.
                        pass
                self.local_active = None
                self.condition.notify_all()

    def _local_jobs(self):
        studio, pid = self.studio, self.pid
        while True:
            with self.condition:
                project = studio.load(pid)
                if self.stop.is_set() or self.local_halted:
                    self.defer_local_queue(project)
                    studio.save(project)
                    self.condition.notify_all()
                    return
                if not self.local:
                    if self.closing:
                        return
                    self.condition.wait(.25)
                    continue
                job = self.local.popleft()
                sid = self.local_active = job['sid']
                shot = studio.shot(project, sid)
                self.clear_queued_fields(shot)
                if 'mode' in job:
                    shot.update(processing_mode=job['mode'], trim_start=job['trim_start'])
                shot.update(status='enhancing' if shot.get('processing_mode', 'none') != 'none' else 'trimming',
                            stage='正在本地处理，云端生成可同时进行。')
                studio.save(project)
                self.condition.notify_all()
            try:
                studio._clip(project, shot)
                with self.condition:
                    attempt = studio.attempt(shot)
                    if attempt:
                        attempt['status'] = 'done'
                    studio.save(project)
            except Exception as error:
                with self.condition:
                    self.failures.append((sid, 'local'))
                    self.local_halted = self.cloud_halted = True
                    studio._failure(pid, shot, error)
                    # An already submitted cloud request is allowed to finish
                    # downloading. It is never replaced or retried here.
                    self.cancel_cloud_queue(project)
                    self.defer_local_queue(project)
                    studio.save(project)
            finally:
                with self.condition:
                    self.local_active = None
                    self.condition.notify_all()

    def _cloud_job(self, job):
        studio, pid = self.studio, self.pid
        sid = job['sid']
        with self.condition:
            ark._cancelled(self.stop)
            project = studio.load(pid)
            shot = studio.shot(project, sid)
            self.clear_queued_fields(shot)
            if job['kind'] == 'generate':
                from shortfilms import now
                attempt = dict(id=uuid.uuid4().hex, resolution=job['resolution'], seconds=job['seconds'],
                               prompt=job['prompt'], created_at=now(), status='running',
                               upscale_mode=job['mode'], generate_audio=job['generate_audio'])
                shot['attempts'].append(attempt)
                shot.update(active_attempt=attempt['id'], trim_start=0, processing_mode=job['mode'])
            else:
                attempt = studio.attempt(shot)
            shot.update(status='running', stage='仅续查原任务，不提交新生成请求。' if job['kind']=='resume'
                        else '准备提交首帧生成；本地处理可同时进行。', error='')
            studio.save(project)
        destination = studio.destination(pid, shot)
        try:
            kwargs = dict(resolution=attempt['resolution'], seconds=attempt['seconds'],
                          progress=studio._progress(pid, sid), stop_event=self.stop)
            if job['kind'] == 'resume':
                result = provider.resume(destination, **kwargs)
            else:
                result = provider.generate(studio.folder(pid)/'shots'/sid/'frame.png', attempt['prompt'],
                                           destination, generate_audio=attempt['generate_audio'], **kwargs)
            with self.condition:
                attempt.update(status='downloaded', task_id=result.get('task_id'), usage=result.get('usage', {}))
                if self.stop.is_set() or self.local_halted:
                    shot.update(status='needs_trim', stage='原片已下载保存，可稍后继续本地处理。', error='')
                else:
                    shot.update(status='waiting_local', stage='原片已下载，等待本地裁切 / AI 超分。')
                    self.local.append(dict(sid=sid, kind='local'))
                studio.save(project)
                self.condition.notify_all()
        except Exception as error:
            with self.condition:
                studio._failure(pid, shot, error)
                self.failures.append((sid, 'cloud'))
                self.cloud_halted = True
                self.cancel_cloud_queue(project)
                studio.save(project)
                self.condition.notify_all()

    def run(self, pid, stop):
        studio = self.studio
        self.stop = stop
        local_thread = threading.Thread(target=self._local_loop, daemon=True,
                                        name='shortfilm-local-'+pid[:8])
        local_started = False
        try:
            local_thread.start()
            local_started = True
            while True:
                with self.condition:
                    project = studio.load(pid)
                    if stop.is_set() or self.cloud_halted:
                        self.cancel_cloud_queue(project)
                    if stop.is_set() or self.local_halted:
                        self.defer_local_queue(project)
                    # Bounded local backlog prevents a slow GPU from causing
                    # an entire batch of paid requests to run far ahead.
                    if self.cloud and len(self.local) < LOCAL_BACKLOG and not self.cloud_halted and not stop.is_set():
                        job = self.cloud.popleft()
                        self.cloud_active = job['sid']
                    elif not self.cloud and not self.local and not self.local_active:
                        self.closing = True
                        self.condition.notify_all()
                        break
                    else:
                        self.condition.wait(.25)
                        continue
                try:
                    self._cloud_job(job)
                except MotionCancelledError:
                    with self.condition:
                        # Pause can arrive after dequeue but before an attempt
                        # exists: restore the unsubmitted job without billing.
                        shot = studio.shot(project, job['sid'])
                        shot.update(status=job['previous_status'], stage='已暂停，尚未提交本条请求。')
                        self.clear_queued_fields(shot)
                        studio.save(project)
                finally:
                    with self.condition:
                        self.cloud_active = None
                        self.condition.notify_all()
        except Exception:
            # Stop a coordinator failure from leaving either lane accepting
            # work or waiting forever. Existing request markers remain intact.
            with self.condition:
                self.failures.append(('', 'coordinator'))
                self.cloud_halted = self.local_halted = True
                stop.set()
        finally:
            with self.condition:
                self.closing = True
                self.condition.notify_all()
            if local_started:
                local_thread.join()
            with self.condition:
                try:
                    project = studio.load(pid)
                    self.cancel_cloud_queue(project)
                    self.defer_local_queue(project)
                    paused = stop.is_set() or bool(self.failures)
                    project.update(status='paused' if paused else 'ready', stage=(
                        '队列已暂停；未提交的请求已停止，已有原片和完成帧已保留。' if stop.is_set() else
                        '队列因异常停止新提交；已在途结果和本地成品已保存，请检查对应镜头。' if self.failures else
                        '云端生成与本地处理队列已完成。'))
                    studio.save(project)
                except Exception:
                    project = studio.projects.get(pid)
                    if project is not None:
                        project.update(status='paused', stage='处理结果的最后记录未能保存；原片和成品仍在，请重开软件核对。')
                finally:
                    studio.pipelines.pop(pid, None)
                    studio.workers.pop(pid, None)
                    studio.stops.pop(pid, None)
