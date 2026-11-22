import {prepareTask, checkPreparation} from './preparation.js';
import {CallConnection} from './connection.js';

const $ = id => document.getElementById(id);
let task, call, voice = false, connected = false, epoch = 0;
let startedAt = 0, timer, turns = 0, hits = 0, records = [], uiUnits = new Map(), inputInFlight = false;
let waveformCanvas, waveformCtx, animationFrame;
let pipelineNodes = [];

// ==================== PIPELINE GRAPH ====================
function renderPipeline() {
  const svg = $('pipelineGraph');
  svg.innerHTML = '';
  
  if (!task || !task.checkpoints) return;
  
  const points = task.checkpoints;
  const startY = 80;
  const stepY = 70;
  const leftPadding = 40;
  
  pipelineNodes = points.map((point, i) => ({
    id: point.id,
    description: point.description,
    x: leftPadding,
    y: startY + i * stepY,
    status: 'inactive'
  }));
  
  // Draw connections with curves
  for (let i = 0; i < pipelineNodes.length - 1; i++) {
    const from = pipelineNodes[i];
    const to = pipelineNodes[i + 1];
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    const d = `M ${from.x} ${from.y + 24} 
               C ${from.x} ${from.y + 40}, 
                 ${to.x} ${to.y - 40}, 
                 ${to.x} ${to.y - 24}`;
    path.setAttribute('d', d);
    path.setAttribute('class', 'pipeline-connection');
    path.dataset.fromId = from.id;
    path.dataset.toId = to.id;
    svg.appendChild(path);
  }
  
  // Draw nodes with collapsible text
  pipelineNodes.forEach((node, i) => {
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.setAttribute('class', 'pipeline-node inactive');
    g.dataset.nodeId = node.id;
    
    // Node circle
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', node.x);
    circle.setAttribute('cy', node.y);
    circle.setAttribute('r', 20);
    g.appendChild(circle);
    
    // Node number
    const number = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    number.setAttribute('x', node.x);
    number.setAttribute('y', node.y + 6);
    number.setAttribute('text-anchor', 'middle');
    number.setAttribute('class', 'node-number');
    number.textContent = i + 1;
    g.appendChild(number);
    
    // Simplified label (always visible)
    const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    label.setAttribute('x', node.x + 35);
    label.setAttribute('y', node.y + 6);
    label.setAttribute('class', 'node-label');
    label.textContent = `Step ${i + 1}`;
    g.appendChild(label);
    
    // Detail text (only visible on hover) - create foreignObject for better text wrapping
    const fo = document.createElementNS('http://www.w3.org/2000/svg', 'foreignObject');
    fo.setAttribute('x', node.x + 35);
    fo.setAttribute('y', node.y + 15);
    fo.setAttribute('width', '240');
    fo.setAttribute('height', '80');
    fo.setAttribute('class', 'node-detail');
    
    const div = document.createElement('div');
    div.className = 'node-detail-text';
    div.textContent = node.description;
    fo.appendChild(div);
    g.appendChild(fo);
    
    svg.appendChild(g);
  });
  
  // Update SVG viewBox to fit content
  const height = Math.max(600, startY + pipelineNodes.length * stepY + 80);
  svg.setAttribute('viewBox', `0 0 300 ${height}`);
}

function updatePipelineNode(nodeId, status) {
  const node = document.querySelector(`[data-node-id="${nodeId}"]`);
  if (!node) return;
  
  node.classList.remove('inactive', 'active', 'completed');
  node.classList.add(status);
  
  // Update connection line to this node
  if (status === 'completed') {
    const prevLine = document.querySelector(`[data-to-id="${nodeId}"]`);
    if (prevLine) prevLine.classList.add('completed');
    
    // Add data card
    const nodeData = pipelineNodes.find(n => n.id === nodeId);
    if (nodeData) {
      addDataCard(nodeData.description, '已完成提取');
    }
  }
}

// ==================== WAVEFORM VISUALIZATION ====================
let audioBuffer = new Array(128).fill(0);
let barHeights = new Array(64).fill(0);
let analyser = null;
let analyserData = null;

function initWaveform() {
  waveformCanvas = $('waveformCanvas');
  if (!waveformCanvas) return;
  
  waveformCtx = waveformCanvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  
  const rect = waveformCanvas.getBoundingClientRect();
  waveformCanvas.width = rect.width * dpr;
  waveformCanvas.height = rect.height * dpr;
  waveformCtx.scale(dpr, dpr);
  
  // Setup audio analyser for real-time visualization
  const {context, mic, player} = call ?? {};
  if (context && !analyser) {
    analyser = context.createAnalyser();
    analyser.fftSize = 128;
    analyser.smoothingTimeConstant = 0.8;
    analyserData = new Uint8Array(analyser.frequencyBinCount);
    
    // Connect microphone and player to analyser
    if (mic?.source) {
      mic.source.connect(analyser);
    }
    if (player?.output) {
      player.output.connect(analyser);
    }
  }
  
  drawWaveform();
}

function drawWaveform() {
  if (!waveformCanvas || !waveformCtx) return;
  
  const width = waveformCanvas.width / (window.devicePixelRatio || 1);
  const height = waveformCanvas.height / (window.devicePixelRatio || 1);
  
  // Background with gradient
  const bgGradient = waveformCtx.createLinearGradient(0, 0, 0, height);
  bgGradient.addColorStop(0, '#F8FAFC');
  bgGradient.addColorStop(1, '#E9EEF6');
  waveformCtx.fillStyle = bgGradient;
  waveformCtx.fillRect(0, 0, width, height);
  
  if (!connected) {
    // Draw idle state with animated circles
    const time = Date.now() / 1000;
    waveformCtx.fillStyle = '#1E40AF';
    waveformCtx.globalAlpha = 0.1;
    for (let i = 0; i < 3; i++) {
      const radius = 40 + Math.sin(time * 2 + i) * 20;
      waveformCtx.beginPath();
      waveformCtx.arc(width / 2, height / 2, radius + i * 30, 0, Math.PI * 2);
      waveformCtx.fill();
    }
    waveformCtx.globalAlpha = 1;
    
    waveformCtx.fillStyle = '#475569';
    waveformCtx.font = '16px Inter';
    waveformCtx.textAlign = 'center';
    waveformCtx.fillText('Ready for audio input', width / 2, height / 2);
    
    animationFrame = requestAnimationFrame(drawWaveform);
    return;
  }
  
  // Spectrum analyzer style visualization
  const barCount = 64;
  const barWidth = width / barCount;
  
  // Get real audio frequency data
  if (analyser && analyserData) {
    analyser.getByteFrequencyData(analyserData);
    
    // Map frequency bins to bars (downsample from 64 bins to 64 bars)
    for (let i = 0; i < barCount; i++) {
      const dataIndex = Math.floor(i * analyserData.length / barCount);
      const targetHeight = (analyserData[dataIndex] / 255) * height * 0.8;
      barHeights[i] += (targetHeight - barHeights[i]) * 0.3; // Smooth transition
    }
  } else {
    // Fallback: minimal idle animation when no audio data
    for (let i = 0; i < barCount; i++) {
      const targetHeight = Math.random() * height * 0.1;
      barHeights[i] += (targetHeight - barHeights[i]) * 0.1;
    }
  }
  
  // Draw bars with gradient
  for (let i = 0; i < barCount; i++) {
    const x = i * barWidth;
    const barH = barHeights[i];
    const y = height - barH;
    
    // Create gradient for each bar
    const gradient = waveformCtx.createLinearGradient(0, y, 0, height);
    const hue = (i / barCount) * 60 + 200; // Blue to cyan
    gradient.addColorStop(0, `hsl(${hue}, 70%, 50%)`);
    gradient.addColorStop(1, `hsl(${hue}, 70%, 70%)`);
    
    waveformCtx.fillStyle = gradient;
    waveformCtx.fillRect(x + 1, y, barWidth - 2, barH);
    
    // Add glow effect
    waveformCtx.shadowColor = `hsl(${hue}, 70%, 50%)`;
    waveformCtx.shadowBlur = 10;
    waveformCtx.fillRect(x + 1, y, barWidth - 2, barH);
    waveformCtx.shadowBlur = 0;
  }
  
  // Add reflection effect
  waveformCtx.globalAlpha = 0.2;
  waveformCtx.save();
  waveformCtx.scale(1, -1);
  waveformCtx.translate(0, -height);
  for (let i = 0; i < barCount; i++) {
    const x = i * barWidth;
    const barH = barHeights[i] * 0.5;
    const y = height - barH;
    const hue = (i / barCount) * 60 + 200;
    const gradient = waveformCtx.createLinearGradient(0, y, 0, height);
    gradient.addColorStop(0, `hsl(${hue}, 70%, 50%)`);
    gradient.addColorStop(1, `hsl(${hue}, 70%, 70%)`);
    waveformCtx.fillStyle = gradient;
    waveformCtx.fillRect(x + 1, y, barWidth - 2, barH);
  }
  waveformCtx.restore();
  waveformCtx.globalAlpha = 1;
  
  animationFrame = requestAnimationFrame(drawWaveform);
}

// ==================== DATA CARDS ====================
let cardCounter = 0;
let recentCardKeys = new Set();

function addDataCard(title, content, options = {}) {
  const grid = $('captureGrid');
  if (!grid) return;
  
  // Prevent duplicate cards within a 2-second window
  const cardKey = `${title}:${content}`;
  if (recentCardKeys.has(cardKey) && !options.allowDuplicate) {
    return;
  }
  recentCardKeys.add(cardKey);
  setTimeout(() => recentCardKeys.delete(cardKey), 2000);
  
  cardCounter++;
  
  const card = document.createElement('div');
  card.className = 'data-card';
  card.style.animationDelay = `${cardCounter * 0.05}s`;
  
  // Card icon and color based on type
  const icon = document.createElement('div');
  icon.className = 'card-icon';
  let iconName = 'document';
  let accentColor = '#1E40AF';
  
  if (title.includes('User') || title.includes('用户') || title.includes('说')) {
    iconName = 'person';
    accentColor = '#D97706';
  } else if (title.includes('AI') || title.includes('Response') || title.includes('回复')) {
    iconName = 'reply';
    accentColor = '#0F766E';
  } else if (title.includes('完成') || title.includes('Complete')) {
    iconName = 'check';
    accentColor = '#16A34A';
  } else if (title.includes('提取') || title.includes('Extract')) {
    iconName = 'search';
    accentColor = '#9333EA';
  } else if (title.includes('Error') || title.includes('错误')) {
    iconName = 'warning';
    accentColor = '#DC2626';
  }
  
  icon.innerHTML = `<svg class="ui-icon" width="16" height="16" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><use href="/static/icons.svg#${iconName}"/></svg>`;
  card.style.setProperty('--card-accent', accentColor);
  
  const textContainer = document.createElement('div');
  textContainer.className = 'card-text';
  
  const header = document.createElement('div');
  header.className = 'data-card-header';
  header.textContent = title;
  
  const body = document.createElement('div');
  body.className = 'data-card-content';
  body.textContent = content;
  
  // Add timestamp
  const time = document.createElement('div');
  time.className = 'card-time';
  const now = new Date();
  time.textContent = `${now.getHours().toString().padStart(2, '0')}:${now.getMinutes().toString().padStart(2, '0')}:${now.getSeconds().toString().padStart(2, '0')}`;
  
  textContainer.appendChild(header);
  textContainer.appendChild(body);
  
  card.appendChild(icon);
  card.appendChild(textContainer);
  card.appendChild(time);
  
  grid.appendChild(card);
  
  // Auto scroll with smooth animation
  setTimeout(() => {
    grid.scrollTo({
      top: grid.scrollHeight,
      behavior: 'smooth'
    });
  }, 100);
  
  // Limit cards to 50
  const cards = grid.querySelectorAll('.data-card');
  if (cards.length > 50) {
    cards[0].style.animation = 'slide-out 0.3s ease forwards';
    setTimeout(() => cards[0].remove(), 300);
  }
}

// ==================== METRICS UPDATE ====================
let metricsState = {
  latency: 0,
  sentiment: 50,
  wpm: 0,
  barge: 0
};

function updateMetrics(data) {
  // Update latency - support both field names
  const latency = data.latency_ms || data.latency;
  if (latency !== undefined && latency !== null) {
    metricsState.latency = Math.round(latency);
    $('latencyValue').textContent = metricsState.latency;
  }
  
  // Simulate sentiment (0-100, text sentiment analysis)
  if (data.text) {
    const positiveWords = ['好', '优秀', '谢谢', '感谢', '完成', '成功', '是的', '可以', '同意'];
    const negativeWords = ['不', '没有', '失败', '错误', '问题', '无法'];
    let score = 50;
    positiveWords.forEach(w => { if (data.text.includes(w)) score += 8; });
    negativeWords.forEach(w => { if (data.text.includes(w)) score -= 8; });
    metricsState.sentiment = Math.max(0, Math.min(100, score));
    $('sentimentValue').textContent = metricsState.sentiment;
  }
  
  // Simulate WPM (words per minute based on text length and time)
  if (data.text && data.duration_ms) {
    const charCount = data.text.length;
    const minutes = data.duration_ms / 60000;
    metricsState.wpm = Math.round((charCount / 2) / minutes); // Rough: 2 chars per word
    $('wpmValue').textContent = metricsState.wpm;
  } else if (data.text) {
    // Fallback: estimate based on typical speaking rate
    const charCount = data.text.length;
    const estimatedWords = charCount / 2;
    const estimatedMinutes = estimatedWords / 150; // 150 WPM average
    metricsState.wpm = 150;
    $('wpmValue').textContent = metricsState.wpm;
  }
  
  // Simulate barge count (user interruptions)
  if (data.barge) {
    metricsState.barge += 1;
    $('bargeValue').textContent = metricsState.barge;
  }
}

// ==================== WEBSOCKET HANDLERS ====================
function send(event) { 
  call?.send(event);
}

function showError(text) { 
  $('error').textContent = text; 
  $('error').classList.remove('hidden'); 
  setTimeout(() => $('error').classList.add('hidden'), 5000);
}

function clearError() { 
  $('error').classList.add('hidden'); 
}

function onMessage({data}) {
  if (data instanceof ArrayBuffer) { 
    call?.player?.packet(data);
    return; 
  }
  
  let event;
  try { event = JSON.parse(data); } catch { return; }
  
  switch (event.type) {
    case 'reply_start':
      epoch = event.epoch;
      call?.player?.begin(epoch);
      clearError();
      break;
      
    case 'utterance':
      if (event.epoch !== epoch || call?.player?.blocked.has(event.epoch)) break;
      call?.player?.unit(event);
      if (event.audio_only) break;
      addDataCard('AI Response', event.text);
      records.push({role: 'assistant', text: event.text});
      if (event.text) {
        uiUnits.set(`${event.epoch}:${event.unit}`, {text: event.text});
        updateMetrics({ text: event.text, duration_ms: event.duration_ms });
      }
      break;
      
    case 'utterance_end':
      call?.player?.finish(event);
      break;
      
    case 'cancel':
      call?.player?.cancel(event.epoch);
      break;
      
    case 'transcript':
      if (event.final) {
        addDataCard('User Said', event.text);
        records.push({role: 'user', text: event.text});
        updateMetrics({ text: event.text, duration_ms: event.duration_ms });
      }
      break;
      
    case 'state':
      const phaseText = {
        ready: 'Ready',
        thinking: 'Thinking...',
        speaking: 'Speaking',
        listening: 'Listening'
      };
      $('phaseIndicator').textContent = phaseText[event.phase] || 'Active';
      
      // Update pipeline nodes
      if (event.delivered) {
        event.delivered.forEach(nodeId => {
          updatePipelineNode(nodeId, 'completed');
        });
      }
      break;
      
    case 'reply_done':
      turns++;
      if (event.reused) hits++;
      break;
      
    case 'latency':
      if (event.voice_to_playback_ms !== null && event.voice_to_playback_ms !== undefined) {
        updateMetrics({ latency: Math.round(event.voice_to_playback_ms) });
      }
      break;
      
    case 'error':
      showError(event.message);
      break;
  }
}

// ==================== CONNECTION LIFECYCLE ====================
async function start(useVoice) {
  if (connected || inputInFlight) return;
  inputInFlight = true;
  clearError();
  
  $('startVoice').disabled = $('startText').disabled = true;
  
  const active = call = new CallConnection({
    onMessage, onError: showError, onClose: () => cleanup('Connection closed'),
    onDone(epoch, unit) {
      const item = uiUnits.get(epoch + ':' + unit);
      if (item) item.delivered = true;
    }
  });
  try {
    voice = useVoice;
    
    await active.resumeAudio(voice);
    if (call !== active) return;
    $('connection').textContent = '正在准备工单';
    checkPreparation(await prepareTask(task, {retry: true}));
    if (call !== active) return;
    await active.open();
    if (call !== active) return;
    records = [];
    uiUnits.clear();
    turns = hits = 0;
    epoch = 0;
    
    connected = true;
    startedAt = performance.now();

    $('startControls').classList.add('hidden');
    $('callControls').classList.remove('hidden');
    $('textForm').classList.remove('hidden');
    
    $('connection').textContent = 'CONNECTED';
    $('connection').classList.add('online');
    
    // Initialize waveform animation
    initWaveform();
    
    send({type: 'start', task, voice, audio_protocol: 2, predictions: true});
    
    if (!voice) $('textInput').focus();
    
  } catch (error) {
    if (call !== active) return;
    await cleanup();
    if (call) return;
    showError(error.name === 'NotAllowedError' ? 
      'Microphone permission denied' : error.message);
  } finally {
    if (!call || call === active) {
      inputInFlight = false;
      $('startVoice').disabled = $('startText').disabled = false;
    }
  }
}

async function cleanup(message) {
  connected = false;
  const active = call;
  call = null;
  const closing = active?.close();
  analyser?.disconnect();
  analyser = analyserData = null;
  if (animationFrame) {
    cancelAnimationFrame(animationFrame);
    animationFrame = null;
  }
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  
  $('startControls').classList.remove('hidden');
  $('callControls').classList.add('hidden');
  $('textForm').classList.add('hidden');
  $('connection').textContent = 'READY';
  $('connection').classList.remove('online');
  $('phaseIndicator').textContent = message || 'Ready';
  
  // Clear metrics
  $('latencyValue').textContent = '--';
  $('sentimentValue').textContent = '--';
  $('wpmValue').textContent = '--';
  $('bargeValue').textContent = '--';
  await closing;
}

function interrupt() {
  if (!connected || !epoch) return;
  const progress = call?.player?.stop(true) || [];
  send({type: 'interrupt', epoch, progress, source: 'manual'});
  $('phaseIndicator').textContent = voice ? 'Listening' : 'Interrupted';
}

// ==================== TEXT INPUT ====================
function sendText(text) {
  if (!text.trim() || !connected) return;
  send({type: 'text', text: text.trim()});
  addDataCard('User Said', text.trim());
  $('textInput').value = '';
}

// ==================== TASK DIALOG ====================
function openTaskDialog() {
  const dialog = $('taskDialog');
  if (!dialog) return;
  
  $('editTitle').value = task?.title || '';
  $('editGoal').value = task?.goal || '';
  $('editFacts').value = task?.facts || '';
  $('editPoints').value = task?.checkpoints?.map(p => p.description).join('\n') || '';
  $('editOpening').value = task?.opening || '';
  
  dialog.showModal();
}

function closeTaskDialog() {
  $('taskDialog')?.close();
}

function saveTask(formData) {
  if (connected || inputInFlight) return;
  const points = formData.get('points').split('\n').filter(l => l.trim());
  
  task = {
    title: formData.get('title'),
    goal: formData.get('goal'),
    facts: formData.get('facts'),
    checkpoints: points.map((desc, i) => ({
      id: `checkpoint_${i}`,
      description: desc.trim()
    })),
    opening: formData.get('opening')
  };
  
  renderPipeline();
  void prepareTask(task, {wait: false});
  closeTaskDialog();
}

// ==================== INITIALIZATION ====================
async function init() {
  try {
    const response = await fetch('/api/config');
    const config = await response.json();
    
    if (config.default_task) {
      task = config.default_task;
      if (config.configured) void prepareTask(task, {wait: false});
      renderPipeline();
    }
    
  } catch (error) {
    console.error('Failed to load config:', error);
  }
  
  // Event listeners
  $('startVoice')?.addEventListener('click', () => start(true));
  $('startText')?.addEventListener('click', () => start(false));
  $('interrupt')?.addEventListener('click', interrupt);
  $('stop')?.addEventListener('click', () => cleanup('Call ended'));
  
  $('textForm')?.addEventListener('submit', (e) => {
    e.preventDefault();
    sendText($('textInput').value);
  });
  
  $('taskForm')?.addEventListener('submit', (e) => {
    e.preventDefault();
    saveTask(new FormData(e.target));
  });
  
  $('closeDialog')?.addEventListener('click', closeTaskDialog);
  
  // Initialize waveform canvas
  if ($('waveformCanvas')) {
    initWaveform();
  }
  
  // Handle window resize
  window.addEventListener('resize', () => {
    if (waveformCanvas) {
      initWaveform();
    }
  });
}

// Start on load
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
