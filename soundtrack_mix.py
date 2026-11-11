"""Sample-aligned multitrack rendering. No cloud calls or video re-generation."""
from pathlib import Path
import shutil

import shortfilm_audio as audio
from soundtrack_script import TRACKS

RATE=48000


def resolve_asset(plan,cue,seen=None):
    seen=set() if seen is None else seen
    if cue['id'] in seen:raise ValueError('声音复用形成循环：'+cue['id'])
    seen.add(cue['id'])
    if cue.get('reuse_of'):
        target=next((c for c in plan['cues'] if c['id']==cue['reuse_of']),None)
        if not target:raise ValueError(cue['id']+'复用的原声音不存在。')
        return resolve_asset(plan,target,seen)
    return plan['assets'].get(cue.get('asset_id'))


def segments(project,scope):
    if scope=='full':return [(0,project['duration_seconds'],0)],project['duration_seconds']
    if scope!='roughcut':raise ValueError('请选择全片脚本或最近试剪时间轴。')
    if not project.get('roughcut_shots'):raise ValueError('请先导出选中镜头试剪。')
    if project.get('roughcut_stale'):raise ValueError('试剪已经过期，请先重新导出试剪，再制作对应声音。')
    result=[];cursor=0
    if project.get('roughcut_segments'):
        segments=project['roughcut_segments']
        if len(segments)!=len(project['roughcut_shots']):raise ValueError('自定义时间线记录不完整，请重新生成连续预览。')
        for sid,segment in zip(project['roughcut_shots'],segments):
            shot=next((s for s in project['shots'] if s['id']==sid),None)
            if not shot or segment.get('shot_id')!=sid:raise ValueError('自定义时间线镜头记录不完整。')
            start=segment['start_seconds'];length=segment['duration_seconds']
            if not shot['start_seconds']-.001<=start<start+length<=shot['start_seconds']+shot['duration_seconds']+.001:
                raise ValueError('自定义时间线取用范围无效，请重新生成连续预览。')
            result.append((start,start+length,cursor));cursor+=length
        return result,cursor
    for sid in project['roughcut_shots']:
        shot=next((s for s in project['shots'] if s['id']==sid),None)
        if not shot:raise ValueError('试剪镜头记录不完整，请重新导出。')
        result.append((shot['start_seconds'],shot['start_seconds']+shot['duration_seconds'],cursor))
        cursor+=shot['duration_seconds']
    return result,cursor


def intersections(cue,timeline):
    result=[]
    for start,end,out in timeline:
        left,right=max(start,cue['start']),min(end,cue['end'])
        if right-left>1/RATE:
            result.append((left-cue['start'],right-left,out+left-start))
    return result


def render(plan,project,assets_folder,work,scope,*,stop=None,progress=None):
    timeline,length=segments(project,scope)
    if not 0<length<=1200:raise ValueError('全片声音目前支持20分钟以内的时间轴。')
    needed=[]
    for cue in plan['cues']:
        parts=intersections(cue,timeline)
        if cue['enabled'] and parts:
            asset=resolve_asset(plan,cue)
            if not asset:raise ValueError(cue['id']+'尚无音频素材，请生成、导入或关闭这条声音。')
            if not cue['loop'] and cue['source_offset']>=asset['duration']:
                raise ValueError(cue['id']+'素材取用起点超出音频长度，请调整。')
            if cue['track']=='dialogue' and not cue['loop'] and asset['duration']-cue['source_offset']>cue['end']-cue['start']+.035:
                raise ValueError(f'{cue["id"]}对白长{asset["duration"]:.2f}秒，超过预留{cue["end"]-cue["start"]:.2f}秒。请调整时间或语速，不会截断台词。')
            needed.append((cue,asset,parts))
    # Four stems, final mix, source links and mux need less than this PCM bound.
    if shutil.disk_usage(work.parent).free<max(128*1024**2,int(length*RATE*2*4*8)):
        raise ValueError('磁盘空间不足，无法安全制作全片声音。')
    work.mkdir(parents=True,exist_ok=True)
    inputs=[];graph=[];tracks={k:[] for k in TRACKS}
    for index,(cue,asset,parts) in enumerate(needed):
        source=Path(assets_folder)/asset['file']
        if not source.is_file() or not source.resolve().is_relative_to(Path(assets_folder).resolve()):
            raise ValueError(cue['id']+'音频素材文件缺失。')
        # Short, controlled paths keep Windows CreateProcess below its argument
        # length limit even for a 200-cue sheet. No user text enters the graph.
        local=work/f'in-{index}.wav'
        try:local.hardlink_to(source)
        except OSError:shutil.copyfile(source,local)
        if cue['loop']:inputs+=['-stream_loop','-1']
        inputs+=['-i',local.name]
        duration=cue['end']-cue['start']
        offset=cue['source_offset']
        if cue['loop']:offset=offset%max(asset['duration'],.001)
        filters=(f'[{index}:a]aresample={RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,'
                 f'atrim=start={offset}:duration={duration},asetpts=PTS-STARTPTS,'
                 f'apad=whole_dur={duration},atrim=duration={duration},volume={10**(cue["gain_db"]/20):.9f}')
        if cue['fade_in']>0:filters+=f',afade=t=in:d={cue["fade_in"]}'
        if cue['fade_out']>0:filters+=f',afade=t=out:st={duration-cue["fade_out"]}:d={cue["fade_out"]}'
        labels=[f'n{index}p{i}' for i in range(len(parts))]
        if len(parts)>1:filters+=f',asplit={len(parts)}'+''.join(f'[{label}]' for label in labels)
        else:filters+=f'[{labels[0]}]'
        graph.append(filters)
        for part,(relative,used,out) in enumerate(parts):
            label=f't{index}p{part}'
            graph.append(f'[{labels[part]}]atrim=start={relative}:duration={used},asetpts=PTS-STARTPTS,'
                         f'adelay={round(out*RATE)}S:all=1,apad,atrim=end_sample={round(length*RATE)}[{label}]')
            tracks[cue['track']].append(label)
    for track,labels in tracks.items():
        if labels:
            chain=''.join(f'[{label}]' for label in labels)+f'amix=inputs={len(labels)}:normalize=0'
        else:
            chain=f'anullsrc=r={RATE}:cl=stereo,atrim=duration={length}'
        graph.append(chain+f',volume={10**(plan["gains"][track]/20):.9f},alimiter=limit=0.95:level=0:latency=1,'
                     f'apad,atrim=end_sample={round(length*RATE)}[{track}]')
    script=work/'stems.ffgraph';script.write_text(';\n'.join(graph),encoding='utf-8')
    command=['ffmpeg','-hide_banner','-loglevel','error','-y',*inputs,'-filter_complex_threads','1',
             '-filter_complex_script',script.name]
    for track in TRACKS:
        command+=['-map',f'[{track}]','-c:a','pcm_s16le','-ar',str(RATE),'-ac','2','-t',str(length),track+'.wav']
    if progress:progress('正在按时间轴制作对白、音效、环境声和音乐四条分轨…')
    audio._run(command,work/'dialogue.wav',stop,cwd=work)
    inputs=[]
    for track in TRACKS:inputs+=['-i',str(work/(track+'.wav'))]
    if plan['duck_music'] and tracks['dialogue']:
        mix=('[0:a]asplit=2[d][key];[3:a][key]sidechaincompress=threshold=0.015:ratio=8:attack=15:release=350[m];'
             '[d][1:a][2:a][m]amix=inputs=4:normalize=0')
    else:mix='[0:a][1:a][2:a][3:a]amix=inputs=4:normalize=0'
    mix+=f',alimiter=limit=0.95:level=0:latency=1,apad,atrim=end_sample={round(length*RATE)}[out]'
    if progress:progress('正在统一混音、压低对白期间的音乐并限制峰值…')
    audio._run(['ffmpeg','-hide_banner','-loglevel','error','-y',*inputs,'-filter_complex',mix,'-map','[out]',
                '-c:a','pcm_s16le','-ar',str(RATE),'-ac','2','-t',str(length),str(work/'master.wav')],work/'master.wav',stop)
    result=audio.inspect(work/'master.wav')
    if result['has_audio'] is not True or abs(result['duration']-length)>1/RATE+.001:
        raise ValueError('混音时长校验未通过，上一版声音保持原样。')
    for linked in work.glob('in-*.wav'):linked.unlink()
    return {'duration':length,'scope':scope,'cue_count':len(needed)}
