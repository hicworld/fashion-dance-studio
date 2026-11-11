"""Offline sound workflow tests, including real sample-accurate FFmpeg mixing."""
import array
import copy
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

from flask import Flask
import app_settings
import providers_soundtrack as provider
from providers_jimeng import MotionSubmissionUncertainError
import shortfilm_audio as audio
from shortfilms import ShortFilmStudio, register, probe
import soundtrack_mix as mix
import soundtrack_script as script
from soundtrack_studio import SoundtrackStudio, binding
from test_shortfilms import package


def tone(seconds=.25,frequency=440,volume=.15):
    output=io.BytesIO();samples=array.array('h',(round(math.sin(i*frequency*2*math.pi/48000)*volume*32767) for i in range(round(seconds*48000))))
    with wave.open(output,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(48000);wav.writeframes(samples.tobytes())
    return output.getvalue()


class ScriptAndProviderTests(unittest.TestCase):
    def test_generic_extractor_ignores_forbidden_quote(self):
        project={'duration_seconds':3,'shots':[{'id':'S01','start_seconds':0,'duration_seconds':3,
            'sound':'先不使用‘余额不足’播报。朋友：‘下楼走走？’'}]}
        plan=script.draft(project)
        spoken=[c['text'] for c in plan['cues'] if c['track']=='dialogue']
        self.assertEqual(spoken,['下楼走走？'])
        self.assertFalse(next(c for c in plan['cues'] if c['track']=='sfx')['enabled'])

    def test_provider_one_post_hex_audio_and_no_repost(self):
        with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,{'MINIMAX_API_KEY':'offline-minimax'}):
            dest=Path(t)/'raw.mp3';cue={'track':'dialogue','text':'你好','role':'主角','emotion':'auto'}
            voices={'主角':{'voice_id':'male-qn-qingse','speed':1}}
            raw=tone();response=Mock(status_code=200,headers={})
            response.iter_content.return_value=[json.dumps({'base_resp':{'status_code':0},'data':{'audio':raw.hex()},
                'extra_info':{'usage_characters':2}}).encode()]
            response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
            with patch.object(provider.requests,'post',return_value=response) as post:
                result=provider.generate(cue,voices,dest)
                self.assertEqual(dest.read_bytes(),raw);self.assertEqual(result['usage']['usage_characters'],2)
                self.assertEqual(post.call_args.kwargs['json']['model'],'speech-2.8-hd')
                with self.assertRaises(MotionSubmissionUncertainError):provider.generate(cue,voices,dest)
                self.assertEqual(post.call_count,1)
                self.assertNotIn('offline-minimax',dest.with_suffix('.submission.json').read_text())

    def test_sfx_payload_loop_duration_and_whisper_model_compatibility(self):
        data=provider.payload({'track':'ambience','text':'fan','generation_seconds':8,'loop':True},{})
        self.assertTrue(data['loop']);self.assertEqual(data['duration_seconds'],8)
        data=provider.payload({'track':'dialogue','text':'你','role':'主角','emotion':'whisper'},
                              {'主角':{'voice_id':'male-qn-qingse','speed':1}})
        self.assertEqual(data['model'],'speech-2.6-hd');self.assertEqual(data['voice_setting']['emotion'],'whisper')


class StudioFixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.films=ShortFilmStudio(self.root/'films')
        self.pid=self.films.import_zip(package((1,1,1)))['id']
        self.sound=SoundtrackStudio(self.films);self.sound.prepare(self.pid)
        self.env=patch.dict(os.environ,{'MINIMAX_API_KEY':'offline-mm','ELEVENLABS_API_KEY':'offline-el'})
        self.env.start();self.addCleanup(self.env.stop)

    @property
    def plan(self):return self.sound.load(self.pid)

    def new(self,track,start,end,text='声音',**extra):
        old={c['id'] for c in self.plan['cues']}
        result=self.sound.add(self.pid,{'track':track})
        cid=next(c['id'] for c in result['cues'] if c['id'] not in old)
        self.sound.update(self.pid,{'cue':{'id':cid,'enabled':True,'start':start,'end':end,'text':text,**extra}})
        return cid

    def join(self):
        for worker in list(self.films.workers.values()):worker.join(20);self.assertFalse(worker.is_alive())

    def request(self,ids,**extra):
        return {'confirmed':True,'cue_ids':ids,'max_characters':300,'max_sfx_seconds':30,**extra}

    def import_tone(self,cid,seconds=.25,frequency=440):
        self.sound.upload(self.pid,cid,io.BytesIO(tone(seconds,frequency)),'test.wav');self.join()
        self.assertFalse(self.plan.get('error'),self.plan.get('error'))


class StudioTests(StudioFixture,unittest.TestCase):
    def test_unconfirmed_or_over_cap_batch_cannot_call_paid_provider(self):
        cid=self.new('dialogue',0,1,'你好')
        with patch.object(provider,'generate') as generate:
            for body in (self.request([cid],confirmed=False),self.request([cid],max_characters=1)):
                with self.assertRaises(ValueError):self.sound.generate(self.pid,body)
            generate.assert_not_called();self.assertFalse(self.sound.cue(self.plan,cid)['attempts'])

    def test_unknown_request_stops_next_and_blocks_automatic_rebilling(self):
        first=self.new('dialogue',0,1,'你好');second=self.new('dialogue',1,2,'再见')
        def unknown(cue,voices,dest,**kw):
            dest.parent.mkdir(parents=True);dest.with_suffix('.submission.json').write_text('{}')
            raise MotionSubmissionUncertainError('response unknown')
        with patch.object(provider,'generate',side_effect=unknown) as generate:
            self.sound.generate(self.pid,self.request([first,second]));self.join()
            self.assertEqual(self.sound.cue(self.plan,first)['status'],'uncertain')
            self.assertFalse(self.sound.cue(self.plan,second)['attempts'])
            with self.assertRaises(ValueError):self.sound.generate(self.pid,self.request([first]))
            self.assertEqual(generate.call_count,1)
        reopened=SoundtrackStudio(ShortFilmStudio(self.films.root))
        self.assertEqual(reopened.public(self.pid)['cues'][-2]['state'],'uncertain')

    def test_cue_validation_rejects_nan_cycles_and_path_injection(self):
        cid=self.new('sfx',0,1,'click');other=self.new('sfx',1,2,'click')
        for changes in ({'start':float('nan')},{'reuse_of':'../../secret'},{'source_offset':-1},{'asset_id':'a'*32}):
            with self.assertRaises(ValueError):self.sound.update(self.pid,{'cue':{'id':cid,**changes}})
        self.sound.update(self.pid,{'cue':{'id':other,'reuse_of':cid}})
        with self.assertRaises(ValueError):self.sound.update(self.pid,{'cue':{'id':cid,'reuse_of':other}})

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
    def test_pause_saved_response_recovers_locally_and_tracks_old_voice_binding(self):
        cid=self.new('dialogue',0,1,'你好')
        def downloaded(cue,voices,dest,**kw):
            dest.parent.mkdir(parents=True);dest.write_bytes(tone(.2));kw['stop'].set();return {'usage':{'usage_characters':2}}
        with patch.object(provider,'generate',side_effect=downloaded) as generate:
            self.sound.generate(self.pid,self.request([cid]));self.join()
            self.assertEqual(self.sound.cue(self.plan,cid)['status'],'needs_process')
            voices=copy.deepcopy(self.plan['voices']);voices['主角']['voice_id']='a-different-voice'
            self.sound.update(self.pid,{'voices':voices})
            self.sound.recover(self.pid,cid);self.join()
            self.assertEqual(self.sound.public(self.pid)['cues'][-1]['state'],'needs_update')
            self.assertEqual(generate.call_count,1)

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
    def test_import_music_assigns_both_regions_and_is_local(self):
        second=self.new('music',2,3,'')
        with patch.object(provider,'generate') as generate:
            self.import_tone('music',.5)
            music=[c for c in self.plan['cues'] if c['track']=='music']
            self.assertEqual(len({c['asset_id'] for c in music}),1)
            generate.assert_not_called()

    def test_http_file_allowlist_and_explicit_confirmation(self):
        app=Flask(__name__);register(app,self.films);client=app.test_client()
        base=f'/api/shortfilms/{self.pid}/soundtrack'
        self.assertTrue(client.get(base).json['created'])
        self.assertEqual(client.post(base+'/generate',json={'confirmed':False}).status_code,400)
        self.assertEqual(client.get(base+'/assets/plan.json').status_code,404)
        self.assertEqual(client.get(base+'/renders/'+'a'*32+'/raw.mp3').status_code,404)
        self.assertEqual(client.post(base+'/save',json=[]).status_code,400)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
class MixTests(StudioFixture,unittest.TestCase):
    def decode(self,path):
        r=subprocess.run(['ffmpeg','-v','error','-i',str(path),'-map','0:a:0','-ac','1','-ar','48000','-f','f32le','-'],
                         check=True,capture_output=True,creationflags=0x08000000)
        values=array.array('f');values.frombytes(r.stdout);return values

    def rms(self,values,start,end):
        portion=values[round(start*48000):round(end*48000)]
        return (sum(v*v for v in portion)/len(portion))**.5

    def direct(self,scope='full',name='render'):
        self.plan['gains']={k:0 for k in script.TRACKS};self.plan['duck_music']=False
        work=self.root/name
        mix.render(self.plan,self.films.load(self.pid),self.sound.folder(self.pid)/'assets',work,scope)
        return work

    def disable_music(self):
        for cue in self.plan['cues']:
            if cue['track']=='music':cue['enabled']=False

    def test_four_stems_hard_stop_reused_sample_and_continuous_ambience(self):
        d=self.new('dialogue',.25,.75,'你好');self.import_tone(d,.4)
        fx=self.new('sfx',.8,1,'click');self.import_tone(fx,.2,880)
        self.new('sfx',2,2.2,'',reuse_of=fx)
        fan=self.new('ambience',0,3,'fan',loop=True);self.import_tone(fan,.5,220)
        self.sound.update(self.pid,{'cue':{'id':'M001','end':1.5,'fade_in':0,'fade_out':0}})
        self.new('music',2.5,3,'',loop=True)
        self.import_tone('music',.5,110)
        work=self.direct()
        for track in (*script.TRACKS,'master'):
            with wave.open(str(work/(track+'.wav')),'rb') as wav:
                self.assertEqual(wav.getnframes(),144000);self.assertEqual(wav.getnchannels(),2)
        dialogue=self.decode(work/'dialogue.wav');effects=self.decode(work/'sfx.wav')
        ambience=self.decode(work/'ambience.wav');music=self.decode(work/'music.wav')
        self.assertLess(self.rms(dialogue,0,.2),.0001);self.assertGreater(self.rms(dialogue,.3,.6),.05)
        self.assertEqual(effects[38400:48000],effects[96000:105600])
        self.assertGreater(self.rms(ambience,1.6,2.4),.05)
        self.assertLess(self.rms(music,1.6,2.4),.0001)
        self.assertGreater(self.rms(music,2.6,2.9),.05)

    def test_trial_remaps_global_events_and_mux_preserves_video_packets(self):
        self.disable_music();effect=self.new('sfx',2.2,2.4,'click');self.import_tone(effect,.2)
        project=self.films.load(self.pid);project.update(roughcut_shots=['S01','S03'],roughcut_stale=False,roughcut_version='test')
        source=self.films.folder(self.pid)/'roughcut.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=640x360:r=24:d=2',
            '-c:v','libx264','-pix_fmt','yuv420p',str(source)],check=True,capture_output=True,creationflags=0x08000000)
        original=source.read_bytes();self.films.save(project);self.sound.save(self.pid,self.plan)
        self.sound.render(self.pid,{'scope':'roughcut'});self.join()
        self.assertFalse(self.plan.get('error'),self.plan.get('error'))
        output=self.sound.folder(self.pid)/'renders'/self.plan['render']['id']/'final.mp4'
        self.assertEqual(source.read_bytes(),original);self.assertTrue(self.plan['render']['video'])
        self.assertAlmostEqual(probe(output)['duration'],2,delta=.03)
        samples=self.decode(output)
        self.assertLess(self.rms(samples,.2,.4),.001)
        self.assertAlmostEqual(self.rms(samples,1.22,1.38),.15/math.sqrt(2)*10**(-8/20),delta=.005)
        def stream_hash(path):
            return subprocess.run(['ffmpeg','-v','error','-i',str(path),'-map','0:v:0','-c:v','copy','-f','hash','-hash','sha256','-'],
                capture_output=True,check=True,creationflags=0x08000000).stdout
        self.assertEqual(stream_hash(source),stream_hash(output))

    def test_music_ducking_reduces_music_under_dialogue(self):
        d=self.new('dialogue',.5,1.5,'hello');self.import_tone(d,1,440)
        self.sound.update(self.pid,{'cue':{'id':'M001','fade_in':0,'fade_out':0}});self.import_tone('music',.5,110)
        work=self.direct(name='plain');plain=self.decode(work/'master.wav');speech=self.decode(work/'dialogue.wav')
        self.plan['duck_music']=True
        other=self.root/'ducked';mix.render(self.plan,self.films.load(self.pid),self.sound.folder(self.pid)/'assets',other,'full')
        ducked=self.decode(other/'master.wav')
        plain_music=array.array('f',(a-b for a,b in zip(plain,speech)))
        duck_music=array.array('f',(a-b for a,b in zip(ducked,speech)))
        self.assertLess(self.rms(duck_music,.8,1.3),self.rms(plain_music,.8,1.3)*.5)

    def test_long_dialogue_not_silently_truncated_and_stale_text_blocked(self):
        self.disable_music();cid=self.new('dialogue',0,.1,'你好');self.import_tone(cid,.4)
        with self.assertRaisesRegex(ValueError,'不会截断台词'):self.direct()
        self.sound.update(self.pid,{'cue':{'id':cid,'end':1,'text':'改过的台词'}})
        with self.assertRaisesRegex(ValueError,'旧版本'):self.sound.render(self.pid,{'scope':'full'})


if __name__=='__main__':unittest.main()
