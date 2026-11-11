/* Local studio UI. Only explicit button presses submit billable operations. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const files = new Map();
  const jobs = new Map();
  const publicKeys = ['TOS_BUCKET', 'TOS_ENDPOINT', 'TOS_REGION', 'SEEDREAM_MODEL'];
  const secretKeys = ['ARK_API_KEY', 'VOLC_ACCESS_KEY_ID', 'VOLC_SECRET_ACCESS_KEY', 'FAL_KEY', 'MINIMAX_API_KEY', 'ELEVENLABS_API_KEY'];
  const activeKey = 'fashion-studio-active-job';
  const motionKey = 'fashion-studio-motion-model';
  const themeKey = 'fashion-studio-theme';
  const legacyEditModelId = 'fal-kling-o3-pro-edit';
  const editModelIds = ['minimax-h3-edit', 'volc-seedance-2.5-edit', legacyEditModelId];
  const editResolutions = {'minimax-h3-edit': '768P', 'volc-seedance-2.5-edit': '1080p'};
  const motionOptions = Object.freeze({
    'volc-dreamactor-2': {label: '即梦 · 动作模仿 2.0', shortLabel: '即梦动作模仿2.0', minSeconds: 1, maxSeconds: 30, currency: 'CNY', rate: .4},
    'fal-kling-v3-pro': {label: 'fal · 可灵 3.0 Pro', shortLabel: '可灵3.0 Pro', minSeconds: 3, maxSeconds: 30, currency: 'USD', rate: .168},
    [legacyEditModelId]: {label: 'fal · Kling O3 Pro 视频角色替换', shortLabel: 'O3 Pro角色替换', minSeconds: 3, maxSeconds: 15, currency: 'USD', rate: .168},
    'minimax-h3-edit': {label: 'MiniMax · H3 · 视频角色替换', shortLabel: 'MiniMax H3', minSeconds: 4, maxSeconds: 15, integerSeconds: true, resolutions: ['768P', '2K']},
    'volc-seedance-2.5-edit': {label: '火山方舟 · Seedance 2.5 · 视频角色替换', shortLabel: 'Seedance 2.5', minSeconds: 4, maxSeconds: 30, resolutions: ['720p', '1080p']}
  });
  let selectedMotionId = 'volc-dreamactor-2';
  let selectedEditId = 'minimax-h3-edit';
  const statusLabels = {running: ['生成中', 'running'], awaiting_confirmation: ['待确认', 'waiting'], done: ['已完成', 'done'], error: ['未完成', 'error'], interrupted: ['已中断', 'error']};
  let mode = 'compose';
  let roleCount = 1;
  let garmentMode = 'separate';
  let backgroundMode = 'original';
  let durationMode = 'clip';
  let sourceDuration = null;
  let activeId = null;
  let previewClearedForMode = false;
  let currentMedia = 'image';
  let mediaChosen = false;
  let settings = null;
  let settingsDirty = false;
  let formBusy = false;
  let confirming = false;
  let resuming = false;
  let saving = false;
  let historyFilter = 'all';
  let pollTimer = null;
  let readingJobs = null;
  let pollFailures = 0;
  let lastHistorySignature = '';
  let comparisonSession = null;
  let lastRoleSignature = '';
  let selectedTheme = 'light';
  let preferencesReady = false;
  let preferencesSaving = false;
  let pendingPreferences = null;
  const preferenceEdits = new Set();
  const initialTheme = document.documentElement.dataset.theme;
  if (['light', 'dark'].includes(initialTheme)) selectedTheme = initialTheme;
  try { activeId = localStorage.getItem(activeKey); const savedTheme = localStorage.getItem(themeKey); if (!['light', 'dark'].includes(initialTheme) && ['light', 'dark'].includes(savedTheme)) selectedTheme = savedTheme; } catch (_) { /* Storage is optional. */ }

  function icon(name) { return `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`; }
  function text(selector, value) { $(selector).textContent = value == null ? '' : String(value); }
  function secondsText(value) { return value == null || String(value).trim() === '' || !Number.isFinite(Number(value)) ? '—' : Number(value).toFixed(1).replace(/\.0$/, ''); }
  function money(value, currency = 'CNY') { return typeof value === 'number' && Number.isFinite(value) ? (currency === 'USD' ? '$' : currency === 'CNY' ? '¥' : String(currency) + ' ') + value.toFixed(2) : '待确认'; }
  function effectiveModelId() { return mode === 'video_replace' ? selectedEditId : selectedMotionId; }
  function motionId(job) {
    if (!job) return effectiveModelId();
    const saved = job.selection?.motion?.id || job.selection?.edit?.id || job.edit_model || job.motion_model;
    return typeof saved === 'string' && saved ? saved : job.mode === 'video_replace' ? legacyEditModelId : 'volc-dreamactor-2';
  }
  function resolutionText(job) {
    if (job?.mode !== 'video_replace') return '';
    const resolution = job.video_resolution || job.selection?.edit?.resolution;
    return typeof resolution === 'string' && resolution ? resolution + '请求档位' : 'API默认尺寸';
  }
  function motionOption(id) { return motionOptions[id] || {label: id || '未记录模型', shortLabel: id || '未记录模型', minSeconds: 1, maxSeconds: 30}; }
  function motionConfig(id, data = settings) {
    const configured = editModelIds.includes(id) ? data?.edit_models?.[id] : data?.motion_models?.[id];
    if (configured) return {ready: configured.ready === true, missing: Array.isArray(configured.missing) ? configured.missing : []};
    if (id === 'volc-dreamactor-2') return {ready: data?.motion_ready === true, missing: Array.isArray(data?.missing?.motion) ? data.missing.motion : []};
    return {ready: false, missing: [id === 'fal-kling-v3-pro' || id === legacyEditModelId ? '请配置 fal API Key（FAL_KEY）' : id === 'minimax-h3-edit' ? '请配置 MiniMax API Key' : id === 'volc-seedance-2.5-edit' ? '请配置方舟 API Key 和素材存储，并开通 Seedance 2.5' : '此视频模型的配置尚不可用']};
  }
  function motionPrice(id, seconds, resolution) {
    if (id === 'minimax-h3-edit') {
      const level = resolution || editResolutions[id] || '768P'; const rate = level === '2K' ? .8 : .5;
      return level + '：参考视频输入¥' + rate.toFixed(2) + '/秒＋输出¥' + rate.toFixed(2) + '/秒' + (typeof seconds === 'number' && Number.isFinite(seconds) && seconds > 0 ? '；同为' + secondsText(seconds) + '秒时约' + money(seconds * rate * 2) : '');
    }
    if (id === 'volc-seedance-2.5-edit') return '按平台返回的视频token用量估算，以平台账单为准';
    const option = motionOption(id);
    if (typeof option.rate !== 'number') return '费用以平台账单为准';
    const amount = typeof seconds === 'number' && Number.isFinite(seconds) && seconds > 0 ? '；参考 ' + secondsText(seconds) + ' 秒约 ' + money(seconds * option.rate, option.currency) : '';
    return (option.currency === 'USD' ? '$0.168' : '¥0.40') + '/生成秒' + amount;
  }
  function updateConnectionState() {
    if (!settings) return;
    const selected = motionConfig(effectiveModelId()); const imageReady = !needsImageApi() || settings.image_ready === true;
    const ready = selected.ready && imageReady;
    $('#connection-button').className = 'connection-pill' + (ready ? '' : ' incomplete');
    text('#connection-text', ready ? '所选创作服务已配置' : '所选API待配置'); $('#settings-dot').hidden = ready;
    const missing = [...new Set([...(needsImageApi() && Array.isArray(settings.missing?.image) ? settings.missing.image : []), ...selected.missing])];
    text('#settings-missing', '新建创作使用' + motionOption(effectiveModelId()).shortLabel + '。' + (missing.length ? missing.join('；') : '所需本地配置已齐全，实际服务可用性将在调用时验证。'));
  }
  function byteSize(value) { return value >= 1024 * 1024 ? (value / 1024 / 1024).toFixed(1) + ' MB' : Math.max(1, Math.round(value / 1024)) + ' KB'; }
  function createdTime(job) { const t = job.createdAt || job.created_at; const n = typeof t === 'number' ? t : Date.parse(t || ''); return Number.isFinite(n) ? n : 0; }
  function dateText(job) { const n = createdTime(job); return n ? new Date(n).toLocaleString('zh-CN', {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}) : '时间未记录'; }
  function orderedJobs() { return [...jobs.values()].sort((a, b) => createdTime(b) - createdTime(a)); }
  function runningJob() { return orderedJobs().find(job => job.status === 'running'); }
  function activeJob() { return activeId ? jobs.get(activeId) : null; }
  function safeFile(value) {
    if (typeof value !== 'string' || !value) return null;
    try { const url = new URL(value, location.origin); return url.origin === location.origin && url.pathname.startsWith('/files/') ? url.pathname + url.search : null; } catch (_) { return null; }
  }
  function imageUrl(job) { return safeFile(job?.image || job?.imageURL); }
  function videoUrl(job) { return safeFile(job?.video || job?.videoURL); }
  function sourceUrl(job) { return safeFile(job?.source_video || job?.sourceVideoURL); }
  function referenceUrl(job) { return safeFile(job?.reference_video || job?.referenceVideoURL); }
  function showToast(message, error = false) {
    const item = document.createElement('div'); item.className = 'toast' + (error ? ' error' : '');
    item.innerHTML = icon(error ? 'info' : 'check');
    const copy = document.createElement('span'); copy.textContent = message; item.append(copy);
    $('#toast-stack').append(item); setTimeout(() => item.remove(), error ? 8500 : 5000);
  }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), (options.method && options.method !== 'GET') ? 120000 : 20000);
    try {
      const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', ...options, signal: controller.signal});
      let data;
      try { data = await response.json(); } catch (_) { const error = new Error('本地服务未返回有效数据，请检查软件运行状态。'); error.httpStatus = response.status; throw error; }
      if (!response.ok) { const error = new Error(typeof data.error === 'string' ? data.error : '请求未完成（HTTP ' + response.status + '）'); error.httpStatus = response.status; throw error; }
      return data;
    } catch (error) {
      if (error instanceof TypeError || error.name === 'AbortError') { const failure = new Error('暂时无法连接本地服务，请检查软件是否仍在运行。'); failure.network = true; throw failure; }
      throw error;
    } finally { clearTimeout(timer); }
  }
  function applyTheme(value, persist = false) {
    selectedTheme = value === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.theme = selectedTheme;
    document.documentElement.style.colorScheme = selectedTheme;
    $$('[data-theme-choice]').forEach(button => { const selected = button.dataset.themeChoice === selectedTheme; button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected)); });
    try { localStorage.setItem(themeKey, selectedTheme); } catch (_) { /* Storage is optional. */ }
    if (persist) { preferenceEdits.add('theme'); savePreferences(); }
  }
  async function savePreferences() {
    if (!preferencesReady) return;
    pendingPreferences = {theme: selectedTheme, motion_model: selectedMotionId, edit_model: selectedEditId, creation_mode: mode};
    if (preferencesSaving) return;
    preferencesSaving = true;
    try {
      while (pendingPreferences) {
        const payload = pendingPreferences; pendingPreferences = null;
        await api('/api/preferences', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      }
    } catch (_) { showToast('外观与模型偏好暂未保存，请确认软件仍在运行。', true); }
    finally { preferencesSaving = false; }
  }
  async function loadPreferences() {
    try {
      const data = await api('/api/preferences');
      if (!preferenceEdits.has('theme')) applyTheme(data.theme);
      if (!preferenceEdits.has('motion_model')) selectedMotionId = ['volc-dreamactor-2', 'fal-kling-v3-pro'].includes(data.motion_model) ? data.motion_model : 'volc-dreamactor-2';
      if (!preferenceEdits.has('edit_model')) selectedEditId = editModelIds.includes(data.edit_model) ? data.edit_model : 'minimax-h3-edit';
      if (!preferenceEdits.has('creation_mode')) mode = ['compose', 'ready_image', 'video_replace'].includes(data.creation_mode) ? data.creation_mode : 'compose';
    } catch (_) { /* A local theme fallback remains available if preference storage cannot be read. */ }
    setMode(mode, false);
    preferencesReady = true;
    if (preferenceEdits.size) savePreferences();
  }
  $$('[data-theme-choice]').forEach(button => button.addEventListener('click', () => applyTheme(button.dataset.themeChoice, true)));
  function setView(view, writeHash = true) {
    if (!['studio', 'history', 'settings', 'shortfilm'].includes(view)) view = 'studio';
    closeComparison(); $('#result-video').pause();
    $$('.view').forEach(section => { section.hidden = section.id !== 'view-' + view; });
    $$('.sidebar .nav-item').forEach(button => { const selected = button.dataset.view === view; button.classList.toggle('active', selected); if (selected) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); });
    text('#page-breadcrumb', {studio: '新建创作', history: '创作记录', settings: 'API设置', shortfilm: '短片制作'}[view]);
    document.title = ({studio: '', history: '创作记录 · ', settings: 'API设置 · ', shortfilm: '短片制作 · '}[view]) + '穿搭舞蹈工作室';
    if (view !== 'shortfilm') document.querySelectorAll('#view-shortfilm video').forEach(video => video.pause());
    window.dispatchEvent(new CustomEvent('studio:view', {detail: view}));
    if (writeHash && location.hash !== '#' + view) history.replaceState(null, '', '#' + view);
    if (view === 'history') refreshJobs(true).catch(() => {});
    if (view === 'settings') loadSettings().catch(() => {});
  }
  $$('[data-view]').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
  window.addEventListener('hashchange', () => setView(location.hash.slice(1), false));

  function updateGarmentMode() {
    const separate = garmentMode === 'separate';
    $$('.garment-button').forEach(button => { const selected = button.dataset.garmentMode === garmentMode; button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected)); });
    $('#pants-card').hidden = mode !== 'compose' || !separate;
    if (!separate || mode !== 'compose') $('#pants-card').classList.remove('invalid');
    $('#upload-grid').classList.toggle('has-pants', mode === 'compose' && separate);
    text('#garment-label', separate ? '目标上衣' : '整套穿搭');
    text('#garment-placeholder', separate ? '上传上衣图片' : '上传整套穿搭图');
    text('#garment-hint', separate ? '平铺、挂拍或清晰商品图' : '整套商品图或全身穿搭参考');
    text('#garment-mode-hint', separate ? '上衣和裤子可分别上传；裤子留空时，沿用模特原下装。' : '参考图中的整套服装将穿到虚拟模特身上，人物身份沿用模特图。');
    $('#garment-input').closest('.drop-zone').setAttribute('aria-label', separate ? '上传上衣图片' : '上传整套穿搭或全身服装参考图片');
    text('#background-number', separate ? '04' : '03');
    text('#video-number', mode === 'ready_image' ? '02' : separate ? '05' : '04');
  }
  function effectiveBackgroundMode() { return mode === 'compose' && backgroundMode === 'original' ? 'replace' : backgroundMode; }
  function referenceBackground() { return mode !== 'video_replace' && effectiveBackgroundMode() === 'video_frame'; }
  function replaceReadyBackground() { return mode === 'ready_image' && effectiveBackgroundMode() !== 'original'; }
  function needsImageApi() { return mode === 'compose' || replaceReadyBackground(); }
  function requiredInputs() { return mode === 'video_replace' ? ['video', ...Array.from({length: roleCount}, (_, i) => 'role_' + (i + 1))] : mode === 'compose' ? referenceBackground() ? ['model', 'garment', 'video'] : ['model', 'garment', 'background', 'video'] : effectiveBackgroundMode() === 'replace' ? ['model', 'background', 'video'] : ['model', 'video']; }
  function updateBackgroundMode() {
    if (mode === 'video_replace') {
      $('#background-options').hidden = true; $('#background-card').hidden = true;
      $('#upload-grid').classList.remove('ready-background'); $('#background-card').classList.remove('invalid');
      text('#mode-description', '上传原视频与角色参考图，指定分别替换谁，先核对角色配置。');
      text('#image-billing-note', '原片首帧和角色配置预览在本地准备，不调用图片API；确认后才提交所选视频编辑服务，费用以平台账单为准。');
      return;
    }
    const ready = mode === 'ready_image'; const replace = replaceReadyBackground(); const frame = referenceBackground(); const selectedMode = effectiveBackgroundMode();
    $('#background-options').hidden = false;
    $$('.background-button').forEach(button => { const selected = button.dataset.backgroundMode === selectedMode; button.hidden = !ready && button.dataset.backgroundMode === 'original'; button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected)); });
    $('#background-card').hidden = selectedMode !== 'replace';
    $('#upload-grid').classList.toggle('ready-background', ready && selectedMode === 'replace');
    if ($('#background-card').hidden) $('#background-card').classList.remove('invalid');
    if (ready) { text('#background-number', '02'); text('#video-number', selectedMode === 'replace' ? '03' : '02'); }
    else if (frame) text('#video-number', garmentMode === 'separate' ? '04' : '03');
    text('#background-mode-hint', frame ? '提取所选舞蹈片段的第一帧作为场景参考，先合成预览。画面中的原人物由模型处理，动态背景与镜头运动不会逐帧沿用。' : ready ? replace ? '上传一张场景图，先替换背景并保留人物和现有服装，再确认预览。' : '直接使用穿搭图中的背景，不调用图片合成API。' : '上传背景图片，合成模特、服装与场景后先确认预览。');
    text('#model-hint', ready ? replace ? '保留人物和服装，只替换场景' : '这张图会直接用于动作生成' : '清晰全身照，四肢完整');
    text('#mode-description', frame ? '使用舞蹈片段的首帧场景，合成预览确认后再生成舞蹈。' : ready ? replace ? '上传已有穿搭图、新背景和参考舞蹈，先确认背景合成效果。' : '复用已有穿搭效果，预览确认后直接生成舞蹈。' : '上传模特、服装、背景和参考视频。');
    text('#image-billing-note', frame ? '提取首帧在本地完成；人物与场景合成会调用Seedream计费。确认预览后才提交视频生成。' : ready ? replace ? '替换背景会调用Seedream并按平台计费；预览确认后才会提交视频生成。' : '直接预览上传图片，不收取图片合成费用；确认后再生成视频。' : '图片每次重新合成都按平台计费；确认图片后才会提交视频生成。');
  }
  function clearActivePreview() {
    closeComparison(); activeId = null; currentMedia = 'image'; mediaChosen = false; previewClearedForMode = true;
    $('#result-image').removeAttribute('src'); $('#result-video').pause(); $('#result-video').removeAttribute('src'); $('#result-video').load();
    try { localStorage.removeItem(activeKey); } catch (_) { /* Storage is optional. */ }
  }
  function clearPreviewForModel(modelId) {
    const selectedJob = activeJob();
    if (selectedJob && selectedJob.status !== 'running' && motionId(selectedJob) !== modelId) clearActivePreview();
  }
  function setMode(value, persist = true) {
    const previous = mode;
    mode = ['ready_image', 'video_replace'].includes(value) ? value : 'compose';
    const selectedJob = activeJob();
    if (persist && previous !== mode && (previous === 'video_replace' || mode === 'video_replace')
        && selectedJob && selectedJob.status !== 'running'
        && (selectedJob.mode !== mode || motionId(selectedJob) !== effectiveModelId())) clearActivePreview();
    const ready = mode === 'ready_image';
    const edit = mode === 'video_replace';
    if (edit && previous !== mode) {
      durationMode = 'clip'; $('#seconds-input').value = '5';
      $$('.duration-button').forEach(item => { const selected = item.dataset.duration === durationMode; item.classList.toggle('active', selected); item.setAttribute('aria-pressed', String(selected)); });
      $('#seconds-field').hidden = false; $('#full-duration-summary').hidden = true;
    }
    $$('.mode-button').forEach(button => { const selected = button.dataset.mode === mode; button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected)); });
    $$('.compose-only').forEach(element => { element.hidden = mode !== 'compose'; });
    $$('.video-replace-only').forEach(element => { element.hidden = !edit; });
    $('#model-card').hidden = edit; $('#motion-model-section').hidden = false;
    $('#upload-grid').classList.toggle('video-replace-grid', edit);
    text('#studio-title', edit ? '换上角色，延续原片节奏。' : '一身穿搭，一段舞蹈。');
    text('#studio-description', edit ? '指定原片中的人物与替换角色，核对配置后再生成编辑视频。' : '上传素材，先确认穿搭效果，再让模特跟着参考视频动起来。');
    text('#workflow-confirm-label', edit ? '确认角色' : '确认图片');
    text('#video-label', edit ? '原视频' : '参考舞蹈');
    text('#video-placeholder', edit ? '上传要替换角色的视频' : '上传舞蹈视频');
    text('#video-hint', edit ? '目标人物清晰，尽量减少遮挡' : '单人、全身、镜头稳定');
    $('#video-input').closest('.drop-zone').setAttribute('aria-label', edit ? '上传用于角色替换的原视频' : '上传参考舞蹈视频');
    text('#clip-heading', edit ? '选择原视频片段' : '选择舞蹈片段');
    text('#full-duration-limit', '最长 ' + motionOption(effectiveModelId()).maxSeconds + ' 秒');
    text('#model-label', ready ? '已有穿搭图' : '虚拟模特');
    text('#model-placeholder', ready ? '上传穿搭效果图' : '上传模特图片');
    $('#model-input').closest('.drop-zone').setAttribute('aria-label', ready ? '上传已有穿搭图片' : '上传虚拟模特图片');
    updateGarmentMode();
    updateBackgroundMode();
    updateRoleCards();
    $('#creation-error').hidden = true; renderActive(); updateControls();
    if (persist) { preferenceEdits.add('creation_mode'); savePreferences(); }
  }
  function updateRoleCards() {
    for (let index = 1; index <= 3; index++) {
      const visible = mode === 'video_replace' && index <= roleCount;
      $('#role-' + index + '-card').hidden = !visible;
      $('#role-description-' + index).disabled = !visible;
      if (!visible) $('#role-' + index + '-card').classList.remove('invalid');
    }
    text('#role-count-label', roleCount + ' / 3 个角色');
    $('#add-role-button').disabled = roleCount >= 3;
    $('#remove-role-button').hidden = roleCount <= 1;
    $('#upload-grid').classList.toggle('single-role', roleCount === 1);
    if (mode === 'video_replace') text('#video-number', '01');
  }
  $$('.mode-button').forEach(button => button.addEventListener('click', () => setMode(button.dataset.mode)));
  $$('.background-button').forEach(button => button.addEventListener('click', () => {
    backgroundMode = ['replace', 'video_frame'].includes(button.dataset.backgroundMode) ? button.dataset.backgroundMode : 'original';
    updateBackgroundMode(); $('#creation-error').hidden = true; updateControls();
  }));
  $$('.garment-button').forEach(button => button.addEventListener('click', () => {
    garmentMode = button.dataset.garmentMode === 'outfit' ? 'outfit' : 'separate';
    updateGarmentMode(); updateBackgroundMode(); $('#creation-error').hidden = true; updateControls();
  }));
  $('#add-role-button').addEventListener('click', () => { if (roleCount < 3) { roleCount++; updateRoleCards(); updateControls(); } });
  $('#remove-role-button').addEventListener('click', () => {
    if (roleCount <= 1) return;
    clearFile('role_' + roleCount); $('#role-description-' + roleCount).value = ''; roleCount--;
    updateRoleCards(); updateControls();
  });
  for (let index = 1; index <= 3; index++) $('#role-description-' + index).addEventListener('input', () => { $('#role-description-' + index).classList.remove('invalid'); updateControls(); });
  $('#edit-prompt-input').addEventListener('input', updateControls);

  function clearFile(field) {
    if (field === 'video') $('#video-asset-id').value = '';
    if (/^role_[1-3]$/.test(field)) $('#role-asset-id-' + field.slice(-1)).value = '';
    const previous = files.get(field); if (previous) URL.revokeObjectURL(previous.url); files.delete(field);
    const card = $(`[data-field="${field}"]`); const preview = card.querySelector('.upload-preview');
    if (preview.tagName === 'VIDEO') { preview.pause(); preview.removeAttribute('src'); preview.load(); sourceDuration = null; }
    else preview.removeAttribute('src');
    preview.hidden = true; card.classList.remove('has-file', 'invalid'); card.querySelector('input').value = ''; card.querySelector('.remove-file').hidden = true;
    card.querySelector('.file-caption').textContent = field === 'video' ? 'MP4、MOV 或 WebM' : 'JPG、PNG 或 WebP';
    updateControls();
  }
  function chooseFile(field, file) {
    if (!file) return;
    const allowed = field === 'video' ? /\.(mp4|mov|webm|m4v)$/i.test(file.name) : /\.(jpe?g|png|webp)$/i.test(file.name);
    if (!allowed) { showToast(field === 'video' ? '请选择 MP4、MOV、WebM 或 M4V 视频。' : '请选择 JPG、PNG 或 WebP 图片。', true); return; }
    if (!file.size) { showToast('这个文件是空的，请重新选择。', true); return; }
    clearFile(field);
    const url = URL.createObjectURL(file); files.set(field, {file, url});
    const card = $(`[data-field="${field}"]`); const preview = card.querySelector('.upload-preview');
    card.classList.add('has-file'); preview.hidden = false; preview.src = url; card.querySelector('.remove-file').hidden = false;
    card.querySelector('.file-caption').textContent = file.name + ' · ' + byteSize(file.size);
    card.querySelector('.file-caption').title = file.name;
    preview.onerror = () => { if (files.get(field)?.url !== url) return; showToast('浏览器暂时无法预览这个文件，提交时还会由本地服务检查。', true); if (field === 'video') { sourceDuration = null; updateControls(); } };
    if (field === 'video') {
      preview.onloadedmetadata = () => {
        if (files.get(field)?.url !== url) return;
        sourceDuration = Number.isFinite(preview.duration) && preview.duration > 0 ? preview.duration : null;
        card.querySelector('.file-caption').textContent = file.name + ' · ' + (sourceDuration ? secondsText(sourceDuration) + ' 秒 · ' : '') + byteSize(file.size);
        if (sourceDuration && sourceDuration > .1) preview.currentTime = Math.min(.1, sourceDuration / 2);
        updateControls();
      };
      preview.load();
    }
    $('#creation-error').hidden = true; updateControls();
  }
  $$('.upload-card').forEach(card => {
    const field = card.dataset.field; const input = card.querySelector('input'); const zone = card.querySelector('.drop-zone');
    input.addEventListener('change', () => chooseFile(field, input.files?.[0]));
    zone.addEventListener('keydown', event => { if (event.target === zone && ['Enter', ' '].includes(event.key)) { event.preventDefault(); input.click(); } });
    card.querySelector('.remove-file').addEventListener('click', () => clearFile(field));
    card.addEventListener('dragover', event => { event.preventDefault(); card.classList.add('dragging'); if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'; });
    card.addEventListener('dragleave', event => { if (!card.contains(event.relatedTarget)) card.classList.remove('dragging'); });
    card.addEventListener('drop', event => { event.preventDefault(); card.classList.remove('dragging'); chooseFile(field, event.dataTransfer?.files?.[0]); });
  });
  window.addEventListener('dragover', event => { if (event.dataTransfer?.types?.includes('Files')) event.preventDefault(); });
  window.addEventListener('drop', event => { if (event.dataTransfer?.types?.includes('Files')) event.preventDefault(); });

  function durationSelection() {
    const start = Number($('#start-input').value); const requested = Number($('#seconds-input').value);
    const selected = motionOption(effectiveModelId()); const minSeconds = selected.minSeconds; const maxSeconds = selected.maxSeconds;
    if ($('#start-input').value === '' || !Number.isFinite(start) || start < 0) return {error: '开始位置须是大于等于 0 的秒数。'};
    const remaining = sourceDuration == null ? null : sourceDuration - start;
    if (remaining != null && remaining <= 0) return {error: '开始位置已超过视频长度，请调整。'};
    if (durationMode === 'full') {
      if (remaining == null) return {error: '请先上传可读取时长的视频，再选择完整视频。'};
      if (remaining > maxSeconds + .02) return {error: '从起点到结尾共 ' + secondsText(remaining) + ' 秒，超过 ' + maxSeconds + ' 秒。请调整起点或选择片段。'};
      if (remaining < minSeconds) return {error: selected.shortLabel + '至少需要 ' + minSeconds + ' 秒，剩余视频不足，请调整开始位置。'};
      if (selected.integerSeconds && Math.abs(remaining - Math.round(remaining)) > .02) return {error: 'MiniMax H3需要整数秒片段，请选择“先试5秒”并填写4–15的整数秒数。'};
      return {start, seconds: Math.min(maxSeconds, remaining)};
    }
    if ($('#seconds-input').value === '' || !Number.isFinite(requested) || requested < minSeconds || requested > maxSeconds) return {error: selected.shortLabel + '片段时长可设为 ' + minSeconds + '–' + maxSeconds + ' 秒。'};
    if (selected.integerSeconds && !Number.isInteger(requested)) return {error: 'MiniMax H3的片段时长须为4–15的整数秒数，例如5秒。'};
    if (remaining != null && requested > remaining + .05) return {error: '片段超过视频剩余长度（' + secondsText(remaining) + ' 秒），请调整时长。'};
    return {start, seconds: requested};
  }
  $$('.duration-button').forEach(button => button.addEventListener('click', () => {
    $('#video-asset-id').value = '';
    durationMode = button.dataset.duration === 'full' ? 'full' : 'clip';
    if (durationMode === 'clip') $('#seconds-input').value = '5';
    $$('.duration-button').forEach(item => { const selected = item.dataset.duration === durationMode; item.classList.toggle('active', selected); item.setAttribute('aria-pressed', String(selected)); });
    $('#seconds-field').hidden = durationMode === 'full'; $('#full-duration-summary').hidden = durationMode !== 'full'; updateControls();
  }));
  ['#start-input', '#seconds-input'].forEach(selector => $(selector).addEventListener('input', () => { $('#video-asset-id').value = ''; updateControls(); }));
  $('#motion-model-input').value = selectedMotionId;
  $('#motion-model-input').addEventListener('change', () => {
    const id = $('#motion-model-input').value;
    if (editModelIds.includes(id)) {
      selectedEditId = id; preferenceEdits.add('edit_model');
      clearPreviewForModel(id);
      const changed = mode !== 'video_replace'; setMode('video_replace');
      if (changed) showToast('已切换到视频角色替换：上传原视频和角色参考图，逐一指定替换谁。');
      return;
    }
    selectedMotionId = ['volc-dreamactor-2', 'fal-kling-v3-pro'].includes(id) ? id : 'volc-dreamactor-2';
    clearPreviewForModel(selectedMotionId);
    preferenceEdits.add('motion_model');
    try { localStorage.setItem(motionKey, selectedMotionId); } catch (_) { /* Storage is optional. */ }
    if (mode === 'video_replace') {
      setMode('ready_image');
      showToast('已切换到已有穿搭图：' + motionOption(selectedMotionId).shortLabel + '使用穿搭图片＋参考视频模仿动作，请上传穿搭图。原视频已保留。');
    } else { $('#creation-error').hidden = true; renderActive(); updateControls(); savePreferences(); }
  });

  $('#video-resolution-input').addEventListener('change', () => {
    const resolutions = motionOption(selectedEditId).resolutions || [];
    if (resolutions.includes($('#video-resolution-input').value)) editResolutions[selectedEditId] = $('#video-resolution-input').value;
    updateControls();
  });

  function updateEditOptions() {
    const option = motionOption(selectedEditId); const resolutions = option.resolutions || [];
    const resolutionInput = $('#video-resolution-input');
    if (resolutionInput.dataset.model !== selectedEditId) {
      resolutionInput.replaceChildren(...resolutions.map(value => { const item = document.createElement('option'); item.value = value; item.textContent = value; return item; }));
      resolutionInput.dataset.model = selectedEditId;
    }
    resolutionInput.value = editResolutions[selectedEditId] || resolutions[0] || '';
    $('#video-resolution-field').hidden = mode !== 'video_replace' || !resolutions.length;
    resolutionInput.disabled = mode !== 'video_replace' || !resolutions.length;
    $('#seedance-assets-settings').hidden = mode !== 'video_replace' || selectedEditId !== 'volc-seedance-2.5-edit';
    for (let index = 1; index <= 3; index++) {
      $('#role-asset-' + index + '-field').hidden = index > roleCount;
      $('#role-asset-id-' + index).disabled = mode !== 'video_replace' || selectedEditId !== 'volc-seedance-2.5-edit' || index > roleCount;
    }
    $('#video-asset-id').disabled = mode !== 'video_replace' || selectedEditId !== 'volc-seedance-2.5-edit';
    text('#edit-model-title', option.label);
    const notes = {
      'minimax-h3-edit': ['原视频＋1–3张角色参考图，4–15整数秒；默认先试5秒、768P，也可选2K。使用MiniMax官方API Key，无需TOS，不调用Seedream。', 'H3使用Ref2VA视频参考生成，可能重新绘制人物、背景和镜头细节，不保证逐像素保留。请按开场位置逐一描述目标人物；两人交叉或遮挡时仍可能串角色。'],
      'volc-seedance-2.5-edit': ['原视频＋1–3张角色参考图，4–30秒；默认先试5秒、1080p。使用现有方舟API Key和TOS素材存储，需在方舟另行开通Seedance 2.5。', '普通含真人人脸的原视频或角色图，须符合方舟官方可信素材／授权入库要求。下方“授权素材”可填写已获准的素材ID；填写ID不会自动获得授权，且必须与本次片段和角色图完全对应。'],
      [legacyEditModelId]: ['3–15秒，默认先试5秒，使用fal API Key。原视频宽、高均须在720–3840px，帧率23–60fps。不调用Seedream；输出以API实际尺寸为准。', '按开场人物描述建立对应关系，交叉或遮挡后仍可能误配。背景和动态以原视频为参考，不保证逐像素保持。可提交人像素材，平台仍会审核。']
    };
    text('#edit-model-details', notes[selectedEditId]?.[0] || '');
    text('#edit-model-limits', notes[selectedEditId]?.[1] || '');
    text('#video-resolution-hint', selectedEditId === 'minimax-h3-edit' ? '768P先验证效果；2K费用更高，以实际返回视频和账单为准。' : '这是向模型请求的分辨率；结果保留实际画幅，不再强制放大为竖屏。');
  }

  function updateControls() {
    const edit = mode === 'video_replace'; const effective = effectiveModelId();
    const option = motionOption(effective); const selectedConfig = motionConfig(effective);
    $('#seconds-input').min = String(option.minSeconds);
    $('#seconds-input').max = String(option.maxSeconds);
    $('#seconds-input').step = option.integerSeconds ? '1' : '0.1';
    text('#full-duration-limit', '最长 ' + option.maxSeconds + ' 秒');
    updateEditOptions();
    const duration = durationSelection(); const running = runningJob();
    text('#selected-motion-ready', !settings ? '读取配置中' : selectedConfig.ready ? '已配置' : '待配置');
    $('#selected-motion-ready').classList.toggle('ready', Boolean(settings && selectedConfig.ready));
    text('#selected-edit-ready', !settings ? '读取配置中' : selectedConfig.ready ? '已配置' : '待配置');
    $('#selected-edit-ready').classList.toggle('ready', Boolean(settings && selectedConfig.ready));
    $('#motion-model-input').value = effective;
    text('#motion-model-hint', edit ? effective === 'minimax-h3-edit' ? 'MiniMax官方 · H3 Ref2VA，原视频＋角色参考图，4–15整数秒，可选768P／2K。' : effective === 'volc-seedance-2.5-edit' ? '火山方舟官方 · Seedance 2.5，原视频＋角色参考图，4–30秒，可选720p／1080p。' : 'fal · O3 Pro，原视频＋角色参考图，3–15秒，使用fal API Key。' : selectedMotionId === 'fal-kling-v3-pro' ? '穿搭图片＋参考视频模仿动作，3–30秒，使用fal API Key，无需TOS。5秒约 $0.84。' : '穿搭图片＋参考视频模仿动作，1–30秒，使用火山访问密钥与TOS。合成新穿搭或换背景时另用方舟Seedream。');
    text('#source-duration', sourceDuration ? '原视频 ' + secondsText(sourceDuration) + ' 秒' : (files.has('video') ? '正在读取视频时长' : '上传视频后读取时长'));
    const durationError = Boolean(duration.error && (files.has('video') || durationMode === 'full'));
    $('#duration-message').classList.toggle('is-error', durationError);
    text('#duration-message', durationError ? duration.error : durationMode === 'full' ? '从开始位置至视频结尾，按实际时长生成。' : edit ? '默认取前5秒，先确认角色对应与替换效果；每次可选' + option.minSeconds + '–' + option.maxSeconds + (option.integerSeconds ? '整数秒。' : '秒。') : '默认取前 5 秒，先看动作和服装是否合适。');
    text('#full-duration-summary', duration.error ? '从开始位置播放至结尾' : '将生成 ' + secondsText(duration.seconds) + ' 秒');
    const estimateSeconds = Number.isFinite(duration.seconds) ? duration.seconds : 5;
    text('#cost-estimate', effective === 'minimax-h3-edit' ? secondsText(estimateSeconds) + '秒输入＋输出约 ' + money(estimateSeconds * (editResolutions[effective] === '2K' ? 1.6 : 1)) : typeof option.rate !== 'number' ? '按平台token用量计费' : option.currency === 'USD' ? '$0.168 / 生成秒' : '¥0.40 / 生成秒');
    const create = $('#create-button'); create.disabled = formBusy || Boolean(running);
    text('#create-button-text', formBusy ? '正在上传素材…' : running ? '任务生成中，请稍候' : edit ? '预览原片与角色配置' : needsImageApi() && settings && !settings.image_ready ? '配置图片API后开始' : mode === 'ready_image' ? replaceReadyBackground() ? '先生成背景合成图' : '预览这张穿搭图' : '先生成穿搭图');
    const needed = requiredInputs();
    text('#creation-status', running ? '当前已有生成任务，完成后可以开始下一次创作。' : edit ? '上传原视频、角色参考图，并逐一描述替换谁。先核对配置，确认前不会提交视频生成。' : needsImageApi() && settings && !settings.image_ready ? '点击上方按钮前往 API 设置，上传素材会保留。' : needed.every(key => files.has(key)) ? '素材已就绪。先预览确认，视频不会自动生成。' : referenceBackground() ? mode === 'ready_image' ? '上传穿搭图和舞蹈视频，所选片段首帧将用于场景合成。' : '上传模特、服装和舞蹈视频，首帧将用于场景合成。' : mode === 'ready_image' ? replaceReadyBackground() ? '上传已有穿搭图、背景图和参考舞蹈。' : '上传穿搭图和参考舞蹈，先检查预览。' : '上传模特、服装、背景和参考视频。');
    const job = activeJob(); const waiting = job?.status === 'awaiting_confirmation';
    const jobMotion = motionId(job); const jobConfig = motionConfig(jobMotion);
    $('#confirm-button').disabled = confirming || Boolean(running);
    text('#confirm-button-text', confirming ? '正在提交视频任务…' : running ? '请先等待当前任务完成' : !jobConfig.ready ? '先配置该任务的视频API' : job?.mode === 'video_replace' ? '确认角色配置，生成视频' : '确认穿搭图，生成视频');
    if (waiting) text('#confirmation-cost', motionOption(jobMotion).label + (job.mode === 'video_replace' ? ' · ' + resolutionText(job) : '') + ' · ' + (!jobConfig.ready ? job?.mode === 'video_replace' ? '角色配置已保存，请先配置该模型。' : '图片已保存，请先配置该模型。' : motionPrice(jobMotion, job.seconds, job.video_resolution) + '。最终消耗以平台账单为准。'));
    const editPreview = job ? job.mode === 'video_replace' : edit;
    text('#preview-pricing-note', (job ? '此任务使用' + motionOption(jobMotion).label + '，' : '') + motionPrice(jobMotion, undefined, job?.video_resolution) + (editPreview ? '。角色配置预览不调用图片API；结果保留生成原片画幅，实际尺寸与帧率以文件为准。' : '。图片与素材存储另计；不同币种分别估算。1080p为导出尺寸，原片规格以实际结果为准。'));
    updateConnectionState();
  }

  function setActive(id) {
    if (!jobs.has(id)) return;
    previewClearedForMode = false;
    if (activeId !== id) { closeComparison(); activeId = id; currentMedia = 'image'; mediaChosen = false; $('#result-video').pause(); }
    try { localStorage.setItem(activeKey, id); } catch (_) { /* Storage is optional. */ }
    renderActive(); updateControls();
  }
  function fileLink(selector, value) { const link = $(selector); const url = safeFile(value); link.hidden = !url; if (url) link.href = url; else link.removeAttribute('href'); }
  function renderSteps(job) {
    const phase = !job ? 0 : job.status === 'done' || (imageUrl(job) && job.status === 'running') ? 2 : 1;
    $$('.workflow-steps li').forEach((item, index) => { item.classList.toggle('current', index === phase); item.classList.toggle('complete', index < phase); });
  }
  function usageSeconds(value) { return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(3).replace(/\.?0+$/, '') : '未记录'; }
  function currencyTotals(totals) {
    if (!totals || typeof totals !== 'object' || Array.isArray(totals)) return '';
    return Object.entries(totals).filter(([, value]) => typeof value === 'number' && Number.isFinite(value) && value >= 0).sort(([a], [b]) => a.localeCompare(b)).map(([currency, value]) => money(value, currency) + ' ' + currency).join(' + ');
  }
  function billingTotalText(bill) {
    const totals = currencyTotals(bill.estimated_totals); const known = currencyTotals(bill.known_estimated_totals);
    if (bill.estimate_complete && totals) return '生成项目约 ' + totals;
    if (known) return '已知项目约 ' + known;
    if (bill.mixed_currencies) return '各币种费用待确认';
    if (bill.estimate_complete && typeof bill.estimated_amount === 'number') return '生成项目约 ' + money(bill.estimated_amount, bill.currency || 'CNY');
    if (typeof bill.known_estimated_amount === 'number' && bill.known_estimated_amount > 0) return '已知项目约 ' + money(bill.known_estimated_amount, bill.currency || 'CNY');
    return '合计暂不可估算';
  }
  function billingStageText(item, imageStage = false) {
    if (!item) return '记录不足，暂无法估算';
    if (item.status === 'not_used') return '未调用图片生成 · 0';
    if (item.status === 'not_submitted') return '尚未提交 · 未计入';
    if (item.status === 'not_generated') return '未成功输出 · ' + (item.currency ? '估算 ' + money(0, item.currency) : '未计入');
    if (item.status === 'estimated' && typeof item.estimated_amount === 'number') {
      const duration = !imageStage && item.source_duration_seconds;
      return '约 ' + money(item.estimated_amount, item.currency || 'CNY') + (duration ? ' · 原片 ' + usageSeconds(duration) + ' 秒' : '');
    }
    return item.status === 'pending' ? '等待结果用量，暂无法估算' : '记录不足，暂无法估算';
  }
  function renderBilling(job) {
    const bill = job?.billing; $('#job-billing').hidden = !bill;
    if (!bill) { $('#quality-info').hidden = true; return; }
    text('#billing-total', billingTotalText(bill));
    text('#billing-image', billingStageText(bill.image, true));
    text('#billing-motion', billingStageText(bill.motion));
    text('#billing-actual', bill.actual_available && typeof bill.actual_amount === 'number' && !bill.mixed_currencies ? money(bill.actual_amount, bill.currency || 'CNY') : '平台未返回');
    text('#billing-image-basis', bill.image?.pricing_basis || '图片用量尚未记录。');
    const motionBasis = bill.motion || {};
    const rateText = typeof motionBasis.rate_per_second === 'number' ? (motionBasis.currency === 'USD' ? '$' : '¥') + usageSeconds(motionBasis.rate_per_second) : '待确认';
    text('#billing-motion-basis', (motionBasis.pricing_basis || '视频用量尚未记录。') + (motionBasis.source_duration_seconds && motionBasis.rate_per_second ? ' 本地原片 ' + usageSeconds(motionBasis.source_duration_seconds) + ' 秒 × ' + rateText + '/秒。参考 ' + usageSeconds(motionBasis.reference_duration_seconds) + ' 秒；导出 ' + usageSeconds(motionBasis.output_duration_seconds) + ' 秒。' : ''));
    const usage = bill.image?.usage || {}; const usageItems = [];
    for (const [key, label] of [['generated_images', '输出图片'], ['input_images', '输入图片'], ['output_tokens', '输出tokens'], ['total_tokens', '总tokens']]) {
      if (typeof usage[key] === 'number' && Number.isFinite(usage[key])) usageItems.push(label + ' ' + usage[key]);
    }
    if (typeof usage.tool_usage?.web_search === 'number') usageItems.push('联网检索 ' + usage.tool_usage.web_search + ' 次');
    const videoUsage = bill.motion?.usage || {}; const videoUsageItems = [];
    for (const [key, label] of [['input_seconds', '输入视频秒数'], ['output_seconds', '输出视频秒数'], ['total_seconds', '总秒数'], ['input_image_count', '输入图片数'], ['completion_tokens', '生成tokens'], ['prompt_tokens', '输入tokens'], ['total_tokens', '总tokens']]) {
      if (typeof videoUsage[key] === 'number' && Number.isFinite(videoUsage[key])) videoUsageItems.push(label + ' ' + videoUsage[key]);
    }
    text('#billing-usage', (usageItems.length ? '图片API返回用量：' + usageItems.join('；') + '。' : '没有可展示的图片API用量字段。') + (videoUsageItems.length ? '视频API返回用量：' + videoUsageItems.join('；') + '。' : '视频API尚未返回可展示的逐任务用量。'));
    text('#billing-note', (bill.note || '以上为估算，实际扣费以平台账单为准。') + (bill.mixed_currencies ? ' 人民币与美元分别显示，未作汇率换算。' : ''));
    const motion = bill.motion || {};
    const dimensions = motion.source_width > 0 && motion.source_height > 0 && motion.output_width > 0 && motion.output_height > 0;
    $('#quality-info').hidden = !dimensions;
    if (dimensions) {
      const fps = value => typeof value === 'number' && Number.isFinite(value) ? ' / ' + secondsText(value) + 'fps' : '';
      text('#quality-dimensions', 'API原片：' + motion.source_width + '×' + motion.source_height + fps(motion.source_fps) + '；导出：' + motion.output_width + '×' + motion.output_height + fps(motion.output_fps) + '。');
      text('#quality-model-note', job?.mode === 'video_replace' ? '角色替换结果保留API原片画幅和尺寸，不固定为1080p竖屏。人物一致性、原背景和镜头动态以实际结果为准，不保证逐像素保留。' : (motionId(job) === 'volc-dreamactor-2' ? '即梦动作模仿2.0为720p、25fps档位。' : '可灵原片分辨率与帧率以上方实际文件为准。') + '1080p放大与帧率转换不能补回原片已经丢失的细节，快速动作拖影也无法靠锐化消除。');
      text('#quality-advice', job?.mode === 'video_replace' ? '使用清晰的角色参考图，并按原片开场位置、服装或其他特征描述目标人物；交叉、转身和遮挡时仍可能误配，先用5秒片段检查。' : '优先使用清晰、少遮挡、镜头稳定的参考视频，让穿搭图与视频的人物比例和起始姿态接近；先用5秒片段确认效果。');
    }
  }
  function renderRolePreview(job) {
    const edit = job?.mode === 'video_replace';
    $('#role-reference-preview').hidden = !edit;
    if (!edit) { lastRoleSignature = ''; $('#role-reference-list').replaceChildren(); return; }
    const roles = Array.isArray(job.roles) ? job.roles.filter(role => role && [1, 2, 3].includes(Number(role.index))).slice(0, 3) : [];
    const signature = JSON.stringify([job.id, roles.map(role => [role.index, role.description, role.image]), job.edit_prompt]);
    if (signature === lastRoleSignature) return; lastRoleSignature = signature;
    const fragment = document.createDocumentFragment();
    for (const role of roles) {
      const card = document.createElement('article'); card.className = 'role-reference-card';
      const url = safeFile(role.image);
      if (url) { const img = document.createElement('img'); img.src = url; img.alt = '角色' + Number(role.index) + '参考图'; img.loading = 'lazy'; card.append(img); }
      const copy = document.createElement('div'); const title = document.createElement('strong'); title.textContent = '角色' + Number(role.index);
      const description = document.createElement('p'); description.textContent = '替换：' + (typeof role.description === 'string' ? role.description : '描述未记录');
      copy.append(title, description); card.append(copy); fragment.append(card);
    }
    if (!roles.length) { const empty = document.createElement('p'); empty.textContent = '此记录未保存角色配置，生成前请先核对本地任务记录。'; fragment.append(empty); }
    $('#role-reference-list').replaceChildren(fragment);
    const prompt = typeof job.edit_prompt === 'string' ? job.edit_prompt.trim() : '';
    text('#result-edit-prompt', prompt ? '补充视频要求：' + prompt : ''); $('#result-edit-prompt').hidden = !prompt;
  }
  function renderActive() {
    const job = activeJob(); const image = imageUrl(job); const video = videoUrl(job);
    const edit = job ? job.mode === 'video_replace' : mode === 'video_replace';
    text('#image-tab-label', edit ? '原片首帧' : '穿搭图片'); text('#video-tab-label', edit ? '替换结果' : '舞蹈视频');
    text('#preview-media-label', edit ? '原片首帧 · 配置参考，非替换效果' : '穿搭效果预览');
    $('#result-image').alt = edit ? '原片首帧，仅用于核对角色配置，不是替换效果' : '当前创作的穿搭合成预览';
    $('#result-video').setAttribute('aria-label', edit ? '视频角色替换结果' : '生成的舞蹈视频');
    $('#expand-image').setAttribute('aria-label', edit ? '查看原片首帧大图' : '查看穿搭大图');
    $('#dialog-image').alt = edit ? '原片首帧完整预览，非替换效果' : '穿搭合成图完整预览';
    text('#download-video-label', edit ? '下载替换视频' : '下载1080p视频'); text('#download-image-label', edit ? '原片首帧' : '穿搭图');
    $('#download-image').setAttribute('download', edit ? 'source_first_frame.png' : 'fashion_outfit.png');
    text('#confirmation-copy', edit ? '核对原片、每个角色参考图和替换对象描述；上方首帧不是替换效果。' : '检查人物、服装和背景，满意后再继续。');
    if (!mediaChosen) currentMedia = video ? 'video' : 'image';
    if (currentMedia === 'video' && !video) currentMedia = 'image';
    const showVideo = currentMedia === 'video' && video;
    $('#result-image').hidden = !image || Boolean(showVideo); $('#result-video').hidden = !showVideo;
    if (image && $('#result-image').getAttribute('src') !== image) $('#result-image').src = image;
    if (video && $('#result-video').getAttribute('src') !== video) $('#result-video').src = video;
    if (!showVideo) $('#result-video').pause();
    $('#preview-placeholder').hidden = Boolean(image || video); $('#preview-stage').classList.toggle('has-media', Boolean(image || video));
    $('#preview-media-label').hidden = !image || Boolean(showVideo); $('#expand-image').hidden = !image || Boolean(showVideo);
    $('#video-tab').disabled = !video;
    $$('.preview-tab').forEach(button => { const selected = button.dataset.media === currentMedia; button.classList.toggle('active', selected); button.setAttribute('aria-selected', String(selected)); });
    const [label, badgeClass] = job?.status === 'running' && job.is_recovering ? ['恢复中', 'running'] : job ? statusLabels[job.status] || ['状态未记录', 'neutral'] : ['等待素材', 'neutral'];
    text('#preview-badge', label); $('#preview-badge').className = 'state-badge ' + badgeClass;
    text('#preview-subtitle', job ? dateText(job) + ' · ' + secondsText(job.seconds) + ' 秒 · ' + motionOption(motionId(job)).shortLabel + (job.mode === 'video_replace' ? ' · ' + resolutionText(job) : '') : edit ? '先核对角色配置，确认后才生成替换视频。' : '图片确认后，再开始动作生成。');
    const isError = job && ['error', 'interrupted'].includes(job.status);
    text('#placeholder-title', isError ? '这次创作尚未完成' : job?.status === 'running' ? edit ? '正在处理角色替换任务' : '正在准备你的穿搭图' : edit ? '从原片与角色配置开始' : '下一张穿搭图，从这里开始');
    if (job?.status === 'running') text('#placeholder-description', '素材已提交，请稍候。生成完成后会在这里显示，视频需要你再次确认。');
    else if (isError) text('#placeholder-description', '具体原因见下方说明。已有素材和结果会保留在创作记录中。');
    else text('#placeholder-description', edit ? '先在本地查看原片首帧和角色对应配置。确认后才会生成替换结果，当前没有替换效果预览。' : '模特、服装和背景会先组合成一张图片。你确认满意后，我们再生成舞蹈。');
    $('#placeholder-steps').innerHTML = edit ? '<span>原片</span><i></i><span>核对角色</span><i></i><span>替换</span>' : '<span>合成</span><i></i><span>确认</span><i></i><span>动起来</span>';
    $('#job-progress').hidden = !job || isError;
    if (job) {
      text('#job-stage', job.stage || label);
      text('#job-stage-detail', job.status === 'awaiting_confirmation' ? edit ? '原片首帧与角色配置已就绪，尚未生成替换效果。请核对后确认。' : '图片已就绪，等待你确认。' : job.status === 'done' ? '结果已保存，可以预览或下载。' : job.resume_message || (job.safe_to_close ? '任务编号已保存。关闭或关机后，重开将继续查询并自动下载结果。' : '正在准备或提交任务，尚未确认任务编号，请暂时保持软件运行。'));
      $('#progress-icon').classList.toggle('is-running', job.status === 'running');
    }
    $('#confirmation-area').hidden = job?.status !== 'awaiting_confirmation' || !image;
    $('#job-error').hidden = !isError; text('#job-error', isError ? job.error || '任务已中断。请先检查已有生成记录，避免重复提交。' : '');
    $('#job-persistence-note').hidden = job?.status !== 'running';
    text('#job-persistence-note', job?.safe_to_close ? '恢复信息已保存，关闭或关机后重开可继续处理。云端结果有保留期限，请及时取回。' : '正在准备或提交，请暂时保持软件运行，等待任务编号保存。');
    $('#resume-area').hidden = !job?.can_resume || ['running', 'done'].includes(job.status);
    $('#resume-button').disabled = resuming;
    text('#resume-button-text', resuming ? '正在恢复查询…' : '继续查询并取回结果');
    text('#resume-note', job?.resume_message || '继续处理已保存的云端任务或原片，不会重新提交生成。');
    fileLink('#download-image', image); fileLink('#download-video', video); fileLink('#download-source', sourceUrl(job)); fileLink('#download-info', job?.info);
    fileLink('#download-background-reference', job?.background_reference);
    $('#result-downloads').hidden = !image && !video && !safeFile(job?.info) && !safeFile(job?.background_reference);
    $('#compare-videos').hidden = !video || !referenceUrl(job);
    $('#comparison-missing').hidden = !video || Boolean(referenceUrl(job));
    text('#output-note', job?.output_note || (video ? edit ? 'API原片与本地结果分别保留，结果保持原片画幅；尺寸与帧率以实际文件为准。' : 'API原片与1080p导出分别保留；导出尺寸不代表新增原生细节。' : ''));
    $('#output-note').hidden = !video;
    renderBilling(job);
    renderRolePreview(job);
    renderSteps(job);
  }
  $$('.preview-tab').forEach(button => button.addEventListener('click', () => { currentMedia = button.dataset.media; mediaChosen = true; renderActive(); }));
  $('#expand-image').addEventListener('click', () => { const url = imageUrl(activeJob()); if (!url) return; $('#dialog-image').src = url; $('#image-dialog').showModal(); });
  $('#close-image-dialog').addEventListener('click', () => $('#image-dialog').close());
  $('#image-dialog').addEventListener('click', event => { if (event.target === $('#image-dialog')) $('#image-dialog').close(); });

  // Comparison playback is local. The generated result is the single clock.
  const comparisonVideos = [$('#comparison-reference'), $('#comparison-result')];
  function comparisonTime(value) {
    const seconds = Number.isFinite(value) ? Math.max(0, value) : 0;
    return String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + (seconds % 60).toFixed(1).padStart(4, '0');
  }
  function comparisonLength() {
    const durations = comparisonVideos.map(video => video.duration);
    return durations.every(value => Number.isFinite(value) && value > 0) ? Math.min(...durations) : 0;
  }
  function comparisonMessage(message, error = false) {
    text('#comparison-status', message); $('#comparison-status').classList.toggle('is-error', error);
  }
  function renderComparison() {
    const session = comparisonSession; if (!session) return;
    const length = comparisonLength(); const current = Math.min(length || Infinity, comparisonVideos[1].currentTime || 0);
    const slider = $('#comparison-seek'); slider.max = String(length); slider.disabled = !length || session.failed;
    if (!session.scrubbing) slider.value = String(current);
    slider.setAttribute('aria-valuetext', comparisonTime(current) + ' / ' + comparisonTime(length));
    text('#comparison-current', comparisonTime(current)); text('#comparison-duration', comparisonTime(length));
    $('#comparison-play').disabled = !length || session.failed; $('#comparison-restart').disabled = !length || session.failed;
    $('#comparison-play').innerHTML = icon(session.wantPlay ? 'pause' : 'play') + '<span>' + (session.wantPlay ? '暂停' : '播放') + '</span>';
  }
  function pauseComparisonVideos(session = comparisonSession) {
    for (const video of comparisonVideos) {
      if (!video.paused) { if (session) session.expectedPause.add(video); video.pause(); }
    }
  }
  function closeComparison(closeDialog = true) {
    const session = comparisonSession; comparisonSession = null;
    if (session) { session.wantPlay = false; cancelAnimationFrame(session.frame); }
    pauseComparisonVideos(session);
    const dialog = $('#comparison-dialog'); if (closeDialog && dialog.open) dialog.close();
    if (session) for (const video of comparisonVideos) { video.removeAttribute('src'); video.load(); }
  }
  function comparisonSeek(value) {
    const session = comparisonSession; if (!session) return;
    const length = comparisonLength(); if (!length) return;
    const position = Math.max(0, Math.min(length, Number(value) || 0));
    pauseComparisonVideos(session);
    for (const video of comparisonVideos) {
      if (Math.abs(video.currentTime - position) > .015) { session.waiting.add(video); video.currentTime = position; }
    }
    renderComparison(); reconcileComparison();
  }
  function synchronizeComparison() {
    const session = comparisonSession; if (!session) return false;
    const [reference, result] = comparisonVideos;
    if (reference.seeking || result.seeking || reference.readyState < 2 || result.readyState < 2) return false;
    if (Math.abs(reference.currentTime - result.currentTime) > .12) {
      const position = Math.min(result.currentTime, Number.isFinite(reference.duration) ? reference.duration : result.currentTime);
      session.waiting.add(reference); pauseComparisonVideos(session); reference.currentTime = position; return true;
    }
    return false;
  }
  function comparisonEnded() {
    const session = comparisonSession; if (!session) return;
    session.wantPlay = false; pauseComparisonVideos(session); renderComparison(); comparisonMessage('播放结束，可以从头再看。');
  }
  async function reconcileComparison() {
    const session = comparisonSession; if (!session || !$('#comparison-dialog').open) return;
    renderComparison();
    if (!session.wantPlay || session.failed) return;
    if (session.scrubbing || session.waiting.size || comparisonVideos.some(video => video.readyState < 3 || video.seeking)) {
      pauseComparisonVideos(session); comparisonMessage(session.scrubbing ? '正在调整同步进度…' : '正在缓冲，两路视频会同步继续。'); return;
    }
    if (session.playAttempt) return;
    if (synchronizeComparison()) return;
    if (comparisonVideos.every(video => !video.paused)) { comparisonMessage('正在同步播放。'); return; }
    session.playAttempt = true;
    try {
      await Promise.all(comparisonVideos.map(async video => {
        await video.play();
        if (comparisonSession !== session) { if (!comparisonSession) video.pause(); return; }
        if (!session.wantPlay || session.waiting.size || session.scrubbing) pauseComparisonVideos(session);
      }));
      if (comparisonSession === session && session.wantPlay && !session.waiting.size) comparisonMessage('正在同步播放。');
    } catch (error) {
      if (comparisonSession === session && session.wantPlay && !session.waiting.size && !session.scrubbing && !comparisonVideos.some(video => video.seeking)) {
        session.wantPlay = false; pauseComparisonVideos(session); comparisonMessage('暂时无法播放，请点击播放重试或检查本地视频文件。', true);
      }
    } finally {
      if (comparisonSession === session) { session.playAttempt = false; renderComparison(); if (session.wantPlay) reconcileComparison(); }
    }
  }
  function comparisonTick() {
    const session = comparisonSession; if (!session) return;
    const length = comparisonLength();
    if (session.wantPlay && length && comparisonVideos[1].currentTime >= length - .015) comparisonEnded();
    else if (session.wantPlay && !session.playAttempt && comparisonVideos.every(video => !video.paused && !video.seeking)) synchronizeComparison();
    renderComparison(); session.frame = requestAnimationFrame(comparisonTick);
  }
  function openComparison() {
    const job = activeJob(); const reference = referenceUrl(job); const result = videoUrl(job);
    if (!reference || !result) { showToast('这个记录缺少参考片段或生成结果，无法同步对比。', true); return; }
    closeComparison(); $('#result-video').pause(); $$('.upload-preview').forEach(preview => { if (preview.tagName === 'VIDEO') preview.pause(); });
    comparisonSession = {jobId: job.id, wantPlay: false, waiting: new Set(comparisonVideos), expectedPause: new WeakSet(), playAttempt: false, scrubbing: false, failed: false, frame: null};
    const start = Number(job.start); const seconds = Number(job.seconds);
    text('#comparison-output-spec', job.mode === 'video_replace' ? '保留生成原片画幅' : '1080p 导出');
    text('#comparison-interval', job.start != null && job.seconds != null && Number.isFinite(start) && Number.isFinite(seconds) ? secondsText(start) + '–' + secondsText(start + seconds) + ' 秒' : '截取片段');
    $('#comparison-rate').value = '1'; $('#comparison-audio').checked = true;
    for (const [index, video] of comparisonVideos.entries()) { video.muted = index === 1; video.playbackRate = 1; video.src = index === 0 ? reference : result; video.load(); }
    comparisonMessage('正在载入两路视频…'); $('#comparison-dialog').showModal(); renderComparison(); comparisonSession.frame = requestAnimationFrame(comparisonTick);
  }
  $('#compare-videos').addEventListener('click', openComparison);
  $('#close-comparison').addEventListener('click', () => closeComparison());
  $('#comparison-dialog').addEventListener('close', () => { if (!$('#comparison-dialog').open) closeComparison(false); });
  $('#comparison-dialog').addEventListener('cancel', event => { event.preventDefault(); closeComparison(); });
  $('#comparison-dialog').addEventListener('click', event => { if (event.target === $('#comparison-dialog')) { const box = event.target.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) closeComparison(); } });
  $('#comparison-play').addEventListener('click', () => {
    const session = comparisonSession; if (!session || session.failed) return;
    if (session.wantPlay) { session.wantPlay = false; pauseComparisonVideos(session); comparisonMessage('已暂停。'); renderComparison(); return; }
    session.wantPlay = true;
    if (comparisonVideos[1].currentTime >= comparisonLength() - .025) comparisonSeek(0);
    reconcileComparison();
  });
  $('#comparison-restart').addEventListener('click', () => { if (comparisonSession && !comparisonSession.failed) { comparisonSession.wantPlay = true; comparisonSeek(0); } });
  $('#comparison-seek').addEventListener('pointerdown', () => { if (comparisonSession) { comparisonSession.scrubbing = true; pauseComparisonVideos(); } });
  $('#comparison-seek').addEventListener('input', event => comparisonSeek(event.target.value));
  function finishComparisonSeek() { if (comparisonSession) { comparisonSession.scrubbing = false; reconcileComparison(); } }
  ['change', 'pointerup', 'pointercancel'].forEach(name => $('#comparison-seek').addEventListener(name, finishComparisonSeek));
  $('#comparison-rate').addEventListener('change', event => { const rate = Number(event.target.value); if (![.5, 1, 1.5, 2].includes(rate)) return; comparisonVideos.forEach(video => { video.playbackRate = rate; }); });
  $('#comparison-audio').addEventListener('change', event => { comparisonVideos[0].muted = !event.target.checked; comparisonVideos[1].muted = true; });
  comparisonVideos.forEach((video, index) => {
    video.addEventListener('loadedmetadata', () => { if (!comparisonSession) return; renderComparison(); if (comparisonLength() && !comparisonSession.wantPlay) comparisonMessage('已就绪，点击播放开始同步对比。'); });
    const ready = () => { const session = comparisonSession; if (!session) return; if (video.readyState >= 3 && !video.seeking) session.waiting.delete(video); renderComparison(); reconcileComparison(); };
    ['canplay', 'canplaythrough', 'playing', 'seeked'].forEach(name => video.addEventListener(name, ready));
    const buffering = () => { const session = comparisonSession; if (!session) return; session.waiting.add(video); pauseComparisonVideos(session); if (session.wantPlay) comparisonMessage('正在缓冲，两路视频会同步继续。'); };
    video.addEventListener('waiting', buffering);
    video.addEventListener('stalled', () => { if (video.readyState < 3) buffering(); });
    video.addEventListener('seeking', () => {
      const session = comparisonSession; if (!session) return; session.waiting.add(video); pauseComparisonVideos(session);
      if (index === 1 && comparisonVideos[0].readyState >= 1 && Math.abs(comparisonVideos[0].currentTime - video.currentTime) > .04) { session.waiting.add(comparisonVideos[0]); comparisonVideos[0].currentTime = Math.min(video.currentTime, comparisonVideos[0].duration); }
      renderComparison();
    });
    video.addEventListener('play', () => { const session = comparisonSession; if (!session) { video.pause(); return; } if (!video.paused && !session.wantPlay) { session.wantPlay = true; reconcileComparison(); } });
    video.addEventListener('pause', () => { const session = comparisonSession; if (!session) return; if (session.expectedPause.has(video)) { session.expectedPause.delete(video); return; } if (session.wantPlay && !video.ended) { session.wantPlay = false; pauseComparisonVideos(session); comparisonMessage('已暂停。'); renderComparison(); } });
    video.addEventListener('ended', comparisonEnded);
    video.addEventListener('timeupdate', () => { if (comparisonSession && index === 1) renderComparison(); });
    video.addEventListener('ratechange', () => { if (!comparisonSession) return; const rate = comparisonVideos[1].playbackRate; if (comparisonVideos[0].playbackRate !== rate) comparisonVideos[0].playbackRate = rate; if ([.5, 1, 1.5, 2].includes(rate)) $('#comparison-rate').value = String(rate); });
    video.addEventListener('error', () => { const session = comparisonSession; if (!session) return; session.failed = true; session.wantPlay = false; pauseComparisonVideos(session); comparisonMessage((index === 0 ? '参考舞蹈片段' : '生成结果') + '加载失败，请检查本地文件是否仍存在。', true); renderComparison(); });
  });
  $('#result-video').addEventListener('play', () => { if (comparisonSession) closeComparison(); });
  // End local comparison controller.

  function renderHistory() {
    const all = orderedJobs(); const filtered = all.filter(job => historyFilter === 'all' || (historyFilter === 'error' ? ['error', 'interrupted'].includes(job.status) : job.status === historyFilter));
    const signature = JSON.stringify(filtered.map(job => [job.id, job.mode, job.status, job.stage, job.image, job.imageURL, job.video, job.videoURL, job.seconds, motionId(job)]));
    text('#history-count', all.length); $('#history-dot').hidden = !all.some(job => job.status === 'awaiting_confirmation');
    $('#empty-history').hidden = filtered.length > 0;
    $('#empty-history h2').textContent = all.length ? '这个分类暂时没有记录' : '还没有创作记录';
    $('#empty-history p').textContent = all.length ? '试试切换其他分类，或开始一次新的创作。' : '第一次穿搭预览，会成为这里的第一份作品。';
    if (signature === lastHistorySignature) return; lastHistorySignature = signature;
    const fragment = document.createDocumentFragment();
    for (const job of filtered) {
      const card = document.createElement('button'); card.type = 'button'; card.className = 'panel history-card';
      card.innerHTML = '<div class="history-thumbnail"></div><div class="history-card-body"><h3 class="history-card-title"></h3><p class="history-card-time"></p><div class="history-card-bottom"><span></span>' + icon('arrow') + '</div></div>';
      const thumbnail = card.querySelector('.history-thumbnail'); const image = imageUrl(job);
      if (image) { const preview = document.createElement('img'); preview.src = image; preview.alt = job.mode === 'video_replace' ? '原片首帧，非替换效果预览' : '穿搭创作预览'; preview.loading = 'lazy'; thumbnail.append(preview); } else thumbnail.innerHTML = icon('image');
      const [label, className] = job.status === 'running' && job.is_recovering ? ['恢复中', 'running'] : statusLabels[job.status] || ['状态未记录', 'neutral']; const badge = document.createElement('span'); badge.className = 'state-badge ' + className; badge.textContent = label; thumbnail.append(badge);
      if (videoUrl(job)) { const tag = document.createElement('span'); tag.className = 'history-video-tag'; tag.innerHTML = icon('play'); const span = document.createElement('span'); span.textContent = secondsText(job.seconds) + ' 秒'; tag.append(span); thumbnail.append(tag); }
      card.querySelector('.history-card-title').textContent = (job.mode === 'video_replace' ? '视频角色替换' + (Array.isArray(job.roles) ? ' · ' + job.roles.length + '个角色' : '') : job.background_mode === 'video_frame' ? '舞蹈首帧场景' : job.mode === 'ready_image' ? job.background_mode === 'replace' ? '穿搭图换背景' : '穿搭图复用' : '穿搭创作') + ' · ' + secondsText(job.seconds) + ' 秒';
      card.querySelector('.history-card-time').textContent = dateText(job) + ' · ' + motionOption(motionId(job)).shortLabel;
      card.querySelector('.history-card-bottom span').textContent = job.status === 'awaiting_confirmation' ? job.mode === 'video_replace' ? '核对角色配置并继续确认' : '查看图片并继续确认' : job.status === 'done' ? '查看结果与下载' : job.status === 'running' && job.is_recovering ? '查看恢复进度' : job.can_resume ? '查看并继续取回' : '查看任务详情';
      card.addEventListener('click', () => { setActive(job.id); setView('studio'); $('#preview-title').scrollIntoView({behavior: 'smooth', block: 'start'}); });
      fragment.append(card);
    }
    $('#history-grid').replaceChildren(fragment);
  }
  $$('.filter-button').forEach(button => button.addEventListener('click', () => { historyFilter = button.dataset.filter; $$('.filter-button').forEach(item => item.classList.toggle('active', item === button)); lastHistorySignature = ''; renderHistory(); }));
  function mergeJob(job) { if (job && typeof job.id === 'string') jobs.set(job.id, {...jobs.get(job.id), ...job}); }
  function schedulePoll(delay = 2800) { clearTimeout(pollTimer); if (runningJob()) pollTimer = setTimeout(() => refreshJobs().catch(() => {}), delay); }
  async function refreshJobs(userInitiated = false) {
    if (readingJobs) return readingJobs;
    readingJobs = (async () => {
      try {
        const data = await api('/api/jobs'); if (!Array.isArray(data.jobs)) throw new Error('创作记录格式异常，请重新打开软件。');
        for (const job of data.jobs) {
          if (!job || typeof job.id !== 'string') continue;
          const old = jobs.get(job.id); mergeJob(job);
          if (old?.status === 'running' && job.status === 'awaiting_confirmation') showToast(job.mode === 'video_replace' ? '原片与角色配置已就绪，尚未生成替换效果，请核对后确认。' : '穿搭图已就绪，检查后确认即可生成视频。');
          if (old?.status === 'running' && job.status === 'done') showToast(job.mode === 'video_replace' ? '角色替换视频已完成，可以预览和下载。' : '舞蹈视频已完成，可以预览和下载。');
        }
        if (!previewClearedForMode && (!activeId || !jobs.has(activeId))) { const selected = orderedJobs().find(job => job.status === 'running') || orderedJobs().find(job => job.status === 'awaiting_confirmation') || orderedJobs()[0]; if (selected) setActive(selected.id); }
        pollFailures = 0; $('#history-error').hidden = true; renderHistory(); renderActive(); updateControls(); schedulePoll();
      } catch (error) {
        pollFailures += 1; text('#history-error', error.message + ' 当前画面已保留，不会自动重新生成。'); $('#history-error').hidden = false;
        if (userInitiated) showToast(error.message, true);
        if (runningJob()) schedulePoll(Math.min(15000, 4000 * pollFailures));
        throw error;
      } finally { readingJobs = null; }
    })();
    return readingJobs;
  }
  $('#refresh-history').addEventListener('click', () => refreshJobs(true).catch(() => {}));

  $('#resume-button').addEventListener('click', async () => {
    const job = activeJob();
    if (!job?.can_resume || resuming || ['running', 'done'].includes(job.status)) return;
    resuming = true; renderActive();
    try {
      const result = await api('/api/jobs/' + encodeURIComponent(job.id) + '/resume', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      if (result.state) mergeJob({...result.state, id: job.id});
      else mergeJob({...await api('/api/jobs/' + encodeURIComponent(job.id)), id: job.id});
      renderActive(); renderHistory(); schedulePoll(); updateControls();
      showToast('已开始继续处理原任务，不会重新生成。');
    } catch (error) { showToast(error.message, true); refreshJobs().catch(() => {}); }
    finally { resuming = false; renderActive(); }
  });

  function displaySettings(data, fillFields = false) {
    settings = data;
    text('#settings-data-directory', data.data_directory || '暂时无法读取保存位置');
    const configured = data.configured || {};
    for (const key of secretKeys) {
      const saved = Boolean(configured[key]); const input = $('#' + key); input.placeholder = saved ? '已保存，留空保留原密钥' : ({ARK_API_KEY: '粘贴火山方舟 API Key', VOLC_ACCESS_KEY_ID: '粘贴火山引擎 AK', VOLC_SECRET_ACCESS_KEY: '粘贴与 AK 对应的 SK', FAL_KEY: '粘贴 fal API Key', MINIMAX_API_KEY: '粘贴 MiniMax 官方 API Key', ELEVENLABS_API_KEY: '粘贴 ElevenLabs 音效 API Key'}[key]);
      const label = $(`[data-saved="${key}"]`); label.textContent = saved ? '已保存' : '未配置'; label.classList.toggle('missing', !saved);
    }
    if (fillFields) for (const key of publicKeys) $('#' + key).value = typeof data.values?.[key] === 'string' ? data.values[key] : '';
    const imageReady = Boolean(data.image_ready); const motionReady = motionConfig('volc-dreamactor-2', data).ready; const falReady = motionConfig('fal-kling-v3-pro', data).ready; const editReady = motionConfig(legacyEditModelId, data).ready;
    const h3Ready = motionConfig('minimax-h3-edit', data).ready; const seedanceReady = motionConfig('volc-seedance-2.5-edit', data).ready;
    for (const [id, ready] of [['image-api-badge', imageReady], ['motion-api-badge', motionReady], ['fal-api-badge', falReady || editReady], ['minimax-api-badge', h3Ready], ['seedance-api-badge', seedanceReady], ['storage-badge', ['VOLC_ACCESS_KEY_ID', 'VOLC_SECRET_ACCESS_KEY', 'TOS_BUCKET', 'TOS_ENDPOINT', 'TOS_REGION'].every(key => configured[key])]]) { const badge = $('#' + id); badge.textContent = ready ? '已配置' : '待配置'; badge.className = 'state-badge ' + (ready ? 'done' : 'neutral'); }
    for (const [prefix, ready] of [['image', imageReady], ['motion', motionReady], ['fal', falReady], ['edit', editReady], ['h3', h3Ready], ['seedance', seedanceReady]]) { $('#' + prefix + '-ready-dot').classList.toggle('ready', ready); text('#' + prefix + '-ready-label', ready ? '已配置' : '待配置'); }
    updateControls();
  }
  async function loadSettings() {
    try { const data = await api('/api/settings'); displaySettings(data, !settingsDirty); return data; }
    catch (error) { $('#connection-button').classList.add('unavailable'); text('#connection-text', '本地服务连接待检查'); text('#settings-feedback', error.message); $('#settings-feedback').className = 'settings-feedback error'; throw error; }
  }
  $('#settings-form').addEventListener('input', () => { settingsDirty = true; text('#settings-feedback', '有尚未保存的修改。'); $('#settings-feedback').className = 'settings-feedback'; });
  $$('.reveal-secret').forEach(button => button.addEventListener('click', () => { const input = $('#' + button.dataset.target); const show = input.type === 'password'; input.type = show ? 'text' : 'password'; button.setAttribute('aria-pressed', String(show)); }));
  $('#settings-form').addEventListener('submit', async event => {
    event.preventDefault(); if (saving) return; saving = true; $('#save-settings').disabled = true; text('#settings-feedback', '正在保存本地配置…');
    const values = Object.fromEntries([...secretKeys, ...publicKeys].map(key => [key, $('#' + key).value.trim()]));
    try {
      await api('/api/settings', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values)});
      for (const key of secretKeys) { $('#' + key).value = ''; $('#' + key).type = 'password'; }
      $$('.reveal-secret').forEach(button => button.setAttribute('aria-pressed', 'false'));
      settingsDirty = false; await loadSettings(); text('#settings-feedback', '配置已保存，可以返回工作室继续。'); $('#settings-feedback').className = 'settings-feedback success'; showToast('配置已保存在本机。');
    } catch (error) { text('#settings-feedback', error.message); $('#settings-feedback').className = 'settings-feedback error'; }
    finally { saving = false; $('#save-settings').disabled = false; }
  });

  $('#creation-form').addEventListener('submit', async event => {
    event.preventDefault(); if (formBusy || runningJob()) return;
    if (needsImageApi() && !settings?.image_ready) { setView('settings'); showToast('请先完成图片合成 API 配置，上传素材会保留。'); return; }
    const needed = requiredInputs();
    const absent = needed.filter(key => !files.has(key)); $$('.upload-card').forEach(card => card.classList.toggle('invalid', absent.includes(card.dataset.field)));
    if (absent.length) { text('#creation-error', '请先补齐标记的创作素材。'); $('#creation-error').hidden = false; $(`[data-field="${absent[0]}"] .drop-zone`).focus(); return; }
    const duration = durationSelection(); if (duration.error) { text('#creation-error', duration.error); $('#creation-error').hidden = false; return; }
    if (mode === 'video_replace') {
      for (let index = 1; index <= roleCount; index++) {
        const input = $('#role-description-' + index);
        if (!input.value.trim()) { input.classList.add('invalid'); text('#creation-error', '请描述角色' + index + '替换原片中的谁，例如开场位置和服装特征。'); $('#creation-error').hidden = false; input.focus(); return; }
      }
      if ($('#edit-prompt-input').value.trim().length > 1000) { text('#creation-error', '补充视频要求请控制在1000字以内。'); $('#creation-error').hidden = false; $('#edit-prompt-input').focus(); return; }
    }
    const form = new FormData(); for (const key of needed) form.append(key, files.get(key).file);
    if (mode === 'video_replace') {
      form.set('edit_model', selectedEditId); form.set('edit_prompt', $('#edit-prompt-input').value.trim());
      if (motionOption(selectedEditId).resolutions?.length) form.set('video_resolution', editResolutions[selectedEditId]);
      if (selectedEditId === 'volc-seedance-2.5-edit') {
        const videoAsset = $('#video-asset-id').value.trim(); if (videoAsset) form.set('video_asset_id', videoAsset);
        for (let index = 1; index <= roleCount; index++) { const value = $('#role-asset-id-' + index).value.trim(); if (value) form.set('role_asset_id_' + index, value); }
      }
      for (let index = 1; index <= roleCount; index++) form.set('role_description_' + index, $('#role-description-' + index).value.trim());
    } else {
      form.set('garment_mode', garmentMode); form.set('background_mode', effectiveBackgroundMode());
      if (mode === 'compose' && garmentMode === 'separate' && files.has('pants')) form.append('pants', files.get('pants').file);
      form.set('image_model', 'volc-seedream'); form.set('motion_model', selectedMotionId);
      if (mode === 'compose') form.set('prompt', $('#prompt-input').value.trim());
    }
    const submittedMode = mode; const submittedBackground = replaceReadyBackground();
    form.set('mode', mode); form.set('duration_mode', durationMode); form.set('start', String(duration.start)); form.set('seconds', String(duration.seconds));
    formBusy = true; $('#creation-error').hidden = true; updateControls();
    try {
      const data = await api('/api/jobs', {method: 'POST', body: form});
      if (typeof data.id !== 'string') throw new Error('未收到任务编号，请先刷新创作记录核对，避免重复提交。');
      if (data.state) mergeJob({...data.state, id: data.id}); else mergeJob({...await api('/api/jobs/' + encodeURIComponent(data.id)), id: data.id});
      setActive(data.id); renderHistory(); schedulePoll();
      if (submittedMode === 'video_replace') showToast('已准备原片首帧与角色配置，尚未生成替换效果；请核对后确认。');
      else if (submittedMode === 'ready_image') showToast(submittedBackground ? '正在替换背景，完成后请检查预览，再确认生成视频。' : '穿搭图已进入预览，请检查后再确认生成视频。');
      else showToast('穿搭合成已开始，完成后会等待你确认。');
    } catch (error) {
      text('#creation-error', error.message + (error.network ? ' 请先刷新创作记录核对任务，避免重复计费。' : '')); $('#creation-error').hidden = false; refreshJobs().catch(() => {});
    } finally { formBusy = false; updateControls(); }
  });
  $('#confirm-button').addEventListener('click', async () => {
    const job = activeJob(); if (!job || job.status !== 'awaiting_confirmation' || confirming || runningJob()) return;
    const fixedMotion = motionId(job);
    if (!motionConfig(fixedMotion).ready) { setView('settings'); showToast('该任务使用' + motionOption(fixedMotion).label + '，请先完成对应配置。' + (job.mode === 'video_replace' ? '原片与角色配置已经保存。' : '这张穿搭图已经保存。')); return; }
    confirming = true; updateControls();
    try {
      const data = await api('/api/jobs/' + encodeURIComponent(job.id) + '/confirm', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      if (data.state) mergeJob({...data.state, id: job.id}); else mergeJob(await api('/api/jobs/' + encodeURIComponent(job.id)));
      mediaChosen = false; renderActive(); renderHistory(); schedulePoll(); showToast('视频任务已提交，请保留软件运行。');
    } catch (error) { showToast(error.message + (error.network ? ' 请先刷新记录核对，避免重复提交。' : ''), true); refreshJobs().catch(() => {}); }
    finally { confirming = false; updateControls(); }
  });

  window.addEventListener('focus', () => { refreshJobs().catch(() => {}); loadSettings().catch(() => {}); });
  window.addEventListener('beforeunload', () => { for (const item of files.values()) URL.revokeObjectURL(item.url); clearTimeout(pollTimer); });
  applyTheme(selectedTheme);
  setView(location.hash.slice(1) || 'studio', false);
  loadPreferences().finally(() => { renderActive(); renderHistory(); updateControls(); loadSettings().catch(() => {}); refreshJobs().catch(() => {}); });
})();
