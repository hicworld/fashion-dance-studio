"""Editable whole-film cue sheets, explicit paid takes and local stem exports."""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import threading
import uuid

from flask import Blueprint, jsonify, request, send_from_directory

import providers_seedance as records
import providers_soundtrack as provider
from provider_common import redact_error
from providers_jimeng import MotionCancelledError
import shortfilm_audio as audio
import soundtrack_mix as mix
import soundtrack_script as script

CUE=re.compile(r'[DFAM][0-9]{3,4}')
HEX=re.compile(r'[0-9a-f]{32}')


def value(number,low,high,label):
    if isinstance(number,bool) or not isinstance(number,(float,int)) or not math.isfinite(number) or not low<=number<=high:
        raise ValueError(label+'超出范围。')
    return number


def content(text,maximum,label,empty=True):
    if not isinstance(text,str) or len(text)>maximum or ('\x00' in text) or (not empty and not text.strip()):
        raise ValueError(label+'格式不正确或内容过长。')
    return text.strip()


def binding(cue,plan):
    fields={k:cue.get(k) for k in ('track','text')}
    if cue['track']=='dialogue':
        fields.update(role=cue['role'],emotion=cue['emotion'],voice=plan['voices'].get(cue['role']))
    return hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class SoundtrackStudio:
    def __init__(self,films):
        self.films=films
        self.lock=films.lock
        self.plans={}

    def folder(self,pid):return self.films.folder(pid)/'soundtrack'

    def load(self,pid):
        self.films.load(pid)
        if pid not in self.plans:
            file=self.folder(pid)/'plan.json'
            if not file.exists():return None
            plan=json.loads(file.read_text(encoding='utf-8'))
            self.validate(plan,self.films.load(pid))
            if pid not in self.films.workers:
                for cue in plan['cues']:
                    if cue.get('status')=='running':
                        raw=self.raw(pid,cue)
                        cue['status']='needs_process' if raw and raw.exists() else 'uncertain' if raw and raw.with_suffix('.submission.json').exists() else 'pending'
                plan['status']='idle'
                if plan.get('stage','').startswith('正在'):
                    plan['stage']='上次处理被中断；已保存素材可继续使用，未知音频请求请先核对平台记录。'
            self.plans[pid]=plan
        return self.plans[pid]

    def save(self,pid,plan):
        self.folder(pid).mkdir(parents=True,exist_ok=True)
        records._atomic_record(self.folder(pid)/'plan.json',plan)
        self.plans[pid]=plan

    def validate(self,plan,project):
        if not isinstance(plan,dict):raise ValueError('声音计划记录损坏。')
        cues=plan.get('cues');voices=plan.get('voices')
        if not isinstance(cues,list) or len(cues)>200:raise ValueError('每个声音计划最多200条。')
        if not isinstance(voices,dict) or len(voices)>20:raise ValueError('最多配置20个角色。')
        for role,voice in voices.items():
            content(role,40,'角色名称',False)
            if not isinstance(voice,dict) or set(voice)!={'voice_id','speed'}:raise ValueError('角色音色设置无效。')
            content(voice.get('voice_id'),200,'音色编号')
            value(voice.get('speed'),.5,2,'语速')
        ids=set()
        for cue in cues:
            if not isinstance(cue,dict) or not CUE.fullmatch(str(cue.get('id',''))) or cue['id'] in ids:
                raise ValueError('声音编号无效或重复。')
            ids.add(cue['id'])
            if cue.get('track') not in script.TRACKS:raise ValueError('轨道类型无效。')
            for flag in ('enabled','loop'):
                if type(cue.get(flag)) is not bool:raise ValueError('声音开关格式无效。')
            content(cue.get('label'),160,'声音名称',False);content(cue.get('text'),2000,'声音内容')
            if cue.get('role') not in voices:raise ValueError('声音角色尚未配置。')
            start=value(cue.get('start'),0,project['duration_seconds'],'开始时间')
            end=value(cue.get('end'),0,project['duration_seconds'],'结束时间')
            if end-start<.02:raise ValueError(cue['id']+'声音区间至少0.02秒。')
            value(cue.get('gain_db'),-60,12,'音量')
            for key in ('fade_in','fade_out'):value(cue.get(key),0,end-start,'淡入淡出时长')
            value(cue.get('source_offset'),0,6000,'素材取用起点')
            value(cue.get('generation_seconds'),.5,30,'音效生成秒数')
            if cue.get('emotion') not in provider.EMOTIONS:raise ValueError('情绪类型无效。')
            if cue['track']=='dialogue' and cue['loop']:raise ValueError('对白不能循环播放。')
            if cue.get('reuse_of') and not CUE.fullmatch(cue['reuse_of']):raise ValueError('复用编号无效。')
            if cue.get('asset_id') and not HEX.fullmatch(cue['asset_id']):raise ValueError('声音素材编号无效。')
            for attempt in cue.get('attempts',[]):
                if not HEX.fullmatch(str(attempt.get('id',''))):raise ValueError('声音生成记录损坏。')
        assets=plan.get('assets')
        if not isinstance(assets,dict):raise ValueError('声音素材记录损坏。')
        for aid,asset in assets.items():
            if not HEX.fullmatch(aid) or asset.get('file')!=aid+'.wav':raise ValueError('声音素材路径无效。')
            value(asset.get('duration'),.01,600,'声音素材长度')
        for cue in cues:
            if cue.get('reuse_of'):mix.resolve_asset(plan,cue)
        if not isinstance(plan.get('gains'),dict):raise ValueError('轨道音量格式无效。')
        for track in script.TRACKS:value(plan.get('gains',{}).get(track),-60,12,'轨道音量')
        if type(plan.get('duck_music')) is not bool:raise ValueError('音乐自动压低开关无效。')
        if plan.get('render') and not HEX.fullmatch(str(plan['render'].get('id',''))):raise ValueError('声音成品记录损坏。')

    def cue(self,plan,cid):
        cue=next((c for c in plan['cues'] if c['id']==cid),None)
        if cue is None:raise ValueError('声音条目不存在。')
        return cue

    def raw(self,pid,cue):
        attempts=cue.get('attempts',[])
        return self.folder(pid)/'requests'/attempts[-1]['id']/'raw.mp3' if attempts else None

    def public(self,pid):
        with self.lock:
            plan=self.load(pid)
            status={'created':plan is not None,'busy':pid in self.films.workers or pid in self.films.editing.workers,
                    'providers':{'dialogue':{'name':'MiniMax Speech','missing':provider.missing('dialogue')},
                                 'sfx':{'name':'ElevenLabs Sound Effects','missing':provider.missing('sfx')}}}
            if plan is None:return status
            result=copy.deepcopy(plan);result.update(status)
            for cue in result['cues']:
                asset=mix.resolve_asset(plan,cue)
                state=cue.get('status','pending')
                if state not in {'running','uncertain','needs_process'}:
                    if asset:
                        source=cue
                        while source.get('reuse_of'):source=self.cue(plan,source['reuse_of'])
                        state='needs_update' if asset.get('binding')!=binding(source,plan) and source['track']!='music' else 'ready'
                        if state=='ready' and cue['track']=='dialogue' and asset['duration']-cue['source_offset']>cue['end']-cue['start']+.035:state='needs_timing'
                    else:state='pending'
                cue['state']=state if cue['enabled'] else 'disabled'
                cue['audio_url']=f'/api/shortfilms/{pid}/soundtrack/assets/{asset["file"]}' if asset else None
                cue['actual_duration']=asset['duration'] if asset else None
            result['ready_count']=sum(c['state']=='ready' for c in result['cues'])
            for asset in result['assets'].values():asset.pop('binding',None)
            render=result.get('render')
            if render:
                base=f'/api/shortfilms/{pid}/soundtrack/renders/{render["id"]}/'
                render['urls']={k:base+k+'.wav' for k in (*script.TRACKS,'master')}
                if render.get('video'):render['urls']['video']=base+'final.mp4'
                render['stale']=(render['revision']!=plan['revision'] or bool(render.get('video') and
                    (render.get('roughcut_version')!=self.films.load(pid).get('roughcut_version') or self.films.load(pid).get('roughcut_stale'))))
            return result

    def prepare(self,pid):
        with self.lock:
            self.films._idle(pid)
            if self.load(pid) is not None:raise ValueError('声音计划已存在，编辑现有条目即可，不会覆盖你的修改。')
            plan=script.draft(self.films.load(pid));self.validate(plan,self.films.load(pid));self.save(pid,plan)
            return self.public(pid)

    def update(self,pid,body):
        with self.lock:
            self.films._idle(pid)
            plan=copy.deepcopy(self.load(pid))
            if plan is None:raise ValueError('请先从脚本建立声音计划。')
            for key in ('voices','gains','duck_music'):
                if key in body:plan[key]=body[key]
            if 'cue' in body:
                changes=body['cue']
                if not isinstance(changes,dict):raise ValueError('声音修改格式无效。')
                cue=self.cue(plan,changes.get('id'))
                allowed={'id','track','start','end','label','text','role','enabled','gain_db','fade_in','fade_out',
                         'source_offset','loop','generation_seconds','emotion','reuse_of'}
                if set(changes)-allowed:raise ValueError('声音修改包含不支持的字段。')
                cue.update(changes)
                if cue['role'] not in plan['voices']:plan['voices'][cue['role']]={'voice_id':'','speed':1}
            plan['revision']=uuid.uuid4().hex
            self.validate(plan,self.films.load(pid));self.save(pid,plan)
            return self.public(pid)

    def add(self,pid,body):
        with self.lock:
            self.films._idle(pid)
            plan=copy.deepcopy(self.load(pid))
            if plan is None:raise ValueError('请先建立声音计划。')
            track=body.get('track','sfx')
            if track not in script.TRACKS:raise ValueError('轨道类型无效。')
            prefix={'dialogue':'D','sfx':'F','ambience':'A','music':'M'}[track]
            used={c['id'] for c in plan['cues']}
            cid=next(prefix+f'{i:03}' for i in range(1,1000) if prefix+f'{i:03}' not in used)
            total=self.films.load(pid)['duration_seconds']
            plan['cues'].append({'id':cid,'track':track,'start':0,'end':min(3,total),'label':'新'+script.TRACKS[track],
                'text':'','role':'主角','enabled':False,'gain_db':0,'fade_in':0,'fade_out':0,'source_offset':0,
                'loop':track in {'ambience','music'},'generation_seconds':min(3,max(.5,total)),
                'emotion':'auto','reuse_of':'','asset_id':None,'attempts':[]})
            self.validate(plan,self.films.load(pid));plan['revision']=uuid.uuid4().hex;self.save(pid,plan)
            return self.public(pid)

    def _normalise(self,pid,plan,cue,raw,aid,usage=None,request_binding=None):
        info=audio.inspect(raw)
        if info['has_audio'] is not True or not .01<info['duration']<=600:raise ValueError('音频文件无效或超过10分钟，原始下载文件已保留。')
        folder=self.folder(pid)/'assets';folder.mkdir(exist_ok=True)
        destination=folder/(aid+'.wav');temporary=folder/(aid+'.tmp.wav')
        try:
            audio._run(['ffmpeg','-hide_banner','-loglevel','error','-y','-protocol_whitelist','file,pipe',
                '-format_whitelist','mov,mp3,wav,aac,flac,ogg','-i',str(raw),'-map','0:a:0','-vn',
                '-c:a','pcm_s16le','-ar','48000','-ac','2',str(temporary)],temporary,self.films.stops.get(pid))
            info=audio.inspect(temporary)
            if info['has_audio'] is not True:raise ValueError('音频格式转换未完成。')
            temporary.replace(destination)
        finally:temporary.unlink(missing_ok=True)
        with self.lock:
            plan['assets'][aid]={'file':destination.name,'duration':info['duration'],
                'binding':request_binding or binding(cue,plan),'usage':usage or {}}
            cue.update(asset_id=aid,status='ready',error='')
            plan['revision']=uuid.uuid4().hex

    def upload(self,pid,cid,stream,filename):
        with self.lock:
            self.films._idle(pid)
            plan=self.load(pid)
            if plan is None:raise ValueError('请先建立声音计划。')
            cue=next((c for c in plan['cues'] if c['track']=='music'),None) if cid=='music' else self.cue(plan,cid)
            if not cue:raise ValueError('没有可导入的配乐条目，请先新增背景音乐。')
            if cue.get('reuse_of'):raise ValueError('请把音频导入被复用的原条目，或先清空复用编号。')
            suffix=Path(filename or '').suffix.lower()
            if suffix not in {'.wav','.mp3','.m4a','.aac','.flac','.ogg'}:raise ValueError('请选择WAV、MP3、M4A、AAC、FLAC或OGG音频。')
            data=stream.read(40*1024**2+1)
            if not data or len(data)>40*1024**2:raise ValueError('音频须为非空文件，最大40MB。')
            aid=uuid.uuid4().hex;folder=self.folder(pid)/'imports';folder.mkdir(exist_ok=True)
            raw=folder/(aid+suffix);raw.write_bytes(data)
            info=audio.inspect(raw)
            if info['has_audio'] is not True or not .01<info['duration']<=600:
                raw.unlink(missing_ok=True);raise ValueError('音频未通过检查，须含有效音轨且长度不超过10分钟。')
            plan.update(status='importing',stage='正在本机导入声音，保留原片…',error='')
            self.save(pid,plan);self.films._thread(pid,self._run_import,(pid,cue['id'],raw,aid,cid=='music'))
            return self.public(pid)

    def _run_import(self,pid,cid,raw,aid,all_music,stop):
        plan=self.load(pid)
        try:
            cue=self.cue(plan,cid)
            attempt=next((a for a in cue['attempts'] if a['id']==aid),None)
            self._normalise(pid,plan,cue,raw,aid,attempt.get('usage') if attempt else None,
                            attempt.get('binding') if attempt else None)
            if all_music:
                with self.lock:
                    for music in plan['cues']:
                        if music['track']=='music':music.update(asset_id=aid,reuse_of='',status='ready',error='')
            if attempt:attempt['status']='done'
            plan['stage']='声音导入完成；未调用生成 API。'
        except Exception as error:plan.update(stage='声音导入未完成，原有素材保持可用。',error=redact_error(str(error)))
        finally:self._finish(pid,plan)

    def generate(self,pid,body):
        with self.lock:
            self.films._idle(pid);plan=self.load(pid)
            if plan is None:raise ValueError('请先建立声音计划。')
            if body.get('confirmed') is not True:raise ValueError('请确认所选音频请求会产生费用。')
            ids=body.get('cue_ids')
            if not isinstance(ids,list) or not 1<=len(ids)<=50 or len(set(map(str,ids)))!=len(ids):raise ValueError('每批请选择1—50条不同的声音。')
            selected=[self.cue(plan,cid) for cid in ids];characters=0;seconds=0
            for cue in selected:
                if not cue['enabled'] or cue['track']=='music' or cue.get('reuse_of'):raise ValueError(cue['id']+'不能直接生成，请选择启用的原始对白或音效条目。')
                content(cue['text'],2000,'声音内容',False)
                if provider.missing(cue['track']):raise ValueError(provider.missing(cue['track'])[0])
                provider.payload(cue,plan['voices'])
                if cue.get('attempts') and body.get('regenerate') is not True:
                    raise ValueError(cue['id']+'已有请求记录，请先核对平台记录；另付费重做须明确勾选。')
                if cue.get('asset_id') and body.get('regenerate') is not True:raise ValueError(cue['id']+'已有素材，重做须明确勾选另付费生成。')
                if cue['track']=='dialogue':characters+=len(cue['text'])
                else:seconds+=cue['generation_seconds']
            if characters>value(body.get('max_characters'),0,10000,'对白字符上限'):raise ValueError('本批对白字符数超过你设置的上限。')
            if seconds>value(body.get('max_sfx_seconds'),0,1500,'音效生成秒数上限'):raise ValueError('本批音效生成秒数超过你设置的上限。')
            plan.update(status='generating',stage='正在依次生成所选声音…',error='')
            self.save(pid,plan);self.films._thread(pid,self._run_generate,(pid,ids))
            return self.public(pid)

    def _run_generate(self,pid,ids,stop):
        plan=self.load(pid);cue=None
        try:
            for cid in ids:
                if stop.is_set():break
                cue=self.cue(plan,cid);aid=uuid.uuid4().hex
                attempt={'id':aid,'status':'running','request':provider.payload(cue,plan['voices']),'binding':binding(cue,plan)}
                with self.lock:
                    cue['attempts'].append(attempt);cue.update(status='running',error='')
                    plan['stage']='正在生成 '+cid+' · '+cue['label'];self.save(pid,plan)
                raw=self.folder(pid)/'requests'/aid/'raw.mp3'
                result=provider.generate(cue,plan['voices'],raw,stop=stop)
                with self.lock:
                    attempt.update(status='downloaded',usage=result.get('usage',{}));self.save(pid,plan)
                if stop.is_set():raise MotionCancelledError('声音已下载，已暂停。可继续本地处理，不重新请求生成。')
                self._normalise(pid,plan,cue,raw,aid,result.get('usage'))
                attempt['status']='done'
                with self.lock:self.save(pid,plan)
                if cue['track']=='dialogue' and plan['assets'][aid]['duration']>cue['end']-cue['start']+.035:
                    raise ValueError(cid+'对白超过预留时段，音频已保存。请先调整时间或语速；未继续提交后续声音。')
            plan['stage']='声音生成已暂停。' if stop.is_set() else '所选声音已生成，请试听后本地混音。'
        except Exception as error:
            with self.lock:
                if cue and cue.get('status')=='running':
                    raw=self.raw(pid,cue)
                    cue['status']='needs_process' if raw and raw.exists() else 'uncertain' if raw and raw.with_suffix('.submission.json').exists() else 'pending'
                    cue['error']=redact_error(str(error));cue['attempts'][-1]['status']=cue['status']
                plan.update(stage='本批声音已暂停，未继续提交后续请求。',error=redact_error(str(error)))
        finally:self._finish(pid,plan)

    def recover(self,pid,cid):
        with self.lock:
            self.films._idle(pid);plan=self.load(pid)
            if plan is None:raise ValueError('请先建立声音计划。')
            cue=self.cue(plan,cid);raw=self.raw(pid,cue)
            if raw is None or not raw.is_file():raise ValueError('没有已下载的音频可本地继续，请核对平台记录。')
            plan.update(status='importing',stage='正在本地处理已下载音频，不调用生成 API。',error='')
            self.save(pid,plan);self.films._thread(pid,self._run_import,(pid,cid,raw,cue['attempts'][-1]['id'],False))
            return self.public(pid)

    def render(self,pid,body):
        with self.lock:
            self.films._idle(pid);plan=self.load(pid);project=self.films.load(pid)
            if plan is None:raise ValueError('请先建立声音计划。')
            scope=body.get('scope','full');timeline,_=mix.segments(project,scope)
            for cue in plan['cues']:
                if cue['enabled'] and mix.intersections(cue,timeline):
                    asset=mix.resolve_asset(plan,cue)
                    if not asset:raise ValueError(cue['id']+'还没有音频，请生成、导入或关闭这条声音。')
                    original=cue
                    while original.get('reuse_of'):original=self.cue(plan,original['reuse_of'])
                    if original['track']!='music' and asset.get('binding')!=binding(original,plan):
                        raise ValueError(cue['id']+'文字或音色已修改，现有音频是旧版本。请重新生成或导入对应音频。')
            plan.update(status='rendering',stage='正在制作全片分轨和混音…',error='')
            self.save(pid,plan);self.films._thread(pid,self._run_render,(pid,scope))
            return self.public(pid)

    def _run_render(self,pid,scope,stop):
        plan=self.load(pid);project=self.films.load(pid)
        rid=uuid.uuid4().hex;work=self.folder(pid)/'renders'/rid
        work.parent.mkdir(exist_ok=True);success=False
        def progress(stage):
            with self.lock:plan['stage']=stage;self.save(pid,plan)
        try:
            result=mix.render(plan,project,self.folder(pid)/'assets',work,scope,stop=stop,progress=progress)
            roughcut=self.films.folder(pid)/'roughcut.mp4'
            all_ids=[s['id'] for s in project['shots']]
            video=roughcut.is_file() and not project.get('roughcut_stale') and (scope=='roughcut' or
                (project.get('roughcut_shots')==all_ids and abs(project.get('roughcut_duration',0)-project['duration_seconds'])<.02))
            if video:
                from shortfilms import probe
                if abs(probe(roughcut)['duration']-result['duration'])>1/project['fps']+.03:
                    raise ValueError('试剪实际时长与镜头记录不匹配，请先重新导出。')
                progress('正在把完整混音合成到试剪视频，原试剪保持不变…')
                audio.mux(roughcut,work/'master.wav',work/'final.mp4',start=0,length=result['duration'],stop=stop)
            if stop.is_set():raise MotionCancelledError('本地声音制作已暂停，上一版成品已保留。')
            with self.lock:
                plan['render']={**result,'id':rid,'revision':plan['revision'],'video':bool(video),
                    'roughcut_version':project.get('roughcut_version') if video else None}
                plan['stage']='分轨、混音及带声音视频已完成。' if video else '全片分轨和混音已完成。完整画面尚未就绪，可先下载音轨试听。'
                success=True
        except Exception as error:plan.update(stage='本地混音未完成，上一版成品已保留。',error=redact_error(str(error)))
        finally:
            if not success and work.exists() and work.resolve().is_relative_to((self.folder(pid)/'renders').resolve()):shutil.rmtree(work)
            self._finish(pid,plan)

    def _finish(self,pid,plan):
        with self.lock:
            plan['status']='idle';self.save(pid,plan)
            self.films.workers.pop(pid,None);self.films.stops.pop(pid,None)


def register(app,films):
    studio=SoundtrackStudio(films);films.soundtrack=studio
    bp=Blueprint('soundtrack',__name__)
    def respond(action,status=200):
        try:return jsonify(action()),status
        except (ValueError,KeyError,TypeError,StopIteration):
            import sys
            error=sys.exc_info()[1]
            return jsonify(error=redact_error(str(error)) if isinstance(error,ValueError) else '声音请求格式不正确。'),400
        except Exception:
            app.logger.exception('Soundtrack operation failed')
            return jsonify(error='声音操作未完成，请检查本地日志和素材文件。'),500

    @bp.get('/api/shortfilms/<pid>/soundtrack')
    def public(pid):return respond(lambda:studio.public(pid))

    @bp.post('/api/shortfilms/<pid>/soundtrack/<action>')
    def action(pid,action):
        if action=='music':
            file=request.files.get('audio')
            if not file:return jsonify(error='请选择本地音乐文件。'),400
            return respond(lambda:studio.upload(pid,'music',file.stream,file.filename),202)
        handlers={'prepare':lambda body:studio.prepare(pid),'save':lambda body:studio.update(pid,body),
                  'add':lambda body:studio.add(pid,body),'generate':lambda body:studio.generate(pid,body),
                  'render':lambda body:studio.render(pid,body),
                  'pause':lambda body:pause(pid),'voices':lambda body:{'voices':provider.voices()}}
        if action not in handlers:return jsonify(error='未知声音操作。'),404
        body=request.get_json(silent=True)
        if not isinstance(body,dict):return jsonify(error='请提供JSON对象。'),400
        return respond(lambda:handlers[action](body),202 if action in {'generate','render'} else 200)

    def pause(pid):
        films.pause(pid)
        with films.lock:
            plan=studio.load(pid)
            if plan:
                plan['stage']='已请求暂停；已发出的声音请求可能仍会完成，请稍后核对。';studio.save(pid,plan)
        return studio.public(pid)

    @bp.post('/api/shortfilms/<pid>/soundtrack/cues/<cid>/<action>')
    def cue_action(pid,cid,action):
        if action=='upload':
            file=request.files.get('audio')
            if not file:return jsonify(error='请选择本地音频。'),400
            return respond(lambda:studio.upload(pid,cid,file.stream,file.filename),202)
        if action=='recover':return respond(lambda:studio.recover(pid,cid),202)
        return jsonify(error='未知声音操作。'),404

    @bp.get('/api/shortfilms/<pid>/soundtrack/assets/<filename>')
    def asset_file(pid,filename):
        try:
            plan=studio.load(pid)
            if not re.fullmatch(r'[0-9a-f]{32}\.wav',filename) or not plan or filename[:-4] not in plan['assets']:raise ValueError()
            return send_from_directory(studio.folder(pid)/'assets',filename,conditional=True)
        except ValueError:return jsonify(error='音频文件不存在。'),404

    @bp.get('/api/shortfilms/<pid>/soundtrack/renders/<rid>/<filename>')
    def render_file(pid,rid,filename):
        try:
            if not HEX.fullmatch(rid) or filename not in {*(k+'.wav' for k in (*script.TRACKS,'master')),'final.mp4'}:raise ValueError()
            studio.load(pid)
            return send_from_directory(studio.folder(pid)/'renders'/rid,filename,conditional=True)
        except ValueError:return jsonify(error='成品文件不存在。'),404
    app.register_blueprint(bp)
    return studio
