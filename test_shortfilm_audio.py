"""Offline regression tests for sound generation and real FFmpeg audio paths."""
import array
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask
import providers_seedance as ark
import providers_storyboard as provider
import shortfilm_audio as audio
import video_superresolution as sr
from shortfilms import ShortFilmStudio, register, probe
from test_shortfilms import package
import test_shortfilms as helpers


class AudioGenerationTests(unittest.TestCase):
    def test_provider_sends_and_records_true_and_resume_keeps_it(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'ARK_API_KEY':'offline-test-key'}):
            root=Path(folder); frame=root/'frame.png'
            from PIL import Image
            Image.new('RGB',(640,360),'gray').save(frame)
            response=Mock(status_code=200,json=Mock(return_value={'id':'cgt-offline'}))
            with patch.object(provider.requests,'post',return_value=response) as post,patch.object(ark,'_poll'):
                result=provider.generate(frame,'test sound',root/'source.mp4',resolution='480p',seconds=4,generate_audio=True)
                self.assertTrue(post.call_args.kwargs['json']['generate_audio'])
                self.assertTrue(result['generate_audio'])
                result=provider.resume(root/'source.mp4',resolution='480p',seconds=4)
                self.assertTrue(result['generate_audio']);self.assertEqual(post.call_count,1)

    def test_batch_snapshots_sound_and_reconciles_standard_silent_instruction(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'ARK_API_KEY':'offline-test-key'}):
            studio=ShortFilmStudio(Path(folder));pid=studio.import_zip(package((.5,)))['id']
            s=studio.load(pid)['shots'][0]
            s.update(video_prompt='不生成字幕、可读文字、配乐或旁白，声音后期制作。主角惊喜。',sound='主角中文台词：“这个也能做？”')
            with patch.object(provider,'generate',side_effect=helpers.StudioTests.generate) as generate,patch.object(studio,'_clip',side_effect=helpers.fake_clip):
                studio.start(pid,{'confirmed':True,'shot_ids':['S01'],'resolution':'480p','max_generation_seconds':4,'generate_audio':True})
                for w in list(studio.workers.values()):w.join(10)
                self.assertEqual(generate.call_count,1)
                self.assertTrue(generate.call_args.kwargs['generate_audio'])
                attempt=s['attempts'][0];self.assertTrue(attempt['generate_audio'])
                self.assertIn('这个也能做',attempt['prompt']);self.assertNotIn('声音后期制作',attempt['prompt'])

    def test_legacy_saved_silent_task_still_resumes_without_post(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.mp4';task=provider.task_record('cgt-old','480p',4);task.pop('generate_audio')
            ark._atomic_record(ark._paths(source)[0],task)
            with patch.object(provider.requests,'post') as post,patch.object(ark,'_poll'):
                self.assertFalse(provider.resume(source,resolution='480p',seconds=4)['generate_audio'])
                post.assert_not_called()


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
class LocalAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.studio=ShortFilmStudio(self.root/'shortfilms')
        self.pid=self.studio.import_zip(package((.5,.5,.5)))['id'];self.p=self.studio.load(self.pid)
        for index,s in enumerate(self.p['shots']):
            aid=hex(index+1)[2:]*32;s.update(active_attempt=aid,attempts=[{'id':aid,'resolution':'480p','seconds':4}],trim_start=.25)
            source=self.studio.destination(self.pid,s);source.parent.mkdir(parents=True)
            inputs=['-f','lavfi','-i','color=c=blue:s=640x360:r=24:d=1']
            if index!=1:inputs+=['-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=1']
            self.run_ffmpeg([*inputs,'-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-t','1',str(source)])
        self.voice=self.root/'voice.wav'
        self.run_ffmpeg(['-f','lavfi','-i','sine=frequency=880:sample_rate=48000:duration=0.2',str(self.voice)])

    @staticmethod
    def run_ffmpeg(arguments):
        return subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',*arguments],
                              check=True,capture_output=True,creationflags=0x08000000)

    def clip(self,sid):return self.studio.folder(self.pid)/'shots'/sid/'clip.mp4'

    def samples(self,path):
        data=self.run_ffmpeg(['-i',str(path),'-map','0:a:0','-ac','1','-ar','48000','-f','f32le','-']).stdout
        samples=array.array('f');samples.frombytes(data);return samples

    @staticmethod
    def rms(samples,start,end):
        values=samples[round(start*48000):round(end*48000)]
        return (sum(x*x for x in values)/len(values))**.5

    def video_hash(self,path):
        return self.run_ffmpeg(['-i',str(path),'-map','0:v:0','-c:v','copy','-f','hash','-hash','sha256','-']).stdout

    def join(self):
        for w in list(self.studio.workers.values()):w.join(15);self.assertFalse(w.is_alive())

    def prepare(self):
        for s in self.p['shots']:self.studio._clip(self.p,s)
        self.studio.save(self.p)

    def test_trim_retains_audible_audio_and_source_bytes(self):
        s=self.p['shots'][0];source=self.studio.destination(self.pid,s)
        before=hashlib.sha256(source.read_bytes()).hexdigest()
        self.studio._clip(self.p,s)
        self.assertTrue(audio.inspect(self.clip('S01'))['has_audio'])
        self.assertGreater(self.rms(self.samples(self.clip('S01')),.1,.4),.01)
        self.assertAlmostEqual(probe(self.clip('S01'))['duration'],.5,places=2)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)

    def test_ai_branch_keeps_source_sound_after_frame_encoding(self):
        s=self.p['shots'][0];s['processing_mode']='ai-light'
        def frame_only(source,output,**kwargs):
            self.run_ffmpeg(['-i',str(source),'-an','-t','0.5','-c:v','libx264',str(output)])
            return {'mode':'ai-light','label':'test frame path','output_width':640,'output_height':360}
        with patch.object(sr,'enhance',side_effect=frame_only),patch.object(provider,'generate') as generate:
            self.studio._clip(self.p,s);generate.assert_not_called()
        self.assertTrue(audio.inspect(self.clip('S01'))['has_audio'])
        self.assertGreater(self.rms(self.samples(self.clip('S01')),.1,.4),.01)

    def test_http_import_and_reset_change_audio_only_without_cloud(self):
        self.prepare();path=self.clip('S02');video=self.video_hash(path)
        app=Flask(__name__);register(app,self.studio);client=app.test_client()
        with patch.object(provider,'generate') as generate,patch.object(provider,'resume') as resume:
            response=client.post(f'/api/shortfilms/{self.pid}/shots/S02/audio',
                data={'audio':(io.BytesIO(self.voice.read_bytes()),'voice.wav')})
            self.assertEqual(response.status_code,202);self.join()
            self.assertEqual(video,self.video_hash(path));self.assertTrue(audio.inspect(path)['has_audio'])
            samples=self.samples(path)
            self.assertGreater(self.rms(samples,.03,.13),.01)
            self.assertLess(self.rms(samples,.35,.48),.001)
            self.assertTrue(self.studio.public(self.p)['shots'][1]['audio_status']['imported'])
            self.assertEqual(client.delete(f'/api/shortfilms/{self.pid}/shots/S02/audio').status_code,202);self.join()
            self.assertFalse(audio.inspect(path)['has_audio']);self.assertEqual(video,self.video_hash(path))
            generate.assert_not_called();resume.assert_not_called()

    def test_export_mixed_audio_and_silent_shots_keeps_timeline(self):
        self.prepare();self.studio.export(self.pid,{});self.join()
        result=self.studio.folder(self.pid)/'roughcut.mp4'
        self.assertTrue(result.is_file(),self.p.get('error'))
        self.assertTrue(audio.inspect(result)['has_audio'])
        self.assertAlmostEqual(probe(result)['duration'],1.5,delta=.025)
        samples=self.samples(result)
        self.assertGreater(self.rms(samples,.1,.4),.01)
        self.assertLess(self.rms(samples,.6,.9),.001)
        self.assertGreater(self.rms(samples,1.1,1.4),.01)

    def test_invalid_audio_upload_leaves_clip_unchanged(self):
        self.prepare();path=self.clip('S02');before=path.read_bytes()
        with self.assertRaises(ValueError):self.studio.import_audio(self.pid,'S02',io.BytesIO(b'not sound'),'fake.wav')
        self.assertEqual(path.read_bytes(),before);self.assertFalse(self.studio.active())


if __name__=='__main__':unittest.main()
