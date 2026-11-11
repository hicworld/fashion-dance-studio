/* Local non-destructive editing. Generation and enhancement keep their own queues. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  if(!$('sf-editor'))return;
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clone=items=>items.map(i=>({...i}));
  const signature=items=>JSON.stringify(items);
  const uid=()=>{const a=new Uint8Array(16);crypto.getRandomValues(a);return Array.from(a,b=>b.toString(16).padStart(2,'0')).join('');};
  const num=n=>Number(Number(n||0).toFixed(6));
  const time=n=>{n=Math.max(0,Number(n)||0);return String(Math.floor(n/60)).padStart(2,'0')+':'+(n%60).toFixed(3).padStart(6,'0');};
  let project=null,state=null,items=[],revision=0,selected='',dirty=false,fieldDirty=false,conflict=false;
  let loading=false,saving=null,renderingRequest=false,editVersion=0,epoch=0,saveTimer,pollTimer,materialSignature='',lastCompletedRender='';
  let cursor=0,playing=false,playIndex=-1,playToken=0,assetMode=false,raf=0,drag=null,materialPending=false;
  const materialNodes=new Map();
  const video=$('sfe-sequence-video');
  const shot=sid=>project?.shots.find(s=>s.id===sid);
  const ready=s=>Boolean(s?.status==='done'&&s.clip_url);
  const fps=()=>Number(project?.fps)||24;
  const length=i=>num(i.out_seconds-i.in_seconds);
  const total=()=>num(items.reduce((sum,i)=>sum+length(i),0));
  const selectedItem=()=>items.find(i=>i.id===selected);
  const endpoint=(pid=project?.id)=>'/api/shortfilms/'+pid+'/editing';
  const visible=()=>!$('view-shortfilm').hidden;
  const invalidItems=()=>items.some(i=>!ready(shot(i.sid))||i.in_seconds<0||i.out_seconds>Number(shot(i.sid)?.duration_seconds)+.0001||length(i)<1/fps()-.0001);
  function error(value=''){$('sfe-error').textContent=value.message||value;$('sfe-error').hidden=!value;}
  function announce(value){$('sfe-announcement').textContent=value;}
  async function api(url,body){
    const response=await fetch(url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await response.json();
    if(!response.ok){const e=new Error(data.error||'剪辑操作未完成，请检查本地记录。');e.status=response.status;throw e;}
    return data;
  }
  function stopPlayback(){
    playing=false;playToken++;video.pause();cancelAnimationFrame(raf);updateTransport();
  }
  function updateTransport(){
    const duration=total();cursor=Math.max(0,Math.min(cursor,duration));
    $('sfe-seek').max=String(duration);$('sfe-seek').value=String(cursor);
    $('sfe-seek').disabled=!items.length;
    $('sfe-cursor').textContent=time(cursor)+' / '+time(duration);
    $('sfe-play').textContent=playing?'暂停播放':'播放时间线';
    $('sfe-play').disabled=!items.length||loading||invalidItems();
    $('sfe-restart').disabled=!items.length;
    $('sfe-playback-mode').textContent=assetMode?'素材预览 · 尚未改变时间线':'顺序预览 · 切镜可能短暂加载';
    $('sfe-timeline').querySelectorAll('[data-edit-item]').forEach(n=>n.classList.toggle('is-playing',playing&&items[playIndex]?.id===n.dataset.editItem));
  }
  function renderStatus(){
    if(!project)return;
    $('sfe-conflict').hidden=!conflict;
    $('sfe-save-state').textContent=loading?'读取时间线…':conflict?'版本冲突 · 草稿已保留':saving?'正在保存…':fieldDirty?'取用范围有未应用修改':dirty?'草稿待保存':'已自动保存 · 版本 '+revision;
    $('sfe-save').disabled=loading||Boolean(saving)||conflict||(!dirty&&!fieldDirty);
    $('sfe-add-ready').disabled=loading||!project.shots.some(ready)||items.length>=200;
    $('sfe-render').disabled=loading||renderingRequest||Boolean(state?.busy)||!items.length||invalidItems()||conflict;
    $('sfe-render').textContent=state?.busy?'连续预览生成中…':dirty||fieldDirty?'保存并生成连续预览 · 本地':'生成连续预览 · 本地';
    $('sfe-pause-render').disabled=!state?.busy;
    $('sfe-render-stage').textContent=state?.stage||'草稿自动保存，连续预览使用本机 FFmpeg，不调用生成 API。';
    if(invalidItems())$('sfe-render-stage').textContent+=' 时间线中有待处理、缺失或取用范围已变化的素材，请检查。';
    $('sfe-total').textContent=items.length+' 段 · '+time(total());
    const render=state?.render,url=render?.preview_url;
    $('sfe-render-video').hidden=!url;$('sfe-download').hidden=!url;
    if(url){
      if($('sfe-render-video').getAttribute('src')!==url)$('sfe-render-video').src=url;
      $('sfe-download').href=url;
      const stale=Boolean(render.stale||render.revision!==revision||dirty||fieldDirty);
      $('sfe-render-note').classList.toggle('is-stale',stale);
      $('sfe-render-note').textContent=stale?'此为旧版本预览：草稿或素材已变化，请重新生成。':render.published?'已按当前剪辑顺序生成，并更新为最近试剪，可继续制作全片声音。':'连续预览已生成。当前版本未发布为最近试剪，请检查草稿或素材是否已更新。';
    }else{$('sfe-render-note').textContent='';$('sfe-render-note').classList.remove('is-stale');}
    updateTransport();
  }
  function materialLabel(s){return ready(s)?'可加入':s.source_url?'待处理':s.status==='failed'?'生成失败':'尚未完成';}
  function renderMaterials(){
    if(drag?.started){materialPending=true;return;}
    const grid=$('sfe-materials'),seen=new Set(),materials=project.shots.filter(s=>s.source_url||s.clip_url);
    for(const s of materials){
      seen.add(s.id);let node=materialNodes.get(s.id);
      if(!node){
        node=document.createElement('article');node.className='sfe-material';node.dataset.materialSid=s.id;
        node.innerHTML='<div class="sfe-material-drag" data-sfe-drag="material" role="button" tabindex="0"><img alt="" draggable="false" loading="lazy"><span class="sfe-material-status"><span></span><small></small></span></div><div class="sfe-material-body"><strong class="sfe-material-name"></strong><div class="sfe-material-actions"><button type="button" data-sfe-preview>预览片段</button><button type="button" data-sfe-add>＋ 加入</button></div></div>';
        materialNodes.set(s.id,node);grid.append(node);
      }
      const handle=node.querySelector('[data-sfe-drag]');handle.dataset.sid=s.id;handle.setAttribute('aria-disabled',String(!ready(s)));
      handle.setAttribute('aria-label','拖动 '+s.id+' '+s.title+' 到时间线，或按 Enter 加入');
      const image=node.querySelector('img');if(image.getAttribute('src')!==s.frame_url)image.src=s.frame_url;
      node.querySelector('.sfe-material-status span').textContent=s.id+' · '+materialLabel(s);
      node.querySelector('.sfe-material-status small').textContent=num(s.duration_seconds)+' 秒';
      node.querySelector('.sfe-material-name').textContent=s.title;
      for(const key of ['preview','add']){const b=node.querySelector('[data-sfe-'+key+']');b.dataset.sid=s.id;b.disabled=loading||!ready(s);b.setAttribute('aria-label',(key==='preview'?'预览片段 ':'加入片段 ')+s.id+' '+s.title);}
    }
    for(const [sid,node] of materialNodes)if(!seen.has(sid)){node.remove();materialNodes.delete(sid);}
    const usable=materials.filter(ready).length;
    $('sfe-material-count').textContent=usable+' 个片段可用 · '+(materials.length-usable)+' 个待处理';
    let empty=grid.querySelector('.sfe-material-empty');
    if(!materials.length&&!empty){empty=document.createElement('p');empty.className='sfe-material-empty';empty.textContent='还没有生成的视频片段。先在上方选择镜头生成，原片下载后会出现在这里，处理完成即可加入时间线。';grid.append(empty);}
    else if(materials.length&&empty)empty.remove();
    materialPending=false;
  }
  function renderTimeline(focusId=''){
    const strip=$('sfe-timeline'),scroll=$('sfe-timeline-viewport').scrollLeft;
    if(!items.some(i=>i.id===selected))selected=items[0]?.id||'';
    if(!items.length){strip.innerHTML='<div class="sfe-timeline-empty">将上方片段拖到这里<br><small>可以重复加入、裁短、静音，再生成连续预览</small></div>';}
    else{
      let start=0;
      strip.innerHTML=items.map((i,index)=>{const s=shot(i.sid),from=start;start+=length(i);return `<article class="sfe-clip ${i.id===selected?'is-selected':''} ${ready(s)?'':'is-unavailable'}" data-edit-item="${i.id}" tabindex="0" aria-label="第 ${index+1} 段 ${esc(i.sid+' '+(s?.title||'素材缺失'))}，${length(i)} 秒${i.muted?'，静音':''}"><div class="sfe-clip-thumb"><img src="${esc(s?.frame_url||'')}" alt="" draggable="false"><span class="sfe-clip-index">${index+1} · ${time(from)}</span><button class="sfe-clip-handle" type="button" data-sfe-drag="item" data-item-id="${i.id}" aria-label="拖动第 ${index+1} 段 ${esc(i.sid)} 调整顺序"><svg viewBox="0 0 12 16" fill="currentColor" stroke="none" aria-hidden="true"><circle cx="3" cy="3" r="1"/><circle cx="9" cy="3" r="1"/><circle cx="3" cy="8" r="1"/><circle cx="9" cy="8" r="1"/><circle cx="3" cy="13" r="1"/><circle cx="9" cy="13" r="1"/></svg>移动</button></div><div class="sfe-clip-info"><strong>${esc(i.sid+' · '+(s?.title||'素材缺失'))}</strong><small>${num(i.in_seconds)}—${num(i.out_seconds)} 秒 · ${i.muted?'静音':ready(s)?'保留声音':'等待素材处理'}</small></div></article>`;}).join('')+'<div class="sfe-drop-end">拖到这里<br>添加到结尾</div>';
    }
    $('sfe-timeline-viewport').scrollLeft=scroll;
    renderInspector(true);renderStatus();
    if(focusId)strip.querySelector(`[data-edit-item="${focusId}"]`)?.focus({preventScroll:true});
  }
  function renderInspector(force=false){
    const item=selectedItem();$('sfe-inspector').hidden=!item;if(!item)return;
    const index=items.indexOf(item),s=shot(item.sid);
    $('sfe-item-title').textContent='第 '+(index+1)+' 段 · '+item.sid+' · '+(s?.title||'素材缺失');
    if(force||!fieldDirty){$('sfe-in').value=num(item.in_seconds);$('sfe-out').value=num(item.out_seconds);$('sfe-muted').checked=item.muted;fieldDirty=false;}
    for(const id of ['sfe-in','sfe-out']){$(id).max=String(s?.duration_seconds||0);$(id).step=String(num(1/fps()));}
    $('sfe-move-left').disabled=index===0;$('sfe-move-right').disabled=index===items.length-1;
    $('sfe-duplicate').disabled=items.length>=200||total()+length(item)>1200;
    $('sfe-trim-note').textContent=`范围相对于当前成片片段（${num(s?.duration_seconds)} 秒），按 ${fps()} fps 对齐，至少保留一帧；应用后自动保存，原片不变。`;
  }
  function mutate(action,focusId=''){
    if(loading||!state){error('时间线还在读取，请稍候。');return false;}
    stopPlayback();assetMode=false;error();action();editVersion++;dirty=true;fieldDirty=false;
    cursor=Math.min(cursor,total());renderTimeline(focusId);scheduleSave();return true;
  }
  function canLeaveFields(){if(!fieldDirty)return true;error('取用范围有未应用修改，请先应用或撤回本次修改。');return false;}
  function add(sid,index=items.length){
    if(!canLeaveFields())return;
    const s=shot(sid);if(!ready(s)){error('此片段尚未处理完成，暂不能加入。');return;}
    const end=num(Math.round(s.duration_seconds*fps())/fps());
    if(items.length>=200||total()+end>1200){error('时间线最多 200 段、20 分钟。');return;}
    const item={id:uid(),sid,in_seconds:0,out_seconds:end,muted:false};
    mutate(()=>{items.splice(index,0,item);selected=item.id;},item.id);announce(sid+' 已加入时间线');
  }
  function move(id,index){
    if(!canLeaveFields())return;
    const from=items.findIndex(i=>i.id===id);if(from<0)return;
    index=Math.max(0,Math.min(index,items.length-1));if(index===from)return;
    mutate(()=>{const [item]=items.splice(from,1);items.splice(index,0,item);selected=id;},id);announce('已移动到第 '+(index+1)+' 段');
  }
  function removeSelected(){
    if(!canLeaveFields())return;const index=items.findIndex(i=>i.id===selected);if(index<0)return;
    mutate(()=>{items.splice(index,1);selected=items[Math.min(index,items.length-1)]?.id||'';},items[index+1]?.id||items[index-1]?.id||'');
  }
  function duplicateSelected(){
    if(!canLeaveFields())return;const item=selectedItem();if(!item)return;
    if(items.length>=200||total()+length(item)>1200){error('时间线最多 200 段、20 分钟。');return;}
    const copy={...item,id:uid()},index=items.indexOf(item);
    mutate(()=>{items.splice(index+1,0,copy);selected=copy.id;},copy.id);announce('已重复此段');
  }
  function applyInspector(){
    const item=selectedItem();if(!item)return true;
    const a=Number($('sfe-in').value),b=Number($('sfe-out').value),maximum=Math.round(Number(shot(item.sid)?.duration_seconds)*fps());
    const left=Math.round(a*fps()),right=Math.round(b*fps());
    if(!Number.isFinite(a)||!Number.isFinite(b)||$('sfe-in').value===''||$('sfe-out').value===''||left<0||right>maximum||right-left<1){error('请填写有效入出点：范围需在当前片段内，并至少保留一帧。');return false;}
    const nextDuration=total()-length(item)+(right-left)/fps();if(nextDuration>1200+.00001){error('时间线不能超过 20 分钟。');return false;}
    return mutate(()=>Object.assign(item,{in_seconds:num(left/fps()),out_seconds:num(right/fps()),muted:$('sfe-muted').checked}));
  }
  function scheduleSave(){clearTimeout(saveTimer);if(!conflict)saveTimer=setTimeout(()=>{if(!fieldDirty)saveDraft().catch(error);},650);renderStatus();}
  async function saveDraft(){
    clearTimeout(saveTimer);if(!project||!state||loading)return false;
    if(fieldDirty&&!applyInspector())return false;
    if(conflict){error('请先处理时间线版本冲突。');return false;}
    if(saving){await saving;if(dirty)return saveDraft();return true;}
    if(!dirty)return true;
    const pid=project.id,guard=epoch,version=editVersion,snapshot=clone(items),expected=revision;
    const task=(async()=>{
      try{
        const data=await api(endpoint(pid)+'/save',{expected_revision:expected,items:snapshot});
        if(guard!==epoch)return false;
        state=data;revision=data.revision;
        if(editVersion===version){const changed=signature(items)!==signature(data.items);items=clone(data.items);dirty=false;if(changed&&!fieldDirty&&!drag?.started)renderTimeline();}
        else dirty=true;
        return true;
      }catch(e){
        if(guard===epoch){if(e.status===409||e.status===412||/版本|revision/i.test(e.message))conflict=true;error(e);}
        return false;
      }
    })();
    saving=task;renderStatus();
    const result=await task;if(saving===task)saving=null;
    if(guard===epoch){renderStatus();if(result&&dirty)return saveDraft();}
    return result;
  }
  function receive(data,initial=false){
    if(drag?.started){drag.pendingState=data;return;}
    if(!initial&&data.revision<revision)return;
    if(initial||(!dirty&&!saving&&!fieldDirty)){
      const changed=signature(items)!==signature(data.items);items=clone(data.items);revision=data.revision;dirty=false;conflict=false;
      state=data;if(changed||initial)renderTimeline();
    }else{
      if(data.revision>revision&&!saving)conflict=true;
      state={...data,items:state?.items||data.items};
    }
    if(data.error)error(data.error);renderStatus();
    clearTimeout(pollTimer);
    if(data.busy&&visible())pollTimer=setTimeout(()=>loadState().catch(error),1800);
    if(!data.busy&&data.render?.id&&lastCompletedRender!==data.render.id){
      lastCompletedRender=data.render.id;
      if(!initial)window.dispatchEvent(new CustomEvent('studio:shortfilm-refresh'));
    }
  }
  async function loadState(initial=false){
    if(!project)return;const guard=epoch,pid=project.id;
    const data=await api(endpoint(pid));if(guard!==epoch)return;receive(data,initial);
  }
  async function changeProject(p){
    clearTimeout(saveTimer);clearTimeout(pollTimer);finishDrag(false);stopPlayback();
    epoch++;project=p;state=null;items=[];revision=0;selected='';dirty=false;fieldDirty=false;conflict=false;saving=null;renderingRequest=false;
    cursor=0;playIndex=-1;assetMode=false;loading=true;editVersion=0;materialSignature='';lastCompletedRender='';
    video.removeAttribute('src');video.load();$('sfe-preview-empty').hidden=false;error();
    materialNodes.clear();$('sfe-materials').replaceChildren();$('sf-editor').hidden=false;renderMaterials();renderTimeline();
    const guard=epoch;
    try{await loadState(true);}finally{if(guard===epoch){loading=false;renderMaterials();renderStatus();}}
  }
  function locationAt(position){
    let start=0;
    for(let index=0;index<items.length;index++){const duration=length(items[index]);if(position<start+duration-.000001||index===items.length-1)return {index,start,offset:Math.min(duration,Math.max(0,position-start))};start+=duration;}
    return null;
  }
  async function displaySequence(position,autoplay=false){
    const located=locationAt(position);if(!located)return;
    const item=items[located.index],s=shot(item.sid);if(!ready(s)){stopPlayback();error('此段素材暂不可用，请等待处理完成或移除。');return;}
    const token=++playToken;assetMode=false;playIndex=located.index;cursor=Math.max(0,Math.min(position,total()));
    $('sfe-preview-empty').hidden=true;video.muted=item.muted;video.controls=false;
    const wanted=item.in_seconds+located.offset;
    const seekAndPlay=async()=>{
      if(token!==playToken)return;
      try{video.currentTime=Math.min(wanted,item.out_seconds);if(autoplay){await video.play();if(token!==playToken){video.pause();return;}playing=true;tick();}else{video.pause();playing=false;}updateTransport();}
      catch(e){if(token===playToken){playing=false;error('预览未能播放，请重试或生成连续预览。');updateTransport();}}
    };
    if(video.getAttribute('src')!==s.clip_url){
      video.src=s.clip_url;
      const loaded=()=>{video.removeEventListener('loadedmetadata',loaded);seekAndPlay();};
      video.addEventListener('loadedmetadata',loaded,{once:true});video.load();
    }else if(video.readyState>=1){await seekAndPlay();}
    else video.addEventListener('loadedmetadata',seekAndPlay,{once:true});
    updateTransport();
  }
  function nextSegment(){
    const next=playIndex+1;
    if(next>=items.length){cursor=total();stopPlayback();return;}
    const position=num(items.slice(0,next).reduce((n,i)=>n+length(i),0));
    video.pause();playing=false;displaySequence(position,true).catch(error);
  }
  function tick(){
    cancelAnimationFrame(raf);if(!playing||assetMode)return;
    const item=items[playIndex];if(!item){stopPlayback();return;}
    const before=items.slice(0,playIndex).reduce((n,i)=>n+length(i),0);
    cursor=Math.min(total(),before+Math.max(0,Math.min(length(item),video.currentTime-item.in_seconds)));
    if(video.currentTime>=item.out_seconds-.005){nextSegment();return;}
    updateTransport();raf=requestAnimationFrame(tick);
  }
  async function previewAsset(sid){
    const s=shot(sid);if(!ready(s))return;stopPlayback();assetMode=true;playIndex=-1;
    $('sfe-preview-empty').hidden=true;video.controls=true;video.muted=false;video.src=s.clip_url;
    try{await video.play();}catch(_){error('请点击播放器开始素材预览。');}updateTransport();
  }
  function clearDropMarker(){
    $('sfe-timeline-viewport').classList.remove('is-drop-target');
    $('sfe-timeline').querySelectorAll('.drop-before,.drop-after').forEach(n=>n.classList.remove('drop-before','drop-after'));
  }
  function dragPosition(x,y){
    const view=$('sfe-timeline-viewport'),rect=view.getBoundingClientRect();
    if(x<rect.left||x>rect.right||y<rect.top||y>rect.bottom)return null;
    if(x<rect.left+35)view.scrollLeft-=16;else if(x>rect.right-35)view.scrollLeft+=16;
    const nodes=[...$('sfe-timeline').querySelectorAll('[data-edit-item]')];
    let index=nodes.findIndex(n=>x<n.getBoundingClientRect().left+n.getBoundingClientRect().width/2);if(index<0)index=nodes.length;
    view.classList.add('is-drop-target');
    if(index<nodes.length)nodes[index].classList.add('drop-before');else if(nodes.length)nodes[nodes.length-1].classList.add('drop-after');else $('sfe-timeline-empty')?.classList.add('drop-before');
    return index;
  }
  function finishDrag(commit,event){
    if(!drag)return;const activeDrag=drag;drag=null;
    activeDrag.ghost?.remove();document.body.classList.remove('sfe-is-dragging');clearDropMarker();
    try{activeDrag.handle.releasePointerCapture(activeDrag.pointerId);}catch(_){}
    if(commit&&activeDrag.started&&event){
      const index=dragPosition(event.clientX,event.clientY);clearDropMarker();
      if(index!==null){
        if(activeDrag.kind==='material')add(activeDrag.sid,index);
        else{const from=items.findIndex(i=>i.id===activeDrag.id);move(activeDrag.id,index-(from<index?1:0));}
      }
    }
    if(materialPending)renderMaterials();
    if(activeDrag.pendingState)receive(activeDrag.pendingState);
  }
  $('sf-editor').addEventListener('pointerdown',e=>{
    const handle=e.target.closest('[data-sfe-drag]');if(!handle||e.button!==0||loading)return;
    if(!canLeaveFields())return;
    if(handle.dataset.sfeDrag==='material'&&!ready(shot(handle.dataset.sid)))return;
    e.preventDefault();handle.focus({preventScroll:true});
    drag={handle,kind:handle.dataset.sfeDrag,sid:handle.dataset.sid,id:handle.dataset.itemId,pointerId:e.pointerId,startX:e.clientX,startY:e.clientY,started:false};
    handle.setPointerCapture(e.pointerId);
  });
  document.addEventListener('pointermove',e=>{
    if(!drag||e.pointerId!==drag.pointerId)return;
    if(!drag.started&&Math.hypot(e.clientX-drag.startX,e.clientY-drag.startY)<6)return;
    if(!drag.started){drag.started=true;drag.ghost=document.createElement('div');drag.ghost.className='sfe-drag-ghost';drag.ghost.textContent=drag.kind==='material'?'加入 '+drag.sid:'移动 '+(items.find(i=>i.id===drag.id)?.sid||'片段');document.body.append(drag.ghost);document.body.classList.add('sfe-is-dragging');}
    e.preventDefault();drag.ghost.style.left=e.clientX+'px';drag.ghost.style.top=e.clientY+'px';clearDropMarker();dragPosition(e.clientX,e.clientY);
  },{passive:false});
  document.addEventListener('pointerup',e=>{if(drag?.pointerId===e.pointerId)finishDrag(true,e);});
  document.addEventListener('pointercancel',()=>finishDrag(false));
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&drag){e.preventDefault();finishDrag(false);}});
  $('sfe-materials').addEventListener('click',e=>{const addButton=e.target.closest('[data-sfe-add]'),preview=e.target.closest('[data-sfe-preview]');if(addButton)add(addButton.dataset.sid);else if(preview)previewAsset(preview.dataset.sid);});
  $('sfe-materials').addEventListener('keydown',e=>{const handle=e.target.closest('[data-sfe-drag="material"]');if(handle&&(e.key==='Enter'||e.key===' ')){e.preventDefault();add(handle.dataset.sid);}});
  $('sfe-timeline').addEventListener('click',e=>{
    const node=e.target.closest('[data-edit-item]');if(!node||drag?.started||!canLeaveFields())return;
    selected=node.dataset.editItem;$('sfe-timeline').querySelectorAll('[data-edit-item]').forEach(n=>n.classList.toggle('is-selected',n===node));renderInspector(true);renderStatus();
  });
  $('sfe-timeline').addEventListener('keydown',e=>{
    const node=e.target.closest('[data-edit-item]');if(!node)return;
    if(!canLeaveFields())return;
    selected=node.dataset.editItem;const index=items.findIndex(i=>i.id===selected);
    if(e.key==='Delete'||e.key==='Backspace'){e.preventDefault();removeSelected();}
    else if((e.key==='d'||e.key==='D')&&(e.ctrlKey||e.metaKey)){e.preventDefault();duplicateSelected();}
    else if(e.key==='ArrowLeft'||e.key==='ArrowRight'){
      e.preventDefault();const next=index+(e.key==='ArrowLeft'?-1:1);
      if(e.altKey)move(selected,next);else if(items[next]){selected=items[next].id;renderTimeline(selected);}
    }else if(e.key==='Enter'||e.key===' '){e.preventDefault();renderInspector(true);renderStatus();}
  });
  for(const id of ['sfe-in','sfe-out','sfe-muted'])$(id).addEventListener('input',()=>{fieldDirty=true;renderStatus();});
  $('sfe-apply').addEventListener('click',applyInspector);
  $('sfe-reset-fields').addEventListener('click',()=>{fieldDirty=false;error();renderInspector(true);if(dirty)scheduleSave();else renderStatus();});
  $('sfe-move-left').addEventListener('click',()=>move(selected,items.findIndex(i=>i.id===selected)-1));
  $('sfe-move-right').addEventListener('click',()=>move(selected,items.findIndex(i=>i.id===selected)+1));
  $('sfe-duplicate').addEventListener('click',duplicateSelected);$('sfe-remove').addEventListener('click',removeSelected);
  $('sfe-add-ready').addEventListener('click',()=>{
    if(!canLeaveFields())return;
    const additions=project.shots.filter(ready).map(s=>({id:uid(),sid:s.id,in_seconds:0,out_seconds:num(Math.round(s.duration_seconds*fps())/fps()),muted:false}));
    if(items.length+additions.length>200||total()+additions.reduce((n,i)=>n+length(i),0)>1200){error('加入后会超过 200 段或 20 分钟上限，请逐段加入。');return;}
    mutate(()=>{items.push(...additions);selected=additions[0]?.id||selected;});
  });
  $('sfe-save').addEventListener('click',()=>saveDraft().catch(error));
  $('sfe-load-saved').addEventListener('click',async()=>{try{const guard=epoch,data=await api(endpoint());if(guard!==epoch)return;stopPlayback();fieldDirty=false;dirty=false;conflict=false;receive(data,true);error();}catch(e){error(e);}});
  $('sfe-keep-draft').addEventListener('click',async()=>{
    try{if(fieldDirty&&!applyInspector())return;const guard=epoch,data=await api(endpoint());if(guard!==epoch)return;revision=data.revision;state=data;conflict=false;dirty=true;error();await saveDraft();}catch(e){error(e);}
  });
  $('sfe-render').addEventListener('click',async()=>{
    if(renderingRequest)return;renderingRequest=true;renderStatus();const requestEpoch=epoch;
    try{if(!await saveDraft())return;const guard=epoch,data=await api(endpoint()+'/render',{expected_revision:revision});if(guard!==epoch)return;error();receive(data);window.dispatchEvent(new CustomEvent('studio:shortfilm-refresh'));}catch(e){error(e);}finally{if(requestEpoch===epoch){renderingRequest=false;renderStatus();}}
  });
  $('sfe-pause-render').addEventListener('click',async()=>{try{const guard=epoch,data=await api(endpoint()+'/pause',{});if(guard===epoch)receive(data);}catch(e){error(e);}});
  $('sfe-play').addEventListener('click',()=>{if(playing){stopPlayback();return;}assetMode=false;if(cursor>=total()-.00001)cursor=0;displaySequence(cursor,true).catch(error);});
  $('sfe-restart').addEventListener('click',()=>{stopPlayback();cursor=0;displaySequence(0,false).catch(error);});
  $('sfe-seek').addEventListener('input',()=>{const position=Number($('sfe-seek').value);stopPlayback();cursor=position;displaySequence(cursor,false).catch(error);});
  video.addEventListener('ended',()=>{if(!assetMode&&playing)nextSegment();});
  video.addEventListener('error',()=>{if(video.getAttribute('src')){stopPlayback();error('片段预览加载失败。请检查素材，或生成连续预览后播放。');}});
  $('sf-open-editor')?.addEventListener('click',()=>{if(project)$('sf-editor').scrollIntoView({behavior:'smooth',block:'start'});});
  window.addEventListener('studio:shortfilm',e=>{
    const p=e.detail;if(!p?.id)return;
    if(project?.id!==p.id){changeProject(p).catch(error);return;}
    project=p;renderMaterials();renderStatus();
    $('sfe-timeline').querySelectorAll('[data-edit-item]').forEach(node=>{
      const item=items.find(i=>i.id===node.dataset.editItem),s=item&&shot(item.sid);if(!item)return;
      node.classList.toggle('is-unavailable',!ready(s));
      node.querySelector('.sfe-clip-info small').textContent=`${num(item.in_seconds)}—${num(item.out_seconds)} 秒 · ${item.muted?'静音':ready(s)?'保留声音':'等待素材处理'}`;
    });
    const next=JSON.stringify(p.shots.map(s=>[s.id,s.status,s.clip_url,s.clip_version,s.duration_seconds]));
    if(next!==materialSignature){materialSignature=next;if(!loading)loadState().catch(error);}
  });
  window.addEventListener('studio:shortfilm-changing',e=>{
    if(project&&e.detail?.id!==project.id&&(dirty||fieldDirty||saving)){e.preventDefault();error('剪辑草稿尚未保存，请先保存，或处理取用范围与版本冲突后切换项目。');}
  });
  $('sf-package')?.addEventListener('change',e=>{
    if(dirty||fieldDirty||saving){e.stopImmediatePropagation();$('sf-package').value='';error('当前剪辑草稿尚未保存，请先保存或撤回本次范围修改，再导入另一个项目。');}
  },true);
  window.addEventListener('studio:view',e=>{if(e.detail!=='shortfilm'){stopPlayback();$('sfe-render-video').pause();clearTimeout(pollTimer);}else if(project)loadState().catch(error);});
  window.addEventListener('focus',()=>{if(visible()&&project&&!loading)loadState().catch(error);});
  window.addEventListener('beforeunload',e=>{clearTimeout(pollTimer);if(dirty||fieldDirty||saving){e.preventDefault();e.returnValue='';}});
})();
