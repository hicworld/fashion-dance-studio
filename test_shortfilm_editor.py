"""Offline timeline editing contracts and real FFmpeg preview verification."""
import array
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from flask import Flask

import providers_storyboard as provider
import providers_seedance as records
import shortfilm_audio as audio
from shortfilms import ShortFilmStudio, register, probe
from soundtrack_studio import SoundtrackStudio
import soundtrack_mix as mix
from test_shortfilms import package, fake_clip
import test_shortfilms as helpers


def item(sid='S01', start=0, end=1, muted=False):
    return {'id': uuid.uuid4().hex, 'sid': sid, 'in_seconds': start,
            'out_seconds': end, 'muted': muted}


class EditorFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.films = ShortFilmStudio(self.root / 'films')
        self.releases = []
        self.guards = []
        for name in ('generate', 'resume'):
            guard = patch.object(provider, name, side_effect=AssertionError('No cloud API is allowed'))
            self.guards.append(guard.start())
            self.addCleanup(guard.stop)
        availability = patch.object(provider, 'missing', return_value=[])
        availability.start()
        self.addCleanup(availability.stop)
        self.pid = self.films.import_zip(package((1, 1, 1)))['id']
        self.project = self.films.load(self.pid)
        self.app = Flask(__name__)
        register(self.app, self.films)
        self.client = self.app.test_client()
        self.editor = self.films.editing
        self.base = f'/api/shortfilms/{self.pid}/editing'
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        for event in self.releases:
            event.set()
        for group in (self.films, self.editor):
            for stop in list(group.stops.values()):
                stop.set()
        self.join(assert_finished=False)

    def join(self, assert_finished=True):
        deadline = time.monotonic() + 15
        while (self.editor.workers or self.films.workers) and time.monotonic() < deadline:
            for group in (self.editor, self.films):
                for worker in list(group.workers.values()):
                    if worker is not threading.current_thread():
                        worker.join(.1)
        if assert_finished:
            self.assertFalse(self.editor.workers, 'Editing preview worker did not finish')
            self.assertFalse(self.films.workers, 'Video pipeline did not finish')

    def state(self):
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def save(self, items, revision=None):
        if revision is None:
            revision = self.state()['revision']
        return self.client.post(self.base + '/save', json={'expected_revision': revision, 'items': items})

    def render(self):
        response = self.client.post(self.base + '/render', json={'expected_revision': self.state()['revision']})
        self.assertEqual(response.status_code, 202, response.json)
        return response.json

    def gate_compile(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.editor._compile

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(8):
                raise RuntimeError('Test compile gate timed out')
            return original(*args, **kwargs)

        return entered, release, patch.object(self.editor, '_compile', side_effect=blocked)

    def clip(self, sid):
        return self.films.folder(self.pid) / 'shots' / sid / 'clip.mp4'


class EditorApiTests(EditorFixture, unittest.TestCase):
    def test_empty_default_and_draft_roundtrip_preserve_repeats_without_cloud(self):
        initial = self.state()
        self.assertEqual(initial['revision'], 0)
        self.assertEqual(initial['items'], [])
        self.assertEqual(initial['duration_seconds'], 0)
        draft = [item('S03', .25, .75), item('S01'), item('S03', 0, .25, True)]
        response = self.save(draft, 0)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['revision'], 1)
        self.assertEqual([i['sid'] for i in response.json['items']], ['S03', 'S01', 'S03'])
        self.assertAlmostEqual(response.json['duration_seconds'], 1.75)
        plan_file = self.films.folder(self.pid) / 'editing' / 'plan.json'
        self.assertEqual(json.loads(plan_file.read_text(encoding='utf-8'))['revision'], 1)
        reopened = ShortFilmStudio(self.films.root)
        app = Flask('reopened-editor')
        register(app, reopened)
        recovered = app.test_client().get(self.base)
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json['items'], response.json['items'])
        self.assertEqual(recovered.json['revision'], 1)
        for guard in self.guards:
            guard.assert_not_called()

    def test_revision_conflict_does_not_replace_saved_draft(self):
        original = [item('S01')]
        saved = self.save(original, 0)
        self.assertEqual(saved.status_code, 200)
        conflict = self.save([item('S02')], 0)
        self.assertIn(conflict.status_code, (400, 409))
        self.assertEqual(self.state()['items'], saved.json['items'])
        self.assertEqual(self.state()['revision'], 1)

    def test_frame_quantization_is_stable_on_repeat_save(self):
        saved = self.save([item('S01', .13, .87)])
        self.assertEqual(saved.status_code, 200, saved.json)
        cue = saved.json['items'][0]
        for key in ('in_seconds', 'out_seconds'):
            self.assertAlmostEqual(cue[key] * 24, round(cue[key] * 24), places=6)
        next_save = self.save(saved.json['items'])
        self.assertEqual(next_save.status_code, 200, next_save.json)
        self.assertEqual(next_save.json['items'], saved.json['items'])

    def test_rejects_invalid_items_atomically_and_bounds_timeline_size(self):
        good = self.save([item('S02')])
        self.assertEqual(good.status_code, 200)
        duplicate = item('S01')
        bad_lists = (
            [duplicate, copy.deepcopy(duplicate)],
            [item('../S01')], [item('S99')],
            [{**item(), 'id': '../outside'}],
            [item(start=-.1)], [item(start=.75, end=.5)],
            [item(end=1.1)], [item(muted=1)], [item(start=float('nan'))],
            [item(end=float('inf'))], [item(start=True)],
            [item('S01', 0, .001)],
            [item() for _ in range(201)],
        )
        for bad in bad_lists:
            with self.subTest(bad=bad[:2]):
                response = self.save(bad)
                self.assertEqual(response.status_code, 400, response.json)
                self.assertEqual(self.state()['items'], good.json['items'])
                self.assertEqual(self.state()['revision'], good.json['revision'])
        long_id = self.films.import_zip(package((30,)))['id']
        response = self.client.post(f'/api/shortfilms/{long_id}/editing/save', json={
            'expected_revision': 0, 'items': [item('S01', 0, 30) for _ in range(41)]})
        self.assertEqual(response.status_code, 400, response.json)

    def test_unfinished_shot_keeps_draft_but_cannot_render(self):
        draft = [item('S03')]
        self.assertEqual(self.save(draft).status_code, 200)
        response = self.client.post(self.base + '/render', json={'expected_revision': 1})
        self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(self.state()['items'][0]['sid'], 'S03')
        self.assertFalse(self.editor.workers)
        self.assertFalse((self.films.folder(self.pid) / 'roughcut.mp4').exists())

    def test_http_routes_reject_private_files_and_wrong_request_shapes(self):
        for action in ('save', 'render'):
            response = self.client.post(self.base + '/' + action, json=[])
            self.assertEqual(response.status_code, 400, response.json)
        for suffix in ('plan.json', '../plan.json', 'a' * 32 + '/plan.json', 'a' * 32 + '/snapshot-S01.mp4'):
            self.assertEqual(self.client.get(self.base + '/renders/' + suffix).status_code, 404)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class EditorRenderTests(EditorFixture, unittest.TestCase):
    @staticmethod
    def ffmpeg(args):
        return subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *args],
                              check=True, capture_output=True, creationflags=0x08000000)

    def setUp(self):
        super().setUp()
        for number, (shot, color) in enumerate(zip(self.project['shots'], ('red', 'blue', 'green')), 1):
            destination = self.clip(shot['id'])
            inputs = ['-f', 'lavfi', '-i', f'color=c={color}:s=640x360:r=24:d=1']
            if number != 2:
                inputs += ['-f', 'lavfi', '-i', f'sine=frequency={number * 440}:sample_rate=48000:duration=1']
            self.ffmpeg([*inputs, '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                         '-c:a', 'aac', '-t', '1', str(destination)])
            aid = str(number) * 32
            shot.update(status='done', clip_version=aid, active_attempt=aid,
                        attempts=[{'id': aid, 'resolution': '480p', 'seconds': 4}], processing_mode='none')
            raw = self.films.destination(self.pid, shot)
            raw.parent.mkdir(parents=True)
            shutil.copyfile(destination, raw)
        self.films.save(self.project)

    def output(self):
        state = self.state()
        self.assertIsNotNone(state.get('render'), state)
        rid = state['render']['id']
        return self.films.folder(self.pid) / 'editing' / 'renders' / rid / 'preview.mp4'

    def samples(self, path):
        data = self.ffmpeg(['-i', str(path), '-map', '0:a:0', '-ac', '1', '-ar', '48000', '-f', 'f32le', '-']).stdout
        values = array.array('f')
        values.frombytes(data)
        return values

    @staticmethod
    def rms(samples, start, end):
        values = samples[round(start * 48000):round(end * 48000)]
        return (sum(x * x for x in values) / len(values)) ** .5

    def color(self, path, second):
        return self.ffmpeg(['-ss', str(second), '-i', str(path), '-frames:v', '1',
                            '-vf', 'scale=1:1,format=rgb24', '-f', 'rawvideo', '-']).stdout[:3]

    def hashes(self):
        paths = [self.clip(s['id']) for s in self.project['shots']]
        paths += [self.films.destination(self.pid, s) for s in self.project['shots']]
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}

    def test_real_render_reorders_repeats_trims_mutes_and_preserves_originals(self):
        before = self.hashes()
        draft = [item('S02', .25, .75), item('S01', .25, .75, True), item('S01', 0, .5)]
        self.assertEqual(self.save(draft).status_code, 200)
        self.render()
        self.join()
        state = self.state()
        output = self.output()
        self.assertTrue(output.is_file(), state)
        self.assertFalse(state.get('error'), state)
        self.assertTrue(state['render']['published'])
        self.assertFalse(state['render']['stale'])
        self.assertAlmostEqual(probe(output)['duration'], 1.5, delta=.025)
        self.assertEqual((probe(output)['width'], probe(output)['height']), (640, 360))
        self.assertTrue(audio.inspect(output)['has_audio'])
        samples = self.samples(output)
        self.assertLess(self.rms(samples, .08, .42), .001)
        self.assertLess(self.rms(samples, .58, .92), .001)
        self.assertGreater(self.rms(samples, 1.08, 1.42), .01)
        self.assertGreater(self.color(output, .1)[2], 180)
        self.assertGreater(self.color(output, .6)[0], 180)
        self.assertGreater(self.color(output, 1.1)[0], 180)
        self.assertEqual(self.hashes(), before)
        project = self.films.load(self.pid)
        self.assertEqual(project['roughcut_shots'], ['S02', 'S01', 'S01'])
        segments = project['roughcut_segments']
        expected = [('S02', .25, .5, 1.25, 0, False),
                    ('S01', .25, .5, .25, .5, True), ('S01', 0, .5, 0, 1, False)]
        keys = ('shot_id', 'source_start_seconds', 'duration_seconds', 'start_seconds', 'out_start_seconds', 'muted')
        self.assertEqual([tuple(s[k] for k in keys) for s in segments], expected)
        timeline, length = mix.segments(project, 'roughcut')
        self.assertEqual(length, 1.5)
        parts = mix.intersections({'start': .125, 'end': 1.75}, timeline)
        self.assertEqual(parts, [(1.125, .5, 0), (.125, .5, .5), (0, .375, 1.125)])
        response = self.client.get(state['render']['preview_url'])
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_rendering_failure_and_pause_keep_previous_preview_and_roughcut(self):
        self.assertEqual(self.save([item('S01')]).status_code, 200)
        self.render()
        self.join()
        old = self.output()
        old_bytes = old.read_bytes()
        roughcut = self.films.folder(self.pid) / 'roughcut.mp4'
        before = roughcut.read_bytes()
        with patch.object(self.editor, '_compile', side_effect=RuntimeError('Offline encoder failed')):
            self.render()
            self.join()
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(roughcut.read_bytes(), before)
        self.assertTrue(self.state().get('error'))
        entered, release, gate = self.gate_compile()
        with gate:
            self.render()
            self.assertTrue(entered.wait(4))
            paused = self.client.post(self.base + '/pause', json={})
            self.assertEqual(paused.status_code, 202, paused.json)
            release.set()
            self.join()
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(roughcut.read_bytes(), before)
        self.assertFalse(self.state()['busy'])

    def test_editing_draft_while_rendering_does_not_publish_old_revision(self):
        roughcut = self.films.folder(self.pid) / 'roughcut.mp4'
        roughcut.write_bytes(b'previous published rough cut')
        self.assertEqual(self.save([item('S02')]).status_code, 200)
        entered, release, gate = self.gate_compile()
        with gate:
            self.render()
            self.assertTrue(entered.wait(4))
            changed = self.save([item('S01')])
            self.assertEqual(changed.status_code, 200, changed.json)
            release.set()
            self.join()
        state = self.state()
        self.assertEqual(state['revision'], 2)
        self.assertTrue(state['render']['stale'])
        self.assertFalse(state['render']['published'])
        self.assertTrue(self.output().is_file())
        self.assertEqual(roughcut.read_bytes(), b'previous published rough cut')

    def test_replacing_clip_after_snapshot_keeps_preview_stable_and_unpublished(self):
        roughcut = self.films.folder(self.pid) / 'roughcut.mp4'
        roughcut.write_bytes(b'previous published rough cut')
        self.assertEqual(self.save([item('S02')]).status_code, 200)
        entered, release, gate = self.gate_compile()
        with gate:
            self.render()
            self.assertTrue(entered.wait(4))
            replacement = self.clip('S02').with_suffix('.replace.mp4')
            shutil.copyfile(self.clip('S01'), replacement)
            replacement.replace(self.clip('S02'))
            self.project['shots'][1]['clip_version'] = uuid.uuid4().hex
            self.films.save(self.project)
            release.set()
            self.join()
        state = self.state()
        self.assertTrue(state['render']['stale'])
        self.assertFalse(state['render']['published'])
        self.assertGreater(self.color(self.output(), .2)[2], 180, 'Snapshot should still contain the previous blue take')
        self.assertEqual(roughcut.read_bytes(), b'previous published rough cut')

    def test_video_pipeline_and_editor_render_are_independent_but_other_mutations_locked(self):
        sound = SoundtrackStudio(self.films)
        sound.prepare(self.pid)
        for cue in sound.load(self.pid)['cues']:
            cue['enabled'] = False
        sound.save(self.pid, sound.load(self.pid))
        self.assertEqual(self.save([item('S01')]).status_code, 200)
        cloud_entered, release_cloud = threading.Event(), threading.Event()
        self.releases.append(release_cloud)

        def generate(frame, prompt, destination, **kwargs):
            cloud_entered.set()
            if not release_cloud.wait(8):
                raise RuntimeError('Cloud gate timed out')
            return helpers.StudioTests.generate(frame, prompt, destination, **kwargs)

        entered, release_compile, gate = self.gate_compile()
        with gate, patch.object(provider, 'generate', side_effect=generate) as cloud, \
                patch.object(self.films, '_clip', side_effect=fake_clip):
            body = {'confirmed': True, 'shot_ids': ['S02'], 'resolution': '480p',
                    'max_generation_seconds': 4, 'regenerate': True}
            self.films.start(self.pid, body)
            self.assertTrue(cloud_entered.wait(4))
            self.render()
            self.assertTrue(entered.wait(4))
            self.assertIn(self.pid, self.editor.workers)
            self.assertIn(self.pid, self.films.workers)
            body['shot_ids'] = ['S03']
            self.films.start(self.pid, body)
            for operation in (lambda: self.films.export(self.pid, {}),
                              lambda: self.films.edit(self.pid, 'S01', {'video_prompt': 'change'}),
                              lambda: sound.render(self.pid, {'scope': 'full'})):
                with self.assertRaises(ValueError):
                    operation()
            release_cloud.set()
            deadline = time.monotonic() + 4
            while self.films.workers and time.monotonic() < deadline:
                for worker in list(self.films.workers.values()):
                    worker.join(.05)
            self.assertFalse(self.films.workers)
            self.assertIn(self.pid, self.editor.workers)
            self.assertTrue(self.films.public(self.project)['busy'])
            self.assertTrue(self.films.public(self.project)['pipeline']['can_enqueue'])
            # With the generation lane gone, these must still be blocked by
            # the editor itself rather than incidentally by a video worker.
            for operation in (lambda: self.films.export(self.pid, {'shot_ids': ['S01']}),
                              lambda: self.films.edit(self.pid, 'S01', {'video_prompt': 'changed'}),
                              lambda: sound.render(self.pid, {'scope': 'full'})):
                with self.assertRaises(ValueError):
                    operation()
            release_compile.set()
            self.join()
        self.assertEqual(cloud.call_count, 2)
        self.assertTrue(self.state()['render']['published'])

    def test_backup_creation_failure_does_not_delete_previous_roughcut(self):
        roughcut = self.films.folder(self.pid) / 'roughcut.mp4'
        roughcut.write_bytes(b'previous published rough cut must survive failed backup')
        self.assertEqual(self.save([item('S01')]).status_code, 200)
        hardlink, copy_file = Path.hardlink_to, shutil.copyfile

        def fail_backup_link(destination, source):
            if destination.name == 'previous-roughcut.mp4':
                raise OSError('Simulated hardlink failure')
            return hardlink(destination, source)

        def fail_backup_copy(source, destination, *args, **kwargs):
            if Path(destination).name == 'previous-roughcut.mp4':
                raise OSError('Simulated disk full while backing up old output')
            return copy_file(source, destination, *args, **kwargs)

        with patch.object(Path, 'hardlink_to', new=fail_backup_link), \
                patch.object(shutil, 'copyfile', side_effect=fail_backup_copy):
            self.render()
            self.join()
        self.assertEqual(roughcut.read_bytes(), b'previous published rough cut must survive failed backup')
        self.assertTrue(self.state().get('error'))
        self.assertFalse(self.editor.stops)

    def test_plan_commit_failure_rolls_back_new_roughcut_and_preserves_old_preview(self):
        self.assertEqual(self.save([item('S01')]).status_code, 200)
        self.render()
        self.join()
        old_state = self.state()
        old_rid = old_state['render']['id']
        old_preview = self.output()
        old_preview_bytes = old_preview.read_bytes()
        roughcut = self.films.folder(self.pid) / 'roughcut.mp4'
        old_video = roughcut.read_bytes()
        self.assertEqual(self.save([item('S02')]).status_code, 200)
        previous_project = {key: copy.deepcopy(value) for key, value in self.project.items() if key.startswith('roughcut_')}
        persisted = self.editor.persist
        failed = threading.Event()

        def fail_new_render_commit(pid, plan):
            if plan.get('render', {}).get('id') not in (None, old_rid) and not failed.is_set():
                failed.set()
                raise OSError('Simulated editor plan commit failure after new video is ready')
            return persisted(pid, plan)

        with patch.object(self.editor, 'persist', side_effect=fail_new_render_commit):
            self.render()
            self.join()
        self.assertTrue(failed.is_set())
        state = self.state()
        self.assertEqual(state['render']['id'], old_rid)
        self.assertTrue(state.get('error'))
        self.assertEqual(old_preview.read_bytes(), old_preview_bytes)
        self.assertEqual(roughcut.read_bytes(), old_video)
        restored_project = {key: copy.deepcopy(value) for key, value in self.project.items() if key.startswith('roughcut_')}
        self.assertEqual(restored_project, previous_project)
        self.assertFalse(self.editor.stops)

    def test_error_record_save_failure_still_releases_editor_worker(self):
        roughcut = self.films.folder(self.pid) / 'roughcut.mp4'
        roughcut.write_bytes(b'old output')
        self.assertEqual(self.save([item('S01')]).status_code, 200)
        persisted = self.editor.persist
        failed = threading.Event()

        def fail_error_save(pid, plan):
            if plan['status'] == 'idle' and plan.get('error'):
                failed.set()
                raise OSError('Simulated error-record persistence failure')
            return persisted(pid, plan)

        with patch.object(self.editor, 'persist', side_effect=fail_error_save), \
                patch.object(self.editor, '_compile', side_effect=RuntimeError('Offline encoder failed')):
            self.render()
            self.join()
        self.assertTrue(failed.is_set())
        self.assertEqual(roughcut.read_bytes(), b'old output')
        self.assertFalse(self.editor.stops)
        self.assertFalse(self.state()['busy'])

    def test_pause_acknowledged_just_before_publication_keeps_previous_output(self):
        roughcut = self.films.folder(self.pid) / 'roughcut.mp4'
        roughcut.write_bytes(b'previous published video')
        self.assertEqual(self.save([item('S01')]).status_code, 200)
        compiled, checked, release = threading.Event(), threading.Event(), threading.Event()
        self.releases.append(release)
        original_compile, cancelled = self.editor._compile, records._cancelled

        def finish_compile(*args, **kwargs):
            result = original_compile(*args, **kwargs)
            compiled.set()
            return result

        def pause_between_check_and_lock(stop):
            # The first post-compile check succeeds, then the pause request
            # wins the lock. The publication check must observe that pause.
            if compiled.is_set() and not checked.is_set():
                cancelled(stop)
                checked.set()
                if not release.wait(8):
                    raise RuntimeError('Publication race gate timed out')
                return
            cancelled(stop)

        with patch.object(self.editor, '_compile', side_effect=finish_compile), \
                patch.object(records, '_cancelled', side_effect=pause_between_check_and_lock):
            self.render()
            self.assertTrue(checked.wait(6), 'Render did not reach the pre-publication boundary')
            paused = self.client.post(self.base + '/pause', json={})
            self.assertEqual(paused.status_code, 202)
            release.set()
            self.join()
        self.assertEqual(roughcut.read_bytes(), b'previous published video')
        self.assertFalse(self.state().get('render'))

    def test_legacy_export_clears_custom_edit_segments(self):
        self.project['roughcut_segments'] = [{'shot_id': 'S03', 'duration_seconds': .5}]
        self.films.save(self.project)
        self.films.export(self.pid, {'shot_ids': ['S01', 'S02']})
        self.join()
        self.assertFalse(self.project.get('roughcut_segments'))
        self.assertEqual(self.project['roughcut_shots'], ['S01', 'S02'])


if __name__ == '__main__':
    unittest.main()
