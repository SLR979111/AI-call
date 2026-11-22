import {readPhone} from './phone-file.js';
import {workOrders, sameCard as sameTask} from './work-orders.js';
import {prepareTask, checkPreparation} from './preparation.js';
import {CallConnection} from './connection.js';
import {LiveConversation} from './conversation.js';
import {bindCallAnalysis} from './call-analysis.js';

const $ = id => document.getElementById(id);
const view = new LiveConversation($('transcript'), $('viewport'));
let task, defaultTask, call, voice = false, connected = false, epoch = 0;
let inputInFlight = false, replyActive = false;
let composing = false, savedCustom;
const packs = new Map();
let activePhone, flowState;
let mode = localStorage.getItem('g2-call-mode') === 'local' ? 'local' : 'cloud';
bindCallAnalysis(() => ({mode}));
let cloudConfigured = false, preparation = null;
$('callMode').value = mode;
$('callMode').onchange = () => {
  if (connected || inputInFlight) { $('callMode').value = mode; return; }
  mode = $('callMode').value;
  localStorage.setItem('g2-call-mode', mode);
  if (task) warmTask(task);
  clearError();
};
const phaseNames = {ready: '准备好了，开始聊吧', thinking: '正在组织回答', speaking: '正在说话，可以随时打断', listening: '在听，请说', ended: '对话已结束'};
const dialStages = {
  component: '正在载入消回声组件…',
  permission: '等待麦克风权限，请在浏览器提示中选择允许',
  capture: '正在等待麦克风开始采集…',
};

function send(event) { call?.send(event); }
function warmTask(card, retry = false) {
  if (mode !== 'cloud' || !cloudConfigured) {
    preparation = null;
    notice('当前工单：' + card.title);
    return null;
  }
  if (sameTask(preparation?.card, card) && preparation.file === activePhone && (!retry || ['preparing', 'ready', 'disabled'].includes(preparation.status))) return preparation;
  const job = {card, file: activePhone, status: 'preparing'};
  preparation = job;
  notice((job.file ? '正在准备开场首句 · ' : '正在准备电话流程和开场首句 · ') + card.title);
  job.done = prepareTask(card, {retry, phoneFile: activePhone})
    .then(status => {
      job.status = status;
      if (preparation === job && !connected && !inputInFlight) {
        if (status === 'failed' || status === 'unconfigured') notice('通话准备失败，点击开始可重试');
        else notice((status === 'ready' ? '工单已就绪：' : '当前工单：') + card.title);
      }
      return status;
    });
  return job;
}

async function waitToDial(job) {
  notice(job.file ? '正在准备开场第一句，准备好后自动接通' : '正在准备电话流程和开场首句，准备好后自动接通');
  try {
    const cancelled = new Promise(resolve => setDialCancel(() => resolve('cancelled')));
    return await Promise.race([job.done, cancelled]);
  } finally {
    setDialCancel();
  }
}
function setDialCancel(action = null) {
  const button = $('cancelDial');
  button.onclick = action;
  button.hidden = !action;
}
function showError(text) { $('error').textContent = text; $('error').hidden = false; }
function clearError() { $('error').hidden = true; }
function notice(text = '') { $('callNotice').textContent = text; $('callNotice').hidden = !text; }
const orders = workOrders({
  mode: () => mode, locked: () => connected || inputInFlight,
  progress: () => sameTask(flowState?.card,task) ? flowState.progress : null,
  activate(card, file, id) {
    if (!sameTask(card,task) || activePhone !== file) flowState = null;
    activePhone = file; rememberTask(card);
    if (file) localStorage.setItem('g2-phone', JSON.stringify(file));
    else localStorage.removeItem('g2-phone');
    localStorage.setItem('g2-active-order',id);
    warmTask(card);
  }
});
function packId(card) {
  for (const [id, pack] of packs) if (sameTask(card, pack)) return id;
  return '';
}
function rememberTask(card) {
  task = card;
  const id = packId(card);
  localStorage.setItem('g2-task', JSON.stringify(card));
  if (id) {
    localStorage.setItem('g2-task-pack', JSON.stringify({id, card}));
  } else {
    localStorage.removeItem('g2-task-pack');
    savedCustom = card;
    localStorage.setItem('g2-custom-task', JSON.stringify(card));
  }
}
function interrupt(source = 'manual', evidence = null) {
  if (!connected || !epoch || !replyActive) return;
  const progress = call?.player?.stop(true) || [];
  view.cancel(epoch);
  send({type: 'interrupt', epoch, progress, source, evidence});
  $('phase').textContent = voice ? '在听，请说' : '已打断，可以继续输入';
}
function showText(value) {
  $('textForm').hidden = !value;
  $('toggleText').setAttribute('aria-expanded', String(value));
  $('toggleText').setAttribute('aria-label', value ? '收起文字输入' : '打开文字输入');
  $('toggleText').title = value ? '收起文字输入' : '打开文字输入';
  if (value) $('textInput').focus();
}
async function start(useVoice) {
  if (connected || inputInFlight || !task) return;
  if (mode === 'cloud' && !cloudConfigured) {
    showError('云端模式需要 API 凭据。可在工单的通话选项中切换到纯本地。'); return;
  }
  inputInFlight = true; clearError();
  $('editTask').disabled = true;
  $('startVoice').disabled = $('startText').disabled = true;
  const active = call = new CallConnection({
    onMessage, onError: showError, onClose: () => cleanup('连接已结束'),
    onLevel: input => view.setInput(input),
    onStage: step => { if (call === active) notice(dialStages[step]); },
  });
  try {
    const cancel = () => cleanup('已取消拨打');
    setDialCancel(cancel);
    voice = useVoice;
    await active.resumeAudio(voice);
    if (call !== active) return;
    const job = warmTask(task, true);
    if (job?.status === 'preparing') {
      const status = await waitToDial(job);
      if (status === 'cancelled') { await cleanup('已取消拨打，工单继续准备'); return; }
      checkPreparation(status);
    }
    if (call !== active) return;
    notice('正在连接本地服务…');
    setDialCancel(cancel);
    await active.open();
    if (call !== active) return;
    setDialCancel();
    flowState = null; epoch = 0; replyActive = false; view.reset();
    $('textInput').value = ''; composing = false;
    $('latest').hidden = true;
    connected = true;
    $('startControls').hidden = true; $('callControls').hidden = false;
    $('mute').hidden = !voice;
    $('mute').setAttribute('aria-label', '暂停麦克风'); $('mute').title = '暂停麦克风'; $('mute').setAttribute('aria-pressed', 'false');
    $('callMode').disabled = $('prediction').disabled = true;
    $('connection').textContent = '已连接';
    send({type: 'start', ...(activePhone ? {phone_file: activePhone, analysis_task: task} : {task}),
      voice, mode, audio_protocol: 2, predictions: $('prediction').checked});
    notice(mode === 'local' ? '正在准备本地模型…' : '');
    showText(!voice);
  } catch (error) {
    if (call !== active) return;
    await cleanup();
    if (call) return;
    notice('未能开始，可以重试');
    showError(error.name === 'NotAllowedError' ? '没有获得麦克风权限。允许后可重试，也可以先用文字试聊。' : error.message);
  } finally {
    if (!call || call === active) {
      inputInFlight = false; $('editTask').disabled = false; $('startVoice').disabled = $('startText').disabled = false;
    }
  }
}
function onMessage({data}) {
  if (data instanceof ArrayBuffer) { call?.player?.packet(data); return; }
  let event; try { event = JSON.parse(data); } catch { return; }
  switch (event.type) {
    case 'ready':
      if (event.phone_file) { activePhone = event.phone_file; orders.ready(activePhone); }
      notice(); break;
    case 'reply_start':
      if (!view.handle(event)) break;
      epoch = event.epoch; replyActive = true; call?.player?.begin(epoch); clearError(); break;
    case 'utterance':
      if (!view.handle(event)) break;
      call?.player?.unit(event);
      break;
    case 'utterance_end': call?.player?.finish(event); break;
    case 'cancel':
      call?.player?.cancel(event.epoch); view.cancel(event.epoch);
      if (event.epoch === epoch) replyActive = false;
      break;
    case 'transcript': case 'candidate_progress': case 'candidates': view.handle(event); break;
    case 'state': {
      if (event.epoch < epoch) break;
      $('phase').textContent = phaseNames[event.phase] || '在听，请说';
      if (event.flow) {
        flowState = {progress: event.flow, card: task};
        orders.refresh();
      }
      break;
    }
    case 'reply_done':
      if (!view.handle(event)) break;
      replyActive = false;
      break;
    case 'hangup':
      if (event.epoch !== epoch) break;
      cleanup(event.reason === 'complete' ? '沟通已完成，已道别挂断' : '已道别，通话结束');
      break;
    case 'error': showError(event.message); break;
  }
}
async function cleanup(label = '对话已结束，可以重新开始') {
  connected = false; replyActive = false;
  inputInFlight = false;
  setDialCancel();
  $('editTask').disabled = $('startVoice').disabled = $('startText').disabled = false;
  view.stop();
  const active = call;
  call = null;
  const closing = active?.close();
  $('startControls').hidden = false; $('callControls').hidden = true; $('textForm').hidden = true;
  $('callMode').disabled = $('prediction').disabled = false; orders.refresh();
  $('connection').textContent = '未连接'; $('phase').textContent = label;
  notice(label);
  await closing;
}
$('startVoice').onclick = () => start(true);
$('startText').onclick = () => start(false);
$('interrupt').onclick = () => interrupt();
$('stop').onclick = () => { send({type: 'stop'}); cleanup(); };
$('toggleText').onclick = () => showText($('textForm').hidden);
$('mute').onclick = () => {
  const mic = call?.mic;
  if (!mic) return;
  mic.muted = !mic.muted;
  const label = mic.muted ? '恢复麦克风' : '暂停麦克风';
  $('mute').setAttribute('aria-label', label); $('mute').title = label; $('mute').setAttribute('aria-pressed', String(mic.muted));
  notice(mic.muted ? '麦克风已暂停' : '');
  if (mic.muted) view.setInput({active: false});
};
$('textForm').onsubmit = event => {
  event.preventDefault(); if (composing) return;
  const text = $('textInput').value.trim();
  if (!text || !connected) return;
  interrupt('text'); send({type: 'text', text}); $('textInput').value = '';
};
$('textInput').addEventListener('compositionstart', () => { composing = true; });
$('textInput').addEventListener('compositionend', () => { composing = false; });
$('textInput').addEventListener('keydown', event => {
  if (event.key === 'Enter' && (event.isComposing || event.keyCode === 229)) event.preventDefault();
});
view.engine.addEventListener('followchange', event => { $('latest').hidden = event.detail.following; });
$('latest').onclick = () => view.engine.followLatest();
window.addEventListener('beforeunload', () => { call?.close(); view.destroy(); });
try {
  const config = await (await fetch('/api/config')).json();
  defaultTask = config.default_task;
  for (const [id, card] of Object.entries(config.task_packs || {})) {
    packs.set(id, card);
  }
  try { task = JSON.parse(localStorage.getItem('g2-task')); } catch {}
  try {
    const stored = JSON.parse(localStorage.getItem('g2-phone'));
    const selected = localStorage.getItem('g2-active-order');
    const bound = stored?.task || JSON.parse(localStorage.getItem('g2-order/' + selected))?.card
      || JSON.parse(localStorage.getItem('g2-task-pack'))?.card;
    if (stored && sameTask(bound,task)) activePhone = await readPhone(stored, true);
  } catch { /* The catalog retains the confirmed file if the service is temporarily unavailable. */ }
  try { savedCustom = JSON.parse(localStorage.getItem('g2-custom-task')); } catch {}
  const legacyDefaults = config.legacy_default_tasks || [config.legacy_default_task];
  let savedPack;
  try { savedPack = JSON.parse(localStorage.getItem('g2-task-pack')); } catch {}
  // The classic UI writes the shared task directly. Preserve its edits when
  // checking whether a previously selected preset can follow a catalog update.
  if (packs.size && savedPack?.id && sameTask(savedPack.card, task)) {
    const latest = packs.get(savedPack.id) || defaultTask;
    if (!sameTask(latest, task)) { activePhone = undefined; rememberTask(latest); }
  } else if (legacyDefaults.some(legacy => sameTask(task, legacy))) {
    activePhone = undefined; rememberTask(defaultTask);
  }
  if (!task?.checkpoints?.length || !task.goal || !task.opening) task = defaultTask;
  if (!packId(task) && packs.size) rememberTask(task);
  const url = new URL(location.href);
  const requested = url.searchParams.get('pack');
  if (requested !== null) {
    const card = packs.get(requested);
    if (card) {
      if (!sameTask(card,task)) activePhone = undefined;
      rememberTask(card);
    } else {
      showError('找不到这张工单，可在任务设置中重新选择。');
    }
    url.searchParams.delete('pack');
    history.replaceState(null, '', url);
  }
  cloudConfigured = config.configured;
  $('prediction').checked = config.predictions;
  await orders.init(packs,task,activePhone,savedCustom,localStorage.getItem('g2-active-order'));
  $('connection').textContent = '服务就绪'; $('startVoice').disabled = $('startText').disabled = false;
} catch { showError('无法载入任务配置，请确认本地服务已启动。'); }
