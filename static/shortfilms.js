/* Storyboard imports are local; only an explicit confirmed batch creates tasks. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  if (!$('view-shortfilm')) return;
  const labels = {pending:'未生成',queued:'等待提交',running:'处理中',done:'已完成',failed:'生成失败',
    awaiting_resume:'待续查',uncertain:'待核对提交',needs_trim:'待本地处理',waiting_local:'等待本地处理',trimming:'裁切中',enhancing:'AI 超分中',dubbing:'合成声音中'};
  let project = null, selected = new Set(), current = '', media = 'frame', dirty = false, timer, working = false;
  let projects = [], loadRevision = 0;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const seconds = n => Number(n || 0).toLocaleString('zh-CN',{maximumFractionDigits:3});
  function error(e=''){ $('sf-error').hidden=!e; $('sf-error').textContent=e.message||e; }
  async function api(path, options={}) {
    const response=await fetch(path,options);
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||'操作未完成，请刷新记录核对。');
    return result;
  }
  const post=(path,body={})=>api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const endpoint=()=>'/api/shortfilms/'+project.id;
  const active=()=>project?.shots.find(s=>s.id===current);
  const visible=()=>!$('view-shortfilm').hidden;
  const pipeline=()=>project?.pipeline||{};
  const canEnqueue=()=>!project?.busy||Boolean(pipeline().can_enqueue);
  const occupied=id=>Boolean(pipeline().active&&(pipeline().cloud_shot_id===id||pipeline().local_shot_id===id||
    (pipeline().cloud_queued||[]).includes(id)||(pipeline().local_queued||[]).includes(id)||
    project.shots.some(s=>s.id===id&&['queued','running','waiting_local','trimming','enhancing'].includes(s.status))));
  function renderPipeline(){
    const q=pipeline(), cloud=q.cloud_queued||[], local=q.local_queued||[];
    const state=q.stopping?'正在暂停两个队列':q.local_halted?'本地处理已暂停，请检查失败镜头':q.cloud_halted?'云端提交已暂停，请检查镜头记录':q.active?'两个队列独立运行':project.busy?'正在执行其他项目操作':'准备就绪';
    $('sf-pipeline-state').textContent=state;
    $('sf-cloud-card').classList.toggle('is-active',Boolean(q.cloud_shot_id));
    $('sf-local-card').classList.toggle('is-active',Boolean(q.local_shot_id));
    $('sf-cloud-current').textContent=q.cloud_shot_id?'当前 '+q.cloud_shot_id:q.stopping||q.cloud_halted?'已暂停':'空闲';
    $('sf-local-current').textContent=q.local_shot_id?'当前 '+q.local_shot_id:q.stopping||q.local_halted?'已暂停':'空闲';
    $('sf-cloud-stage').textContent=q.cloud_stage||'逐镜提交、查询并下载原片。';
    $('sf-local-stage').textContent=q.local_stage||'按队列裁切、AI 超分并保存片段。';
    $('sf-cloud-queue').textContent=cloud.length?`待云端处理 ${cloud.length} 镜：${cloud.join('、')}`:'暂无镜头等待云端处理';
    $('sf-local-queue').textContent=local.length?`待本地处理 ${local.length} 镜：${local.join('、')}`:'暂无原片等待本地处理';
    $('sf-pipeline-note').textContent=q.stopping?'正在停止后续提交和本地处理。已受理的云端任务可能仍在运行，可随后续查原任务。':q.local_halted?'本地失败后会停止后续新提交；已提交的云端任务仍可保存原片。检查错误后继续本地处理或续查。':q.cloud_halted?'后续新提交已停止，已有本地任务按队列处理。核对失败或不确定的云端记录后再继续。':'本地超分时仍可选择其他镜头，确认后追加云端生成。最多暂存 2 个等待本地处理的原片，队列满时云端会等待，避免原片持续堆积。';
  }
  function confirmReset(){ $('sf-confirm').checked=false; summary(); }
  function summary(){
    if(!project)return;
    const shots=project.shots.filter(s=>selected.has(s.id));
    const total=shots.reduce((n,s)=>n+s.generation_seconds,0);
    const used=shots.reduce((n,s)=>n+s.duration_seconds,0);
    $('sf-batch-summary').textContent=`已选 ${shots.length} 镜 · 生成 ${seconds(total)} 秒 · 剪辑取用 ${seconds(used)} 秒`;
    const limit=Number($('sf-limit').value);
    const aiMode=$('sf-upscale').value;
    const aiReady=aiMode==='none'||project.superresolution?.modes[aiMode]?.available;
    const blocked=shots.some(s=>occupied(s.id)||['awaiting_resume','uncertain'].includes(s.status));
    const needsRegenerate=shots.some(s=>s.attempts.length)&&!$('sf-regenerate').checked;
    $('sf-start').disabled=working||!canEnqueue()||!shots.length||!$('sf-confirm').checked||!limit||total>limit||Boolean(project.missing.length)||!aiReady||blocked||needsRegenerate;
    $('sf-start-text').textContent=pipeline().active?'确认追加选中镜头':'确认生成选中镜头';
    $('sf-pause').disabled=working||!project.busy||Boolean(project.editing_busy&&!pipeline().active);
    $('sf-pause').textContent=pipeline().active?'暂停两个队列':'暂停处理';
    $('sf-export-all').disabled=working||project.busy||project.ready_count!==project.shots.length;
    $('sf-export-selected').disabled=working||project.busy||!shots.length||shots.some(s=>s.status!=='done');
    for(const id of ['sf-resolution','sf-regenerate','sf-upscale','sf-generate-audio','sf-limit','sf-confirm'])$(id).disabled=working||!canEnqueue();
    for(const id of ['sf-select-trial','sf-select-pending'])$(id).disabled=working||!canEnqueue();
    $('sf-ai-status').textContent=(aiReady?'本地组件已就绪（实际运行仍需显卡驱动支持）':'缺少本地超分组件，请使用包含模型的新版软件')+' · '+project.width+'×'+project.height+' 输出 · '+(aiMode==='none'?'普通缩放，无 AI 增强':'Real-ESRGAN · 本机运算，无超分 API 费用');
    if(total>limit)$('sf-batch-summary').textContent+=' · 已超出你设置的本批上限';
    if(blocked)$('sf-batch-summary').textContent+=' · 排队或处理中镜头不能重复追加；待核对记录请先续查';
    else if(needsRegenerate)$('sf-batch-summary').textContent+=' · 已有记录需勾选另付费重新生成';
    else if(pipeline().active&&shots.length)$('sf-batch-summary').textContent+=' · 确认后追加到队列';
    renderPipeline();
  }
  function renderRows(){
    $('sf-shots').innerHTML=project.shots.map(s=>`<div class="sf-shot-row ${s.id===current?'active':''}"><input type="checkbox" class="sf-shot-check" data-sid="${s.id}" aria-label="选择 ${esc(s.id+' '+s.title)}" ${selected.has(s.id)?'checked':''} ${!canEnqueue()||occupied(s.id)?'disabled':''}><button type="button" class="sf-open-shot" data-sid="${s.id}"><img src="${esc(s.frame_url)}" alt="" loading="lazy"><span><strong>${esc(s.id+' '+s.title)}</strong><small>取用 ${seconds(s.duration_seconds)} 秒 · 生成 ${s.generation_seconds} 秒</small><span class="sf-status sf-status-${s.status}">${labels[s.status]||esc(s.status)}</span></span></button></div>`).join('');
  }
  function renderDetail(force=false){
    const s=active(); if(!s)return;
    $('sf-detail-title').textContent=s.id+' · '+s.title;
    $('sf-detail-description').textContent=`时间轴 ${seconds(s.start_seconds)}—${seconds(s.start_seconds+s.duration_seconds)} 秒 · ${labels[s.status]||s.status}`;
    if(force||!dirty){ $('sf-prompt').value=s.video_prompt; $('sf-sound').value=s.sound||''; dirty=false; }
    $('sf-save-prompt').textContent=dirty?'保存提示词与声音（有修改）':'保存提示词与声音说明';
    $('sf-prompt').disabled=project.busy; $('sf-save-prompt').disabled=working||project.busy||!dirty;
    $('sf-sound').disabled=project.busy;
    $('sf-detail-note').textContent=s.stage||'';
    const sound=s.audio_status||{};
    const audioLabel=value=>value===true?'有音轨':value===false?'无音轨':'待检查音轨';
    $('sf-audio-status').textContent=s.source_url?`生成原片：${audioLabel(sound.source_has_audio)}；当前片段：${audioLabel(sound.clip_has_audio)}。`+(sound.imported?'已使用本地导入的声音。':sound.source_has_audio===false?'原片没有生成声音，可导入配音/音效补声音；开启新生成的声音开关不会改变旧片段。':'裁切、超分与导出会保留声音。'):'尚未生成。新任务可勾选“生成同步声音”，声音内容按下方说明。';
    $('sf-audio-file').disabled=working||project.busy||s.status!=='done'||!s.clip_url;
    $('sf-audio-reset').disabled=working||project.busy||!sound.imported;
    $('sf-shot-error').hidden=!s.error; $('sf-shot-error').textContent=s.error||'';
    if($('sf-frame').getAttribute('src')!==s.frame_url)$('sf-frame').src=s.frame_url;
    const url=media==='source'?s.source_url:media==='clip'?s.clip_url:null;
    if(media!=='frame'&&!url)media='frame';
    $('sf-frame').hidden=media!=='frame'; $('sf-video').hidden=media==='frame';
    if(media==='frame')$('sf-video').pause();
    else if($('sf-video').getAttribute('src')!==url){$('sf-video').src=url;}
    document.querySelectorAll('[data-sf-media]').forEach(b=>{
      b.classList.toggle('active',b.dataset.sfMedia===media);
      b.disabled=b.dataset.sfMedia==='source'?!s.source_url:b.dataset.sfMedia==='clip'?!s.clip_url:false;
    });
    $('sf-notes').replaceChildren();
    for(const [key,title] of [['action','动作'],['camera','运镜'],['sound','声音'],['post','后期']]){
      const p=document.createElement('p');const strong=document.createElement('strong');strong.textContent=title+'：';p.append(strong,document.createTextNode(s[key]||'未填写'));$('sf-notes').append(p);
    }
    if(document.activeElement!==$('sf-trim-start'))$('sf-trim-start').value=s.trim_start||0;
    $('sf-trim').disabled=working||!canEnqueue()||occupied(s.id)||!s.source_url;
    $('sf-enhance').disabled=working||!canEnqueue()||occupied(s.id)||!s.source_url||!project.superresolution?.modes[$('sf-local-upscale').value]?.available;
    $('sf-local-upscale').disabled=!canEnqueue()||occupied(s.id)||!s.source_url;
    $('sf-trim-start').disabled=!canEnqueue()||occupied(s.id)||!s.source_url;
    $('sf-trim').textContent=pipeline().active?'加入本地裁切队列':'重新裁切本镜';
    $('sf-enhance').textContent=pipeline().active?'加入本地 AI 超分队列':'本地 AI 超分 / 继续';
    for(const [id,url] of [['sf-download-source',s.source_url],['sf-download-clip',s.clip_url]]){
      $(id).hidden=!url;if(url)$(id).href=url;
    }
    const a=s.attempts.find(a=>a.id===s.active_attempt);
    const resumable=['awaiting_resume','uncertain'].includes(s.status);
    $('sf-resume-area').hidden=!resumable; $('sf-resume').disabled=working||!canEnqueue()||occupied(s.id);
    $('sf-resume').textContent=pipeline().active?'加入原任务续查队列':'续查本镜原任务';
    $('sf-task-id-label').hidden=!resumable||Boolean(a?.task_id);
    $('sf-attempt-info').textContent=a?`已建 ${s.attempts.length} 次生成记录 · 本次 ${a.resolution} / ${a.seconds} 秒 · ${a.generate_audio?'请求生成声音':'请求无声生成'}`+(a.task_id?' · 原编号 '+a.task_id:''):'';
    const processing=s.clip_processing;
    const isAI=processing&&processing.mode!=='none';
    $('sf-clip-tab').textContent=isAI?'AI 超分成片':'成片片段';
    $('sf-processing-info').textContent=(processing?`当前片段：${isAI?'AI 超分 · '+processing.label:'普通缩放'} · ${processing.output_width}×${processing.output_height}。`:'')+'从生成原片处理，不重新调用生成 API；暂停后选择相同模型和起点可复用完成帧。重新裁切按钮使用普通缩放。';
    if(force&&['ai-light','ai-general'].includes(s.processing_mode))$('sf-local-upscale').value=s.processing_mode;
  }
  function render(p,force=false){
    project=p; $('sf-empty').hidden=true; $('sf-workspace').hidden=false;
    const upscale=$('sf-upscale');
    for(const option of upscale.options){
      option.disabled=option.value!=='none'&&!p.superresolution?.modes?.[option.value]?.available;
    }
    if(upscale.selectedOptions[0]?.disabled)upscale.value='none';
    $('sf-project-title').textContent=p.title+' · '+p.shot_count+' 镜'; $('sf-stage').textContent=p.stage||'';
    $('sf-connection').textContent=p.missing.length?'需配置方舟 API Key':'Seedance 2.5 · 已配置';
    $('sf-stats').innerHTML=[['原分镜',p.shot_count+' 镜'],['成片时长',seconds(p.duration_seconds)+' 秒'],['首轮生成量',seconds(p.generation_seconds)+' 秒'],['已完成',p.ready_count+' / '+p.shot_count]].map(([a,b])=>`<div class="panel"><span>${a}</span><strong>${b}</strong></div>`).join('');
    if(!current||!p.shots.some(s=>s.id===current))current=p.shots[0].id;
    selected=new Set([...selected].filter(id=>p.shots.some(s=>s.id===id)&&!occupied(id)));
    renderRows();renderDetail(force);summary();
    $('sf-manifest').href=endpoint()+'/manifest';
    $('sf-view-animatic').hidden=!p.animatic_url;
    if(p.animatic_url&&$('sf-animatic').getAttribute('src')!==p.animatic_url)$('sf-animatic').src=p.animatic_url;
    $('sf-download-roughcut').hidden=!p.roughcut_url;
    $('sf-roughcut').hidden=!p.roughcut_url;
    if(p.roughcut_url){
      $('sf-download-roughcut').href=p.roughcut_url;
      if($('sf-roughcut').getAttribute('src')!==p.roughcut_url)$('sf-roughcut').src=p.roughcut_url;
    }
    $('sf-export-note').textContent=p.roughcut_url?`最近试剪：${p.roughcut_shots.length} 镜，${seconds(p.roughcut_duration)} 秒 · ${p.roughcut_has_audio?'含音轨':'无音轨'}。`+(p.roughcut_stale?'镜头已更新，请重新导出。':''):'';
    if(p.error)error(p.error);
    window.dispatchEvent(new CustomEvent('studio:shortfilm',{detail:p}));
    clearTimeout(timer);
    if(p.busy&&visible())timer=setTimeout(()=>loadProject().catch(error),2200);
  }
  async function refreshProjects(keep=true){
    const result=await api('/api/shortfilms');projects=result.projects;
    const prior=keep?project?.id:null;
    let stored;try{stored=localStorage.getItem('studio-shortfilm-project');}catch(_){}
    $('sf-project').innerHTML='<option value="">选择或导入短片</option>'+projects.map(p=>`<option value="${p.id}">${esc(p.title)} · ${p.ready_count}/${p.shot_count} 镜</option>`).join('');
    const id=prior||stored||projects[0]?.id;
    if(projects.some(p=>p.id===id)){$('sf-project').value=id;if(visible()||prior)await loadProject(id);}
    $('sf-connection').textContent=result.missing.length?'需配置方舟 API Key':'Seedance 2.5 · 已配置';
  }
  async function loadProject(id){
    id=id||project?.id||$('sf-project').value;if(!id)return;
    const revision=++loadRevision;
    const changed=project?.id!==id;
    if(changed&&!window.dispatchEvent(new CustomEvent('studio:shortfilm-changing',{cancelable:true,detail:{id}}))){
      $('sf-project').value=project?.id||'';throw new Error('全片声音有未保存的修改，请先保存。');
    }
    if(changed){selected.clear();current='';dirty=false;media='frame';$('sf-confirm').checked=false;$('sf-regenerate').checked=false;$('sf-animatic').hidden=true;$('sf-video').pause();}
    const data=await api('/api/shortfilms/'+id);
    if(revision!==loadRevision)return;
    render(data,changed);
    try{localStorage.setItem('studio-shortfilm-project',id);}catch(_){}
  }
  async function operation(action){
    if(working)return;
    working=true;error();summary();
    try{await action();}catch(e){error(e);}finally{working=false;if(project){summary();renderDetail();}}
  }
  $('sf-package').addEventListener('change',()=>operation(async()=>{
    const file=$('sf-package').files[0];if(!file)return;
    $('sf-import-text').textContent='正在导入…';
    try{
      const body=new FormData();body.append('package',file);
      const p=await api('/api/shortfilms/import',{method:'POST',body});
      project=null;selected.clear();current='';dirty=false;media='frame';$('sf-confirm').checked=false;
      render(p,true);await refreshProjects();
    }finally{$('sf-import-text').textContent='导入分镜 ZIP';$('sf-package').value='';}
  }));
  $('sf-project').addEventListener('change',()=>operation(()=>loadProject($('sf-project').value)));
  $('sf-refresh').addEventListener('click',()=>operation(()=>refreshProjects()));
  $('sf-open-soundtrack').addEventListener('click',()=>{if(project)$('sf-soundtrack').scrollIntoView({behavior:'smooth',block:'start'});});
  $('sf-shots').addEventListener('change',e=>{
    if(!e.target.matches('.sf-shot-check'))return;
    e.target.checked?selected.add(e.target.dataset.sid):selected.delete(e.target.dataset.sid);confirmReset();
  });
  $('sf-shots').addEventListener('click',e=>{
    const b=e.target.closest('.sf-open-shot');if(!b)return;
    if(dirty){error('当前提示词尚未保存，请先保存后切换镜头。');return;}
    error();current=b.dataset.sid;media='frame';$('sf-task-id').value='';renderRows();renderDetail(true);
  });
  for(const id of ['sf-resolution','sf-limit','sf-regenerate','sf-upscale','sf-generate-audio'])$(id).addEventListener('change',confirmReset);
  $('sf-local-upscale').addEventListener('change',()=>renderDetail());
  $('sf-confirm').addEventListener('change',summary);
  $('sf-select-trial').addEventListener('click',()=>{selected=new Set(['S03','S07','S28'].filter(id=>project.shots.some(s=>s.id===id)&&!occupied(id)));if(!selected.size)selected=new Set(project.shots.filter(s=>!occupied(s.id)).slice(0,3).map(s=>s.id));renderRows();confirmReset();});
  $('sf-select-pending').addEventListener('click',()=>{selected=new Set(project.shots.filter(s=>s.status==='pending'&&!s.attempts.length&&!occupied(s.id)).map(s=>s.id));renderRows();confirmReset();});
  $('sf-select-clear').addEventListener('click',()=>{selected.clear();renderRows();confirmReset();});
  $('sf-prompt').addEventListener('input',()=>{dirty=true;renderDetail();});
  $('sf-sound').addEventListener('input',()=>{dirty=true;renderDetail();});
  $('sf-save-prompt').addEventListener('click',()=>operation(async()=>{
    const p=await post(endpoint()+'/shots/'+current+'/edit',{video_prompt:$('sf-prompt').value,sound:$('sf-sound').value});dirty=false;render(p,true);
  }));
  document.querySelectorAll('[data-sf-media]').forEach(b=>b.addEventListener('click',()=>{media=b.dataset.sfMedia;renderDetail();}));
  $('sf-start').addEventListener('click',()=>operation(async()=>{
    if(dirty)throw new Error('请先保存当前镜头提示词。');
    const submitted=[...selected];
    const p=await post(endpoint()+'/start',{shot_ids:submitted,resolution:$('sf-resolution').value,upscale_mode:$('sf-upscale').value,generate_audio:$('sf-generate-audio').checked,
      max_generation_seconds:Number($('sf-limit').value),regenerate:$('sf-regenerate').checked,confirmed:$('sf-confirm').checked});
    submitted.forEach(id=>selected.delete(id));
    $('sf-confirm').checked=false;$('sf-regenerate').checked=false;render(p);
  }));
  $('sf-pause').addEventListener('click',()=>operation(async()=>render(await post(endpoint()+'/pause'))));
  $('sf-resume').addEventListener('click',()=>operation(async()=>{render(await post(endpoint()+'/shots/'+current+'/resume',{task_id:$('sf-task-id').value.trim()}));$('sf-task-id').value='';}));
  $('sf-trim').addEventListener('click',()=>operation(async()=>render(await post(endpoint()+'/shots/'+current+'/trim',{trim_start:Number($('sf-trim-start').value),upscale_mode:'none'}))));
  $('sf-enhance').addEventListener('click',()=>operation(async()=>render(await post(endpoint()+'/shots/'+current+'/trim',{trim_start:Number($('sf-trim-start').value),upscale_mode:$('sf-local-upscale').value}))));
  $('sf-audio-file').addEventListener('change',()=>operation(async()=>{
    const file=$('sf-audio-file').files[0];if(!file)return;
    $('sf-audio-import-text').textContent='正在导入声音…';
    try{
      const body=new FormData();body.append('audio',file);
      render(await api(endpoint()+'/shots/'+current+'/audio',{method:'POST',body}));
    }finally{$('sf-audio-file').value='';$('sf-audio-import-text').textContent='为当前片段导入配音 / 音效';}
  }));
  $('sf-audio-reset').addEventListener('click',()=>operation(async()=>render(await api(endpoint()+'/shots/'+current+'/audio',{method:'DELETE'}))));
  $('sf-export-selected').addEventListener('click',()=>operation(async()=>render(await post(endpoint()+'/export',{shot_ids:[...selected]}))));
  $('sf-export-all').addEventListener('click',()=>operation(async()=>render(await post(endpoint()+'/export'))));
  $('sf-view-animatic').addEventListener('click',()=>{$('sf-animatic').hidden=!$('sf-animatic').hidden;if($('sf-animatic').hidden)$('sf-animatic').pause();});
  window.addEventListener('studio:view',e=>{if(e.detail==='shortfilm')refreshProjects().catch(error);else{clearTimeout(timer);document.querySelectorAll('#view-shortfilm video').forEach(v=>v.pause());}});
  window.addEventListener('focus',()=>{if(visible())refreshProjects().catch(error);});
  window.addEventListener('studio:shortfilm-refresh',()=>{if(visible()&&project)loadProject().catch(error);});
  window.addEventListener('beforeunload',()=>clearTimeout(timer));
  refreshProjects().catch(error);
})();
