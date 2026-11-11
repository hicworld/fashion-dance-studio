"""Offline failure/recovery tests. Real neural inference is a separate local check."""
import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import providers_storyboard as provider
import video_superresolution as sr
from providers_jimeng import MotionCancelledError
from shortfilms import ShortFilmStudio, probe
from test_shortfilms import package
import test_shortfilms as helpers


class ChildProcessTests(unittest.TestCase):
    def test_pause_kills_and_reaps_local_child_process(self):
        with tempfile.TemporaryDirectory() as folder:
            stop=threading.Event();timer=threading.Timer(.3,stop.set);timer.start()
            began=time.monotonic()
            try:
                with self.assertRaises(MotionCancelledError):
                    sr.run_process([sys.executable,'-c','import time; time.sleep(60)'],
                                   stop=stop,log=Path(folder)/'engine.log')
            finally:
                timer.cancel()
            self.assertLess(time.monotonic()-began,4)
            self.assertFalse(sr.PROCESSES)


class StudioEnhancementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.studio = ShortFilmStudio(Path(self.tmp.name)/'shortfilms')
        self.pid = self.studio.import_zip(package((.125,)))['id']
        self.p = self.studio.load(self.pid); self.s = self.p['shots'][0]
        self.s.update(active_attempt='a'*32, attempts=[{'id':'a'*32,'resolution':'480p','seconds':4}], status='done')
        self.source = self.studio.destination(self.pid,self.s)
        self.source.parent.mkdir(parents=True); self.source.write_bytes(b'original source')
        self.clip = self.studio.folder(self.pid)/'shots/S01/clip.mp4'; self.clip.write_bytes(b'existing clip')
        self.studio.save(self.p)

    def join(self):
        for w in list(self.studio.workers.values()):
            w.join(10); self.assertFalse(w.is_alive())

    def test_local_failure_preserves_source_and_old_clip_without_cloud_call(self):
        with patch.object(sr,'require'), patch.object(sr,'enhance',side_effect=RuntimeError('GPU failed')), \
             patch('shortfilms.probe',return_value={'duration':4,'width':854,'height':480}), \
             patch.object(provider,'generate') as generate, patch.object(provider,'resume') as resume:
            self.studio.trim(self.pid,'S01',{'upscale_mode':'ai-light'}); self.join()
            self.assertEqual(self.s['status'],'needs_trim')
            self.assertEqual(self.clip.read_bytes(),b'existing clip')
            self.assertEqual(self.source.read_bytes(),b'original source')
            generate.assert_not_called(); resume.assert_not_called()

    def test_reopening_local_enhancement_does_not_request_cloud_resume(self):
        self.s.update(status='enhancing',processing_mode='ai-light'); self.p['status']='enhancing'
        self.studio.save(self.p)
        with patch.object(provider,'generate') as generate, patch.object(provider,'resume') as resume:
            reopened=ShortFilmStudio(self.studio.root).load(self.pid)
            self.assertEqual(reopened['shots'][0]['status'],'needs_trim')
            self.assertEqual(reopened['status'],'paused')
            self.assertEqual(reopened['shots'][0]['processing_mode'],'ai-light')
            generate.assert_not_called(); resume.assert_not_called()

    def test_missing_engine_blocks_batch_before_paid_request(self):
        with patch.object(provider,'missing',return_value=[]), patch.object(sr,'require',side_effect=ValueError('missing')), \
             patch.object(provider,'generate') as generate:
            with self.assertRaisesRegex(ValueError,'missing'):
                self.studio.start(self.pid,{'confirmed':True,'shot_ids':['S01'],'resolution':'480p',
                    'upscale_mode':'ai-light','max_generation_seconds':4,'regenerate':True})
            generate.assert_not_called()
            self.assertEqual(len(self.s['attempts']),1)

    def test_automatic_batch_snapshots_ai_choice_and_stops_on_ai_failure(self):
        pid=self.studio.import_zip(package((.125,.125)))['id']
        with patch.object(provider,'missing',return_value=[]), patch.object(sr,'require'), \
             patch.object(provider,'generate',side_effect=helpers.StudioTests.generate) as generate, \
             patch.object(sr,'enhance',side_effect=MotionCancelledError('paused')), \
             patch('shortfilms.probe',return_value={'duration':4,'width':854,'height':480}):
            self.studio.start(pid,{'confirmed':True,'shot_ids':['S01','S02'],'resolution':'480p',
                'upscale_mode':'ai-light','max_generation_seconds':8}); self.join()
            first,second=self.studio.load(pid)['shots']
            self.assertEqual(first['attempts'][0]['upscale_mode'],'ai-light')
            self.assertEqual(first['status'],'needs_trim')
            # Cloud generation and local enhancement now overlap. S02 can
            # already be in flight when S01 fails, but must be saved for local
            # recovery rather than discarded or automatically resubmitted.
            self.assertIn(generate.call_count,(1,2))
            if second['attempts']:
                self.assertEqual(len(second['attempts']),1)
                self.assertEqual(second['attempts'][0]['upscale_mode'],'ai-light')
                self.assertTrue(self.studio.destination(pid,second).is_file())
                self.assertEqual(second['status'],'needs_trim')
            else:
                self.assertEqual(second['status'],'pending')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
class FrameRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.source=self.root/'source.mp4'
        self.output=self.root/'clip.mp4'; self.cache=self.root/'cache'
        # The inference process is simulated; model paths and cache hashes are
        # still exercised with local fixture files rather than a GPU download.
        engine=self.root/'test-engine'
        (engine/'models').mkdir(parents=True)
        for name in ('realesrgan-ncnn-vulkan.exe','vcomp140.dll',
                     'models/realesr-animevideov3-x3.param','models/realesr-animevideov3-x3.bin'):
            (engine/name).write_bytes(b'local test fixture')
        engine_patch=patch.object(sr,'ENGINE_ROOT',engine)
        engine_patch.start(); self.addCleanup(engine_patch.stop)
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i',
                        'testsrc2=s=320x180:r=24:d=0.125','-c:v','libx264','-pix_fmt','yuv420p',str(self.source)],
                       check=True,capture_output=True)
        self.original_hash=hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.calls=[]; self.interrupt=True

    def fake_process(self,command,**kwargs):
        if 'realesrgan-ncnn-vulkan.exe' not in command[0]:
            return self.original_run(command,**kwargs)
        raw=Path(command[command.index('-i')+1]); output=Path(command[command.index('-o')+1])
        names=sorted(raw.glob('*.png')); self.calls.append([p.name for p in names])
        for i,path in enumerate(names):
            with Image.open(path) as im:
                im.resize((im.width*3,im.height*3)).save(output/path.name)
            if self.interrupt and i==0:
                # A half-written PNG must never count as a resumable frame.
                (output/names[1].name).write_bytes(b'partial PNG')
                raise MotionCancelledError('paused')

    def enhance(self,**extra):
        return sr.enhance(self.source,self.output,cache_root=self.cache,mode='ai-light',start=0,
                          frames=3,fps=24,width=640,height=360,**extra)

    def test_partial_resume_reuses_only_valid_frames_and_preserves_timing(self):
        self.original_run=sr.run_process
        with patch.object(sr,'run_process',side_effect=self.fake_process):
            with self.assertRaises(MotionCancelledError):self.enhance()
            self.assertFalse(self.output.exists())
            self.interrupt=False
            evidence=self.enhance()
        self.assertEqual(self.calls,[['00000001.png','00000002.png','00000003.png'],['00000002.png','00000003.png']])
        self.assertEqual(evidence['frames'],3); self.assertEqual(evidence['mode'],'ai-light')
        metrics=probe(self.output)
        self.assertEqual((metrics['width'],metrics['height']),(640,360))
        self.assertAlmostEqual(metrics['duration'],.125,places=3)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),self.original_hash)
        self.assertFalse(list(self.cache.glob('*/enhanced')))

    def test_pre_cancel_creates_no_cache_or_gpu_process(self):
        stop=threading.Event();stop.set()
        with patch.object(sr,'run_process') as run:
            with self.assertRaises(MotionCancelledError):self.enhance(stop=stop)
            run.assert_not_called();self.assertFalse(self.cache.exists())

    def test_low_disk_stops_before_inference(self):
        with patch.object(sr.shutil,'disk_usage',return_value=type('Disk',(),{'free':1})()),patch.object(sr,'run_process') as run:
            with self.assertRaisesRegex(ValueError,'磁盘空间'):self.enhance()
            run.assert_not_called();self.assertFalse(self.output.exists())


if __name__=='__main__':unittest.main()
