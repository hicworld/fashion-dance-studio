"""Turn storyboard sound notes into an editable, deterministic cue sheet."""
import re
import uuid

TRACKS = {'dialogue':'对白', 'sfx':'音效', 'ambience':'环境声', 'music':'背景音乐'}


def draft(project):
    cues=[]
    def add(track,start,end,label,content='',role='主角',**extra):
        prefix={'dialogue':'D','sfx':'F','ambience':'A','music':'M'}[track]
        cue={'id':prefix+f'{1+sum(c["track"]==track for c in cues):03d}', 'track':track,
             'start':round(start,3),'end':round(end,3),'label':label,'text':content,'role':role,
             'enabled':True,'gain_db':0,'fade_in':0,'fade_out':0,'source_offset':0,
             'loop':track in {'ambience','music'},'generation_seconds':round(min(8,max(.5,end-start)),3),
             'emotion':'auto','reuse_of':'','asset_id':None,'attempts':[],**extra}
        cues.append(cue)
        return cue['id']

    total=project['duration_seconds']
    shots=project['shots']
    for s in shots:
        note=s.get('sound','');start=s['start_seconds'];end=start+s['duration_seconds']
        for match in re.finditer(r'[‘“「]([^’”」]+)[’”」]',note):
            before=re.split(r'[。；;！!？?]',note[:match.start()])[-1]
            if re.search(r'不使用|不用|不要|不说|禁止',before):continue
            role='朋友' if before.rfind('朋友')>before.rfind('主角') else '主角'
            add('dialogue',start,end,role+' · '+match[1],match[1],role)
        if note.strip():
            add('sfx',start,end,s['id']+' 音效草稿',note,enabled=False)
    add('music',0,total,'全片背景音乐',fade_in=min(1,total/4),fade_out=min(1,total/4))
    warnings=['已从带引号的台词提取对白草稿；音效说明默认关闭，需删去对白/配乐说明后再启用。',
              '请核对角色、时间和遗漏的台词；自动提取不代表已经完成声音导演与口型对齐。']
    return {'version':1,'revision':uuid.uuid4().hex,'cues':cues,'assets':{},'voices':{
        '主角':{'voice_id':'male-qn-qingse','speed':1},'朋友':{'voice_id':'male-qn-jingying','speed':1}},
        'gains':{'dialogue':0,'sfx':-8,'ambience':-18,'music':-18},'duck_music':True,
        'warnings':warnings,'stage':'声音计划已建立，请检查后选择要生成或导入的声音。','render':None}
