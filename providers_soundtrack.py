"""Explicit single-request speech and effects generation. No automatic retries."""
import hashlib
import json
import os
from pathlib import Path

import requests

from providers_jimeng import MotionCancelledError, MotionSubmissionUncertainError
import providers_seedance as records

EMOTIONS={'auto','happy','sad','angry','fearful','disgusted','surprised','calm','fluent','whisper'}
TTS_URL='https://api.minimax.cn/v1/t2a_v2'
SFX_URL='https://api.elevenlabs.io/v1/sound-generation'


def payload(cue, voices):
    if cue['track']=='dialogue':
        voice=voices.get(cue['role'],{})
        voice_id=voice.get('voice_id','')
        if not isinstance(voice_id,str) or not 1<=len(voice_id.strip())<=200:
            raise ValueError(cue['role']+'尚未选择有效音色。')
        emotion=cue.get('emotion','auto')
        if emotion not in EMOTIONS:raise ValueError('对白情绪不支持。')
        settings={'voice_id':voice_id.strip(),'speed':voice.get('speed',1),'vol':1,'pitch':0}
        if emotion!='auto':settings['emotion']=emotion
        # Official 2.8 documentation excludes whisper/fluent; use a supported
        # 2.6 model for these choices instead of submitting invalid parameters.
        model='speech-2.6-hd' if emotion in {'whisper','fluent'} else 'speech-2.8-hd'
        return {'model':model,'text':cue['text'],'stream':False,'voice_setting':settings,
                'audio_setting':{'sample_rate':32000,'bitrate':128000,'format':'mp3','channel':1},
                'language_boost':'Chinese','output_format':'hex'}
    if cue['track'] in {'sfx','ambience'}:
        return {'text':cue['text'],'duration_seconds':cue['generation_seconds'],
                'loop':cue.get('loop',False),'model_id':'eleven_text_to_sound_v2','prompt_influence':.5}
    raise ValueError('背景音乐请导入本地文件；不会调用未配置的音乐生成服务。')


def signature(cue, voices):
    return hashlib.sha256(json.dumps(payload(cue,voices),sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def missing(track):
    name='MINIMAX_API_KEY' if track=='dialogue' else 'ELEVENLABS_API_KEY'
    return [] if os.environ.get(name,'').strip() else [f'请在 API 设置中保存 {name}。']


def _body(response, maximum=40*1024**2):
    data=bytearray()
    for chunk in response.iter_content(chunk_size=65536):
        data.extend(chunk)
        if len(data)>maximum:raise RuntimeError('声音接口返回内容过大，已停止读取。请核对平台记录。')
    return bytes(data)


def generate(cue, voices, destination, *, stop=None):
    """One synchronous paid POST; ambiguous outcomes remain explicitly marked."""
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    marker=destination.with_suffix('.submission.json')
    if destination.exists() or marker.exists():
        raise MotionSubmissionUncertainError('本次声音已有请求记录，不能再次自动提交。请核对历史或另建明确付费的重做记录。')
    if stop and stop.is_set():raise MotionCancelledError('声音生成已暂停，尚未提交本条。')
    data=payload(cue,voices)
    if missing(cue['track']):raise ValueError(missing(cue['track'])[0])
    speech=cue['track']=='dialogue'
    url=TTS_URL if speech else SFX_URL
    headers={'Authorization':'Bearer '+os.environ['MINIMAX_API_KEY']} if speech else {'xi-api-key':os.environ['ELEVENLABS_API_KEY']}
    try:
        with marker.open('x',encoding='utf-8') as handle:
            json.dump({'status':'submitting','provider':'minimax' if speech else 'elevenlabs',
                'model':data.get('model') or data['model_id'],'signature':signature(cue,voices)},handle)
            handle.flush();os.fsync(handle.fileno())
    except OSError:
        raise MotionSubmissionUncertainError('声音提交标记未能独占保存，已阻止本条提交。') from None
    try:
        with requests.post(url,headers=headers,json=data,stream=True,timeout=(20,180),allow_redirects=False) as response:
            if response.status_code!=200:
                raise MotionSubmissionUncertainError(f'声音接口返回 HTTP {response.status_code}。请核对权限、余额和平台请求记录；不会自动重试。')
            body=_body(response)
            if speech:
                result=json.loads(body)
                if result.get('base_resp',{}).get('status_code')!=0:
                    raise MotionSubmissionUncertainError('MiniMax 声音请求未确认成功，请核对平台记录后决定是否另付费重做。')
                encoded=result.get('data',{}).get('audio')
                if not isinstance(encoded,str) or not 2<=len(encoded)<=40*1024**2:
                    raise MotionSubmissionUncertainError('声音响应缺少有效音频，请核对平台请求记录。')
                raw=bytes.fromhex(encoded)
                extra=result.get('extra_info') or {}
                usage={k:extra[k] for k in ('usage_characters','audio_length') if isinstance(extra.get(k),(int,float))}
            else:
                raw=body
                cost=response.headers.get('character-cost','')
                usage={'character_cost':int(cost)} if cost.isdigit() else {}
            if not raw:raise ValueError('声音接口返回空文件。')
            temporary=destination.with_suffix('.download.tmp')
            temporary.write_bytes(raw);temporary.replace(destination)
            records._atomic_record(marker,{'status':'downloaded','provider':'minimax' if speech else 'elevenlabs',
                'model':data.get('model') or data['model_id'],'signature':signature(cue,voices),'usage':usage})
            return {'usage':usage,'signature':signature(cue,voices)}
    except MotionSubmissionUncertainError:raise
    except Exception:
        raise MotionSubmissionUncertainError('本条声音响应未可靠保存，可能已经计费。请核对平台记录；不会自动重新请求。') from None


def voices():
    if missing('dialogue'):raise ValueError(missing('dialogue')[0])
    # Read-only catalogue endpoint, called only by the user's explicit button.
    with requests.post('https://api.minimax.cn/v1/get_voice',
            headers={'Authorization':'Bearer '+os.environ['MINIMAX_API_KEY']},json={'voice_type':'all'},
            stream=True,timeout=(15,45),allow_redirects=False) as response:
        if response.status_code!=200:raise ValueError('音色列表未取回，请检查 MiniMax 语音权限和网络。')
        data=json.loads(_body(response,5*1024**2))
    if data.get('base_resp',{}).get('status_code')!=0:raise ValueError('音色列表查询失败，请检查语音权限。')
    result=[]
    for group in ('system_voice','voice_cloning','voice_generation'):
        for value in (data.get(group) or [])[:1000]:
            identifier=value.get('voice_id')
            if isinstance(identifier,str) and 1<=len(identifier)<=200:
                result.append({'id':identifier,'name':str(value.get('voice_name') or identifier)[:160]})
    return result
