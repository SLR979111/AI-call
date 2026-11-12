/**
 * livekit.mjs —— LiveKit 媒体面客户端的单测（不依赖真实 LiveKit Server）
 *
 *   A. 缺 token / SDK 加载失败 / 入房超时 —— 三种失败都必须能抛出明确错误码，
 *      因为 session.js 靠它们决定"回退 ws_pcm"。（现场不允许 LiveKit 一挂就全哑。）
 *   B. 成功路径：**复用 audio-engine 那条麦克风 track**（不二次 getUserMedia），
 *      AEC 读数取自 track.getSettings()，远端音频 attach 到容器。
 *   C. 打断：duck() 只静音 <audio>、不销毁房间；unduck() 立刻恢复。
 *   D. 集成：session.startLiveKit() 失败时 transport 必须回到 ws_pcm，
 *      并真的把 transport_degraded 发出去（服务端才知道降级）。
 *
 * 运行：node web/rtc/test/livekit.mjs
 *
 * 测试手法：SDK 用 data: URL 注入一个假模块（cfg.LIVEKIT_SDK_URL），
 * 这样既不用跑 LiveKit Server，也不会真的去加载 1.4MB 的 vendor 包。
 */

/* ---------------- 最小 DOM / WS stub（node 里没有） ---------------- */

globalThis.document = globalThis.document || {
  body: {
    appendChild(el) { el.parentNode = this; return el; },
  },
  createElement() {
    return {
      id: '', hidden: false, parentNode: null,
      setAttribute() {},
      appendChild(el) { el.parentNode = this; return el; },
      remove() { this.removed = true; if (this.parentNode) this.parentNode = null; },
    };
  },
  getElementById() { return null; },
  documentElement: { appendChild(el) { el.parentNode = this; return el; } },
};
globalThis.WebSocket = globalThis.WebSocket || { OPEN: 1, CLOSED: 3 };

const fails = [];
const check = (name, ok, extra) => {
  console.log(`${ok ? '✅' : '❌'} ${name}${ok || extra === undefined ? '' : ' → ' + JSON.stringify(extra)}`);
  if (!ok) fails.push(name);
};

/* ---------------- 假 SDK（data: URL） ---------------- */

const FAKE_SDK = `
export const Track = { Kind: { Audio: 'audio', Video: 'video' }, Source: { Microphone: 'microphone' } };
export const RoomEvent = {
  TrackSubscribed: 'trackSubscribed',
  TrackUnsubscribed: 'trackUnsubscribed',
  ParticipantConnected: 'participantConnected',
  ParticipantDisconnected: 'participantDisconnected',
  AudioPlaybackStatusChanged: 'audioPlaybackChanged',
  MediaDevicesError: 'mediaDevicesError',
  Reconnecting: 'reconnecting',
  Reconnected: 'reconnected',
  Disconnected: 'disconnected',
};
export class LocalAudioTrack {
  constructor(track, constraints, userProvided) {
    this.mediaStreamTrack = track;
    this.userProvided = userProvided;
  }
}
export class Room {
  constructor(opts) {
    this.opts = opts;
    this.handlers = new Map();
    this.name = 'room-fake';
    this.canPlaybackAudio = true;
    this.remoteParticipants = new Map();
    this.published = null;
    this.micEnabled = false;
    this.localParticipant = {
      identity: 'browser-test',
      publishTrack: async (t, o) => { this.published = { track: t, options: o }; return { track: t }; },
      setMicrophoneEnabled: async () => { this.micEnabled = true; },
      getTrackPublication: () => null,
    };
  }
  on(ev, fn) { const list = this.handlers.get(ev) || []; list.push(fn); this.handlers.set(ev, list); return this; }
  emit(ev, ...args) { for (const fn of this.handlers.get(ev) || []) fn(...args); }
  async connect(url, token) {
    this.url = url; this.token = token;
    if (globalThis.__LK_CONNECT_MODE__ === 'fail') throw new Error('connect refused');
    if (globalThis.__LK_CONNECT_MODE__ === 'hang') return new Promise(() => {});
  }
  async disconnect() { this.emit(RoomEvent.Disconnected); }
  async startAudio() { this.canPlaybackAudio = true; }
}
/** 测试用：模拟一条远端音频订阅 */
export function __remote(room, identity) {
  const el = {
    paused: false, muted: false, removed: false, parentNode: null,
    play: () => Promise.resolve(), remove() { this.removed = true; this.parentNode = null; },
  };
  const track = { kind: 'audio', attach: () => el, detach: () => [el] };
  room.emit('trackSubscribed', track, {}, { identity });
  return { track, el };
}
`;
const SDKS = {
  ok: 'data:text/javascript,' + encodeURIComponent(FAKE_SDK),
  boom: 'data:text/javascript,' + encodeURIComponent("throw new Error('sdk exploded')"),
};

const { LiveKitTransport } = await import('../src/livekit.js');
const { SessionClient } = await import('../src/session.js');

/** @param {any} extra */
function makeCfg(extra = {}) {
  return {
    TRANSPORT_PREF: 'auto',
    LIVEKIT_SDK_URL: SDKS.ok,
    LIVEKIT_CONNECT_TIMEOUT_MS: 500,
    LIVEKIT_AUDIO: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    UPLINK_RATE: 16000, FRAME_MS: 20, PING_MS: 10000, RECONNECT_BACKOFF: [10],
    VAD: {}, FIRST_FRAME_WAIT_MS: 40, DEMO_WAV: '', ENABLE_MIC: true, DOWNLINK: 'auto',
    ...extra,
  };
}

/** 带 getSettings 的假麦克风 track（模拟 Chrome 实际生效的约束） */
function fakeMicTrack(settings) {
  return {
    kind: 'audio', enabled: true,
    getSettings: () => settings,
    stop() { this.stopped = true; },
  };
}

/* ================= A. 三种失败都要有明确错误码 ================= */

{
  const lk = new LiveKitTransport(makeCfg());
  let err = null;
  try { await lk.connect({ url: '', token: '' }); } catch (e) { err = e; }
  check('A1 缺 url/token → LIVEKIT_TOKEN_MISSING',
    !!err && /LIVEKIT_TOKEN_MISSING/.test(err.message), err && err.message);
}

{
  const lk = new LiveKitTransport(makeCfg({ LIVEKIT_SDK_URL: SDKS.boom }));
  let err = null;
  try { await lk.connect({ url: 'ws://127.0.0.1:7880', token: 'tk' }); } catch (e) { err = e; }
  check('A2 SDK 加载失败 → LIVEKIT_SDK_LOAD_FAILED（可被 session 捕获后回退）',
    !!err && /LIVEKIT_SDK_LOAD_FAILED/.test(err.message), err && err.message);
}

{
  globalThis.__LK_CONNECT_MODE__ = 'hang';
  const lk = new LiveKitTransport(makeCfg({ LIVEKIT_CONNECT_TIMEOUT_MS: 40 }));
  let err = null;
  const t0 = Date.now();
  try { await lk.connect({ url: 'ws://127.0.0.1:7880', token: 'tk' }); } catch (e) { err = e; }
  check('A3 connect 挂住 → LIVEKIT_CONNECT_TIMEOUT（不无限等）',
    !!err && /LIVEKIT_CONNECT_TIMEOUT/.test(err.message) && Date.now() - t0 < 400, err && err.message);

  globalThis.__LK_CONNECT_MODE__ = 'fail';
  const lk2 = new LiveKitTransport(makeCfg());
  let err2 = null;
  try { await lk2.connect({ url: 'ws://127.0.0.1:7880', token: 'tk' }); } catch (e) { err2 = e; }
  check('A4 connect 被拒 → LIVEKIT_CONNECT_FAILED',
    !!err2 && /LIVEKIT_CONNECT_FAILED/.test(err2.message), err2 && err2.message);
  globalThis.__LK_CONNECT_MODE__ = '';
}

/* ================= B. 成功路径：入房 + 复用采集轨 + AEC 读数 ================= */

let transport = null;
{
  const mic = fakeMicTrack({
    echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    sampleRate: 48000, channelCount: 1,
  });
  transport = new LiveKitTransport(makeCfg());
  const stats = await transport.connect({ url: 'ws://127.0.0.1:7880', token: 'tok-1', micTrack: mic });

  check('B1 入房成功 state=connected', stats.state === 'connected', stats.state);
  check('B2 room/identity 取自 SDK', stats.room === 'room-fake' && stats.identity === 'browser-test', stats);
  check('B3 麦克风复用 audio-engine 的 track（不二次 getUserMedia）',
    stats.publishedVia === 'shared-track' && transport.room.published.track.mediaStreamTrack === mic,
    stats.publishedVia);
  check('B4 LocalAudioTrack 标记为 userProvided（SDK 不会 stop/重建它）',
    transport.room.published.track.userProvided === true, transport.room.published.track.userProvided);
  check('B5 发布源标为 microphone',
    transport.room.published.options.source === 'microphone', transport.room.published.options);
  check('B6 AEC 读数来自 track.getSettings()',
    stats.aec.echoCancellation === true && stats.aec.sampleRate === 48000 && stats.aec.channelCount === 1,
    stats.aec);
}

/* ================= C. 播放 + 打断（duck/unduck） ================= */

{
  const { el } = transport.sdk.__remote(transport.room, 'agent');
  check('C1 订阅远端音频后挂上 <audio>', transport.stats().audioElements === 1 && el.parentNode !== null,
    transport.stats().audioElements);
  check('C2 remoteAudio 记录对端 identity', transport.stats().remoteAudio === 'agent', transport.stats().remoteAudio);
  check('C3 远端在播 → isPlaying()=true', transport.isPlaying() === true, transport.isPlaying());

  transport.duck();
  check('C4 duck() 静音 <audio> 且不销毁房间', el.muted === true && transport.room !== null, el.muted);
  check('C5 duck 期间 isPlaying()=false（打断门控据此判断"没在播"）',
    transport.isPlaying() === false && transport.stats().ducked === true, transport.isPlaying());

  transport.unduck();
  check('C6 unduck() 恢复出声（流没断，不丢缓冲）',
    el.muted === false && transport.isPlaying() === true && transport.room !== null, el.muted);

  await transport.stop();
  check('C7 挂断：退房 + 清空音频元素', transport.state === 'closed' && transport.stats().audioElements === 0,
    transport.stats());
}

/* ================= D. 集成：失败必须回退 ws_pcm 并上报服务端 ================= */

{
  const sent = [];
  const client = new SessionClient(makeCfg({ LIVEKIT_SDK_URL: SDKS.boom }));
  client.ws = { readyState: 1, send: (raw) => sent.push(JSON.parse(raw)) };
  client.t0 = 0;

  const stats = await client.startLiveKit({ url: 'ws://127.0.0.1:7880', token: 'tk', room: 'r1' });
  const degraded = sent.find((m) => m.type === 'transport_degraded');

  check('D1 入房失败时 startLiveKit 返回 null（不把异常抛给调用方）', stats === null, stats);
  check('D2 transport 回退 ws_pcm', client.transport === 'ws_pcm', client.transport);
  check('D3 向服务端发出 transport_degraded{from:livekit,to:ws_pcm}',
    !!degraded && degraded.from === 'livekit' && degraded.to === 'ws_pcm',
    degraded);
  check('D4 失败原因可追溯', !!degraded && /LIVEKIT_SDK_LOAD_FAILED/.test(degraded.reason || degraded.message || ''),
    degraded && degraded.reason);
}

/* ================= 收尾 ================= */

console.log('');
if (fails.length) {
  console.error(`❌ livekit 测试失败 ${fails.length} 项：\n  - ` + fails.join('\n  - '));
  process.exit(1);
}
console.log('✅ livekit 全部通过');
