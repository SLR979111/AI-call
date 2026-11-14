// @ts-check
/**
 * decision.js —— 新前端的两个接口（唯一实现）
 *
 * 设计前提（与产品对齐过）：
 *   1. **打断不是协议**。用户一开始说话，前端只做两件本地事：立刻停掉正在播的音频、
 *      把输入推上去。不需要 local_muted / cancel / turn_aborted 这套握手。
 *   2. **音频只从 hook 来**。下行的一切（音频 + 它的字幕 + 阶段）都由同一路流广播，
 *      音频块自带元数据（epoch / kind / reply_id / sample_rate / sentences / is_last）。
 *   3. 旧 epoch 的音频**无条件丢弃**——这就是"取消"，不需要任何控制消息。
 *
 * 因此对外只有两个方法：
 *
 *   push(part)          —— ① 把用户输入推给后端 Decision
 *   onAudio(handler)    —— ② 挂一个 hook 等音频（流式）
 *
 * 其它东西（connect / close / 心跳 / 重连）是生命周期，不算"接口"。
 *
 * 上行帧格式刻意沿用 protocol.js 的 0xA1 二进制帧：服务端那一侧已经有解析器，
 * 新逻辑不该因为换了决策方式就换编码。
 */
import {
  PROTOCOL_VERSION, buildAudioFrame, makeSeq, now
} from './protocol.js';

/** 后端还没合并新契约时的能力探测用路径 */
export const STREAM_WS_PATH = '/ws/decision';

/** 下行消息类型（服务端 → 前端） */
export const DOWN = Object.freeze({
  READY: 'stream_ready',
  /** 一段音频的元数据；后续 chunk 只带 utt + seq */
  SEGMENT: 'audio_segment',
  /** 音频块；可整块自带元数据（前端两种都收） */
  CHUNK: 'audio_chunk',
  /** 段结束（也可用 chunk.is_last=true 表达） */
  END: 'audio_end',
  /** 业务控制面：state / slots / compliance / tools / turn_result 等 */
  CONTROL: 'control',
  ERROR: 'error'
});

/** 上行消息类型（前端 → 服务端）；音频帧仍走二进制 */
export const UP = Object.freeze({
  HELLO: 'hello',
  INPUT: 'input'
});

/**
 * @typedef {Object} AudioMeta
 * @property {string} utt          段 id（同一段后续 chunk 只带这个）
 * @property {number} epoch        轮次归属；前端只播最大 epoch
 * @property {'filler'|'answer'|string} kind
 * @property {string} [reply_id]
 * @property {number} sample_rate
 * @property {string} [format]
 * @property {number} [channels]
 * @property {Array<any>} [sentences]
 * @property {boolean} is_last
 * @property {string} [stage]
 */

export class DecisionClient extends EventTarget {
  /** @param {any} cfg window.__AICALL_CONFIG__ */
  constructor(cfg) {
    super();
    this.cfg = cfg;
    this.t0 = now();
    this.ws = null;
    this.wsUrl = '';
    this.sessionId = '';
    this.connected = false;
    this.intentionalClose = false;
    this.reconnectAttempt = 0;
    this.seq = makeSeq(1);
    this.frameSeq = 0;
    /** 已见过的最大 epoch —— 下行的"取消"就是 epoch 变大 */
    this.epoch = 0;
    /** @type {Map<string, AudioMeta>} utt → 段元数据 */
    this.segments = new Map();
    /** 诊断计数 */
    this.droppedOldEpoch = 0;
    this.audioChunks = 0;
    this.lastError = '';
    /** 当前阶段（由业务控制面或段元数据推进） */
    this.stage = '';
    /** @type {Set<(ev:{meta:AudioMeta|null, pcm:Int16Array|null, ended:boolean, reason?:string})=>void>} */
    this.audioHandlers = new Set();
    this.pingTimer = 0;
  }

  /* ============================ 接口 ②：挂 hook 等音频 ============================ */

  /**
   * 挂一个音频 handler。返回取消订阅的函数。
   * handler 收到的是**已经按 epoch 归好类**的块：旧 epoch 不会送到这里（在 dispatch 里就丢了）。
   * @param {(ev:{meta:AudioMeta|null, pcm:Int16Array|null, ended:boolean, reason?:string}) => void} handler
   * @returns {() => void}
   */
  onAudio(handler) {
    this.audioHandlers.add(handler);
    return () => this.audioHandlers.delete(handler);
  }

  /* ============================ 接口 ①：推用户输入 ============================ */

  /**
   * 把用户输入推给后端 Decision。一个方法吃三种 payload：
   *
   *   push({ audio })   —— 采集帧（走 0xA1 二进制帧，与旧链路同格式）
   *   push({ text })    —— 文本输入（无麦演示 / 文本模式）
   *   push({ signal })  —— 本地信号：speech_started(b0) / speech_end / mute / heard
   *
   * 注意 signal 只是**数据**，不是控制指令：服务端不需要为它回 ack，
   * 打断也不依赖它（打断靠"用户开口就推音频"这个事实本身）。
   *
   * @param {{audio?:{pcm:Int16Array, monoMs:number, muted?:boolean},
   *          text?:string, final?:boolean,
   *          signal?:{name:string, [k:string]:any},
   *          heard?:any, extra?:Record<string, any>}} part
   * @returns {boolean} 是否真的发出去了
   */
  push(part) {
    if (!part || !this.connected) return false;

    if (part.audio) {
      const frame = buildAudioFrame({
        frameSeq: this.frameSeq++,
        clientMonoMs: part.audio.monoMs,
        flags: part.audio.muted ? 1 : 0,
        pcm: part.audio.pcm
      });
      this.sendRaw(frame);
      return true;
    }

    /** @type {any} */
    const msg = {
      type: UP.INPUT,
      protocol: PROTOCOL_VERSION,
      session_id: this.sessionId,
      client_mono_ms: Math.round(this.mono()),
      epoch: this.epoch,
      stage: this.stage || undefined
    };
    if (typeof part.text === 'string') {
      msg.text = part.text;
      msg.final = part.final !== false;
    }
    if (part.signal) msg.signal = part.signal;
    if (part.heard) msg.heard = part.heard;
    if (part.extra) Object.assign(msg, part.extra);
    return this.send(msg);
  }

  /** 打断那一刻本地已经播到哪儿 —— 后端"针对性回答"的上下文 */
  heardCursor(cursor) {
    if (!cursor) return null;
    return {
      reply_id: cursor.reply_id || null,
      played_ms: Math.round(cursor.played_ms || 0),
      sentence_id: cursor.sentence_id ?? null,
      char_idx: cursor.char_idx ?? null
    };
  }

  /* ============================ 生命周期 ============================ */

  /** @param {{sessionId:string, wsUrl:string}} p */
  connect(p) {
    this.sessionId = p.sessionId || '';
    this.wsUrl = p.wsUrl;
    this.intentionalClose = false;
    this.openSocket();
  }

  openSocket() {
    const ws = new WebSocket(this.wsUrl);
    this.ws = ws;
    this.connected = false;

    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.connected = true;
      this.reconnectAttempt = 0;
      this.send({
        type: UP.HELLO,
        protocol: PROTOCOL_VERSION,
        session_id: this.sessionId,
        client_mono_ms: Math.round(this.mono()),
        epoch: this.epoch
      });
      this.startPing();
      this.dispatchEvent(new CustomEvent('open'));
    };

    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      if (typeof ev.data === 'string') this.dispatch(ev.data);
    };

    ws.onerror = () => { this.lastError = 'WS_ERROR'; };

    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.connected = false;
      clearInterval(this.pingTimer);
      this.dispatchEvent(new CustomEvent('close', { detail: { code: ev.code } }));
      if (this.intentionalClose) return;
      const backoff = this.cfg?.STREAM?.RECONNECT_BACKOFF || [300, 800, 2000, 4000];
      const delay = backoff[Math.min(this.reconnectAttempt, backoff.length - 1)];
      this.reconnectAttempt++;
      setTimeout(() => { if (!this.intentionalClose && this.wsUrl) this.openSocket(); }, delay);
    };
  }

  close() {
    this.intentionalClose = true;
    clearInterval(this.pingTimer);
    this.connected = false;
    const ws = this.ws;
    this.ws = null;
    try { ws?.close(); } catch { /* noop */ }
  }

  startPing() {
    clearInterval(this.pingTimer);
    const ms = this.cfg?.STREAM?.HEARTBEAT_MS || 10000;
    this.pingTimer = setInterval(() => {
      if (this.connected) this.send({ type: 'ping', client_mono_ms: Math.round(this.mono()) });
    }, ms);
  }

  /* ============================ 下行分发 ============================ */

  /** @param {string} raw */
  dispatch(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    const type = msg && msg.type;

    switch (type) {
      case DOWN.READY:
        this.dispatchEvent(new CustomEvent('ready', { detail: msg }));
        return;
      case DOWN.SEGMENT:
        this.onSegment(msg);
        return;
      case DOWN.CHUNK:
        this.onChunk(msg);
        return;
      case DOWN.END:
        this.onEnd(msg);
        return;
      case DOWN.CONTROL:
        this.onControl(msg);
        return;
      case DOWN.ERROR:
        this.lastError = (msg.error && msg.error.code) || 'STREAM_ERROR';
        this.dispatchEvent(new CustomEvent('server-error', { detail: msg.error || msg }));
        return;
      default:
        // 兼容：老服务端会把 audio_chunk 直接带 pcm_b64 发（没有外层 type 约定时按 chunk 试一次）
        if (msg && msg.pcm_b64) { this.onChunk(msg); return; }
        this.dispatchEvent(new CustomEvent('unknown', { detail: msg }));
    }
  }

  /** @param {any} msg */
  onSegment(msg) {
    const meta = normalizeMeta(msg);
    if (!meta) return;
    if (!this.acceptEpoch(meta.epoch)) return;
    this.segments.set(meta.utt, meta);
    if (!this.stage && meta.stage) this.stage = meta.stage;
    this.dispatchEvent(new CustomEvent('segment', { detail: meta }));
    if (meta.is_last) this.onEnd({ utt: meta.utt, epoch: meta.epoch });
  }

  /** @param {any} msg */
  onChunk(msg) {
    // 先按 epoch 判废：旧 epoch 的块即使对应的段元数据已经被清掉（epoch 切换会清），
    // 也必须记进 droppedOldEpoch —— 这是"打断后旧音频被丢弃"唯一可观测的计数。
    const stored = this.segments.get(String(msg.utt || '')) || null;
    const rawEpoch = Number(msg.epoch != null ? msg.epoch : (stored ? stored.epoch : 0));
    if (rawEpoch && rawEpoch < this.epoch) { this.droppedOldEpoch++; return; }

    // 两种形态都收，优先级是"块自带元数据 > 段声明元数据"。
    // 判据只能是 kind / sample_rate：epoch 与 utt 每个块都带，拿它们判断会把
    // 只带 {utt,seq,pcm_b64} 的块误当自带元数据，套话就被错标成正式回答。
    const hasOwn = msg.kind != null || msg.sample_rate != null;
    const own = hasOwn ? normalizeMeta(msg) : null;
    const meta = own || stored || null;
    if (!meta) return;
    if (!this.acceptEpoch(meta.epoch)) { this.droppedOldEpoch++; return; }

    const pcm = decodePcm(msg);
    const ended = msg.is_last === true || meta.is_last === true;
    this.audioChunks++;
    this.emitAudio({ meta, pcm, ended: false });
    if (ended) this.onEnd({ utt: meta.utt, epoch: meta.epoch });
  }

  /** @param {any} msg */
  onEnd(msg) {
    const utt = String(msg.utt || '');
    const meta = utt ? (this.segments.get(utt) || null) : null;
    if (meta && !this.acceptEpoch(meta.epoch)) return;
    this.segments.delete(utt);
    this.emitAudio({ meta, pcm: null, ended: true });
  }

  /** @param {any} msg */
  onControl(msg) {
    const name = String(msg.name || msg.kind || '');
    const payload = msg.payload || msg.data || {};
    if (name === 'state' && payload.flow_state) this.stage = String(payload.flow_state);
    this.dispatchEvent(new CustomEvent('control', { detail: { name, payload } }));
  }

  /* ============================ epoch：这就是"取消" ============================ */

  /**
   * 新 epoch 到来时立刻广播一次"旧的全部作废"，播放器据此硬停 + 清空。
   * @param {number} epoch
   * @returns {boolean} 该块是否属于当前（最新）epoch
   */
  acceptEpoch(epoch) {
    const e = Number(epoch) || 0;
    if (e < this.epoch) return false;
    if (e > this.epoch) {
      this.epoch = e;
      this.segments.clear();
      this.dispatchEvent(new CustomEvent('epoch', { detail: { epoch: e } }));
      this.emitAudio({ meta: null, pcm: null, ended: false, reason: 'epoch_switch' });
    }
    return true;
  }

  /* ============================ 小工具 ============================ */

  /** @param {any} obj */
  send(obj) {
    if (!this.connected || !this.ws) return false;
    this.ws.send(JSON.stringify({ ...obj, seq: this.seq.next() }));
    return true;
  }

  /** @param {ArrayBuffer} buf */
  sendRaw(buf) {
    if (!this.connected || !this.ws) return false;
    this.ws.send(buf);
    return true;
  }

  /** @param {any} ev */
  emitAudio(ev) {
    for (const h of this.audioHandlers) {
      try { h(ev); } catch (err) { console.error('[decision] audio handler failed', err); }
    }
  }

  mono() { return now() - this.t0; }

  stats() {
    return {
      connected: this.connected,
      epoch: this.epoch,
      stage: this.stage,
      segments: this.segments.size,
      audioChunks: this.audioChunks,
      droppedOldEpoch: this.droppedOldEpoch,
      error: this.lastError
    };
  }
}

/**
 * 把服务端消息归一化成 AudioMeta。两种形态：
 *   A. 段消息带全量元数据
 *   B. 块消息自带全量元数据（我们讨论过的"音频块本身带元数据"）
 * @param {any} msg
 * @returns {AudioMeta|null}
 */
export function normalizeMeta(msg) {
  if (!msg) return null;
  const utt = msg.utt ?? msg.segment_id ?? msg.utt_id;
  const hasMeta = msg.epoch != null || msg.kind != null || msg.sample_rate != null;
  if (!utt && !hasMeta) return null;
  return {
    utt: String(utt || `utt_${msg.epoch || 0}_${msg.seq ?? 0}`),
    epoch: Number(msg.epoch || 0),
    kind: String(msg.kind || 'answer'),
    reply_id: msg.reply_id ? String(msg.reply_id) : undefined,
    sample_rate: Number(msg.sample_rate || 24000),
    format: String(msg.format || 'pcm16le'),
    channels: Number(msg.channels || 1),
    sentences: Array.isArray(msg.sentences) ? msg.sentences : undefined,
    is_last: msg.is_last === true,
    stage: msg.stage ? String(msg.stage) : undefined
  };
}

/**
 * 解 PCM：优先 pcm_b64（JSON 块），其次二进制块（ArrayBuffer/base64 字符串）。
 * @param {any} msg
 * @returns {Int16Array}
 */
export function decodePcm(msg) {
  const raw = msg.pcm_b64 ?? msg.pcm ?? msg.data;
  if (!raw) return new Int16Array(0);
  if (raw instanceof Int16Array) return raw;
  if (raw instanceof ArrayBuffer) return new Int16Array(raw);
  if (typeof raw === 'string') {
    try { return b64ToInt16Safe(raw); } catch { return new Int16Array(0); }
  }
  return new Int16Array(0);
}

/** base64 → Int16LE（不依赖 protocol.js 的 atob 版本，便于 node 测试） */
function b64ToInt16Safe(b64) {
  const bin = typeof atob === 'function' ? atob(b64) : Buffer.from(b64, 'base64').toString('binary');
  const n = bin.length >> 1;
  const out = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    const lo = bin.charCodeAt(i * 2);
    const hi = bin.charCodeAt(i * 2 + 1);
    let v = (hi << 8) | lo;
    if (v & 0x8000) v -= 0x10000;
    out[i] = v;
  }
  return out;
}
