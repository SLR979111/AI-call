// @ts-check
/**
 * stream-app.js —— 新前端逻辑的装配层
 *
 * 与旧的 main.js 的区别（这是重构的核心）：
 *
 *   旧：WS 上跑 session/turn 状态机 + tts_started/tts_sentence/audio_chunk 四条消息族，
 *       打断要发 local_muted 等服务端裁决（cancel / turn_aborted）。
 *
 *   新：只有两个接口 —— push() 上行、onAudio() 下行。
 *       打断 = 用户开口 → 本地立刻停播 + 继续推音频；新的 epoch 一到，旧音频自然作废。
 *       字幕、阶段、套话/正式回答的区分，全部来自音频块自带的元数据。
 *
 * 复用（一行没改）：audio-engine（采集 + AEC/NS/AGC）、vad-local（b0 与语音段）、
 *                  uplink-file（无麦回放）、store、ui。
 */
import { now } from './protocol.js';
import * as store from './store.js';
import { AudioEngine } from './audio-engine.js';
import { LocalVad } from './vad-local.js';
import { FileUplink } from './uplink-file.js';
import { DecisionClient } from './decision.js';
import { StreamPlayer } from './player.js';
import { initUi } from './ui.js';

const cfg = /** @type {any} */ (window.__AICALL_CONFIG__);
const decision = new DecisionClient(cfg);
const engine = new AudioEngine(cfg);
const vad = new LocalVad(cfg);
const fileUplink = new FileUplink(engine, cfg);

let ui;
let muted = false;
let lastSpeechEnd = 0;
let audioSinceEnd = false;
let startedAt = 0;

const player = new StreamPlayer(engine, cfg, {
  onEpochSwitch: ({ epoch }) => {
    store.log(`epoch → ${epoch}（旧的音频与字幕全部作废）`);
    paintMedia();
  },
  onSegmentStart: (seg) => {
    store.log(`段开始 ${seg.utt} · ${seg.kind}${seg.reply_id ? ' · ' + seg.reply_id : ''}`);
    // 字幕来自音频段自带的元数据，不再有独立的 agent_text 通道
    for (const s of seg.sentences || []) {
      if (s && s.text) store.pushAgentSentence(seg.reply_id || seg.utt, s.sentence_id ?? null, s.text, seg.kind);
    }
    paintMedia();
  },
  onSegmentEnd: (seg) => {
    store.log(`段结束 ${seg.utt} · 播放 ${seg.played_ms}ms · ${seg.reason}`);
    paintMedia();
  },
  onFirstFrame: (f) => {
    // 首帧：以"音频真正开始出声"为准，套话单独记口径
    const ms = startedAt ? Math.round(now() - startedAt) : null;
    store.set({ ttfb: ms });
    store.log(`首帧 utt=${f.utt} kind=${f.kind} formal=${f.formal} ${ms != null ? ms + 'ms' : ''}`);
    decision.push({ signal: { name: 'first_frame', utt: f.utt, kind: f.kind, formal: f.formal, ttfb_ms: ms } });
  },
  onUnderrun: (u) => store.log(`下行断流 ${u.gap_ms}ms ${u.utt || ''}`)
});

/* ============================ 接口 ②：音频 hook ============================ */

decision.onAudio((ev) => player.push(ev));

decision.addEventListener('open', () => store.log('decision 流已连接'));
decision.addEventListener('close', (ev) => store.log(`decision 流断开 code=${ev.detail.code}`));
decision.addEventListener('server-error', (ev) => {
  const e = ev.detail || {};
  store.log(`decision 错误 ${e.code || '?'}`);
  ui.status(e.message || e.code || '决策服务返回错误', 'err');
});
decision.addEventListener('control', (ev) => applyControl(ev.detail.name, ev.detail.payload));

/* ============================ 采集 → 接口 ① ============================ */

engine.onMeter = (db) => store.set({ db });
engine.onFrame = (f) => {
  const playing = player.isPlaying();
  const r = vad.push({
    energyDb: muted ? -99 : f.energyDb,
    monoMs: f.monoMs,
    playing,
    refDb: playing ? -20 : -99
  });
  store.set({ db: r.db });

  // 打断：用户一开口就本地硬停 + 把"听到哪儿"一起推上去。
  // 这是纯本地行为 —— 不等后端、不需要 cancel，新的 epoch 来了自然顶掉旧的。
  if (playing && r.start != null) {
    const heard = player.interruptCursor();
    const b0 = r.start;
    store.set({ b0: Math.round(b0), b3: Math.round(b0), stopLatency: 0 });
    decision.push({
      signal: { name: 'barge_in', b0_ms: Math.round(b0), reason: 'local_vad' },
      heard
    });
    store.log(`打断 b0=${Math.round(b0)}ms heard=${heard ? Math.round(heard.played_ms) + 'ms' : '—'}`);
  }

  // 上行：每个采集帧都推（这就是"用户输入"，文本由服务端 ASR 产出）
  decision.push({ audio: { pcm: f.pcm, monoMs: f.monoMs, muted } });
  audioSinceEnd = true;

  if (r.start != null && !playing) {
    store.set({ b0: Math.round(r.start) });
    decision.push({ signal: { name: 'speech_started', b0_ms: Math.round(r.start) } });
  }
  if (r.end != null) {
    audioSinceEnd = false;
    lastSpeechEnd = r.end;
    decision.push({ signal: { name: 'speech_end', last_voiced_ms: Math.round(r.end) } });
    store.set({ db: r.db });
  }
};

/* ============================ 控制面（业务状态） ============================ */

/** @param {string} name @param {any} p */
function applyControl(name, p) {
  const payload = p || {};
  switch (name) {
    case 'state':
      store.set({
        turnId: payload.turn_id || store.get().turnId,
        turnState: payload.turn_state || store.get().turnState,
        flowState: payload.flow_state || store.get().flowState,
        slots: payload.slots || store.get().slots
      });
      break;
    case 'transcript':
      store.pushUserLine(payload.text || '', payload.final !== false);
      break;
    case 'compliance': {
      const list = store.get().compliance.slice();
      list.push(payload);
      store.set({ compliance: list });
      break;
    }
    case 'tool_progress': {
      const tools = { ...store.get().tools };
      tools[payload.call_id || payload.tool || 'tool'] = payload;
      store.set({ tools });
      break;
    }
    case 'turn_result':
      store.set({ result: normalizeResult(payload) });
      break;
    case 'session_ended':
      endCall('服务端结束会话');
      break;
    default:
      store.log(`控制面 ${name}`);
  }
}

/** @param {any} d */
function normalizeResult(d) {
  const r = d.result || d;
  const inner = r.result || {};
  return {
    outcome: r.outcome || d.outcome || '',
    ticket: inner.ticket || inner.ticket_id || '',
    newTime: inner.new_time || '',
    recordId: inner.writeback?.record_id || ''
  };
}

/* ============================ UI ============================ */

ui = initUi({
  initialSource: !cfg.ENABLE_MIC || cfg.UPLINK_MODE === 'file' ? 'file' : 'mic',
  onStart: (source) => startCall(source).catch((e) => {
    engine.stopCapture();
    ui.setConnected(false);
    console.error(e);
    ui.status('启动失败：' + e.message, 'err');
  }),
  onHangup: () => hangup(),
  onMute: () => toggleMute(),
  onBarge: () => manualInterrupt(),
  onDemo: () => startDemoUplink().catch((e) => ui.status('回放失败：' + e.message, 'err')),
  onAudioFile: (file) => startDemoUplink(file).catch((e) => ui.status('回放失败：' + e.message, 'err')),
  onText: (text) => {
    if (!decision.push({ text, final: true })) { ui.status('连接未就绪，无法发送', 'err'); return; }
    store.log(`→ 文本输入：${text}`);
  }
});

async function startCall(source) {
  store.resetForNewCall();
  startedAt = now();
  ui.setStarting();
  ui.status(source === 'mic' ? '请求麦克风权限…' : '准备录音回放…');
  try { await engine.start(source === 'mic'); } catch (e) { console.warn('[stream] 音频引擎启动失败，继续无麦', e); }

  ui.status('建立会话…');
  const session = await createSession();
  store.set({ sessionId: session.session_id, releaseId: session.version_set?.release_id || '' });

  decision.connect({ sessionId: session.session_id, wsUrl: streamWsUrl(session) });
  ui.setConnected(true, !engine.micAvailable);
  store.set({ phase: 'connected', transport: 'stream', transportNote: 'Decision 流' });
  ui.status('已连接（Decision 流）', 'ok');
}

async function createSession() {
  const res = await fetch(`${cfg.APP_BASE}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: 'voice', scenario_id: cfg.SCENARIO_ID })
  });
  if (!res.ok) throw new Error(`POST /sessions ${res.status}`);
  return res.json();
}

/** @param {any} session */
function streamWsUrl(session) {
  const base = String(cfg.APP_BASE || location.origin).replace(/^http/, 'ws');
  const path = cfg.STREAM?.WS_PATH || '/ws/decision';
  const q = new URLSearchParams({ session_id: session.session_id });
  if (session.token) q.set('token', session.token);
  return `${base}${path}?${q.toString()}`;
}

async function startDemoUplink(source) {
  if (fileUplink.running) throw new Error('当前录音还在回放');
  engine.stopCapture();
  const info = await fileUplink.load(source || cfg.DEMO_WAV);
  store.log(`回放音源 ${fileUplink.sourceLabel} ${info.durationMs}ms`);
  await fileUplink.start({
    onFrame: (f) => engine.onFrame && engine.onFrame(f),
    onEnd: () => {
      store.log('回放结束');
      decision.push({ signal: { name: 'speech_end', last_voiced_ms: Math.round(lastSpeechEnd || 0) } });
    }
  });
  ui.status(`回放中：${fileUplink.sourceLabel}`, 'ok');
}

function toggleMute() {
  muted = !muted;
  engine.setMuted(muted);
  const b = /** @type {HTMLButtonElement} */ (document.getElementById('btnMute'));
  if (b) b.textContent = muted ? '取消静音' : '静音';
}

/** 手动"模拟打断"：和 VAD 打断走完全相同的本地路径 */
function manualInterrupt() {
  const heard = player.interruptCursor();
  decision.push({
    signal: { name: 'barge_in', b0_ms: Math.round(now() - decision.t0), reason: 'manual' },
    heard
  });
  store.log(`手动打断 heard=${heard ? Math.round(heard.played_ms) + 'ms' : '—'}`);
  ui.status('已打断（本地静音）', 'ok');
}

function hangup() {
  decision.close();
  player.dispose();
  fileUplink.stop();
  engine.stopCapture();
  ui.setConnected(false);
  store.set({ phase: 'ended', transport: 'stream', transportNote: '已挂断' });
  ui.status('已挂断');
}

function endCall(reason) {
  decision.close();
  player.dispose();
  engine.stopCapture();
  ui.setConnected(false);
  ui.status(`通话结束（${reason}）`);
}

/** @param {any} s */
function paintMedia() {
  const st = player.stats();
  store.set({
    downlink: st.kind || '—',
    transport: 'stream',
    transportNote: `epoch ${st.epoch}${st.utt ? ' · ' + st.kind : ''}`
  });
}

paintMedia();
store.log(`前端（新逻辑）已就绪｜ APP_BASE=${cfg.APP_BASE}（${cfg.APP_BASE_SOURCE}）｜ 接口：push / onAudio`);
