import {Player, Microphone} from './audio.js';

const $ = id => document.getElementById(id);
let task, socket, context, player, mic, voice = false, connected = false, epoch = 0;
let startedAt = 0, timer, turns = 0, hits = 0, records = [], uiUnits = new Map(), inputInFlight = false;
const phaseNames = {ready:'准备好了，开始聊吧', thinking:'正在组织回答', speaking:'正在说话 · 可以随时打断', listening:'在听，请说'};

function send(event) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(event)); }
function showError(text) { $('error').textContent = text; $('error').classList.remove('hidden'); }
function clearError() { $('error').classList.add('hidden'); }
function renderTask() {
  $('taskTitle').textContent = task.title;
  $('taskGoal').textContent = task.goal;
  $('taskFacts').textContent = task.facts;
  $('checkpoints').replaceChildren();
  for (const point of task.checkpoints) {
    const li = document.createElement('li'); li.dataset.point = point.id;
    const icon = document.createElement('span'); icon.className = 'check';
    const text = document.createElement('span'); text.textContent = point.description;
    li.append(icon, text); $('checkpoints').append(li);
  }
}
function append(role, text, key = null) {
  $('empty')?.remove();
  const message = document.createElement('div'); message.className = `message ${role}`;
  const speaker = document.createElement('div'); speaker.className = 'speaker'; speaker.textContent = role==='user' ? '你' : 'AI CALL';
  const bubble = document.createElement('div'); bubble.className = 'bubble'; bubble.textContent = text;
  message.append(speaker, bubble); $('transcript').append(message);
  const record = {role, text, delivery:role==='user' || !voice ? 'complete' : 'pending'};
  records.push(record);
  if (key) uiUnits.set(key, {message, record});
  $('transcript').scrollTop = $('transcript').scrollHeight;
}
function markCancelled(cancelledEpoch) {
  for (const [key, value] of uiUnits) {
    if (key.startsWith(`${cancelledEpoch}:`) && value.record.delivery === 'pending') {
      value.record.delivery = 'interrupted';
      const note = document.createElement('div'); note.className = 'delivery-note'; note.textContent = '这一句未完整播出';
      value.message.append(note);
    }
  }
}
function interrupt(source = 'manual', evidence = null) {
  if (!connected || !epoch) return;
  const progress = player?.stop(true) || [];
  markCancelled(epoch);
  send({type:'interrupt', epoch, progress, source, evidence});
  $('phase').textContent = voice ? '在听，请说' : '已打断，可以继续输入';
}
async function start(useVoice) {
  if (connected || inputInFlight) return;
  inputInFlight = true;
  clearError();
  $('startVoice').disabled = $('startText').disabled = true;
  try {
    voice = useVoice;
    // Resume within the click gesture, before network or microphone awaits.
    if (voice) {
      context = new AudioContext();
      await context.resume();
    }
    socket = new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);
    socket.binaryType = 'arraybuffer';
    await new Promise((resolve,reject) => {
      const timeout = setTimeout(() => reject(new Error('连接超时，请确认服务已启动。')), 6000);
      socket.onopen = () => { clearTimeout(timeout); resolve(); };
      socket.onerror = () => { clearTimeout(timeout); reject(new Error('无法连接到本地服务。')); };
    });
    if (voice) {
      player = new Player(context, send, (e,u) => {
        const item = uiUnits.get(`${e}:${u}`);
        if (item) item.record.delivery = 'complete';
      });
      mic = new Microphone(context, socket, showError, player.output);
      await mic.open();
    }
    records = []; uiUnits.clear(); turns = hits = 0; epoch = 0;
    $('transcript').replaceChildren();
    $('latency').textContent = '';
    renderTask();
    connected = true; startedAt = performance.now();
    socket.onmessage = onMessage;
    socket.onclose = () => cleanup('连接已结束');
    socket.onerror = () => showError('连接发生错误，请重新开始。');
    $('startControls').classList.add('hidden');
    $('callControls').classList.remove('hidden');
    $('textForm').classList.remove('hidden');
    $('mute').classList.toggle('hidden', !voice);
    $('mute').textContent = '暂停麦克风'; $('mute').setAttribute('aria-pressed','false');
    $('editTask').disabled = $('prediction').disabled = true;
    $('connection').textContent = '已连接'; $('connection').classList.add('online');
    $('modeLabel').textContent = voice ? '实时语音' : '文字对话';
    $('activity').classList.add('live');
    timer = setInterval(() => {
      const seconds = Math.floor((performance.now()-startedAt)/1000);
      $('duration').textContent = `${Math.floor(seconds/60).toString().padStart(2,'0')}:${(seconds%60).toString().padStart(2,'0')}`;
    },1000);
    send({type:'start', task, voice, audio_protocol:2, predictions:$('prediction').checked});
    if (!voice) $('textInput').focus();
  } catch (error) {
    await cleanup();
    showError(error.name==='NotAllowedError' ? '没有获得麦克风权限。允许后可重试，也可以先用文字试聊。' : error.message);
  } finally {
    inputInFlight = false;
    $('startVoice').disabled = $('startText').disabled = false;
  }
}
function onMessage({data}) {
  if (data instanceof ArrayBuffer) { player?.packet(data); return; }
  let event;
  try { event = JSON.parse(data); } catch { return; }
  switch (event.type) {
    case 'reply_start':
      epoch = event.epoch; player?.begin(epoch); $('latency').textContent=''; clearError(); break;
    case 'utterance':
      if (event.epoch !== epoch || player?.blocked.has(event.epoch)) break;
      player?.unit(event);
      append('assistant',event.text,`${event.epoch}:${event.unit}`); break;
    case 'utterance_end': player?.finish(event); break;
    case 'cancel': player?.cancel(event.epoch); markCancelled(event.epoch); break;
    case 'transcript':
      if (event.final) { $('partial').textContent=''; append('user',event.text); }
      else $('partial').textContent = event.text ? `听到：${event.text}` : '';
      break;
    case 'state': {
      $('phase').textContent = phaseNames[event.phase] || '在听，请说';
      $('activity').classList.toggle('talking', event.phase==='speaking');
      const status = {active:'对话进行中',complete:'已说明并得到明确确认',paused:'已尊重对方意愿，暂停沟通',declined:'对方已拒绝，结束本次目标'};
      $('outcomeText').textContent = status[event.status] || '等待开始';
      for (const li of $('checkpoints').children) {
        const done = event.delivered.includes(li.dataset.point);
        li.classList.toggle('done',done); li.querySelector('.check').textContent = done ? '✓' : '';
      }
      break;
    }
    case 'reply_done':
      turns++; if(event.reused) hits++;
      $('cacheStat').textContent=`完成 ${turns} 次回复 · 复用预测 ${hits} 次`;
      break;
    case 'latency':
      if (event.voice_to_playback_ms !== null && event.voice_to_playback_ms !== undefined)
        $('latency').textContent=`接话 ${(event.voice_to_playback_ms/1000).toFixed(2)}s`;
      break;
    case 'error': showError(event.message); break;
  }
}
async function cleanup(label='对话已结束，可以重新开始') {
  connected = false;
  clearInterval(timer);
  mic?.close(); mic = null;
  player?.close(); player = null;
  if (context && context.state !== 'closed') await context.close();
  context = null;
  if (socket && socket.readyState < WebSocket.CLOSING) { socket.onclose = null; socket.close(); }
  $('startControls').classList.remove('hidden');
  $('callControls').classList.add('hidden'); $('textForm').classList.add('hidden');
  $('editTask').disabled = $('prediction').disabled = false;
  $('connection').textContent='未连接'; $('connection').classList.remove('online');
  $('activity').classList.remove('live','talking');
  $('partial').textContent=''; $('phase').textContent=label;
}
$('startVoice').onclick=()=>start(true);
$('startText').onclick=()=>start(false);
$('interrupt').onclick=()=>interrupt();
$('stop').onclick=()=>{send({type:'stop'});cleanup();};
$('mute').onclick=()=>{
  if (!mic) return;
  mic.muted=!mic.muted;
  $('mute').textContent=mic.muted?'恢复麦克风':'暂停麦克风';
  $('mute').setAttribute('aria-pressed',String(mic.muted));
};
$('textForm').onsubmit=event=>{
  event.preventDefault();
  const text=$('textInput').value.trim();
  if (!text || !connected) return;
  interrupt('text'); send({type:'text',text}); $('textInput').value='';
};
$('editTask').onclick=()=>{
  $('editTitle').value=task.title; $('editGoal').value=task.goal; $('editFacts').value=task.facts;
  $('editPoints').value=task.checkpoints.map(p=>p.description).join('\n'); $('editOpening').value=task.opening;
  $('taskDialog').showModal();
};
$('closeDialog').onclick=()=>$('taskDialog').close();
$('taskForm').onsubmit=event=>{
  event.preventDefault();
  const descriptions=$('editPoints').value.split('\n').map(s=>s.trim()).filter(Boolean);
  if (!descriptions.length || descriptions.length>8 || descriptions.some(s=>s.length>300)) {
    $('editPoints').setCustomValidity('请输入1至8个要点，每项不超过300字。'); $('editPoints').reportValidity(); return;
  }
  task={title:$('editTitle').value.trim(),goal:$('editGoal').value.trim(),facts:$('editFacts').value.trim(),
    opening:$('editOpening').value.trim(),checkpoints:descriptions.map((description,i)=>({id:`point_${i+1}`,description}))};
  localStorage.setItem('g2-task',JSON.stringify(task)); renderTask(); $('taskDialog').close();
};
$('editPoints').oninput=()=>$('editPoints').setCustomValidity('');
$('export').onclick=()=>{
  const blob=new Blob([JSON.stringify({task,conversation:records},null,2)],{type:'application/json'});
  const url=URL.createObjectURL(blob), a=document.createElement('a'); a.href=url; a.download='ai-call-g2-conversation.json'; a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
};
window.addEventListener('beforeunload',()=>{mic?.close();player?.close();socket?.close();});
try {
  const config=await (await fetch('/api/config')).json();
  try { task=JSON.parse(localStorage.getItem('g2-task')); } catch {}
  if (!task?.checkpoints?.length || !task.goal || !task.opening) task=config.default_task;
  $('prediction').checked=config.predictions; renderTask();
  $('connection').textContent='服务就绪'; $('connection').classList.add('online');
  if (!config.configured) showError('服务尚未配置 API 凭据，请在本地 .env 中填写后重启。');
} catch { showError('无法载入任务配置，请确认本地服务已启动。'); }
