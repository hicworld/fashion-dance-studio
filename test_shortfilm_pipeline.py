"""Offline concurrency boundaries for one cloud lane and one local video lane.

Events hold each side at real hand-off boundaries; no provider or GPU is used.
"""
import threading
import time
import tempfile
import unittest
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import providers_seedance as ark
import providers_storyboard as provider
import shortfilm_audio as audio
import video_superresolution as sr
from providers_jimeng import MotionCancelledError, MotionSubmissionUncertainError
from shortfilms import ShortFilmStudio
from soundtrack_studio import SoundtrackStudio
from test_shortfilms import package, fake_clip


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.studio = ShortFilmStudio(Path(self.tmp.name) / 'shortfilms')
        self.release_events = []
        # Availability and audio inspection are unrelated to queue scheduling.
        # Keep all provider credentials, FFmpeg and GPU access outside these tests.
        for obj, name, result in (
            (provider, 'missing', []), (sr, 'require', None),
            (sr, 'availability', {'ready': True}),
            (audio, 'inspect', {'has_audio': False, 'duration': 0}),
        ):
            mock = patch.object(obj, name, return_value=result)
            mock.start()
            self.addCleanup(mock.stop)
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        # Release fixtures even if an assertion fails, before temporary data and
        # patches are removed. Never leave a worker touching the next test.
        for event in self.release_events:
            event.set()
        for stop in list(self.studio.stops.values()):
            stop.set()
        deadline = time.monotonic() + 5
        while self.studio.workers and time.monotonic() < deadline:
            for worker in list(self.studio.workers.values()):
                if worker is not threading.current_thread():
                    worker.join(.1)

    def gate(self):
        event = threading.Event()
        self.release_events.append(event)
        return event

    def imported(self, count=3):
        return self.studio.import_zip(package((.125,) * count))['id']

    @staticmethod
    def batch(ids, **extra):
        return dict(confirmed=True, shot_ids=ids, resolution='480p',
                    upscale_mode='ai-light', generate_audio=False,
                    max_generation_seconds=4 * len(ids), **extra)

    @staticmethod
    def delivered(frame, prompt, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        record = provider.task_record('cgt-offline', kwargs['resolution'],
                                      kwargs['seconds'], kwargs.get('generate_audio', False))
        ark._atomic_record(ark._paths(destination)[0], record)
        destination.write_bytes(b'offline original video')
        return {**record, 'usage': {'completion_tokens': 1}}

    def await_event(self, event, message):
        self.assertTrue(event.wait(4), message)

    def await_state(self, pid, predicate, message):
        deadline = time.monotonic() + 4
        state = None
        while time.monotonic() < deadline:
            state = self.studio.public(self.studio.load(pid))
            if predicate(state):
                return state
            # Short bounded polling is solely for observed public transitions.
            threading.Event().wait(.01)
        self.fail(f'{message}: {state and state.get("pipeline")}')

    def join(self):
        deadline = time.monotonic() + 8
        while self.studio.workers and time.monotonic() < deadline:
            for worker in list(self.studio.workers.values()):
                worker.join(.1)
        self.assertFalse(self.studio.workers, 'Pipeline did not release its project lock')

    def test_cloud_can_submit_next_shot_while_one_local_job_runs(self):
        pid = self.imported(3)
        local_started, second_cloud = threading.Event(), threading.Event()
        release_local, release_cloud = self.gate(), self.gate()
        counters = {'cloud': 0, 'local': 0, 'max_cloud': 0, 'max_local': 0}
        lock = threading.Lock()
        cloud_calls, local_calls = [], []

        def generate(frame, prompt, destination, **kwargs):
            sid = frame.parent.name
            with lock:
                cloud_calls.append(sid)
                counters['cloud'] += 1
                counters['max_cloud'] = max(counters['max_cloud'], counters['cloud'])
            try:
                if sid == 'S02':
                    second_cloud.set()
                    if not release_cloud.wait(5):
                        raise RuntimeError('Test did not release cloud gate')
                return self.delivered(frame, prompt, destination, **kwargs)
            finally:
                with lock:
                    counters['cloud'] -= 1

        def clip(p, s):
            with lock:
                local_calls.append(s['id'])
                counters['local'] += 1
                counters['max_local'] = max(counters['max_local'], counters['local'])
            try:
                if s['id'] == 'S01':
                    local_started.set()
                    if not release_local.wait(5):
                        raise RuntimeError('Test did not release local gate')
                fake_clip(p, s)
            finally:
                with lock:
                    counters['local'] -= 1

        with patch.object(provider, 'generate', side_effect=generate), \
                patch.object(self.studio, '_clip', side_effect=clip):
            self.studio.start(pid, self.batch(['S01', 'S02', 'S03']))
            self.await_event(local_started, 'First local enhancement never started')
            self.await_event(second_cloud, 'Second paid request waited for local enhancement')
            state = self.studio.public(self.studio.load(pid))['pipeline']
            self.assertTrue(state['active'])
            self.assertTrue(state['can_enqueue'])
            self.assertEqual(state['cloud_shot_id'], 'S02')
            self.assertEqual(state['local_shot_id'], 'S01')
            release_cloud.set()
            release_local.set()
            self.join()
        self.assertEqual(cloud_calls, ['S01', 'S02', 'S03'])
        self.assertEqual(local_calls, ['S01', 'S02', 'S03'])
        self.assertEqual((counters['max_cloud'], counters['max_local']), (1, 1))
        self.assertTrue(all(s['status'] == 'done' for s in self.studio.load(pid)['shots']))
        self.assertFalse(self.studio.public(self.studio.load(pid))['pipeline']['active'])

    def test_appended_batch_keeps_own_options_and_rejects_active_duplicates(self):
        pid = self.imported(2)
        first_cloud = threading.Event()
        release = self.gate()

        def generate(frame, prompt, destination, **kwargs):
            if frame.parent.name == 'S01':
                first_cloud.set()
                if not release.wait(5):
                    raise RuntimeError('Cloud gate timed out')
            return self.delivered(frame, prompt, destination, **kwargs)

        with patch.object(provider, 'generate', side_effect=generate) as cloud, \
                patch.object(self.studio, '_clip', side_effect=fake_clip):
            first = self.batch(['S01'])
            first.update(resolution='720p', upscale_mode='none')
            self.studio.start(pid, first)
            self.await_event(first_cloud, 'Initial request never started')
            second = self.batch(['S02'])
            second['generate_audio'] = True
            self.studio.start(pid, second)
            for sid in ('S01', 'S02'):
                duplicate = self.batch([sid])
                duplicate['regenerate'] = True
                with self.assertRaises(ValueError, msg=f'{sid} was admitted twice'):
                    self.studio.start(pid, duplicate)
            # Mutating the caller's request object cannot change the paid queue.
            second.update(resolution='1080p', upscale_mode='none', generate_audio=False)
            state = self.studio.public(self.studio.load(pid))['pipeline']
            self.assertIn('S02', state['cloud_queued'])
            release.set()
            self.join()
        self.assertEqual(cloud.call_count, 2)
        one, two = self.studio.load(pid)['shots']
        self.assertEqual((one['attempts'][0]['resolution'], one['attempts'][0]['upscale_mode'],
                          one['attempts'][0]['generate_audio']), ('720p', 'none', False))
        self.assertEqual((two['attempts'][0]['resolution'], two['attempts'][0]['upscale_mode'],
                          two['attempts'][0]['generate_audio']), ('480p', 'ai-light', True))
        self.assertEqual(cloud.call_args_list[1].kwargs['resolution'], '480p')
        self.assertTrue(cloud.call_args_list[1].kwargs['generate_audio'])

    def test_backpressure_limits_downloaded_waiting_jobs_to_two(self):
        pid = self.imported(5)
        first_local, fourth_cloud = threading.Event(), threading.Event()
        release = self.gate()
        generated = []

        def generate(frame, prompt, destination, **kwargs):
            generated.append(frame.parent.name)
            if frame.parent.name == 'S04':
                fourth_cloud.set()
            return self.delivered(frame, prompt, destination, **kwargs)

        def clip(p, s):
            if s['id'] == 'S01':
                first_local.set()
                if not release.wait(5):
                    raise RuntimeError('Local gate timed out')
            fake_clip(p, s)

        with patch.object(provider, 'generate', side_effect=generate), \
                patch.object(self.studio, '_clip', side_effect=clip):
            self.studio.start(pid, self.batch(['S01', 'S02', 'S03', 'S04', 'S05']))
            self.await_event(first_local, 'First local job never started')
            state = self.await_state(pid, lambda p: len(p['pipeline']['local_queued']) == 2,
                                     'Cloud did not fill exactly two waiting local jobs')
            self.assertEqual(state['pipeline']['local_queued'], ['S02', 'S03'])
            self.assertFalse(fourth_cloud.wait(.2), 'Cloud exceeded its downloaded-raw backlog bound')
            self.assertEqual(generated, ['S01', 'S02', 'S03'])
            release.set()
            self.join()
        self.assertEqual(generated, ['S01', 'S02', 'S03', 'S04', 'S05'])

    def test_pause_stops_future_paid_work_and_keeps_raw_and_local_cache(self):
        pid = self.imported(3)
        first_local, second_cloud = threading.Event(), threading.Event()
        release_cloud, release_local = self.gate(), self.gate()
        cache_files = []

        def generate(frame, prompt, destination, **kwargs):
            if frame.parent.name == 'S02':
                second_cloud.set()
                if not release_cloud.wait(5):
                    raise RuntimeError('Cloud gate timed out')
            # Simulate the already-paid download arriving just after Pause.
            return self.delivered(frame, prompt, destination, **kwargs)

        def clip(p, s):
            path = self.studio.destination(pid, s).parent / 'superresolution' / 'completed-frame.png'
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'valid frame cache placeholder')
            cache_files.append(path)
            first_local.set()
            if not release_local.wait(5):
                raise RuntimeError('Local gate timed out')
            raise MotionCancelledError('Paused after one locally enhanced frame')

        with patch.object(provider, 'generate', side_effect=generate) as cloud, \
                patch.object(self.studio, '_clip', side_effect=clip):
            self.studio.start(pid, self.batch(['S01', 'S02', 'S03']))
            self.await_event(first_local, 'First local job never started')
            self.await_event(second_cloud, 'Second cloud request never started')
            state = self.studio.pause(pid)['pipeline']
            self.assertTrue(state['stopping'])
            self.assertFalse(state['can_enqueue'])
            with self.assertRaises(ValueError):
                self.studio.start(pid, self.batch(['S03']))
            release_cloud.set()
            release_local.set()
            self.join()
        shots = self.studio.load(pid)['shots']
        self.assertEqual(cloud.call_count, 2)
        self.assertTrue(all(self.studio.destination(pid, s).is_file() for s in shots[:2]))
        self.assertTrue(cache_files[0].is_file())
        self.assertTrue(all(s['status'] == 'needs_trim' for s in shots[:2]))
        self.assertEqual(shots[2]['status'], 'pending')
        self.assertFalse(shots[2]['attempts'])

    def test_local_failure_preserves_inflight_cloud_result_without_more_submissions(self):
        pid = self.imported(3)
        local_entered, second_cloud = threading.Event(), threading.Event()
        release_cloud, fail_local = self.gate(), self.gate()
        local_calls = []

        def generate(frame, prompt, destination, **kwargs):
            if frame.parent.name == 'S02':
                second_cloud.set()
                if not release_cloud.wait(5):
                    raise RuntimeError('Cloud gate timed out')
            return self.delivered(frame, prompt, destination, **kwargs)

        def clip(p, s):
            local_calls.append(s['id'])
            local_entered.set()
            if not fail_local.wait(5):
                raise RuntimeError('Failure gate timed out')
            raise RuntimeError('GPU inference failed')

        with patch.object(provider, 'generate', side_effect=generate) as cloud, \
                patch.object(self.studio, '_clip', side_effect=clip):
            self.studio.start(pid, self.batch(['S01', 'S02', 'S03']))
            self.await_event(local_entered, 'First local job never started')
            self.await_event(second_cloud, 'Second cloud request never started')
            fail_local.set()
            state = self.await_state(pid, lambda p: p['pipeline']['local_halted'],
                                     'Local failure did not stop future submission')
            self.assertFalse(state['pipeline']['can_enqueue'])
            self.assertEqual(state['pipeline']['cloud_shot_id'], 'S02')
            release_cloud.set()
            self.join()
        shots = self.studio.load(pid)['shots']
        self.assertEqual(cloud.call_count, 2)
        self.assertEqual(local_calls, ['S01'])
        self.assertEqual([s['status'] for s in shots], ['needs_trim', 'needs_trim', 'pending'])
        self.assertTrue(self.studio.destination(pid, shots[1]).is_file())
        self.assertFalse(shots[2]['attempts'])

    def test_unknown_cloud_failure_allows_downloaded_local_work_to_finish(self):
        pid = self.imported(3)
        local_entered = threading.Event()
        release_local, fail_cloud = self.gate(), self.gate()

        def generate(frame, prompt, destination, **kwargs):
            if frame.parent.name == 'S02':
                if not fail_cloud.wait(5):
                    raise RuntimeError('Cloud failure gate timed out')
                destination.parent.mkdir(parents=True, exist_ok=True)
                ark._atomic_record(ark._paths(destination)[1], {'status': 'submitting'})
                raise MotionSubmissionUncertainError('Paid submission response was lost')
            return self.delivered(frame, prompt, destination, **kwargs)

        def clip(p, s):
            local_entered.set()
            if not release_local.wait(5):
                raise RuntimeError('Local gate timed out')
            fake_clip(p, s)

        with patch.object(provider, 'generate', side_effect=generate) as cloud, \
                patch.object(self.studio, '_clip', side_effect=clip):
            self.studio.start(pid, self.batch(['S01', 'S02', 'S03']))
            self.await_event(local_entered, 'First local job never started')
            fail_cloud.set()
            state = self.await_state(pid, lambda p: p['pipeline']['cloud_halted'],
                                     'Unknown cloud state did not halt paid queue')
            self.assertEqual(state['pipeline']['local_shot_id'], 'S01')
            self.assertFalse(state['pipeline']['can_enqueue'])
            release_local.set()
            self.join()
        shots = self.studio.load(pid)['shots']
        self.assertEqual(cloud.call_count, 2)
        self.assertEqual([s['status'] for s in shots], ['done', 'uncertain', 'pending'])
        self.assertFalse(shots[2]['attempts'])
        with self.assertRaises(ValueError):
            self.studio.start(pid, {**self.batch(['S02']), 'regenerate': True})

    def prepare_attempt(self, pid, sid, *, raw=False, status='awaiting_resume'):
        p = self.studio.load(pid)
        s = self.studio.shot(p, sid)
        aid = ('a' if sid == 'S01' else 'b' if sid == 'S02' else 'c') * 32
        s.update(active_attempt=aid, attempts=[{'id': aid, 'resolution': '480p',
                 'seconds': 4, 'upscale_mode': 'ai-light', 'generate_audio': False}],
                 status=status, processing_mode='ai-light')
        dest = self.studio.destination(pid, s)
        dest.parent.mkdir(parents=True, exist_ok=True)
        ark._atomic_record(ark._paths(dest)[0], provider.task_record('cgt-offline', '480p', 4))
        if raw:
            dest.write_bytes(b'previously downloaded original')
        self.studio.save(p)
        return s

    def test_local_only_trim_and_original_task_resume_share_the_pipeline(self):
        pid = self.imported(3)
        self.prepare_attempt(pid, 'S01', raw=True, status='needs_trim')
        self.prepare_attempt(pid, 'S02')
        local_entered, resumed, third_cloud = (threading.Event() for _ in range(3))
        release_local = self.gate()

        def clip(p, s):
            if s['id'] == 'S01':
                local_entered.set()
                if not release_local.wait(5):
                    raise RuntimeError('Local gate timed out')
            fake_clip(p, s)

        def resume(destination, **kwargs):
            destination.write_bytes(b'resumed original video')
            resumed.set()
            return {'task_id': 'cgt-offline'}

        def generate(frame, prompt, destination, **kwargs):
            third_cloud.set()
            return self.delivered(frame, prompt, destination, **kwargs)

        with patch.object(self.studio, '_clip', side_effect=clip), \
                patch.object(provider, 'resume', side_effect=resume) as original, \
                patch.object(provider, 'generate', side_effect=generate) as cloud:
            self.studio.trim(pid, 'S01', {'upscale_mode': 'ai-light'})
            self.await_event(local_entered, 'Local-only job never entered the shared lane')
            self.studio.resume(pid, 'S02', {})
            self.await_event(resumed, 'Original cloud task was blocked by unrelated local work')
            self.studio.start(pid, self.batch(['S03']))
            self.await_event(third_cloud, 'New request was blocked by local-only enhancement')
            with self.assertRaises(ValueError):
                self.studio.trim(pid, 'S01', {'upscale_mode': 'none'})
            release_local.set()
            self.join()
        original.assert_called_once()
        self.assertEqual(cloud.call_count, 1)
        self.assertTrue(all(s['status'] == 'done' for s in self.studio.load(pid)['shots']))
        self.assertEqual([len(s['attempts']) for s in self.studio.load(pid)['shots']], [1, 1, 1])

    def test_restart_restores_queued_and_downloaded_jobs_without_submission(self):
        pid = self.imported(4)
        p = self.studio.load(pid)
        p['shots'][0]['status'] = 'queued'
        self.prepare_attempt(pid, 'S02', raw=True, status='waiting_local')
        self.prepare_attempt(pid, 'S03', status='running')
        fourth = p['shots'][3]
        fourth.update(active_attempt='d' * 32,
                      attempts=[{'id': 'd' * 32, 'resolution': '480p', 'seconds': 4}],
                      status='enhancing', processing_mode='ai-light')
        raw = self.studio.destination(pid, fourth)
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b'original four')
        cache = raw.parent / 'superresolution' / 'completed.png'
        cache.parent.mkdir()
        cache.write_bytes(b'completed local frame')
        p['status'] = 'running'
        self.studio.save(p)
        with patch.object(provider, 'generate') as generate, patch.object(provider, 'resume') as resume:
            reopened = ShortFilmStudio(self.studio.root)
            state = reopened.load(pid)
            self.assertEqual([s['status'] for s in state['shots']],
                             ['pending', 'needs_trim', 'awaiting_resume', 'needs_trim'])
            self.assertEqual(state['status'], 'paused')
            self.assertFalse(reopened.public(state)['pipeline']['active'])
            self.assertFalse(reopened.workers)
            generate.assert_not_called()
            resume.assert_not_called()
        self.assertTrue(cache.is_file())
        self.assertEqual(raw.read_bytes(), b'original four')

    def test_export_sound_and_edit_remain_exclusive_with_video_pipeline(self):
        pid = self.imported(2)
        sound = SoundtrackStudio(self.studio)
        sound.prepare(pid)
        local_entered = threading.Event()
        release_local = self.gate()

        def clip(p, s):
            local_entered.set()
            if not release_local.wait(5):
                raise RuntimeError('Local gate timed out')
            fake_clip(p, s)

        with patch.object(provider, 'generate', side_effect=self.delivered), \
                patch.object(self.studio, '_clip', side_effect=clip):
            self.studio.start(pid, self.batch(['S01']))
            self.await_event(local_entered, 'Local processing never started')
            operations = (
                lambda: self.studio.export(pid, {'shot_ids': ['S01']}),
                lambda: self.studio.import_audio(pid, 'S01'),
                lambda: self.studio.edit(pid, 'S02', {'video_prompt': 'changed'}),
                lambda: sound.update(pid, {}),
                lambda: sound.generate(pid, {'confirmed': True, 'cue_ids': ['D001']}),
                lambda: sound.render(pid, {'scope': 'full'}),
            )
            for operation in operations:
                with self.assertRaises(ValueError):
                    operation()
            release_local.set()
            self.join()

    def test_ordinary_non_pipeline_worker_still_blocks_new_video_batches(self):
        pid = self.imported(1)
        for status in ('exporting', 'dubbing', 'soundtrack'):
            with self.subTest(status=status):
                self.studio.load(pid)['status'] = status
                # Existing export/sound operations own the same project worker.
                self.studio.workers[pid] = threading.current_thread()
                try:
                    state = self.studio.public(self.studio.load(pid))['pipeline']
                    self.assertFalse(state['can_enqueue'])
                    with self.assertRaises(ValueError):
                        self.studio.start(pid, self.batch(['S01']))
                finally:
                    self.studio.workers.pop(pid, None)

    def test_local_queue_setup_save_failure_cannot_strand_coordinator(self):
        pid = self.imported(1)
        failed = threading.Event()
        save = self.studio.save

        def fail_local_setup(project):
            if threading.current_thread().name.startswith('shortfilm-local-') and not failed.is_set():
                failed.set()
                raise OSError('Simulated local setup persistence failure')
            save(project)

        with patch.object(provider, 'generate', side_effect=self.delivered) as cloud, \
                patch.object(self.studio, 'save', side_effect=fail_local_setup), \
                patch.object(self.studio, '_clip') as clip:
            self.studio.start(pid, self.batch(['S01']))
            self.await_event(failed, 'Local setup fault was not exercised')
            self.join()
            clip.assert_not_called()
        self.assertEqual(cloud.call_count, 1)
        shot = self.studio.load(pid)['shots'][0]
        self.assertEqual(shot['status'], 'needs_trim')
        self.assertTrue(self.studio.destination(pid, shot).is_file())
        self.assertEqual(self.studio.load(pid)['status'], 'paused')
        self.assertFalse(self.studio.pipelines)
        self.assertFalse(self.studio.stops)

    def test_final_save_failure_releases_project_and_keeps_finished_output(self):
        pid = self.imported(1)
        failed = threading.Event()
        save = self.studio.save

        def fail_final_save(project):
            if project['status'] == 'ready' and threading.current_thread().name == 'shortfilm-' + pid[:8]:
                failed.set()
                raise OSError('Simulated final persistence failure')
            save(project)

        with patch.object(provider, 'generate', side_effect=self.delivered), \
                patch.object(self.studio, 'save', side_effect=fail_final_save), \
                patch.object(self.studio, '_clip', side_effect=fake_clip):
            self.studio.start(pid, self.batch(['S01']))
            self.join()
        self.assertTrue(failed.is_set())
        self.assertEqual(self.studio.load(pid)['shots'][0]['status'], 'done')
        self.assertEqual(self.studio.load(pid)['status'], 'paused')
        self.assertIn('未能保存', self.studio.load(pid)['stage'])
        self.assertFalse(self.studio.pipelines)
        self.assertFalse(self.studio.stops)

    def test_pause_durably_restores_unstarted_redos_before_inflight_cloud_returns(self):
        pid = self.imported(5)
        self.prepare_attempt(pid, 'S01', raw=True, status='needs_trim')
        for sid in ('S03', 'S04', 'S05'):
            shot = self.prepare_attempt(pid, sid, raw=True, status='done')
            shot.update(trim_start=.25, processing_mode='none')
            clip = self.studio.folder(pid) / 'shots' / sid / 'clip.mp4'
            clip.write_bytes(b'previous finished clip')
        self.studio.save(self.studio.load(pid))
        local_entered, cloud_entered = threading.Event(), threading.Event()
        release_local, release_cloud = self.gate(), self.gate()

        def clip(project, shot):
            local_entered.set()
            if not release_local.wait(5):
                raise RuntimeError('Local gate timed out')
            raise MotionCancelledError('User paused local processing')

        def generate(frame, prompt, destination, **kwargs):
            cloud_entered.set()
            if not release_cloud.wait(5):
                raise RuntimeError('Cloud gate timed out')
            return self.delivered(frame, prompt, destination, **kwargs)

        with patch.object(provider, 'generate', side_effect=generate) as cloud, \
                patch.object(self.studio, '_clip', side_effect=clip):
            self.studio.trim(pid, 'S01', {'upscale_mode': 'ai-light'})
            self.await_event(local_entered, 'Active local job never started')
            batch = self.batch(['S02', 'S04'])
            batch['regenerate'] = True
            self.studio.start(pid, batch)
            self.await_event(cloud_entered, 'In-flight cloud request never started')
            self.studio.trim(pid, 'S03', {'upscale_mode': 'ai-light', 'trim_start': 1.5})
            third = self.studio.load(pid)['shots'][2]
            self.assertEqual((third['trim_start'], third['processing_mode']), (.25, 'none'))
            # One waiting manual redo plus one active cloud download reserves
            # both slots. Another manual operation must not consume its slot.
            with self.assertRaises(ValueError):
                self.studio.trim(pid, 'S05', {'upscale_mode': 'ai-light'})

            # A crash while those requests are still queued also restores their
            # previous usable take without changing old clip timing metadata.
            restart_root = Path(self.tmp.name) / 'restart-copy'
            shutil.copytree(self.studio.folder(pid), restart_root / pid)
            with patch.object(provider, 'resume') as resume:
                reopened = ShortFilmStudio(restart_root).load(pid)
                self.assertEqual(reopened['shots'][2]['status'], 'done')
                self.assertEqual(reopened['shots'][3]['status'], 'done')
                self.assertEqual((reopened['shots'][2]['trim_start'],
                                  reopened['shots'][2]['processing_mode']), (.25, 'none'))
                resume.assert_not_called()

            self.studio.pause(pid)
            # Read durable JSON while cloud is deliberately still blocked: the
            # pause response, not eventual HTTP completion, must persist this.
            saved = json.loads((self.studio.folder(pid) / 'project.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['shots'][2]['status'], 'done')
            self.assertEqual(saved['shots'][3]['status'], 'done')
            self.assertNotIn('queued_from_status', saved['shots'][3])
            self.assertEqual(len(saved['shots'][3]['attempts']), 1)
            self.assertEqual((saved['shots'][2]['trim_start'], saved['shots'][2]['processing_mode']), (.25, 'none'))
            self.assertFalse(self.studio.public(self.studio.load(pid))['pipeline']['cloud_queued'])
            release_cloud.set()
            release_local.set()
            self.join()
        self.assertEqual(cloud.call_count, 1)
        for sid in ('S03', 'S04'):
            old = self.studio.folder(pid) / 'shots' / sid / 'clip.mp4'
            self.assertEqual(old.read_bytes(), b'previous finished clip')


if __name__ == '__main__':
    unittest.main()
