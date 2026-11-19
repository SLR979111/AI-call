import {Player, Microphone} from './audio.js';
import {LiveConversation} from './conversation.js';

const $ = id => document.getElementById(id);
const view = new LiveConversation($('transcript'), $('viewport'));
let task, defaultTask, socket, context, player, mic, voice = false, connected = false, epoch = 0;
let timer, turns = 0, hits = 0, records = [], uiUnits = new Map(), inputInFlight = false, replyActive = false;
let composing = false, editorTask;
const phaseNames = {ready: '准备好了，开始聊吧', thinking: '正在组织回答', speaking: '正在说话，可以随时打断', listening: '在听，请说', ended: '对话已结束'};

function send(event) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(event)); }
function warmTask(card) {
  fetch('/api/warm', {method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({task: card})}).catch(() => {});
}
function showError(text) { $('error').textContent = text; $('error').hidden = false; }
function clearError() { $('error').hidden = true; }
function notice(text = '') { $('callNotice').textContent = text; $('callNotice').hidden = !text; }
function fillTaskEditor(card) {
  editorTask = card;
  $('editTitle').value = card.title; $('editGoal').value = card.goal; $('editFacts').value = card.facts;
  $('editPoints').value = card.checkpoints.map(p => p.description).join('\n'); $('editOpening').value = card.opening;
}
function renderTask() {
  $('checkpoints').replaceChildren();
  for (const point of task.checkpoints) {
    const li = document.createElement('li'); li.dataset.point = point.id;
    const icon = document.createElement('span'); icon.className = 'check';
    const text = document.createElement('span'); text.textContent = point.description;
    li.append(icon, text); $('checkpoints').append(li);
  }
}
function record(role, text, key = null) {
  const item = {role, text, delivery: role === 'user' || !voice ? 'complete' : 'pending'};
  records.push(item); if (key) uiUnits.set(key, item);
}
function markCancelled(cancelledEpoch) {
  view.cancel(cancelledEpoch);
  for (const [key, item] of uiUnits) {
    if (key.startsWith(cancelledEpoch + ':') && item.delivery === 'pending') item.delivery = 'interrupted';
  }
}
function interrupt(source = 'manual', evidence = null) {
  if (!connected || !epoch || !replyActive) return;
  const progress = player?.stop(true) || [];
  markCancelled(epoch);
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
  inputInFlight = true; clearError();
  notice(useVoice ? '正在连接，首次使用请允许麦克风' : '正在连接');
  $('startVoice').disabled = $('startText').disabled = true;
  try {
    voice = useVoice;
    if (voice) { context = new AudioContext(); await context.resume(); }
    socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    const activeSocket = socket;
    socket.binaryType = 'arraybuffer';
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('连接超时，请确认服务已启动。')), 6000);
      socket.onopen = () => { clearTimeout(timeout); resolve(); };
      socket.onerror = () => { clearTimeout(timeout); reject(new Error('无法连接到本地服务。')); };
    });
    if (voice) {
      player = new Player(context, send, (e, u) => {
        const item = uiUnits.get(e + ':' + u); if (item) item.delivery = 'complete';
      });
      mic = new Microphone(context, socket, showError, player.output, input => view.setInput(input));
      await mic.open();
    }
    records = []; uiUnits.clear(); turns = hits = 0; epoch = 0; replyActive = false; view.reset();
    $('textInput').value = ''; composing = false;
    $('latency').textContent = ''; $('cacheStat').textContent = ''; $('latest').hidden = true;
    renderTask(); connected = true;
    socket.onmessage = event => { if (socket === activeSocket && connected) onMessage(event); };
    socket.onclose = () => { if (socket === activeSocket) cleanup('连接已结束'); };
    socket.onerror = () => { if (socket === activeSocket) showError('连接发生错误，请重新开始。'); };
    $('startControls').hidden = true; $('callControls').hidden = false;
    $('mute').hidden = !voice;
    $('mute').setAttribute('aria-label', '暂停麦克风'); $('mute').title = '暂停麦克风'; $('mute').setAttribute('aria-pressed', 'false');
    $('prediction').disabled = true; $('saveTask').disabled = true; $('restoreTask').disabled = true;
    for (const id of ['editTitle', 'editGoal', 'editFacts', 'editPoints', 'editOpening']) $(id).disabled = true;
    $('connection').textContent = '已连接';
    const startedAt = performance.now();
    timer = setInterval(() => {
      const seconds = Math.floor((performance.now() - startedAt) / 1000);
      $('duration').textContent = Math.floor(seconds / 60).toString().padStart(2, '0') + ':' + (seconds % 60).toString().padStart(2, '0');
    }, 1000);
    send({type: 'start', task, voice, audio_protocol: 2, predictions: $('prediction').checked});
    notice();
    showText(!voice);
  } catch (error) {
    await cleanup();
    notice('未能开始，可以重试');
    showError(error.name === 'NotAllowedError' ? '没有获得麦克风权限。允许后可重试，也可以先用文字试聊。' : error.message);
  } finally {
    inputInFlight = false; $('startVoice').disabled = $('startText').disabled = false;
  }
}
function onMessage({data}) {
  if (data instanceof ArrayBuffer) { player?.packet(data); return; }
  let event; try { event = JSON.parse(data); } catch { return; }
  switch (event.type) {
    case 'reply_start':
      if (!view.handle(event)) break;
      epoch = event.epoch; replyActive = true; player?.begin(epoch); $('latency').textContent = ''; clearError(); break;
    case 'utterance':
      if (!view.handle(event)) break;
      player?.unit(event); record('assistant', event.text, event.epoch + ':' + event.unit); break;
    case 'utterance_end': player?.finish(event); break;
    case 'cancel':
      player?.cancel(event.epoch); markCancelled(event.epoch);
      if (event.epoch === epoch) replyActive = false;
      break;
    case 'transcript':
      if (view.handle(event) && event.final) record('user', event.text);
      break;
    case 'candidates': view.handle(event); break;
    case 'state': {
      if (event.epoch < epoch) break;
      $('phase').textContent = phaseNames[event.phase] || '在听，请说';
      const status = {active: '对话进行中', complete: '已说明并得到明确确认', paused: '已尊重对方意愿，暂停沟通', declined: '对方已拒绝，结束本次目标'};
      $('outcomeText').textContent = status[event.status] || '等待开始';
      for (const li of $('checkpoints').children) {
        const done = event.delivered.includes(li.dataset.point);
        li.classList.toggle('done', done); li.querySelector('.check').textContent = done ? '✓' : '';
      }
      break;
    }
    case 'reply_done':
      if (!view.handle(event)) break;
      replyActive = false;
      turns++; if (event.reused) hits++;
      $('cacheStat').textContent = '完成 ' + turns + ' 次回复 · 复用预测 ' + hits + ' 次'; break;
    case 'latency':
      const latency = 'voice_to_answer_ms' in event ? event.voice_to_answer_ms : event.voice_to_playback_ms;
      if (event.epoch === epoch && latency != null) $('latency').textContent = '接话 ' + (latency / 1000).toFixed(2) + 's';
      break;
    case 'hangup':
      if (event.epoch !== epoch) break;
      cleanup(event.reason === 'complete' ? '沟通已完成，已道别挂断' : '已道别，通话结束');
      break;
    case 'error': showError(event.message); break;
  }
}
async function cleanup(label = '对话已结束，可以重新开始') {
  connected = false; replyActive = false; clearInterval(timer);
  for (const item of uiUnits.values()) if (item.delivery === 'pending') item.delivery = 'interrupted';
  mic?.close(); mic = null; player?.close(); player = null; view.stop();
  const oldContext = context; context = null;
  const oldSocket = socket; socket = null;
  if (oldSocket && oldSocket.readyState < WebSocket.CLOSING) { oldSocket.onclose = null; oldSocket.close(); }
  $('startControls').hidden = false; $('callControls').hidden = true; $('textForm').hidden = true;
  $('prediction').disabled = false; $('saveTask').disabled = false; $('restoreTask').disabled = false;
  for (const id of ['editTitle', 'editGoal', 'editFacts', 'editPoints', 'editOpening']) $(id).disabled = false;
  $('connection').textContent = '未连接'; $('phase').textContent = label;
  notice(label);
  if (oldContext && oldContext.state !== 'closed') await oldContext.close();
}
$('startVoice').onclick = () => start(true);
$('startText').onclick = () => start(false);
$('interrupt').onclick = () => interrupt();
$('stop').onclick = () => { send({type: 'stop'}); cleanup(); };
$('toggleText').onclick = () => showText($('textForm').hidden);
$('mute').onclick = () => {
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
$('editTask').onclick = () => {
  if (!task) return;
  fillTaskEditor(task);
  $('taskDialog').showModal();
};
$('restoreTask').onclick = () => { if (!connected && defaultTask) fillTaskEditor(defaultTask); };
$('closeDialog').onclick = () => $('taskDialog').close();
$('taskForm').onsubmit = event => {
  event.preventDefault(); if (connected) return;
  const descriptions = $('editPoints').value.split('\n').map(s => s.trim()).filter(Boolean);
  if (!descriptions.length || descriptions.length > 8 || descriptions.some(s => s.length > 300)) {
    $('editPoints').setCustomValidity('请输入 1 至 8 个要点，每项不超过 300 字。'); $('editPoints').reportValidity(); return;
  }
  const samePoints = descriptions.length === editorTask.checkpoints.length
    && descriptions.every((description, i) => description === editorTask.checkpoints[i].description);
  task = {title: $('editTitle').value.trim(), goal: $('editGoal').value.trim(), facts: $('editFacts').value.trim(),
    opening: $('editOpening').value.trim(), checkpoints: samePoints ? editorTask.checkpoints
      : descriptions.map((description, i) => ({id: 'point_' + (i + 1), description}))};
  localStorage.setItem('g2-task', JSON.stringify(task)); renderTask(); warmTask(task); $('taskDialog').close();
};
$('editPoints').oninput = () => $('editPoints').setCustomValidity('');
$('export').onclick = () => {
  const blob = new Blob([JSON.stringify({task, conversation: records}, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob), a = document.createElement('a'); a.href = url; a.download = 'ai-call-g2-conversation.json'; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
view.engine.addEventListener('followchange', event => { $('latest').hidden = event.detail.following; });
$('latest').onclick = () => view.engine.followLatest();
window.addEventListener('beforeunload', () => { mic?.close(); player?.close(); socket?.close(); view.destroy(); });
try {
  const config = await (await fetch('/api/config')).json();
  defaultTask = config.default_task;
  try { task = JSON.parse(localStorage.getItem('g2-task')); } catch {}
  const sameTask = (a, b) => a && b && ['title', 'goal', 'facts', 'opening'].every(key => a[key] === b[key])
    && JSON.stringify(a.checkpoints) === JSON.stringify(b.checkpoints);
  if ((config.legacy_default_tasks || [config.legacy_default_task]).some(legacy => sameTask(task, legacy))) {
    task = config.default_task; localStorage.setItem('g2-task', JSON.stringify(task));
  }
  if (!task?.checkpoints?.length || !task.goal || !task.opening) task = config.default_task;
  $('prediction').checked = config.predictions; renderTask(); warmTask(task);
  $('connection').textContent = '服务就绪'; $('startVoice').disabled = $('startText').disabled = false;
  if (!config.configured) showError('服务尚未配置 API 凭据，请在本地 .env 中填写后重启。');
} catch { showError('无法载入任务配置，请确认本地服务已启动。'); }
