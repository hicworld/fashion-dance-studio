"""Offline tests: archive boundaries, paid-request identity, recovery, and editing."""
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask
from PIL import Image

import providers_seedance as ark
import providers_storyboard as provider
from providers_jimeng import MotionSubmissionUncertainError, MotionTransientError
from shortfilms import ShortFilmStudio, register, probe


def package(durations=(2,3,5), extra=None):
    frame=io.BytesIO();Image.new('RGB',(640,360),'#567987').save(frame,'PNG')
    shots=[{'id':f'S{i:02}', 'title':f'镜头{i}', 'duration_seconds':d,
            'video_prompt':'人物缓慢抬头，固定机位。','generated_start_frame_file':f'S{i:02}.png',
            'sound':'声音后期制作','post':'文字后期制作'} for i,d in enumerate(durations,1)]
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('分镜.json',json.dumps({'metadata':{'title':'测试短片','fps':24,'delivery_resolution':'640x360'},'shots':shots},ensure_ascii=False))
        for i in range(1,len(shots)+1):z.writestr(f'S{i:02}.png',frame.getvalue())
        if extra:z.writestr(*extra)
    out.seek(0);return out


def fake_clip(p,s):
    s.update(status='done',stage='已裁切')


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.frame=self.root/'frame.png'
        Image.new('RGB',(640,360),'gray').save(self.frame)
        self.dest=self.root/'source.mp4'
        self.env=patch.dict(os.environ,{'ARK_API_KEY':'offline-test-key'});self.env.start();self.addCleanup(self.env.stop)

    def response(self,body=None,status=200):
        return Mock(status_code=status,json=Mock(return_value=body or {'id':'cgt-offline'}))

    def test_first_frame_payload_and_one_post_only(self):
        with patch.object(provider.requests,'post',return_value=self.response()) as post, patch.object(ark,'_poll'):
            info=provider.generate(self.frame,'抬头',self.dest,resolution='720p',seconds=4)
            payload=post.call_args.kwargs['json']
            self.assertEqual(payload['duration'],4);self.assertEqual(payload['ratio'],'adaptive')
            self.assertFalse(payload['generate_audio']);self.assertEqual(payload['content'][1]['role'],'first_frame')
            self.assertTrue(payload['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
            self.assertEqual(info['option'],provider.OPTION)
            saved=provider.saved_task(self.dest,'720p',4)
            self.assertEqual(saved['task_id'],'cgt-offline')
            with self.assertRaises(MotionSubmissionUncertainError):provider.generate(self.frame,'再来',self.dest,resolution='720p',seconds=4)
            self.assertEqual(post.call_count,1)
            provider.resume(self.dest,resolution='720p',seconds=4)
            self.assertEqual(post.call_count,1)
            self.assertNotIn('base64',ark._paths(self.dest)[0].read_text())

    def test_timeout_keeps_marker_and_blocks_repost(self):
        with patch.object(provider.requests,'post',side_effect=TimeoutError) as post:
            for _ in range(2):
                with self.assertRaises(MotionSubmissionUncertainError):provider.generate(self.frame,'动作',self.dest,resolution='720p',seconds=4)
            self.assertEqual(post.call_count,1)
            self.assertTrue(ark._paths(self.dest)[1].exists())

    def test_480p_payload_and_resume_keep_original_resolution(self):
        with patch.object(provider.requests,'post',return_value=self.response()) as post,patch.object(ark,'_poll'):
            info=provider.generate(self.frame,'抬头',self.dest,resolution='480p',seconds=4)
            self.assertEqual(post.call_args.kwargs['json']['resolution'],'480p')
            self.assertEqual(info['resolution'],'480p')
            self.assertEqual(provider.saved_task(self.dest,'480p',4)['resolution'],'480p')
            info=provider.resume(self.dest,resolution='480p',seconds=4)
            self.assertEqual(info['resolution'],'480p');self.assertEqual(post.call_count,1)

    def test_unknown_http_error_is_uncertain_and_cannot_post_again(self):
        with patch.object(provider.requests,'post',return_value=self.response({'error':{'message':'no'}},402)) as post:
            for _ in range(2):
                with self.assertRaises(MotionSubmissionUncertainError):provider.generate(self.frame,'动作',self.dest,resolution='720p',seconds=4)
            self.assertEqual(post.call_count,1)

    def test_wrong_model_identity_cannot_resume(self):
        ark._atomic_record(ark._paths(self.dest)[0],{**provider.task_record('cgt-offline','720p',4),'model':'other-model'})
        with patch.object(provider.requests,'post') as post,patch.object(ark,'_query') as query:
            with self.assertRaises(MotionSubmissionUncertainError):provider.resume(self.dest,resolution='720p',seconds=4)
            post.assert_not_called();query.assert_not_called()

    def test_cancelled_before_submission_creates_no_marker(self):
        stop=threading.Event();stop.set()
        with patch.object(provider.requests,'post') as post:
            with self.assertRaises(Exception):provider.generate(self.frame,'动作',self.dest,resolution='720p',seconds=4,stop_event=stop)
            post.assert_not_called();self.assertFalse(ark._paths(self.dest)[1].exists())


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.studio=ShortFilmStudio(Path(self.tmp.name)/'shortfilms')
        self.env=patch.dict(os.environ,{'ARK_API_KEY':'offline-test-key'});self.env.start();self.addCleanup(self.env.stop)

    def imported(self,durations=(2,3,5)):
        p=self.studio.import_zip(package(durations));return p['id']

    def batch(self,**extra):
        return dict(confirmed=True,shot_ids=['S01','S02','S03'],resolution='720p',max_generation_seconds=15,**extra)

    def join(self):
        for worker in list(self.studio.workers.values()):worker.join(timeout=15);self.assertFalse(worker.is_alive())

    @staticmethod
    def generate(frame,prompt,destination,**kwargs):
        destination.parent.mkdir(parents=True,exist_ok=True)
        record=provider.task_record('cgt-offline',kwargs['resolution'],kwargs['seconds'])
        ark._atomic_record(ark._paths(destination)[0],record)
        destination.write_bytes(b'offline test video')
        return {**record,'usage':{'completion_tokens':42}}

    def test_import_is_offline_and_computes_generation_durations(self):
        with patch.object(provider.requests,'post',side_effect=AssertionError('import must be offline')):
            p=self.studio.import_zip(package())
        self.assertEqual(p['duration_seconds'],10);self.assertEqual(p['generation_seconds'],13)
        self.assertEqual([s['generation_seconds'] for s in p['shots']],[4,4,5])
        self.assertEqual(p['ready_count'],0);self.assertFalse(p['busy'])
        self.assertTrue((self.studio.folder(p['id'])/'shots/S01/frame.png').is_file())

    def test_rejects_archive_traversal_without_writing_outside_project(self):
        with self.assertRaises(ValueError):self.studio.import_zip(package(extra=('../escape.txt','bad')))
        self.assertFalse((Path(self.tmp.name)/'escape.txt').exists())
        self.assertEqual(list(self.studio.root.iterdir()),[])

    def test_rejects_duplicate_basename(self):
        with self.assertRaises(ValueError):self.studio.import_zip(package(extra=('other/S01.png','bad')))

    def test_requires_confirmation_and_enforces_seconds_cap(self):
        pid=self.imported()
        with patch.object(provider,'generate') as generate:
            args=self.batch();args['confirmed']=False
            with self.assertRaises(ValueError):self.studio.start(pid,args)
            args['confirmed']=True;args['max_generation_seconds']=12
            with self.assertRaises(ValueError):self.studio.start(pid,args)
            generate.assert_not_called()

    def test_explicit_batch_and_regeneration_keep_attempt_history(self):
        pid=self.imported()
        with patch.object(provider,'generate',side_effect=self.generate) as generate,patch.object(self.studio,'_clip',side_effect=fake_clip):
            self.studio.start(pid,self.batch());self.join()
            self.assertEqual(generate.call_count,3)
            self.assertTrue(all(s['status']=='done' for s in self.studio.load(pid)['shots']))
            with self.assertRaises(ValueError):self.studio.start(pid,self.batch())
            self.assertEqual(generate.call_count,3)
            args=self.batch();args.update(shot_ids=['S01'],regenerate=True)
            self.studio.start(pid,args);self.join()
            self.assertEqual(generate.call_count,4)
            self.assertEqual(len(self.studio.load(pid)['shots'][0]['attempts']),2)

    def test_480p_batch_records_resolution_for_each_shot(self):
        pid=self.imported();args=self.batch();args['resolution']='480p'
        with patch.object(provider,'generate',side_effect=self.generate) as generate,patch.object(self.studio,'_clip',side_effect=fake_clip):
            self.studio.start(pid,args);self.join()
            self.assertEqual(generate.call_count,3)
            self.assertTrue(all(call.kwargs['resolution']=='480p' for call in generate.call_args_list))
            self.assertTrue(all(s['attempts'][0]['resolution']=='480p' for s in self.studio.load(pid)['shots']))

    def test_query_failure_restores_and_resumes_without_new_generation(self):
        pid=self.imported()
        def submitted(frame,prompt,dest,**kw):
            dest.parent.mkdir(parents=True)
            ark._atomic_record(ark._paths(dest)[0],provider.task_record('cgt-offline','720p',4))
            raise MotionTransientError('query offline')
        args=self.batch();args['shot_ids']=['S01']
        with patch.object(provider,'generate',side_effect=submitted) as generate:
            self.studio.start(pid,args);self.join();self.assertEqual(generate.call_count,1)
            self.assertEqual(self.studio.load(pid)['shots'][0]['status'],'awaiting_resume')
            reloaded=ShortFilmStudio(self.studio.root)
            with patch.object(provider,'resume',return_value={'task_id':'cgt-offline'}) as resume,patch.object(reloaded,'_clip',side_effect=fake_clip):
                reloaded.resume(pid,'S01',{})
                for w in list(reloaded.workers.values()):w.join(10)
                self.assertEqual(reloaded.load(pid)['shots'][0]['status'],'done')
                resume.assert_called_once();self.assertEqual(generate.call_count,1)

    def test_unknown_submission_blocks_paid_regeneration(self):
        pid=self.imported()
        def unknown(frame,prompt,dest,**kw):
            dest.parent.mkdir(parents=True);ark._atomic_record(ark._paths(dest)[1],{'status':'submitting'})
            raise MotionSubmissionUncertainError('response lost')
        args=self.batch();args['shot_ids']=['S01']
        with patch.object(provider,'generate',side_effect=unknown) as generate:
            self.studio.start(pid,args);self.join()
            self.assertEqual(self.studio.load(pid)['shots'][0]['status'],'uncertain')
            args['regenerate']=True
            with self.assertRaises(ValueError):self.studio.start(pid,args)
            self.assertEqual(generate.call_count,1)

    def test_pause_prevents_submitting_next_shot(self):
        pid=self.imported()
        def first(frame,prompt,dest,**kw):
            result=self.generate(frame,prompt,dest,**kw)
            kw['stop_event'].set();return result
        with patch.object(provider,'generate',side_effect=first) as generate:
            self.studio.start(pid,self.batch());self.join()
            self.assertEqual(generate.call_count,1)
            self.assertEqual(self.studio.load(pid)['shots'][1]['status'],'pending')
            self.assertFalse(self.studio.load(pid)['shots'][1]['attempts'])

    def test_http_only_allows_public_file_kinds(self):
        app=Flask(__name__);register(app,self.studio);client=app.test_client()
        response=client.post('/api/shortfilms/import',data={'package':(package(),'film.zip')})
        self.assertEqual(response.status_code,201)
        pid=response.json['id']
        with client.get(f'/api/shortfilms/{pid}/shots/S01/files/frame') as frame:
            self.assertEqual(frame.status_code,200)
        self.assertEqual(client.get(f'/api/shortfilms/{pid}/shots/S01/files/source_task.json').status_code,404)
        self.assertEqual(client.post(f'/api/shortfilms/{pid}/start',json=[]).status_code,400)
        self.assertEqual(client.get('/api/shortfilms').json['projects'][0]['id'],pid)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
    def test_480p_source_exports_1080p_without_changing_original(self):
        pid=self.imported((2,));p=self.studio.load(pid);s=p['shots'][0]
        p.update(width=1920,height=1080)
        aid='b'*32;s.update(active_attempt=aid,attempts=[{'id':aid,'resolution':'480p','seconds':4}])
        dest=self.studio.destination(pid,s);dest.parent.mkdir(parents=True)
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','color=c=blue:s=854x480:r=24:d=4',
                        '-c:v','libx264','-pix_fmt','yuv420p',str(dest)],check=True,capture_output=True)
        self.studio._clip(p,s)
        original=probe(dest);clip=probe(self.studio.folder(pid)/'shots/S01/clip.mp4')
        self.assertEqual((original['width'],original['height']),(854,480))
        self.assertEqual((clip['width'],clip['height']),(1920,1080))
        self.assertAlmostEqual(clip['duration'],2,places=2)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
    def test_real_trim_and_concat_preserve_authored_duration(self):
        pid=self.imported((2,3));p=self.studio.load(pid)
        for s in p['shots']:
            aid='a'*32;s.update(active_attempt=aid,attempts=[{'id':aid,'resolution':'720p','seconds':4}])
            dest=self.studio.destination(pid,s);dest.parent.mkdir(parents=True)
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','color=c=blue:s=640x360:r=24:d=4',
                            '-c:v','libx264','-pix_fmt','yuv420p',str(dest)],check=True,capture_output=True)
            self.studio._clip(p,s)
            self.assertAlmostEqual(probe(dest.parent.parent.parent/'clip.mp4')['duration'],s['duration_seconds'],places=2)
        self.studio.save(p)
        self.studio.export(pid,{})
        self.join()
        self.assertAlmostEqual(probe(self.studio.folder(pid)/'roughcut.mp4')['duration'],5,places=2)
        self.assertEqual(self.studio.load(pid)['roughcut_shots'],['S01','S02'])


if __name__=='__main__':unittest.main()
