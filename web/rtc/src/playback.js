// @ts-check
/**
 * playback.js —— 下行播放的唯一入口（云端 TTS / 本地 TTS / 浏览器合成 三态收敛在这里）
 *
 * 形态（form）：
 *   'speech' —— W1 Fake TTS：服务端只发 tts_* 元数据，音频由浏览器 speechSynthesis 合成
 *   'pcm'    —— W2 真实 TTS：服务端下发 audio_chunk.pcm_b64（PCM16LE，采样率见 tts_started）
 * auto 模式：tts_started 后 FIRST_FRAME_WAIT_MS 内收到 audio_chunk 用 pcm，否则用 speech。
 *
 * 不变量：
 *   1. 业务层不得按「云端/本地」分支，只按 form 分支
 *   2. playback_first_frame 每轮只发一次（首个采样真正播出时）
 *   3. duck 只静音、不丢缓冲（G11：误打断要能续播）
 *   4. 打断不丢进度：b3 = 本地最后一帧播出的时刻
 */
import { b64ToInt16, now } from './protocol.js';

export class Playback {
  /** @param {import('./audio-engine.js').AudioEngine} engine @param {any} cfg @param {import('./session.js').SessionClient} client */
  constructor(engine, cfg, client) {
    this.engine = engine;
    this.cfg = cfg;
    this.client = client;

    this.form = /** @type {'speech'|'pcm'|null} */ (null);
    this.replyId = '';
    this.turnId = '';
    this.sampleRate = 24000;
    this.estimated = true;
    /** @type {Array<{sentence_id:number,text:string,start_ms:number,end_ms:number,char_start:number,char_end:number,is_last?:boolean}>} */
    this.sentences = [];

    this.firstFrameSent = false;
    this.uttStartCtx = 0;          // pcm：utterance 开始时的 ctx.currentTime
    this.uttStartMono = 0;         // speech：utterance 开始时的 performance.now()
    this.nextAt = 0;               // pcm：下一个块最早可播时刻
    this.sources = /** @type {AudioBufferSourceNode[]} */ ([]);
    this.playing = false;
    this.ducked = false;
    this.underruns = 0;
    this.resumeRefused = 0;        // 服务端已取消后仍尝试恢复的次数（排障用）
    this.spoken = new Set();       // speech：已下发的 sentence_id
    this.droppedWhileDucked = 0;

    this.gain = null;
    this.refDb = -99;              // 下行参考电平（双讲门控用）
  }

  _ensureGain() {
    const ctx = this.engine.ctx;
    if (!ctx) return null;
    if (!this.gain) {
      this.gain = ctx.createGain();
      this.gain.gain.value = 1;
      this.gain.connect(ctx.destination);
    }
    return this.gain;
  }

  /* ============================ 生命周期 ============================ */

  /** @param {any} d tts_started */
  begin(d) {
    this.replyId = d.reply_id;
    this.turnId = d.turn_id;
    this.sampleRate = d.sample_rate || 24000;
    this.estimated = d.estimated === true || d.tts_alignment === false;
    this.sentences = Array.isArray(d.sentences) && d.sentences.length
      ? d.sentences.map(normalizeSentence)
      : [];
    this.firstFrameSent = false;
    this.uttStartCtx = 0;
    this.uttStartMono = 0;
    this.nextAt = 0;
    this.playing = false;
    this.ducked = false;
    this.aborted = false;          // 服务端已取消该 reply：此后禁止任何恢复
    this.currentSentenceId = null; // 正在播的句子（被打断时从下一句续播）
    this.duckSentenceId = null;
    this.spoken.clear();

    if (this.cfg.DOWNLINK === 'speech') this.form = 'speech';
    else if (this.cfg.DOWNLINK === 'pcm') this.form = 'pcm';
    else {
      this.form = null;                       // auto：等首个块或超时
      setTimeout(() => {
        if (this.form === null && this.replyId === d.reply_id) {
          this.form = 'speech';
          this._flushSpeechQueue();
        }
      }, this.cfg.FIRST_FRAME_WAIT_MS);
    }
  }

  /** @param {any} d tts_sentence */
  sentence(d) {
    const s = normalizeSentence(d);
    const i = this.sentences.findIndex((x) => x.sentence_id === s.sentence_id);
    if (i >= 0) this.sentences[i] = { ...this.sentences[i], ...s };
    else this.sentences.push(s);
    this.sentences.sort((a, b) => a.sentence_id - b.sentence_id);

    if (this.form === 'speech') this._speak(s);
    // pcm 形态：时间轴只用于游标，音频由 audio_chunk 提供
  }

  /** @param {any} d audio_chunk */
  pcm(d) {
    if (this.form === null) this.form = 'pcm';
    if (this.form !== 'pcm') return;              // 已判定为 speech，丢弃 pcm
    const ctx = this.engine.ctx;
    const gain = this._ensureGain();
    if (!ctx || !gain) return;

    const pcm = b64ToInt16(d.pcm_b64 || '');
    if (!pcm.length) return;
    const rate = d.sample_rate || this.sampleRate;

    const buf = ctx.createBuffer(1, pcm.length, rate);   // 让浏览器按 ctx 采样率自动重采样
    buf.copyToChannel(pcm, 0);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(gain);

    const when = Math.max(ctx.currentTime + 0.02, this.nextAt);
    if (this.nextAt && when - this.nextAt > 0.04) {       // 队空过久：记 underrun
      this.underruns++;
      this.client.clientEvent('PlaybackUnderrun', { reply_id: this.replyId, gap_ms: Math.round((when - this.nextAt) * 1000) });
    }
    src.start(when);
    this.sources.push(src);
    this.nextAt = when + buf.duration;

    if (!this.uttStartCtx) {
      this.uttStartCtx = when;
      this.playing = true;
      this._fireFirstFrame(when);
    }
    src.onended = () => {
      const k = this.sources.indexOf(src);
      if (k >= 0) this.sources.splice(k, 1);
    };
  }

  /** @param {any} d tts_completed */
  complete(d) {
    if (this.form === 'speech') {
      // speechSynthesis 的 onend 会驱动结束；这里只兜底一次
      setTimeout(() => { if (!this.playing) this._emitEnd(); }, 300);
    } else {
      const remain = Math.max(0, (this.nextAt - (this.engine.ctx ? this.engine.ctx.currentTime : 0)) * 1000);
      setTimeout(() => this._emitEnd(), remain + 60);
    }
    if (d && d.interrupted) this.refDb = -99;
  }

  _emitEnd() {
    if (!this.playing) return;
    this.playing = false;
    this.refDb = -99;
    this.client.playbackEnded(this.replyId, this.playedMs());
  }

  /* ============================ speech 形态 ============================ */

  _flushSpeechQueue() {
    for (const s of this.sentences) this._speak(s);
  }

  /** @param {any} s */
  _speak(s) {
    if (!s || !s.text) return;
    if (this.spoken.has(s.sentence_id)) return;
    if (this.ducked) { this.droppedWhileDucked++; return; }
    if (!hasSpeech()) return;
    this.spoken.add(s.sentence_id);
    this.currentSentenceId = s.sentence_id;

    const u = new SpeechSynthesisUtterance(s.text);
    u.lang = 'zh-CN';
    u.rate = 1.0;
    u.onstart = () => {
      if (!this.uttStartMono) this.uttStartMono = now();
      this.playing = true;
      this.refDb = -20;                                    // 估算参考电平（无音频图可测）
      this._fireFirstFrame(0);
    };
    u.onend = () => {
      const isLast = s.is_last || s.sentence_id === this.sentences[this.sentences.length - 1]?.sentence_id;
      if (isLast) setTimeout(() => this._emitEnd(), 80);
    };
    u.onerror = () => { /* 被打断/合成失败：静默，由 tts_completed 兜底 */ };
    window.speechSynthesis.speak(u);
  }

  /* ============================ 打断 ============================ */

  /** 立即本地静音，**不丢缓冲**（G11）；返回 b3（最后一帧播出的时刻） */
  duck() {
    if (this.ducked) return this.b3Mono || now();
    this.ducked = true;
    this.duckSentenceId = this.currentSentenceId;
    const ctx = this.engine.ctx;
    if (this.gain) this.gain.gain.value = 0;
    if (this.form === 'speech' && hasSpeech()) window.speechSynthesis.cancel();
    if (this.form === 'pcm' && ctx) {
      // 不 stop source（保留缓冲以便恢复），只把增益压 0
      this.nextAt = ctx.currentTime;
    }
    this.refDb = -99;
    this.b3Mono = now();
    this.client.clientEvent('PlaybackCancelled', {
      reply_id: this.replyId,
      played_ms: Math.round(this.playedMs()),
      ...(this.cursor() || {})
    });
    return this.b3Mono;
  }

  /**
   * 误打断恢复（C3）：**从下一句继续，不重播半句**（02 §5.2 规则 4）。
   * 两种情况下直接拒绝恢复：
   *   1. 服务端已裁决取消（abort() 过）——此时必须等新 turn，绝不复活旧 reply
   *   2. 该 reply 已经播完（playing=false 且无未播句）
   * 注意：speechSynthesis 的 cancel() 会丢弃当前 utterance，无法"续播半句"，
   * 所以恢复点 = 被打断那一句的**下一句**；pcm 形态只是解增益，缓冲还在。
   */
  unduck() {
    if (this.aborted) {                     // 服务端已取消：保持静音，且不弄脏 C4 白名单
      this.resumeRefused++;
      return false;
    }
    if (!this.ducked) return false;
    this.ducked = false;
    if (this.gain) this.gain.gain.value = 1;

    if (this.form === 'speech' && this.sentences.length) {
      const fromId = this.duckSentenceId == null ? -Infinity : this.duckSentenceId;
      const rest = this.sentences.filter((s) => s.sentence_id > fromId);
      if (!rest.length) { this.refDb = -99; return false; }   // 没有未播内容，不复活
      for (const s of rest) this._speak(s);
    }
    this.refDb = this.playing ? -20 : -99;
    this.client.clientEvent('BargeInCancelledLocal', {
      reply_id: this.replyId,
      resume_ms: Math.round(this.playedMs()),
      resume_from_sentence: this.duckSentenceId
    });
    return true;
  }

  /**
   * 服务端裁决取消（收到 `cancel` / `turn_aborted`）：硬停 + 永久封禁该 reply。
   * 之后任何同 reply_id 的 tts_started / tts_sentence / audio_chunk 都会被丢弃
   * （见 session 的 abortedReplies），防止"打断后又把原话播完"。
   */
  abort(reason = 'server_cancel') {
    this.aborted = true;
    this.ducked = false;
    this.stopNow();
    // 注意：只能发 C4 白名单里的 name（否则服务端回 CLIENT_EVENT_NOT_ALLOWED 并被丢弃）
    this.client.clientEvent('PlaybackCancelled', {
      reply_id: this.replyId,
      played_ms: Math.round(this.playedMs()),
      reason: 'server_abort:' + reason
    });
  }

  /** 硬停（挂断/取消确认后） */
  stopNow() {
    for (const s of this.sources) { try { s.stop(0); } catch { /* noop */ } }
    this.sources = [];
    if (hasSpeech()) window.speechSynthesis.cancel();
    if (this.gain) this.gain.gain.value = 1;
    this.ducked = false;
    this.playing = false;
    this.refDb = -99;
    this.nextAt = 0;
    this.uttStartCtx = 0;
    this.uttStartMono = 0;
  }

  /* ============================ 度量 ============================ */

  _fireFirstFrame(ctxStartAt) {
    if (this.firstFrameSent) return;
    this.firstFrameSent = true;
    const delayMs = ctxStartAt && this.engine.ctx
      ? Math.max(0, (ctxStartAt - this.engine.ctx.currentTime) * 1000)
      : 0;
    setTimeout(() => {
      this.client.playbackFirstFrame(this.replyId, this.turnId, this.engine.ctxTime());
    }, delayMs);
  }

  playedMs() {
    if (this.form === 'speech') {
      return this.uttStartMono ? Math.max(0, now() - this.uttStartMono) : 0;
    }
    const ctx = this.engine.ctx;
    if (!ctx || !this.uttStartCtx) return 0;
    return Math.max(0, (ctx.currentTime - this.uttStartCtx) * 1000);
  }

  /** 已播游标：tts_alignment=false 时按字数占比估算，并返回 estimated 标记（C6） */
  cursor() {
    const ms = this.playedMs();
    const list = this.sentences;
    if (!list.length) return null;
    let s = list[list.length - 1];
    for (const x of list) { if (ms >= (x.start_ms || 0) && ms < (x.end_ms || 0)) { s = x; break; } }
    const span = Math.max(1, (s.end_ms || 0) - (s.start_ms || 0));
    const ratio = Math.min(1, Math.max(0, (ms - (s.start_ms || 0)) / span));
    const charSpan = Math.max(1, (s.char_end || 0) - (s.char_start || 0));
    return {
      sentence_id: s.sentence_id,
      char_idx: Math.round((s.char_start || 0) + ratio * charSpan),
      estimated: this.estimated
    };
  }

  refEnergyDb() { return this.refDb; }
  isPlaying() { return this.playing; }
}

/** 浏览器 speechSynthesis 是否可用（Node/CI 下为 false：模块不能因为宿主不是浏览器就崩） */
function hasSpeech() {
  return typeof window !== 'undefined' && !!window.speechSynthesis;
}

/** @param {any} s */
function normalizeSentence(s) {
  const text = s.text || '';
  let charStart = s.char_start ?? s.charStart ?? 0;
  let charEnd = s.char_end ?? s.charEnd ?? 0;
  if (charEnd <= charStart) charEnd = charStart + text.length;   // 服务端未给字符区间时兜底
  return {
    sentence_id: s.sentence_id ?? s.sentenceIdx ?? 0,
    text,
    start_ms: s.start_ms ?? s.startMs ?? 0,
    end_ms: s.end_ms ?? s.endMs ?? 0,
    char_start: charStart,
    char_end: charEnd,
    is_last: !!s.is_last
  };
}
