// @ts-check
/**
 * main.js —— 组装层：把 session / engine / playback / vad / ui 接起来
 *
 * 唯一允许写全局时序的地方。规则：
 *  - 本地 VAD 只产生：电平、b0、打断预触发、speech_started（C2）
 *  - 通用端点由服务端裁决；赛题模式使用前端连续静音和最后有声帧
 *  - 打断：duck（≤80ms）→ local_muted → 300ms 无持续语音则本地恢复（C3）
 */
import { buildAudioFrame, now } from './protocol.js';
import * as store from './store.js';
import { SessionClient } from './session.js';
import { AudioEngine } from './audio-engine.js';
import { LocalVad } from './vad-local.js';
import { Playback } from './playback.js';
import { FileUplink } from './uplink-file.js';
import { initUi } from './ui.js';

const cfg = /** @type {any} */ (window.__AICALL_CONFIG__);
const client = new SessionClient(cfg);
const engine = new AudioEngine(cfg);
const playback = new Playback(engine, cfg, client);
const vad = new LocalVad(cfg);
const fileUplink = new FileUplink(engine, cfg);

// LiveKit 复用 audio-engine 已经采集的那条麦克风 track（AEC 在 getUserMedia 时就申请了）。
// 这样「VAD 判定的声音」和「发给服务端的声音」是同一份，不会出现双份 getUserMedia。
client.micTrackProvider = () => {
  const s = /** @type {any} */ (engine).stream;
  return s ? s.getAudioTracks()[0] || null : null;
};
// 远端音频挂载点（index.html 里的隐藏容器；缺失时 livekit.js 会自己建一个）
client.livekitSink = document.getElementById('lkAudioSink');

let ui;
let muted = false;
let inBarge = false;
let bargeTimer = 0;
let lastSpeechEnd = 0;
let timingTurn = null;
let inputSource = 'mic';
let audioSinceEnd = false;
let contestEndpointing = false;
/** @type {{pcm:Int16Array, frameSeq:number, monoMs:number, energyDb:number}[]} */
const playbackPreRoll = [];
/** @type {{pcm:Int16Array, frameSeq:number, monoMs:number, energyDb:number}[]} */
const listeningPreRoll = [];
let backendSince = 0;
let backendCount = 0;
let backendTimer = 0;

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
  onBarge: () => triggerBarge('manual'),
  onDemo: () => startDemoUplink().catch((e) => ui.status('回放失败：' + e.message, 'err')),
  onAudioFile: (file) => startDemoUplink(file).catch((e) => ui.status('回放失败：' + e.message, 'err')),
  onText: (text) => {
    if (!client.sendText(text)) { ui.status('连接未就绪，无法发送', 'err'); return; }
    store.log(`→ text: ${text}`);
  }
});

const contestConfig = fetch(`${cfg.APP_BASE}/contest/cases`).then(async (response) => {
  if (!response.ok) return;
  const catalog = await response.json();
  vad.endFrames = Math.ceil(catalog.silence_ms / cfg.FRAME_MS);
  vad.contestEndpointing = true;
  contestEndpointing = true;
  ui.setContestCases(catalog.cases, catalog.default_case_id);
}).catch(error => console.warn('读取赛题场景失败', error));

/* ============================ 通话生命周期 ============================ */

async function startCall(source) {
  inputSource = source;
  lastSpeechEnd = 0;
  timingTurn = null;
  audioSinceEnd = false;
  playbackPreRoll.length = 0;
  listeningPreRoll.length = 0;
  vad.reset();
  ui.setStarting();
  ui.status(source === 'mic' ? '请求麦克风权限…' : '准备录音回放…');
  try {
    await engine.start(source === 'mic');       // ★ 必须在点击手势内
  } catch (e) {
    console.warn('音频引擎启动失败（继续以无麦模式）', e);
  }

  ui.status('建立会话…');
  await contestConfig;
  await client.startCall(ui.selectedCase());
  showConnected();
  startBackendProbe();
}

function stopBackendProbe() {
  if (backendTimer) clearInterval(backendTimer);
  backendTimer = 0;
}

function startBackendProbe() {
  stopBackendProbe();
  backendSince = 0;
  backendCount = 0;
  const sid = store.get().sessionId;
  if (!sid) return;
  const byId = (id) => document.getElementById(id);
  if (byId('backendBase')) {
    const src = cfg.APP_BASE_SOURCE === 'query' ? 'query 覆盖' : 'same-origin';
    byId('backendBase').textContent = `${cfg.APP_BASE} · ${src}`;
  }
  if (byId('backendSession')) byId('backendSession').textContent = sid;
  if (byId('backendEvents')) byId('backendEvents').textContent = '等待服务端事件…';
  if (byId('backendEventCount')) byId('backendEventCount').textContent = '0';
  if (byId('backendLastEvent')) byId('backendLastEvent').textContent = '—';
  fetch(`${cfg.APP_BASE}/voice/status`).then((r) => r.json()).then((d) => {
    const el = byId('backendVoice');
    if (el) el.textContent = `${d.asr?.provider || '—'} / ${d.tts?.provider || '—'}`;
  }).catch(() => {});
  backendTimer = window.setInterval(pollBackend, 700);
  void pollBackend();
}

async function pollBackend() {
  const sid = store.get().sessionId;
  if (!sid) return;
  try {
    const res = await fetch(`${cfg.APP_BASE}/sessions/${sid}/events?since_seq=${backendSince}`);
    if (!res.ok) return;
    const text = await res.text();
    const lines = text.split('\n').filter(Boolean);
    const events = [];
    for (const line of lines) {
      try { events.push(JSON.parse(line)); } catch { /* ignore partial */ }
    }
    if (!events.length) return;
    const byId = (id) => document.getElementById(id);
    const box = byId('backendEvents');
    const old = box ? box.textContent.split('\n').filter(Boolean) : [];
    for (const e of events) {
      if (e.seq != null) backendSince = Math.max(backendSince, Number(e.seq));
      const p = e.payload || {};
      const detail = p.text || p.state || p.tool || p.decision || p.outcome || '';
      old.push(`${e.seq ?? '?'} ${e.type}${detail ? ' · ' + String(detail).slice(0, 90) : ''}`);
    }
    backendCount += events.length;
    if (box) {
      box.textContent = old.slice(-80).join('\n');
      box.scrollTop = box.scrollHeight;
    }
    if (byId('backendEventCount')) byId('backendEventCount').textContent = String(backendCount);
    const last = events[events.length - 1];
    if (byId('backendLastEvent')) byId('backendLastEvent').textContent = last?.type || '—';
    const s = store.get();
    if (byId('backendTurn')) byId('backendTurn').textContent = `${s.turnId || '—'} / ${s.turnState || '—'}`;
    if (byId('backendFlow')) byId('backendFlow').textContent = s.flowState || '—';
  } catch { /* transient */ }
}

function showConnected() {
  const useFile = inputSource === 'file' || !engine.micAvailable;
  ui.setConnected(true, useFile);
  if (inputSource === 'mic' && useFile) {
    const err = engine.lastError;
    const detail = err ? `${err.name || 'Error'}: ${err.message || err}` : '未获得麦克风轨道';
    ui.status(`麦克风不可用（${detail}）。请用独立 Chrome/Edge 打开并允许麦克风。`, 'err');
  } else if (useFile) {
    ui.status('已连接，请选择录音文件。', 'ok');
  } else {
    ui.status('麦克风已连接，可以说话。', 'ok');
  }
}

/**
 * 把 wav 灌进同一条采集管线（与麦克风同形），因此 VAD/打断/埋点全部照常工作。
 * @param {string|File|Blob} [source]
 */
async function startDemoUplink(source) {
  if (fileUplink.running) throw new Error('当前录音还在回放');
  engine.stopCapture();
  audioSinceEnd = false;
  playbackPreRoll.length = 0;
  listeningPreRoll.length = 0;
  vad.reset();
  const info = await fileUplink.load(source || cfg.DEMO_WAV);
  store.log(`回放音源 ${fileUplink.sourceLabel} ${info.durationMs}ms @${info.sampleRate}Hz`);
  await fileUplink.start({
    onFrame: handleFrame,
    onEnd: () => { store.log('回放结束'); reportSpeechEnd(vad.lastVoicedMono); }
  });
  ui.status(`回放中：${fileUplink.sourceLabel}（${info.durationMs}ms）`, 'ok');
}

function hangup() {
  stopBackendProbe();
  fileUplink.stop();
  playback.stopNow();
  engine.stopCapture();
  client.hangup('user_click');
  ui.setConnected(false);
  ui.status('已挂断');
}

function toggleMute() {
  muted = !muted;
  engine.setMuted(muted);
  const b = /** @type {HTMLButtonElement} */ (document.getElementById('btnMute'));
  b.textContent = muted ? '取消静音' : '静音';
}

/* ============================ 采集 → 上行 ============================ */

engine.onMeter = (db) => store.set({ db });
engine.onFrame = (f) => handleFrame(f);

/**
 * 采集帧统一入口：麦克风（MicUplink）与回放音源（FileUplink）共用同一条路，
 * 所以 VAD / 打断 / 埋点 / 上行帧格式在两种音源下完全一致（也是 E2E 的基础）。
 * @param {{pcm:Int16Array, frameSeq:number, monoMs:number, energyDb:number}} f
 */
function handleFrame(f) {
  if (cfg.TRANSPORT_PREF === 'rtc_pcm' && client.transport !== 'rtc_pcm') return;
  // LiveKit 模式下 agent 的声音来自房间音频，playback 不会播任何东西，
  // 所以双讲门控要用 livekit 的出声状态，否则 VAD 会一直以为「对方没说话」。
  const lkPlaying = client.livekitIsPlaying();
  const playing = (playback.isPlaying() && !playback.ducked) || lkPlaying;
  const r = vad.push({
    energyDb: muted ? -99 : f.energyDb,
    monoMs: f.monoMs,
    playing,
    // LiveKit 没有 PCM 缓冲可测参考电平，沿用 playback 在 speech 形态下的估算值
    refDb: lkPlaying ? -20 : playback.refEnergyDb()
  });
  store.set({ db: r.db });

  if (playing) {
    listeningPreRoll.length = 0;
    // 播报期等本地确认后才送语音，避免回声经 ASR 抢先打断。保留开口音频。
    playbackPreRoll.push(f);
    if (playbackPreRoll.length > vad.bargeFrames) playbackPreRoll.shift();
    if (r.barge != null && triggerBarge('local_vad', r.barge, r.db)) {
      for (const frame of playbackPreRoll) sendUplinkFrame(frame);
      playbackPreRoll.length = 0;
    }
  } else {
    playbackPreRoll.length = 0;
    if (!contestEndpointing || audioSinceEnd) {
      sendUplinkFrame(f);
    } else {
      // Retain the quiet onset while keeping continuous ambient silence out of ASR.
      listeningPreRoll.push(f);
      if (listeningPreRoll.length > 10) listeningPreRoll.shift();
      if (r.start != null) {
        for (const frame of listeningPreRoll) sendUplinkFrame(frame);
        listeningPreRoll.length = 0;
      }
    }
    if (r.start != null) {
      store.set({ b0: Math.round(r.start) });
      client.speechStarted(r.start, r.db);
    }
  }
  if (r.end != null) reportSpeechEnd(vad.lastVoicedMono);
}

/** @param {{pcm:Int16Array, monoMs:number}} f */
function sendUplinkFrame(f) {
  if (client.transport !== 'ws_pcm' && client.transport !== 'rtc_pcm') return;
  client.sendAudioFrame(buildAudioFrame({
    frameSeq: client.frameSeq++,
    clientMonoMs: f.monoMs,
    flags: muted ? 1 : 0,
    pcm: f.pcm
  }));
  audioSinceEnd = true;
}

function reportSpeechEnd(lastVoiced) {
  if (!audioSinceEnd) return;
  audioSinceEnd = false;
  if (lastVoiced > lastSpeechEnd) lastSpeechEnd = lastVoiced;
  client.speechEnded(lastVoiced);
}

/* ============================ 打断（C2 / C3） ============================ */

/** @param {'local_vad'|'manual'} source @param {number} [b0Hint] @param {number} [db] */
function triggerBarge(source, b0Hint, db = store.get().db) {
  if (inBarge) return false;
  const pcmPlaying = playback.isPlaying();
  const lkPlaying = client.livekitIsPlaying();
  if (!pcmPlaying && !lkPlaying) { store.log('当前未播放，忽略打断'); return false; }
  inBarge = true;

  const b0 = b0Hint != null ? b0Hint : (vad.b0Mono || now() - client.t0);
  const cur = pcmPlaying ? playback.cursor() : null;
  // 两种传输都只做「本地静音」：pcm 压增益、LiveKit 静音 <audio>，都不丢缓冲
  const b3 = pcmPlaying ? playback.duck() : client.duckLiveKit();
  const stopLatency = Math.max(0, b3 - b0);

  store.set({ b0: Math.round(b0), b3: Math.round(b3), stopLatency: Math.round(stopLatency) });
  client.localMuted({
    b0Mono: b0, b3Mono: b3,
    playedMs: playback.playedMs(),
    sentenceId: cur ? cur.sentence_id : undefined,
    charIdx: cur ? cur.char_idx : undefined,
    replyId: playback.replyId,
    turnId: playback.turnId,
    reason: source === 'manual' ? 'user_barge_button' : 'barge_in'
  });
  client.speechStarted(b0, db);
  store.log(`打断 source=${source} b0=${b0.toFixed(0)} b3=${b3.toFixed(0)} stop=${stopLatency.toFixed(1)}ms cursor=${JSON.stringify(cur)}`);

  // C3：**只有本地 VAD 判定的打断**才允许 300ms 后自恢复（"误打断"）。
  // 手动按钮没有 VAD 证据 → 一律当真实打断：保持静音，等服务端 cancel/turn_aborted 裁决，
  // 然后进入 LISTENING 等用户说话（绝不自行恢复播报）。
  clearTimeout(bargeTimer);
  if (source === 'local_vad') {
    bargeTimer = window.setTimeout(() => {
      if (playback.aborted) { inBarge = false; return; }      // 服务端已取消：不恢复
      if (!vad.isStillSpeaking()) {
        const resumedPcm = playback.unduck();
        // LiveKit 侧：duck 只是静音 <audio>，恢复等于解静音，音频流没断过
        const resumedLk = client.livekitStats().ducked ? client.unduckLiveKit() : false;
        const resumed = resumedPcm || resumedLk;
        store.log(resumed ? '误打断：从下一句继续（不重播半句）' : '误打断：无未播内容，保持停止');
      }
      inBarge = false;
    }, 300);
  } else {
    inBarge = false;
    ui.status('已打断（本地静音），等待服务端裁决…');
  }
  // 纯 UI 状态提示（不是业务话术）：让用户知道"该你说了"。
  // 真正的"打断后静默兜底话术"由服务端在 N 秒后以 agent_text + tts_* 下发（契约 §0 第 4 条）。
  store.set({ hint: '（已打断，正在等您继续说话…）' });
  return true;
}

/* ============================ 服务端 → 本地 ============================ */

client.addEventListener('ready', () => {
  if (cfg.TRANSPORT_PREF !== 'rtc_pcm') showConnected();
});

client.addEventListener('tts-started', (ev) => playback.begin(ev.detail));
client.addEventListener('tts-sentence', (ev) => playback.sentence(ev.detail));
client.addEventListener('tts-completed', (ev) => {
  playback.complete(ev.detail);
  if (ev.detail.interrupted) store.log('tts_completed interrupted=true');
});
client.addEventListener('pcm', (ev) => playback.pcm(ev.detail));

/* ============================ LiveKit 媒体面（入房 / 播放 / AEC） ============================ */

client.addEventListener('media-ready', (ev) => {
  const p = ev.detail || {};
  store.log(`media_ready provider=${p.provider || '—'} room=${p.room || '—'}`);
});

client.addEventListener('livekit-connecting', (ev) => {
  const room = ev.detail && ev.detail.room;
  store.set({ livekit: { ...store.get().livekit, state: 'connecting', room: room || '' } });
  ui.status(`正在加入 LiveKit 房间${room ? ' ' + room : ''}…`);
});

client.addEventListener('livekit-connected', (ev) => {
  const s = ev.detail || {};
  store.set({ livekit: s });
  const aec = s.aec || {};
  const parts = [
    aec.echoCancellation ? 'AEC 开' : 'AEC 关',
    s.publishedVia === 'shared-track' ? '共用采集轨' : 'SDK 采集',
    aec.sampleRate ? `${aec.sampleRate}Hz` : '',
  ].filter(Boolean);
  ui.status(`LiveKit 已入房（${parts.join(' · ')}）`, 'ok');
  // 自动播放策略：入房瞬间可能被挂起，等用户下一次点击再解锁（不自动重试，避免打脸）
  if (s.autoPlayBlocked) {
    ui.status('LiveKit 已入房，点一下页面任意位置启用声音', 'err');
    document.addEventListener('pointerdown', () => {
      client.resumeLiveKitAudio().then((ok) => {
        if (ok) { ui.status('声音已启用', 'ok'); store.log('livekit 音频已解锁'); }
      });
    }, { once: true });
  }
});

client.addEventListener('livekit-failed', (ev) => {
  const msg = (ev.detail && ev.detail.message) || '未知错误';
  store.set({ livekit: { ...store.get().livekit, state: 'idle', error: msg } });
  ui.status(`LiveKit 入房失败，已回落 ws_pcm：${msg}`, 'err');
});

client.addEventListener('livekit-audio-blocked', (ev) => {
  if (ev.detail && ev.detail.blocked) store.log('livekit 远端音频被自动播放策略挂起');
});

client.addEventListener('livekit-remote-audio', (ev) => {
  const d = ev.detail || {};
  store.log(`livekit 订阅到远端音频 ${d.identity || '—'}（共 ${d.attached} 条）`);
});

client.addEventListener('livekit-stats', (ev) => {
  store.set({ livekit: ev.detail });
});

client.addEventListener('cancel', (ev) => {
  // 服务端已裁决取消：硬停 + 永久封禁该 reply（此后同 reply 的 tts_*/pcm 全部丢弃）
  playback.abort('cancel');
  inBarge = false;
  clearTimeout(bargeTimer);
  store.log(`cancel reply=${ev.detail.reply_id || '—'} → 停止播报，等待用户说话`);
  ui.status('已打断，等待您继续说话', 'ok');
  store.set({ hint: '（已打断，正在等您继续说话…）' });
});

client.addEventListener('turn-aborted', (ev) => {
  // 服务端裁决打断：硬停本 reply，等新的 state 消息（不回退 flow_state）
  playback.abort(`turn_aborted:${ev.detail.reason}`);
  inBarge = false;
  clearTimeout(bargeTimer);
  store.log(`turn_aborted reason=${ev.detail.reason} → 进入 LISTENING，等待新一轮`);
  ui.status(`已打断（${ev.detail.reason}），请继续说`, 'ok');
});

client.addEventListener('transport-switched', showConnected);

client.addEventListener('server-error', (ev) => {
  const e = ev.detail || {};
  if (e.code === 'ASR_NO_MATCH') { ui.status(e.user_message || '没有听清，请再说一遍', 'err'); return; }
  if (e.code === 'SESSION_TIMEOUT') { ui.status('会话超时', 'err'); return; }
  if (e.code === 'TTS_TIMEOUT') { ui.status('语音合成超时，仅显示文本', 'err'); return; }
  ui.status(`${e.code || 'ERROR'}${e.user_message ? '：' + e.user_message : ''}`, 'err');
});

client.addEventListener('ended', (ev) => {
  stopBackendProbe();
  fileUplink.stop();
  playback.stopNow();
  engine.stopCapture();
  ui.setConnected(false);
  ui.status(`通话结束（${ev.detail.reason}）`);
});

client.addEventListener('expired', () => {
  stopBackendProbe();
  playback.stopNow();
  ui.setConnected(false);
  ui.status('会话过期，请重新开始', 'err');
});

/* ============================ 首字时延（本地观测） ============================ */

// 浏览器本地代理指标。对齐最后有声帧和同一轮首播通知，避免混用时钟。
client.addEventListener('final-transcript', (event) => {
  const transcript = /** @type {CustomEvent} */ (event).detail;
  timingTurn = transcript.audio_end_ms != null && lastSpeechEnd
    ? { id: transcript.turn_id, lastVoiced: lastSpeechEnd } : null;
  lastSpeechEnd = 0;
  store.set({ ttfb: null });
});
const origFirstFrame = client.playbackFirstFrame.bind(client);
client.playbackFirstFrame = (replyId, turnId, audioCtxTime) => {
  if (timingTurn?.id === turnId) store.set({ ttfb: Math.round(now() - timingTurn.lastVoiced) });
  origFirstFrame(replyId, turnId, audioCtxTime);
};

store.log(`前端已就绪（v0.2.1）｜ APP_BASE=${cfg.APP_BASE}（${cfg.APP_BASE_SOURCE}）｜ 点「开始通话」`);
