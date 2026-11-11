/* Whole-film sound is independent of paid video generation and original clips. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  if(!$('sf-soundtrack'))return;
  const names={dialogue:'对白',sfx:'音效',ambience:'环境声',music:'背景音乐'};
  const labels={pending:'待生成 / 导入',ready:'素材就绪',disabled:'已关闭',running:'正在生成',
    uncertain:'请求待核对',needs_process:'待本地处理',needs_update:'文字或音色已变更',needs_timing:'对白超出时段'};
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const sec=n=>Number(n||0).toLocaleString('zh-CN',{maximumFractionDigits:3});
  let film=null,plan=null,cid='',selected=new Set(),working=false,timer,revision=0;
  let editDirty=false,voiceDirty=false,mixDirty=false;
  let voices=[{id:'male-qn-qingse',name:'青涩青年 · 男声'},{id:'male-qn-jingying',name:'精英青年 · 男声'},
    {id:'female-shaonv',name:'少女 · 女声'}];
  const base=()=>'/api/shortfilms/'+film.id+'/soundtrack';
  const active=()=>plan?.cues?.find(c=>c.id===cid);
  const dirty=()=>editDirty||voiceDirty||mixDirty;
  const visible=()=>!$('view-shortfilm').hidden;
  function error(message=''){$('st-error').hidden=!message;$('st-error').textContent=message.message||message;}
  async function api(path,options={}){
    const response=await fetch(path,options),data=await response.json();
    if(!response.ok)throw new Error(data.error||'声音操作未完成，请核对本机记录。');
    return data;
  }
  const post=(action,body={})=>api(base()+'/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  function resetConfirm(){$('st-confirm').checked=false;summary();}
  function summary(){
    if(!plan?.created)return;
    const cues=plan.cues.filter(c=>selected.has(c.id));
    const chars=cues.filter(c=>c.track==='dialogue').reduce((n,c)=>n+c.text.length,0);
    const seconds=cues.filter(c=>c.track!=='dialogue').reduce((n,c)=>n+c.generation_seconds,0);
    $('st-batch-summary').textContent=`已选 ${cues.length} 条 · 对白 ${chars} 字符 · 音效生成 ${sec(seconds)} 秒（循环段只生成素材本身）`;
    const limited=chars>Number($('st-character-limit').value)||seconds>Number($('st-sfx-limit').value);
    const keysMissing=cues.some(c=>plan.providers[c.track==='dialogue'?'dialogue':'sfx'].missing.length);
    const existing=cues.some(c=>c.attempts.length||c.asset_id);
    $('st-generate').disabled=working||plan.busy||dirty()||!cues.length||!$('st-confirm').checked||limited||keysMissing||(existing&&!$('st-regenerate').checked);
    $('st-pause').disabled=working||!plan.busy||plan.status==='idle';
    $('st-render').disabled=working||plan.busy||dirty();
    $('st-save-voices').disabled=working||plan.busy||!voiceDirty;
    $('st-save-mix').disabled=working||plan.busy||!mixDirty;
    for(const id of ['st-add','st-load-voices','st-filter','st-new-track','st-music-file','st-duck','st-scope','st-regenerate','st-confirm'])$(id).disabled=working||plan.busy;
    $('st-provider-note').textContent='对白：'+(plan.providers.dialogue.missing.length?'需配置 MiniMax Key':'Key 已填写，权限以实际调用为准')+'；音效：'+(plan.providers.sfx.missing.length?'需配置 ElevenLabs Key':'Key 已填写，权限以实际调用为准');
    $('st-save-cue').disabled=working||plan.busy||!editDirty;
    $('st-cue-file').disabled=working||plan.busy||editDirty||!active()||Boolean(active()?.reuse_of);
    $('st-recover').disabled=working||plan.busy||editDirty||active()?.state!=='needs_process';
    for(const element of document.querySelectorAll('#st-editor input:not([type=file]),#st-editor textarea,#st-editor select,#st-voices input,#st-voices select,#st-gains input'))element.disabled=plan.busy||working;
    if(limited)$('st-batch-summary').textContent+=' · 超出本批上限';
  }
  function renderVoices(){
    if(voiceDirty)return;
    $('st-voices').innerHTML=Object.entries(plan.voices).map(([role,settings])=>{
      const known=voices.some(v=>v.id===settings.voice_id);
      return `<div class="st-role" data-role="${esc(role)}"><label class="sf-field">${esc(role)} · 音色<select class="st-voice-choice">${voices.map(v=>`<option value="${esc(v.id)}" ${v.id===settings.voice_id?'selected':''}>${esc(v.name)}</option>`).join('')}<option value="custom" ${!known?'selected':''}>自定义音色编号</option></select></label><label class="sf-field st-custom-voice" ${known?'hidden':''}>自定义编号<input class="st-voice-id" maxlength="200" value="${esc(settings.voice_id)}"></label><label class="sf-field">语速<input class="st-voice-speed" type="number" min="0.5" max="2" step="0.05" value="${settings.speed}"></label></div>`;
    }).join('');
  }
  function renderMix(){
    if(mixDirty)return;
    $('st-gains').innerHTML=Object.entries(names).map(([track,label])=>`<label class="sf-field">${label}音量（dB）<input data-track="${track}" type="number" min="-60" max="12" step="1" value="${plan.gains[track]}"></label>`).join('');
    $('st-duck').checked=plan.duck_music;
  }
  function renderTimeline(){
    const total=film.duration_seconds;
    $('st-timeline').innerHTML=Object.entries(names).map(([track,label])=>`<div class="st-track-row"><span>${label}</span><div class="st-track-lane">${plan.cues.filter(c=>c.track===track&&c.enabled).map(c=>`<button type="button" class="st-cue-bar ${c.state==='ready'?'ready':''} ${c.id===cid?'active':''}" data-cid="${c.id}" data-track="${track}" style="left:${c.start/total*100}%;width:${(c.end-c.start)/total*100}%" title="${esc(c.id+' '+c.label+' '+sec(c.start)+'—'+sec(c.end)+'秒')}" aria-label="编辑 ${esc(c.id+' '+c.label)}">${esc(c.label)}</button>`).join('')}</div></div>`).join('')+`<div class="st-time-labels"><span>0秒</span><span>${sec(total/4)}</span><span>${sec(total/2)}</span><span>${sec(total*3/4)}</span><span>${sec(total)}秒</span></div>`;
  }
  function renderRows(){
    const filter=$('st-filter').value;
    $('st-cues').innerHTML=plan.cues.filter(c=>filter==='all'||c.track===filter).map(c=>{
      const selectable=c.enabled&&c.track!=='music'&&!c.reuse_of;
      return `<tr class="${cid===c.id?'active':''}"><td><input type="checkbox" data-cid="${c.id}" aria-label="选择生成 ${esc(c.id+' '+c.label)}" ${selected.has(c.id)?'checked':''} ${!selectable||plan.busy?'disabled':''}></td><td><strong>${esc(c.id+' · '+c.label)}</strong><small>${names[c.track]}${c.track==='dialogue'?' · '+esc(c.role):''}${c.reuse_of?' · 复用 '+esc(c.reuse_of):''}</small></td><td>${sec(c.start)}—${sec(c.end)} 秒<small>${c.actual_duration?'素材 '+sec(c.actual_duration)+' 秒':''}</small></td><td class="st-state-${c.state}">${labels[c.state]||esc(c.state)}</td><td><button type="button" class="text-link" data-edit="${c.id}">编辑</button> <button type="button" class="text-link" data-listen="${c.id}" ${!c.audio_url?'disabled':''}>试听</button></td></tr>`;
    }).join('');
  }
  const fields={label:'label',role:'role',start:'start',end:'end',text:'text',emotion:'emotion',generation:'generation_seconds',gain:'gain_db',offset:'source_offset','fade-in':'fade_in','fade-out':'fade_out',reuse:'reuse_of',enabled:'enabled',loop:'loop'};
  function renderEditor(force=false){
    const cue=active();$('st-editor').hidden=!cue;if(!cue)return;
    $('st-editor-title').textContent=cue.id+' · '+names[cue.track];
    for(const id of ['role','emotion'])$('st-cue-'+id).closest('.sf-field').hidden=cue.track!=='dialogue';
    $('st-cue-generation').closest('.sf-field').hidden=['dialogue','music'].includes(cue.track);
    $('st-cue-text').closest('.sf-field').hidden=cue.track==='music';
    $('st-cue-loop').closest('.sf-check').hidden=cue.track==='dialogue';
    if(force||!editDirty){
      for(const [id,key] of Object.entries(fields)){
        const element=$('st-cue-'+id);
        if(element.type==='checkbox')element.checked=cue[key];else element.value=cue[key]??'';
      }
      editDirty=false;
    }
    $('st-cue-info').textContent=(cue.actual_duration?`当前音频 ${sec(cue.actual_duration)} 秒；预留 ${sec(cue.end-cue.start)} 秒。`:'尚无音频素材。')+(cue.reuse_of?'复用原条目的同一音频，不会重新生成。':'')+(cue.error?' '+cue.error:'');
    summary();
  }
  function render(data){
    plan=data;$('st-prepare').hidden=data.created;$('st-content').hidden=!data.created;
    $('st-stage').textContent=data.stage||'从分镜声音说明建立可编辑的四轨计划，不调用生成 API。';
    $('st-prepare').disabled=working||data.busy;
    if(!data.created)return;
    selected=new Set([...selected].filter(id=>data.cues.some(c=>c.id===id&&c.enabled&&!c.reuse_of&&c.track!=='music')));
    if(cid&&!data.cues.some(c=>c.id===cid))cid='';
    $('st-warnings').innerHTML=data.warnings.map(w=>'<p>'+esc(w)+'</p>').join('');
    renderVoices();renderMix();renderTimeline();renderRows();renderEditor();summary();
    const output=data.render;
    $('st-output').hidden=!output;
    if(output){
      $('st-output-note').textContent=`${output.scope==='full'?'全片脚本':'最近试剪'} · ${sec(output.duration)} 秒 · ${output.cue_count} 条声音。`+(output.stale?'计划或画面已更新，请重新混音。':output.video?'已合成带声音影片，原试剪保留。':'已生成完整音轨，完整画面就绪后可再次合成。');
      const titles={dialogue:'下载对白轨',sfx:'下载音效轨',ambience:'下载环境轨',music:'下载音乐轨',master:'下载完整混音',video:'下载带声音影片'};
      $('st-downloads').innerHTML=['dialogue','sfx','ambience','music','master','video'].filter(key=>output.urls[key]).map(key=>`<a class="${key==='video'?'primary-button':'text-link'}" href="${esc(output.urls[key])}" download>${titles[key]}</a>`).join('');
      if($('st-master').getAttribute('src')!==output.urls.master)$('st-master').src=output.urls.master;
      $('st-final').hidden=!output.urls.video;
      if(output.urls.video&&$('st-final').getAttribute('src')!==output.urls.video)$('st-final').src=output.urls.video;
    }
    if(data.error)error(data.error);
    clearTimeout(timer);if(data.busy&&visible())timer=setTimeout(()=>load().catch(error),2200);
  }
  async function load(){
    if(!film)return;const current=film.id,n=++revision;const data=await api(base());
    if(n!==revision||current!==film.id)return;render(data);
  }
  async function operation(action){
    if(working)return;working=true;error();if(plan)summary();
    try{await action();window.dispatchEvent(new Event('studio:shortfilm-refresh'));}
    catch(e){error(e);}finally{working=false;if(plan){summary();renderEditor();}}
  }
  function edit(id){
    if(editDirty){error('当前声音尚未保存，请先保存后切换。');return;}
    error();cid=id;renderTimeline();renderRows();renderEditor(true);
    $('st-editor').scrollIntoView({behavior:'smooth',block:'nearest'});
  }
  window.addEventListener('studio:shortfilm-changing',e=>{if(dirty())e.preventDefault();});
  window.addEventListener('studio:shortfilm',e=>{
    const changed=film?.id!==e.detail.id;film=e.detail;$('sf-soundtrack').hidden=false;
    if(changed){plan=null;cid='';selected.clear();editDirty=voiceDirty=mixDirty=false;$('st-confirm').checked=false;$('st-regenerate').checked=false;$('st-player').pause();$('st-master').pause();$('st-final').pause();load().catch(error);}
    else if(plan){const busy=plan.busy;plan.busy=film.busy;summary();if(busy!==film.busy)load().catch(error);}
  });
  $('st-prepare').addEventListener('click',()=>operation(async()=>render(await post('prepare'))));
  $('st-filter').addEventListener('change',renderRows);
  $('st-timeline').addEventListener('click',e=>{const button=e.target.closest('[data-cid]');if(button)edit(button.dataset.cid);});
  $('st-cues').addEventListener('click',e=>{
    const editButton=e.target.closest('[data-edit]');if(editButton)edit(editButton.dataset.edit);
    const listen=e.target.closest('[data-listen]');if(listen){const cue=plan.cues.find(c=>c.id===listen.dataset.listen);$('st-player-box').hidden=false;$('st-player-name').textContent=cue.id+' · '+cue.label;$('st-player').src=cue.audio_url;$('st-player').play().catch(()=>{});}
  });
  $('st-cues').addEventListener('change',e=>{if(!e.target.matches('input[data-cid]'))return;e.target.checked?selected.add(e.target.dataset.cid):selected.delete(e.target.dataset.cid);resetConfirm();});
  $('st-select-missing').addEventListener('click',()=>{selected=new Set(plan.cues.filter(c=>c.enabled&&!c.reuse_of&&c.track!=='music'&&c.state==='pending').map(c=>c.id));renderRows();resetConfirm();});
  $('st-clear').addEventListener('click',()=>{selected.clear();renderRows();resetConfirm();});
  $('st-add').addEventListener('click',()=>operation(async()=>{
    if(dirty())throw new Error('请先保存声音修改。');const ids=new Set(plan.cues.map(c=>c.id));
    render(await post('add',{track:$('st-new-track').value}));edit(plan.cues.find(c=>!ids.has(c.id)).id);
  }));
  $('st-voices').addEventListener('change',e=>{
    const card=e.target.closest('.st-role');if(!card)return;
    if(e.target.matches('.st-voice-choice')){card.querySelector('.st-custom-voice').hidden=e.target.value!=='custom';if(e.target.value!=='custom')card.querySelector('.st-voice-id').value=e.target.value;}
    voiceDirty=true;resetConfirm();
  });
  $('st-voices').addEventListener('input',()=>{voiceDirty=true;resetConfirm();});
  $('st-save-voices').addEventListener('click',()=>operation(async()=>{
    const values={};for(const card of document.querySelectorAll('.st-role'))values[card.dataset.role]={voice_id:card.querySelector('.st-voice-id').value.trim(),speed:Number(card.querySelector('.st-voice-speed').value)};
    const data=await post('save',{voices:values});voiceDirty=false;render(data);
  }));
  $('st-load-voices').addEventListener('click',()=>operation(async()=>{
    if(voiceDirty)throw new Error('请先保存音色修改。');const result=await post('voices');
    voices=[...voices,...result.voices.filter(v=>!voices.some(old=>old.id===v.id))];renderVoices();
  }));
  $('st-editor').addEventListener('input',e=>{if(!e.target.matches('input:not([type=file]),textarea,select'))return;editDirty=true;resetConfirm();});
  $('st-save-cue').addEventListener('click',()=>operation(async()=>{
    const cue={id:cid};for(const [id,key] of Object.entries(fields)){
      const element=$('st-cue-'+id);cue[key]=element.type==='checkbox'?element.checked:element.type==='number'?Number(element.value):element.value.trim();
    }
    const data=await post('save',{cue});editDirty=false;render(data);
  }));
  async function upload(file,action){
    if(dirty())throw new Error('请先保存声音修改。');const body=new FormData();body.append('audio',file);
    render(await api(base()+'/'+action,{method:'POST',body}));
  }
  $('st-cue-file').addEventListener('change',()=>operation(async()=>{try{const file=$('st-cue-file').files[0];if(file)await upload(file,'cues/'+cid+'/upload');}finally{$('st-cue-file').value='';}}));
  $('st-music-file').addEventListener('change',()=>operation(async()=>{try{const file=$('st-music-file').files[0];if(file)await upload(file,'music');}finally{$('st-music-file').value='';}}));
  $('st-recover').addEventListener('click',()=>operation(async()=>render(await post('cues/'+cid+'/recover'))));
  for(const id of ['st-character-limit','st-sfx-limit','st-regenerate'])$(id).addEventListener('change',resetConfirm);
  $('st-confirm').addEventListener('change',summary);
  $('st-generate').addEventListener('click',()=>operation(async()=>{
    const data=await post('generate',{cue_ids:[...selected],max_characters:Number($('st-character-limit').value),max_sfx_seconds:Number($('st-sfx-limit').value),regenerate:$('st-regenerate').checked,confirmed:$('st-confirm').checked});
    $('st-confirm').checked=false;render(data);
  }));
  $('st-pause').addEventListener('click',()=>operation(async()=>render(await post('pause'))));
  $('st-gains').addEventListener('input',()=>{mixDirty=true;summary();});
  $('st-duck').addEventListener('change',()=>{mixDirty=true;summary();});
  $('st-save-mix').addEventListener('click',()=>operation(async()=>{
    const gains={};for(const element of document.querySelectorAll('#st-gains input'))gains[element.dataset.track]=Number(element.value);
    const data=await post('save',{gains,duck_music:$('st-duck').checked});mixDirty=false;render(data);
  }));
  $('st-render').addEventListener('click',()=>operation(async()=>render(await post('render',{scope:$('st-scope').value}))));
  window.addEventListener('studio:view',e=>{if(e.detail==='shortfilm'&&film)load().catch(error);else if(e.detail!=='shortfilm'){clearTimeout(timer);for(const id of ['st-player','st-master','st-final'])$(id).pause();}});
  window.addEventListener('focus',()=>{if(visible()&&film)load().catch(error);});
  window.addEventListener('beforeunload',()=>clearTimeout(timer));
})();
