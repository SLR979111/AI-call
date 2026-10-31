// @ts-check
/**
 * session.js —— 唯一的 WebSocket 出口（全项目只有这里 new WebSocket()）
 *
 * 职责：建会话 → 连 WS → hello → 分发 S→C 消息 → 发出 C→S 消息（含上行音频帧）
 * 不做：音频采集/播放/VAD（在 audio/），不做业务判断（服务端 state 为准）
 *
 * 契约：v0.2.1（03 + 11 + reply_cx_v0.2.1.md）
 */
import {
  CLIENT_EVENTS, CLIENT_VERSION, C2S, PROTOCOL_VERSION, S2C, makeSeq, now
} from './protocol.js';
import * as store from './store.js';

export class SessionClient extends EventTarget {
  /** @param {any} cfg window.__AICALL_CONFIG__ */
  constructor(cfg) {
    super();
    this.cfg = cfg;
    /** @type {WebSocket|null} */
    this.ws = null;
    this.c2sSeq = makeSeq(1);
    this.t0 = 0;                 // 会话开始时的 performance.now()，client_mono_ms 的零点
    this.wsUrl = '';
    this.sessionId = '';
    this.intentionalClose = false;
    this.reconnectAttempt = 0;
    this.pingTimer = 0;
    this.clockSamples = /** @type {Array<{rtt:number, offset:number}>} */ ([]);
    this.clockSuspect = false;
    this.audioFramesSent = 0;
    this.droppedStaleReply = 0;
    /** 被服务端取消/已打断的 reply：之后同 reply 的 tts_* 与 audio_chunk 一律丢弃（防"打断后又播完"） */
    this.abortedReplies = new Set();
    this.currentReplyId = '';
    this.transport = 'ws_pcm';
    this.frameSeq = 0;
  }

  /** client_mono_ms：距会话开始的单调毫秒 */
  mono() { return now() - this.t0; }

  /* ============================ 建会话 ============================ */

  async startCall() {
    store.resetForNewCall();
    this.t0 = now();
    this.intentionalClose = false;
    this.reconnectAttempt = 0;
    this.frameSeq = 0;
    this.audioFramesSent = 0;

    const body = { mode: 'voice', scenario_id: this.cfg.SCENARIO_ID };
    if (this.cfg.RELEASE_ID) body.release_id = this.cfg.RELEASE_ID;

    const res = await fetch(`${this.cfg.APP_BASE}/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (!res.ok) throw new Error(`POST /sessions 失败 ${res.status} ${await res.text().catch(() => '')}`);
    const data = await res.json();

    this.sessionId = data.session_id;
    store.set({
      sessionId: data.session_id,
      releaseId: (data.version_set && data.version_set.release_id) || data.release_id || '',
      versionSet: data.version_set || null
    });
    store.log(`session=${data.session_id} release=${store.get().releaseId}`);

    // C5：服务端直接给完整 ws_url（含 token），不自行拼 query
    this.wsUrl = this.resolveWsUrl(data.ws_url);
    this.connect();
    return data;
  }

  /** @param {string} u */
  resolveWsUrl(u) {
    if (!u) throw new Error('POST /sessions 未返回 ws_url');
    if (/^wss?:\/\//i.test(u)) return u;
    const base = new URL(this.cfg.APP_BASE);
    base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
    return new URL(u, base).toString();
  }

  /* ============================ 连接 ============================ */

  connect() {
    store.set({ phase: 'connecting' });
    const ws = new WebSocket(this.wsUrl);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;

    ws.onopen = () => {
      store.log('WS open');
      this.reconnectAttempt = 0;
      this.send(C2S.HELLO, {
        session_id: this.sessionId,
        client_version: CLIENT_VERSION,
        // G20 结构化能力草案；服务端当前忽略，但两端以同一形状演进
        capabilities: { local_vad: true, aec: 'browser', duplex: 'full', worklet: true }
      });
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') this.dispatch(ev.data);
      else store.log(`收到二进制帧 ${ev.data.byteLength}B（W1 不使用）`);
    };

    ws.onerror = () => store.log('WS error');
    ws.onclose = (ev) => this.onClose(ev);
  }

  /** @param {CloseEvent} ev */
  onClose(ev) {
    clearInterval(this.pingTimer);
    store.set({ phase: 'error' });
    if (ev.code === 4401) {                      // G6：会话过期，禁止无限重连
      store.log('WS 4401 会话过期 → 停止重连');
      this.intentionalClose = true;
      store.set({ phase: 'ended', transportNote: '会话过期，请重新开始' });
      this.dispatchEvent(new Event('expired'));
      return;
    }
    if (this.intentionalClose) { store.set({ phase: 'ended' }); return; }

    const backoff = this.cfg.RECONNECT_BACKOFF;
    const delay = backoff[Math.min(this.reconnectAttempt, backoff.length - 1)];
    this.reconnectAttempt++;
    store.log(`WS closed code=${ev.code} → ${delay}ms 后重连（第 ${this.reconnectAttempt} 次）`);
    setTimeout(() => this.connect(), delay);
  }

  /* ============================ 发送 ============================ */

  /**
   * @param {string} type
   * @param {Record<string, any>} data
   */
  send(type, data = {}) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify({ type, seq: this.c2sSeq.next(), ...data }));
    return true;
  }

  /** 上行音频帧（ws_pcm 时使用；WebRTC 期停用） @param {ArrayBuffer} frame */
  sendAudioFrame(frame) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(frame);
    this.audioFramesSent++;
  }

  /* ============================ 接收分发 ============================ */

  /** @param {string} raw */
  dispatch(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch { store.log('非法 JSON: ' + raw.slice(0, 120)); return; }
    const { type } = msg;
    const d = msg.data || msg;          // 兼容 envelope{data} 与扁平两种形状

    switch (type) {
      case S2C.SESSION_READY: return this.onSessionReady(d);
      case S2C.HELLO_ACK: return this.onHelloAck(d);
      case S2C.STATE: return this.onState(d);
      case S2C.TRANSCRIPT: return this.onTranscript(d);
      case S2C.AGENT_TEXT: return this.onAgentText(d);
      case S2C.TTS_STARTED: return this.onTtsStarted(d);
      case S2C.TTS_SENTENCE: return this.onTtsSentence(d);
      case S2C.TTS_COMPLETED: return this.onTtsCompleted(d);
      case S2C.AUDIO_CHUNK: return this.onAudioChunk(d);
      case S2C.MEDIA_READY: return this.onMediaReady(d);
      case S2C.CANCEL: return this.onCancel(d);
      case S2C.TURN_ABORTED: return this.onTurnAborted(d);
      case S2C.COMPLIANCE: return this.onCompliance(d);
      case S2C.TOOL_PROGRESS: return this.onToolProgress(d);
      case S2C.TURN_RESULT: return this.onTurnResult(d);
      case S2C.TURN_END: return this.onTurnResult(d);
      case S2C.TRANSPORT_SWITCHED: return this.onTransportSwitched(d);
      case S2C.TRANSPORT_FAILED: return this.onTransportFailed(d);
      case S2C.PONG: return this.onPong(d);
      case S2C.SESSION_ENDED: return this.onSessionEnded(d);
      case S2C.ERROR: return this.onError(d);
      default:
        // 未知消息不致命（04 §6：未知 type 记 warning），但要留痕，避免"静默丢协议"
        store.log(`未处理消息 type=${type}`);
    }
  }

  /** hello 的服务端回执（真实抓包有：{session_id, protocol_version, server_version, transport}） @param {any} d */
  onHelloAck(d) {
    if (d.transport) this.setTransport(d.transport, '');
    if (d.protocol_version) store.log(`hello_ack protocol=${d.protocol_version} server=${d.server_version || '—'}`);
  }

  onSessionReady(d) {
    store.set({
      phase: 'connected',
      turnState: 'CONNECTING',
      // 真实帧里 session_ready 也带 session_id / version_set：以服务端为准（重连时尤其重要）
      ...(d.session_id ? { sessionId: d.session_id } : {}),
      ...(d.version_set && d.version_set.release_id ? { releaseId: d.version_set.release_id } : {}),
      ...(d.version_set ? { versionSet: d.version_set } : {})
    });
    if (d.session_id) this.sessionId = d.session_id;
    store.log(`session_ready turn=${d.turn_id || '—'} protocol=${d.protocol_version || '—'} transport=${d.transport || '—'}`);
    if (d.transport) this.setTransport(d.transport, '');
    this.startPing();
    this.requestMedia();
    this.dispatchEvent(new CustomEvent('ready', { detail: d }));
  }

  async requestMedia() {
    // C1：/rtc 未配置(503) 或 media_ready 超时 → 静默回落 ws_pcm，不阻断
    if (this.cfg.TRANSPORT_PREF === 'ws_pcm') {
      this.setTransport('ws_pcm', '按配置走兼容层');
      return;
    }
    const fallback = setTimeout(() => {
      this.setTransport('ws_pcm', '媒体未配置（超时回落）');
    }, 3000);

    const h = (ev) => {
      clearTimeout(fallback);
      this.removeEventListener('media-ready', h);
      const p = ev.detail || {};
      this.setTransport(p.transport === 'livekit' ? 'livekit' : 'ws_pcm', '');
      this.dispatchEvent(new CustomEvent('media-ready', { detail: p }));
    };
    this.addEventListener('media-ready', h);
    this.send(C2S.MEDIA_REQUEST, {});
  }

  /** @param {string} transport @param {string} note */
  setTransport(transport, note) {
    this.transport = transport;
    store.set({ transport, transportNote: note });
    store.log(`transport=${transport}${note ? ' (' + note + ')' : ''}`);
  }

  onState(d) {
    const flow = d.flow_state || '—';
    const fw = store.get().flowHighWater;
    store.set({
      turnId: d.turn_id || store.get().turnId,
      turnState: d.turn_state || store.get().turnState,
      flowState: flow,
      slots: d.slots || {},
      flowHighWater: Math.max(fw, stageIndex(flow))
    });
  }

  onTranscript(d) {
    store.pushUserLine(d.text || '', !!d.final);
  }

  /**
   * agent_text 按**句**下发（{turn_id, reply_id, sentence_id, text, final}），不是 token 增量。
   * 同一 reply 的多句拼成一条字幕行；同句重发幂等忽略。
   * @param {any} d
   */
  onAgentText(d) {
    const replyId = d.reply_id || this.currentReplyId || `r_${d.turn_id || 'x'}`;
    store.pushAgentSentence(replyId, d.sentence_id ?? null, d.text || '');
  }

  /** 该 reply 是否已被服务端取消（打断后被丢弃的旧播报） */
  isAborted(replyId) { return !!replyId && this.abortedReplies.has(replyId); }

  onTtsStarted(d) {
    if (this.isAborted(d.reply_id)) {       // 服务端已取消该 reply：不许重新开口
      this.droppedStaleReply++;
      store.log(`丢弃已取消 reply 的 tts_started: ${d.reply_id}`);
      return;
    }
    this.currentReplyId = d.reply_id;
    store.beginReply(d.reply_id, d.turn_id, d.estimated === true || d.tts_alignment === false);
    store.set({ downlink: d.estimated ? 'speech(估算)' : (d.tts_alignment === false ? 'speech(估算)' : 'pcm') });
    store.log(`tts_started reply=${d.reply_id} alignment=${d.tts_alignment} est=${d.estimated}`);
    this.dispatchEvent(new CustomEvent('tts-started', { detail: d }));
  }

  onTtsSentence(d) {
    if (this.isAborted(d.reply_id)) { this.droppedStaleReply++; return; }
    store.upsertSentence(d.reply_id, d);
    this.dispatchEvent(new CustomEvent('tts-sentence', { detail: d }));
  }

  onTtsCompleted(d) {
    store.completeReply(d.reply_id, !!d.interrupted);
    this.dispatchEvent(new CustomEvent('tts-completed', { detail: d }));
  }

  onAudioChunk(d) {
    // 旧 reply / 已取消 reply 的残留块一律丢弃（防幽灵音频），并计数以便自检
    if (this.isAborted(d.reply_id)) { this.droppedStaleReply++; return; }
    if (d.reply_id && d.reply_id !== this.currentReplyId) { this.droppedStaleReply++; return; }
    this.dispatchEvent(new CustomEvent('pcm', { detail: d }));
  }

  onMediaReady(d) {
    this.dispatchEvent(new CustomEvent('media-ready', { detail: d }));
  }

  onCancel(d) {
    const rid = d.reply_id || this.currentReplyId;
    if (rid) this.abortedReplies.add(rid);
    store.strikeReplyLines(rid);
    store.log(`cancel turn=${d.turn_id || '—'} reason=${d.reason || '—'} → 封禁 reply=${rid || '—'}`);
    this.dispatchEvent(new CustomEvent('cancel', { detail: { ...d, reply_id: rid } }));
  }

  onTurnAborted(d) {
    if (this.currentReplyId) this.abortedReplies.add(this.currentReplyId);
    store.log(`turn_aborted turn=${d.turn_id} reason=${d.reason} → 封禁 reply=${this.currentReplyId || '—'}`);
    // 服务端已裁决：作废旧 reply、停播、回到 listening（不改 turn_id，等 state 消息）
    this.dispatchEvent(new CustomEvent('turn-aborted', { detail: { ...d, reply_id: this.currentReplyId } }));
  }

  onCompliance(d) {
    const list = store.get().compliance.slice();
    list.push({ ...d, at: this.mono() });
    store.set({ compliance: list });
  }

  onToolProgress(d) {
    const tools = { ...store.get().tools };
    tools[d.call_id] = { ...(tools[d.call_id] || {}), ...d };
    store.set({ tools });
  }

  /**
   * 真实 payload 形状（ws_session_bargein.jsonl 抓包确认）：
   *   {turn_id, result:{kind:'end', reply_id, outcome, slots, result:{new_time, ticket}}}
   * 同时兼容扁平写法 {turn_id, outcome, result:{new_time, ticket}}；两种都归一化后再入 store。
   * @param {any} d
   */
  onTurnResult(d) {
    const r = d.result || {};
    const inner = r.result || (r.kind ? {} : d.result) || {};
    const normalized = {
      turnId: d.turn_id || '',
      outcome: r.outcome || d.outcome || '',
      slots: r.slots || d.slots || {},
      ticket: inner.ticket_id || inner.ticket || (typeof inner === 'string' ? inner : '') || '',
      newTime: inner.new_time || '',
      raw: d
    };
    store.set({ result: normalized });
    store.log(`turn_result outcome=${normalized.outcome || '—'} ticket=${normalized.ticket || '—'}`);
  }

  /** 文本输入（无麦克风演示 / text 模式；消息名 text，03 §4.1） @param {string} text */
  sendText(text) {
    if (!text) return false;
    return this.send(C2S.TEXT, { text });
  }

  onTransportSwitched(d) {
    this.setTransport(d.to || 'ws_pcm', '兼容模式');
    this.dispatchEvent(new CustomEvent('transport-switched', { detail: d }));
  }

  onTransportFailed(d) {
    store.set({ transportNote: d.fallback === 'text_only' ? '仅文本模式' : '媒体不可用' });
    store.log(`transport_failed ${d.reason} fallback=${d.fallback}`);
  }

  onPong(d) {
    const nowMono = this.mono();
    const sent = typeof d.client_mono_ms === 'number' ? d.client_mono_ms : null;
    const rtt = sent == null ? null : Math.max(0, nowMono - sent);
    store.set({ rtt });
    if (rtt != null) {
      this.clockSamples.push({ rtt, offset: 0 });
      if (this.clockSamples.length > 3) this.clockSamples.shift();
      const min = Math.min(...this.clockSamples.map((s) => s.rtt));
      this.clockSuspect = min > 50;      // 03 §2.2：偏差 >50ms 标 clock_suspect
      this.clientEvent('ClockSync', { rtt_ms: rtt, suspect: this.clockSuspect });
    }
  }

  onSessionEnded(d) {
    this.intentionalClose = true;
    clearInterval(this.pingTimer);
    store.set({ phase: 'ended', turnState: 'ENDED' });
    store.log(`session_ended reason=${d.reason} turns=${d.turns}`);
    this.dispatchEvent(new CustomEvent('ended', { detail: d }));
  }

  onError(d) {
    const err = d.error || d;
    store.log(`error ${err.code} ${err.message || ''}`);
    if (err.code === 'MEDIA_NOT_CONFIGURED') {        // C1：静默回落，不弹错
      this.setTransport('ws_pcm', '媒体未配置');
      return;
    }
    this.dispatchEvent(new CustomEvent('server-error', { detail: err }));
  }

  /* ============================ 上行埋点 ============================ */

  /** C2：b0 = 本地 VAD 连续 3 帧越限的**第一帧** client_mono_ms */
  speechStarted(b0Mono, energyDb) {
    this.send(C2S.SPEECH_STARTED, {
      turn_id: store.get().turnId || null,
      client_ts_ms: Date.now(),
      client_mono_ms: round3(b0Mono),
      energy_db: round3(energyDb),
      source: 'local_vad'
    });
  }

  speechEnded(lastVoicedMono) {
    this.send(C2S.SPEECH_ENDED, {
      client_mono_ms: round3(this.mono()),
      last_voiced_mono_ms: round3(lastVoicedMono),
      reason: 'local_vad'
    });
  }

  /**
   * C2/P1：三个时间字段各司其职，不许混用
   * @param {{b0Mono:number, b3Mono:number, playedMs:number, sentenceId?:number,
   *          charIdx?:number, replyId?:string, turnId?:string, reason?:string}} p
   */
  localMuted(p) {
    this.send(C2S.LOCAL_MUTED, {
      turn_id: p.turnId || store.get().turnId || null,
      reply_id: p.replyId || this.currentReplyId || null,
      client_mono_ms: round3(this.mono()),                  // 发出时刻
      b0_mono_ms: round3(p.b0Mono),                         // b0
      last_frame_played_mono_ms: round3(p.b3Mono),           // b3
      stop_latency_ms: round3(Math.max(0, p.b3Mono - p.b0Mono)),
      played_ms: Math.round(p.playedMs || 0),
      sentence_id: p.sentenceId ?? null,
      char_idx: p.charIdx ?? null,
      reason: p.reason || 'barge_in'
    });
  }

  playbackFirstFrame(replyId, turnId, audioCtxTime) {
    this.send(C2S.PLAYBACK_FIRST_FRAME, {
      turn_id: turnId || store.get().turnId || null,
      reply_id: replyId || null,
      client_mono_ms: round3(this.mono()),
      audio_ctx_time: round3(audioCtxTime)
    });
  }

  playbackEnded(replyId, playedMs) {
    this.send(C2S.PLAYBACK_ENDED, {
      turn_id: store.get().turnId || null,
      reply_id: replyId || null,
      client_mono_ms: round3(this.mono()),
      played_ms: Math.round(playedMs || 0)
    });
  }

  /**
   * C4 白名单：非白名单会被服务端以 CLIENT_EVENT_NOT_ALLOWED 拒绝（不会静默丢）
   * @param {string} name @param {Record<string, unknown>} payload
   */
  clientEvent(name, payload) {
    if (!CLIENT_EVENTS.includes(name)) { store.log(`[warn] 非白名单 client_event: ${name}`); return; }
    this.send(C2S.CLIENT_EVENT, { name, payload, client_mono_ms: round3(this.mono()) });
  }

  degrade(from, reason, stats) {
    this.send(C2S.TRANSPORT_DEGRADED, { from, to: 'ws_pcm', reason, stats: stats || null });
  }

  /* ============================ 心跳 / 收尾 ============================ */

  startPing() {
    clearInterval(this.pingTimer);
    this.pingTimer = window.setInterval(() => {
      this.send(C2S.PING, { t: Date.now(), client_mono_ms: round3(this.mono()), protocol: PROTOCOL_VERSION });
    }, this.cfg.PING_MS);
    this.send(C2S.PING, { t: Date.now(), client_mono_ms: round3(this.mono()), protocol: PROTOCOL_VERSION });
  }

  hangup(reason = 'user_click') {
    this.intentionalClose = true;
    this.send(C2S.HANGUP, { reason, client_mono_ms: round3(this.mono()) });
    clearInterval(this.pingTimer);
    setTimeout(() => { try { this.ws && this.ws.close(); } catch { /* noop */ } }, 200);
  }
}

/** @param {number} v */
function round3(v) { return Math.round((v || 0) * 1000) / 1000; }

/** @param {string} flow */
function stageIndex(flow) {
  const m = {
    OPEN: 0, IDENTITY_CONFIRM: 1, NEED_CONFIRM: 2, OFFER: 3,
    RESULT_CONFIRM: 4, WRITEBACK: 5, CLOSE: 5
  };
  return flow in m ? m[flow] : -1;
}
