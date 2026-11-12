// @ts-check
/**
 * livekit.js —— LiveKit（WebRTC）媒体面客户端：入房 / 发布麦克风 / 播放远端音频 / 打断静音 / AEC
 *
 * 职责边界：
 *   本模块只做「媒体面」。会话生命周期、控制消息、字幕/事件仍走 session.js 的 WebSocket。
 *   ws_pcm 与 livekit 是同一层里的两种传输，业务层不得按传输分支（沿用 playback.js 的纪律）。
 *
 * 三条不变量：
 *   1. **懒加载 SDK**：vendor/livekit-client.esm.mjs 有 1.4MB，ws_pcm 用户不该为它付下载代价；
 *      而且用动态 import 才能把「SDK 缺失 / 加载失败」变成可捕获异常 → 回退 ws_pcm。
 *      （静态 import 失败会让整条模块图崩掉，页面直接白屏。）
 *   2. **麦克风只采一次**：直接发布 audio-engine 已经拿到的 MediaStreamTrack，
 *      不另外 getUserMedia。AEC/NR/AGC 是 audio-engine 在 getUserMedia 时申请的约束，
 *      共用同一条 track 才能保证「VAD 判定用的声音」和「发给服务端的声音」是同一份。
 *   3. **打断只静音不销毁**：duck() 把远端 <audio> 置 muted，流继续走、解码不丢，
 *      unduck() 立刻恢复——与 playback.js 的 G11「误打断要能续播」保持一致。
 *
 * 回退约定（与 11-contract-v0.2-patch.md G4 对齐）：
 *   任何一步失败（SDK 加载 / 无 token / connect 超时 / 入房被拒 / 麦克风发布失败）
 *   都抛异常，由 session.js 统一 `transport_degraded → ws_pcm` 并上报服务端。
 */

const AUDIO_KIND = 'audio';
const MIC_SOURCE = 'microphone';

/** @typedef {'idle'|'connecting'|'connected'|'closed'} LkState */

export class LiveKitTransport extends EventTarget {
  /** @param {any} cfg window.__AICALL_CONFIG__ */
  constructor(cfg) {
    super();
    this.cfg = cfg || {};
    /** @type {LkState} */
    this.state = 'idle';
    /** @type {any} Room 实例 */
    this.room = null;
    /** @type {any} SDK 命名空间 */
    this.sdk = null;
    /** 实际用于发布的 MediaStreamTrack（可能是 shared / sdk 两种来源） */
    this.micMediaTrack = null;
    /** 'shared-track' | 'sdk-device' | null */
    this.publishedVia = null;
    /** @type {Set<any>} 已挂载的远端 <audio> */
    this.audioEls = new Set();
    /** @type {HTMLElement|null} */
    this.container = null;
    this.ducked = false;
    this.autoPlayBlocked = false;
    this.lastError = '';
    this.roomName = '';
    this.identity = '';
    /** @type {Set<string>} */
    this.remotes = new Set();
    this.remoteAudioFrom = '';
  }

  /* ============================ 对外状态 ============================ */

  isConnected() { return this.state === 'connected'; }

  /** 远端是否正在出声（打断门控用；被 duck 时视为没在播） */
  isPlaying() {
    if (this.ducked) return false;
    for (const el of this.audioEls) if (el && !el.paused) return true;
    return false;
  }

  /** 已订阅到远端音频轨（不论是否在播），用于 UI 与排障 */
  hasRemoteAudio() { return this.audioEls.size > 0; }

  /** @returns {any} 给调试面板用的一份快照 */
  stats() {
    return {
      state: this.state,
      room: this.roomName,
      identity: this.identity,
      participants: this.remotes.size + (this.identity ? 1 : 0),
      remoteAudio: this.remoteAudioFrom,
      audioElements: this.audioEls.size,
      ducked: this.ducked,
      publishedVia: this.publishedVia,
      autoPlayBlocked: this.autoPlayBlocked,
      aec: this.aecSettings(),
      error: this.lastError,
    };
  }

  /**
   * 采集约束的「实际生效值」。优先读正在发布的那条 track，
   * 拿不到就退回 audio-engine 原始 track，再退回落格的期望值。
   * 注意：getSettings() 返回的是浏览器实际采用的，不是我们请求的，这才是可验收的口径。
   */
  aecSettings() {
    /** @type {any} */
    let track = this.micMediaTrack;
    if (!track && this.room) {
      try {
        const pub = this.room.localParticipant?.getTrackPublication?.(MIC_SOURCE);
        track = pub?.track?.mediaStreamTrack || null;
      } catch { /* SDK 版本差异：忽略 */ }
    }
    let s = {};
    try { s = (track && track.getSettings && track.getSettings()) || {}; } catch { /* noop */ }
    return {
      echoCancellation: s.echoCancellation ?? null,
      noiseSuppression: s.noiseSuppression ?? null,
      autoGainControl: s.autoGainControl ?? null,
      sampleRate: s.sampleRate ?? null,
      channelCount: s.channelCount ?? null,
    };
  }

  /* ============================ 入房 ============================ */

  /** @param {string} u */
  sdkUrl() {
    if (this.cfg.LIVEKIT_SDK_URL) return this.cfg.LIVEKIT_SDK_URL;
    return new URL('../vendor/livekit-client.esm.mjs', import.meta.url).href;
  }

  async loadSdk() {
    if (this.sdk) return this.sdk;
    let mod;
    try {
      mod = await import(/* @vite-ignore */ this.sdkUrl());
    } catch (e) {
      throw new Error('LIVEKIT_SDK_LOAD_FAILED: ' + errText(e));
    }
    if (!mod || typeof mod.Room !== 'function') {
      throw new Error('LIVEKIT_SDK_INCOMPATIBLE: 缺少 Room 导出');
    }
    this.sdk = mod;
    return mod;
  }

  /**
   * 入房 + 发布麦克风。
   * @param {{url:string, token:string, micTrack?:MediaStreamTrack|null, container?:HTMLElement|null}} p
   * @returns {Promise<any>} stats()
   */
  async connect(p) {
    if (this.state === 'connected') return this.stats();
    const url = p && p.url;
    const token = p && p.token;
    if (!url || !token) throw new Error('LIVEKIT_TOKEN_MISSING: 服务端未下发 url/token');

    this.container = p.container || this.container || null;
    this.setState('connecting');
    const sdk = await this.loadSdk();
    const { Room, RoomEvent, Track } = sdk;
    const audioKind = (Track && Track.Kind && Track.Kind.Audio) || AUDIO_KIND;
    const micSource = (Track && Track.Source && Track.Source.Microphone) || MIC_SOURCE;

    const room = new Room({
      // 单路语音场景：关掉自适应流/动态转发，减少不必要的重协商
      adaptiveStream: false,
      dynacast: false,
      audioCaptureDefaults: this.captureOptions(),
      publishDefaults: { dtx: false, red: false },
    });
    this.room = room;
    this.wireRoom(room, RoomEvent, audioKind, sdk);

    const timeoutMs = Number(this.cfg.LIVEKIT_CONNECT_TIMEOUT_MS || 8000);
    try {
      await this.withTimeout(room.connect(url, token), timeoutMs, 'LIVEKIT_CONNECT_TIMEOUT');
    } catch (e) {
      await this.stopQuietly();
      throw new Error('LIVEKIT_CONNECT_FAILED: ' + errText(e));
    }

    this.roomName = String(room.name || '');
    this.identity = String(room.localParticipant?.identity || '');
    for (const p2 of (room.remoteParticipants?.values?.() || [])) this.remotes.add(String(p2.identity));

    // 麦克风：优先复用 audio-engine 已采集的 track（AEC 已生效、且与 VAD 同源）
    try {
      await this.publishMic(sdk, p.micTrack || null, micSource);
    } catch (e) {
      await this.stopQuietly();
      throw new Error('LIVEKIT_PUBLISH_FAILED: ' + errText(e));
    }

    // 入房后立刻尝试解锁音频播放（调用栈里通常还在手势链上）
    await this.resumeAudio();

    this.setState('connected');
    return this.stats();
  }

  /**
   * 发布麦克风。两条路：
   *   A. 复用采集 track：new LocalAudioTrack(track, undefined, true) → publishTrack
   *      （userProvidedTrack=true，SDK 不会去 stop/重建它，VAD 那条图不受影响）
   *   B. 退路：setMicrophoneEnabled(true, 约束)，让 SDK 自己 getUserMedia
   *      —— 会有第二路采集，AEC 由 SDK 的约束再申请一次，仅当 A 不可用时使用。
   */
  async publishMic(sdk, micTrack, micSource) {
    const lp = this.room.localParticipant;
    if (micTrack && typeof sdk.LocalAudioTrack === 'function') {
      try {
        const local = new sdk.LocalAudioTrack(micTrack, undefined, true);
        const pub = await lp.publishTrack(local, { name: 'mic-capture', source: micSource, dtx: false, red: false });
        this.micMediaTrack = (pub && pub.track && pub.track.mediaStreamTrack) || micTrack;
        this.publishedVia = 'shared-track';
        return;
      } catch (e) {
        this.emit('warn', { reason: 'publish-shared-track-failed', message: errText(e) });
      }
    }
    if (typeof lp.setMicrophoneEnabled !== 'function') {
      throw new Error('无法发布麦克风：SDK 缺少 setMicrophoneEnabled');
    }
    await lp.setMicrophoneEnabled(true, this.captureOptions());
    this.publishedVia = 'sdk-device';
    try {
      const pub = lp.getTrackPublication?.(micSource);
      this.micMediaTrack = pub?.track?.mediaStreamTrack || null;
    } catch { this.micMediaTrack = null; }
  }

  /** @param {any} room @param {any} RoomEvent @param {string} audioKind @param {any} sdk */
  wireRoom(room, RoomEvent, audioKind, sdk) {
    const E = RoomEvent || {};
    room.on(E.TrackSubscribed || 'trackSubscribed', (track, pub, participant) => {
      if (!track || track.kind !== audioKind) return;
      this.attachAudio(track, participant);
    });
    room.on(E.TrackUnsubscribed || 'trackUnsubscribed', (track) => this.detachAudio(track));
    room.on(E.ParticipantConnected || 'participantConnected', (p) => {
      this.remotes.add(String(p.identity));
      this.emit('stats', this.stats());
    });
    room.on(E.ParticipantDisconnected || 'participantDisconnected', (p) => {
      this.remotes.delete(String(p.identity));
      this.emit('stats', this.stats());
    });
    room.on(E.AudioPlaybackStatusChanged || 'audioPlaybackChanged', () => {
      this.autoPlayBlocked = !room.canPlaybackAudio;
      this.emit('audio-blocked', { blocked: this.autoPlayBlocked });
    });
    room.on(E.MediaDevicesError || 'mediaDevicesError', (e) => {
      this.lastError = errText(e);
      this.emit('warn', { reason: 'media-devices-error', message: this.lastError });
    });
    room.on(E.Reconnecting || 'reconnecting', () => this.emit('state', { state: 'reconnecting' }));
    room.on(E.Reconnected || 'reconnected', () => this.emit('state', { state: 'connected' }));
    room.on(E.Disconnected || 'disconnected', () => {
      if (this.state === 'closed') return;
      this.lastError = this.lastError || 'LIVEKIT_DISCONNECTED';
      this.setState('closed');
      this.emit('disconnected', { reason: this.lastError });
    });
    void sdk;
  }

  /* ============================ 播放 ============================ */

  ensureContainer() {
    if (this.container) return this.container;
    if (typeof document === 'undefined') return null;
    const el = document.createElement('div');
    el.id = 'lkAudioSink';
    el.hidden = true;
    el.setAttribute('aria-hidden', 'true');
    (document.body || document.documentElement).appendChild(el);
    this.container = el;
    return el;
  }

  /** @param {any} track @param {any} participant */
  attachAudio(track, participant) {
    const identity = String(participant?.identity || '');
    this.remoteAudioFrom = identity;
    const box = this.ensureContainer();
    const el = typeof track.attach === 'function' ? track.attach() : null;
    if (el) {
      el.autoplay = true;
      el.playsInline = true;
      el.muted = this.ducked;          // 入房瞬间若已处于打断静音，保持静音
      if (box && el.parentNode !== box) box.appendChild(el);
      this.audioEls.add(el);
      // 浏览器可能因为自动播放策略挂起：这里再推一次，失败就等 resumeAudio()
      const played = el.play?.();
      if (played && typeof played.catch === 'function') {
        played.catch(() => { this.autoPlayBlocked = true; this.emit('audio-blocked', { blocked: true }); });
      }
    }
    this.emit('remote-audio', { identity, attached: this.audioEls.size });
    this.emit('stats', this.stats());
  }

  /** @param {any} track */
  detachAudio(track) {
    if (!track || typeof track.detach !== 'function') return;
    for (const el of track.detach() || []) {
      this.audioEls.delete(el);
      try { el.remove(); } catch { /* noop */ }
    }
    if (!this.audioEls.size) this.remoteAudioFrom = '';
    this.emit('stats', this.stats());
  }

  /**
   * 解锁被自动播放策略挂起的音频。必须由用户手势触发，所以在 UI 上挂一次性
   * pointerdown 监听（main.js 负责），不要在这里自动重试。
   */
  async resumeAudio() {
    const room = this.room;
    if (!room) return false;
    if (room.canPlaybackAudio) { this.autoPlayBlocked = false; return true; }
    try {
      await room.startAudio();
      this.autoPlayBlocked = !room.canPlaybackAudio;
      return !this.autoPlayBlocked;
    } catch {
      this.autoPlayBlocked = true;
      return false;
    }
  }

  /* ============================ 打断 ============================ */

  /** 本地静音远端播放（≤1 帧生效），不销毁房间、不丢解码缓冲 */
  duck() {
    this.ducked = true;
    for (const el of this.audioEls) el.muted = true;
    this.emit('duck', { ducked: true });
    return true;
  }

  unduck() {
    this.ducked = false;
    for (const el of this.audioEls) el.muted = false;
    this.emit('duck', { ducked: false });
    return true;
  }

  /* ============================ 收尾 ============================ */

  /** @param {any} track */
  async stop() {
    await this.stopQuietly();
    this.setState('closed');
  }

  async stopQuietly() {
    const room = this.room;
    this.room = null;
    if (room) {
      try { await room.disconnect(); } catch { /* 已经断了就算了 */ }
    }
    for (const el of this.audioEls) {
      try { el.remove(); } catch { /* noop */ }
    }
    this.audioEls.clear();
    this.remoteAudioFrom = '';
    this.remotes.clear();
    this.micMediaTrack = null;
    this.publishedVia = null;
    this.ducked = false;
    this.roomName = '';
    this.identity = '';
    this.container = null;
  }

  /* ============================ 小工具 ============================ */

  captureOptions() {
    const a = this.cfg.LIVEKIT_AUDIO || {};
    return {
      echoCancellation: a.echoCancellation !== false,
      noiseSuppression: a.noiseSuppression !== false,
      autoGainControl: a.autoGainControl !== false,
      channelCount: 1,
    };
  }

  /** @param {LkState} s */
  setState(s) {
    if (this.state === s) return;
    this.state = s;
    this.emit('state', { state: s });
  }

  /** @param {string} type @param {any} detail */
  emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }

  /** @param {Promise<any>} p @param {number} ms @param {string} code */
  withTimeout(p, ms, code) {
    if (!ms || ms <= 0) return p;
    let timer = 0;
    const t = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(code)), ms);
    });
    return Promise.race([p, t]).finally(() => clearTimeout(timer));
  }
}

/** @param {unknown} e */
function errText(e) {
  if (!e) return 'unknown';
  if (typeof e === 'string') return e;
  const anyE = /** @type {any} */ (e);
  return String(anyE.message || anyE.name || anyE);
}
