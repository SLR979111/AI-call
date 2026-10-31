// @ts-check
/**
 * main.js —— 组装层：把 session / engine / playback / vad / ui 接起来
 *
 * 唯一允许写全局时序的地方。规则：
 *  - 本地 VAD 只产生：电平、b0、打断预触发、speech_started（C2）
 *  - 端点（用户说完）由服务端 vad 裁决；前端 speech_ended 只报告"最后一个有声帧"
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

let ui;
let muted = false;
let inBarge = false;
let bargeTimer = 0;
let firstVoicedFrameSent = false;

ui = initUi({
  onStart: () => startCall().catch((e) => { console.error(e); ui.status('启动失败：' + e.message, 'err'); }),
  onHangup: () => hangup(),
  onMute: () => toggleMute(),
  onBarge: () => triggerBarge('manual'),
  onDemo: () => startDemoUplink().catch((e) => ui.status('回放失败：' + e.message, 'err')),
  onText: (text) => {
    if (!client.sendText(text)) { ui.status('连接未就绪，无法发送', 'err'); return; }
    store.log(`→ text: ${text}`);
  }
});

/* ============================ 通话生命周期 ============================ */

async function startCall() {
  ui.status('请求麦克风与音频上下文…');
  try {
    await engine.start();                       // ★ 必须在点击手势内
  } catch (e) {
    console.warn('音频引擎启动失败（继续以无麦模式）', e);
  }

  ui.status('建立会话…');
  await client.startCall();
  ui.setConnected(true);
  ui.status('已连接', 'ok');

  // 无麦克风兜底：自动切到回放音源（现场保险），失败则退到纯文本输入
  const wantFile = cfg.UPLINK_MODE === 'file' || !engine.micAvailable;
  if (wantFile) {
    try {
      await startDemoUplink();
      ui.status(engine.micAvailable ? '麦克风不可用 → 已切回放音源' : '无麦克风 → 回放音源模式', 'ok');
    } catch (e) {
      store.log('回放音源不可用：' + e.message);
      ui.status('无麦克风：请用底部输入框（text 消息）', 'err');
    }
  }
}

/**
 * 把 wav 灌进同一条采集管线（与麦克风同形），因此 VAD/打断/埋点全部照常工作。
 * @param {string|File|Blob} [source]
 */
async function startDemoUplink(source) {
  const info = await fileUplink.load(source || cfg.DEMO_WAV);
  store.log(`回放音源 ${fileUplink.sourceLabel} ${info.durationMs}ms @${info.sampleRate}Hz`);
  await fileUplink.start({
    onFrame: handleFrame,
    onEnd: () => { store.log('回放结束'); client.speechEnded(vad.lastVoicedMono); }
  });
  ui.status(`回放中：${fileUplink.sourceLabel}（${info.durationMs}ms）`, 'ok');
}

function hangup() {
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
  // 1) 上行音频（仅 ws_pcm：WebRTC 期音频走 RTP，这里必须停发）
  if (client.transport === 'ws_pcm') {
    const flags = (muted ? 1 : 0) | (firstVoicedFrameSent ? 0 : 0);
    const frame = buildAudioFrame({
      frameSeq: f.frameSeq,
      clientMonoMs: f.monoMs,
      flags,
      pcm: f.pcm
    });
    client.sendAudioFrame(frame);
  }

  // 2) 本地 VAD
  const r = vad.push({
    energyDb: f.energyDb,
    monoMs: f.monoMs,
    playing: playback.isPlaying(),
    refDb: playback.refEnergyDb()
  });
  store.set({ db: r.db });

  if (r.start != null) {
    store.set({ b0: Math.round(r.start) });
    client.speechStarted(r.start, r.db);
    // 首帧标记（服务端可据此优先起 ASR）
    firstVoicedFrameSent = true;
  }
  if (r.barge != null) triggerBarge('local_vad', r.barge);
  if (r.end != null) client.speechEnded(vad.lastVoicedMono);
}

/* ============================ 打断（C2 / C3） ============================ */

/** @param {'local_vad'|'manual'} source @param {number} [b0Hint] */
function triggerBarge(source, b0Hint) {
  if (inBarge) return;
  if (!playback.isPlaying()) { store.log('当前未播放，忽略打断'); return; }
  inBarge = true;

  const b0 = b0Hint != null ? b0Hint : (vad.b0Mono || now() - client.t0);
  const cur = playback.cursor();
  const b3 = playback.duck();                                  // ≤80ms 内本地静音
  const stopLatency = Math.max(0, b3 - b0);

  store.set({ b3: Math.round(b3), stopLatency: Math.round(stopLatency) });
  client.localMuted({
    b0Mono: b0, b3Mono: b3,
    playedMs: playback.playedMs(),
    sentenceId: cur ? cur.sentence_id : undefined,
    charIdx: cur ? cur.char_idx : undefined,
    replyId: playback.replyId,
    turnId: playback.turnId,
    reason: source === 'manual' ? 'user_barge_button' : 'barge_in'
  });
  store.log(`打断 source=${source} b0=${b0.toFixed(0)} b3=${b3.toFixed(0)} stop=${stopLatency.toFixed(1)}ms cursor=${JSON.stringify(cur)}`);

  // C3：300ms 内本地 VAD 未持续 → 视为误打断，本地恢复（不新增消息）
  clearTimeout(bargeTimer);
  bargeTimer = window.setTimeout(() => {
    if (!vad.isStillSpeaking()) {
      playback.unduck();
      store.log('误打断：本地恢复播报');
    }
    inBarge = false;
  }, 300);
}

/* ============================ 服务端 → 本地 ============================ */

client.addEventListener('ready', () => {
  ui.setConnected(true);
  ui.status('会话就绪', 'ok');
});

client.addEventListener('tts-started', (ev) => playback.begin(ev.detail));
client.addEventListener('tts-sentence', (ev) => playback.sentence(ev.detail));
client.addEventListener('tts-completed', (ev) => {
  playback.complete(ev.detail);
  if (ev.detail.interrupted) store.log('tts_completed interrupted=true');
});
client.addEventListener('pcm', (ev) => playback.pcm(ev.detail));

client.addEventListener('media-ready', () => {
  // W2：此处接 LiveKit 入房；W1 由 requestMedia 的回落逻辑置 ws_pcm
  store.log('media_ready（W2 接 LiveKit）');
});

client.addEventListener('cancel', () => {
  playback.duck();                       // 服务端已裁决取消：保持静音
  inBarge = false;
});

client.addEventListener('turn-aborted', (ev) => {
  // 服务端裁决战断：硬停本 reply，等新的 state 消息（不回退 flow_state）
  playback.stopNow();
  inBarge = false;
  clearTimeout(bargeTimer);
  store.log(`turn_aborted reason=${ev.detail.reason}`);
});

client.addEventListener('transport-switched', () => {
  ui.status('已切到兼容模式（ws_pcm）', 'ok');
});

client.addEventListener('server-error', (ev) => {
  const e = ev.detail || {};
  if (e.code === 'SESSION_TIMEOUT') { ui.status('会话超时', 'err'); return; }
  if (e.code === 'TTS_TIMEOUT') { ui.status('语音合成超时，仅显示文本', 'err'); return; }
  ui.status(`${e.code || 'ERROR'}${e.user_message ? '：' + e.user_message : ''}`, 'err');
});

client.addEventListener('ended', (ev) => {
  playback.stopNow();
  engine.stopCapture();
  ui.setConnected(false);
  ui.status(`通话结束（${ev.detail.reason}）`);
});

client.addEventListener('expired', () => {
  playback.stopNow();
  ui.setConnected(false);
  ui.status('会话过期，请重新开始', 'err');
});

/* ============================ 首字时延（本地观测） ============================ */

// playback_first_frame 发出的时刻 ≈ 播放开始；这里只做本地展示，
// 官方口径由服务端用 t9 计算（04 §2.10 TurnLatency）。
const origFirstFrame = client.playbackFirstFrame.bind(client);
client.playbackFirstFrame = (replyId, turnId, audioCtxTime) => {
  const t9 = now();
  const t0 = store.get().b0;
  if (t0 != null) store.set({ ttfb: Math.round(t9 - client.t0 - t0) });
  origFirstFrame(replyId, turnId, audioCtxTime);
};

store.log('前端已就绪（v0.2.1）：点「开始通话」');
