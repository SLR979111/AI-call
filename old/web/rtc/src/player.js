// @ts-check
/**
 * player.js —— 流式音频播放器（新前端逻辑的唯一下行消费方）
 *
 * 三条规则，全部来自"打断不是协议"这个前提：
 *
 *   1. **epoch 变大 = 旧的立刻作废**。没有 cancel 消息，前端自己判：
 *      收到比当前大的 epoch，就硬停当前段、清空队列、丢掉之后所有旧 epoch 的块。
 *   2. **每块自带采样率**。不同段可能来自不同合成器（预渲染套话 / 云端 TTS），
 *      用 createBuffer(1, len, rate) 让浏览器负责重采样，缺 rate 就会变调。
 *   3. **段结束必须有信号**。`is_last` / `audio_end` 二者之一；
 *      否则 playing 永远为真、playback_ended 不上报、下一段拿不到干净状态。
 *
 * 不做的事：不做"误打断恢复"。新模型下答案被丢弃就丢弃，用户重新说，
 * 所以 duck/unduck 那套 C3 逻辑在这里不存在，只有 stopNow。
 */
import { now } from './protocol.js';

export class StreamPlayer {
  /**
   * @param {any} engine audio-engine（只用它的 ctx / ctxTime）
   * @param {any} cfg
   * @param {{onFirstFrame?:Function, onSegmentStart?:Function, onSegmentEnd?:Function,
   *          onEpochSwitch?:Function, onUnderrun?:Function}} [hooks]
   */
  constructor(engine, cfg, hooks = {}) {
    this.engine = engine;
    this.cfg = cfg;
    this.hooks = hooks;

    /** 当前已接受的 epoch；收到更小的 epoch 一律丢弃 */
    this.epoch = 0;
    /** @type {any|null} 当前段 */
    this.cur = null;
    /** @type {AudioBufferSourceNode[]} */
    this.sources = [];
    this.nextAt = 0;
    this.playing = false;
    this.ducked = false;
    this.firstFrameSent = false;
    /** 段开始时的 ctx 时间，用来算 played_ms */
    this.segStartCtx = 0;
    /** 诊断 */
    this.droppedOldEpoch = 0;
    this.droppedNoCtx = 0;
    this.underruns = 0;
    this.completedSegments = 0;
    this.lastMeta = null;
    this._endTimer = 0;
    this.gain = null;
  }

  /* ============================ 下行入口 ============================ */

  /**
   * @param {{meta:any|null, pcm:Int16Array|null, ended:boolean, reason?:string}} ev
   */
  push(ev) {
    if (!ev) return;

    // ① epoch 切换（也可能来自 decision 的 epoch_switch 通知）
    if (ev.reason === 'epoch_switch') { this.hardStop('epoch_switch', false); return; }

    const meta = ev.meta;
    if (!meta) { if (ev.ended) this.finishSegment('audio_end'); return; }

    if (meta.epoch < this.epoch) { this.droppedOldEpoch++; return; }
    if (meta.epoch > this.epoch) {
      this.epoch = meta.epoch;
      this.hardStop('epoch_switch', false);
      this.hooks.onEpochSwitch?.({ epoch: meta.epoch });
    }

    // ② 新的段（同一 epoch 内 utt 变了也算新段：filler → answer）
    if (!this.cur || this.cur.utt !== meta.utt) {
      this.finishSegment('segment_switch');
      this.beginSegment(meta);
    }
    this.lastMeta = meta;

    if (ev.pcm && ev.pcm.length) this.schedule(ev.pcm, meta);
    if (ev.ended || meta.is_last) this.finishSegment('is_last');
  }

  /** @param {any} meta */
  beginSegment(meta) {
    this.cur = {
      utt: meta.utt,
      epoch: meta.epoch,
      kind: meta.kind,
      reply_id: meta.reply_id,
      sentences: Array.isArray(meta.sentences) ? meta.sentences : [],
      sample_rate: meta.sample_rate
    };
    this.firstFrameSent = false;
    this.segStartCtx = 0;
    this.nextAt = 0;
    this.playing = false;
    this.ducked = false;
    clearTimeout(this._endTimer);
    this.hooks.onSegmentStart?.(this.cur);
  }

  /** @param {Int16Array} pcm @param {any} meta */
  schedule(pcm, meta) {
    const ctx = this.engine?.ctx;
    const gain = this.ensureGain();
    if (!ctx || !gain) { this.droppedNoCtx++; return; }

    const rate = Number(meta.sample_rate) || 24000;
    const buf = ctx.createBuffer(1, pcm.length, rate);
    buf.copyToChannel(Float32Array.from(pcm, (v) => v / 32768), 0);

    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(gain);

    const when = Math.max(ctx.currentTime + 0.02, this.nextAt);
    if (this.nextAt && when - this.nextAt > 0.04) {
      this.underruns++;
      this.hooks.onUnderrun?.({ utt: this.cur?.utt, gap_ms: Math.round((when - this.nextAt) * 1000) });
    }
    src.start(when);
    this.sources.push(src);
    this.nextAt = when + buf.duration;

    if (!this.segStartCtx) {
      this.segStartCtx = when;
      this.playing = true;
      this.fireFirstFrame(when);
    }
    src.onended = () => {
      const i = this.sources.indexOf(src);
      if (i >= 0) this.sources.splice(i, 1);
    };
  }

  /* ============================ 结束与打断 ============================ */

  /**
   * 段结束：等队列里最后一块播完再上报，played_ms 才算得准。
   * 注意 startCtx 必须在这里快照 —— 延时回调里 this.segStartCtx 可能已经被下一段覆盖。
   * @param {string} reason
   */
  finishSegment(reason) {
    const seg = this.cur;
    if (!seg) return;
    const ctx = this.engine?.ctx;
    const startCtx = this.segStartCtx;
    const remain = ctx && this.nextAt ? Math.max(0, (this.nextAt - ctx.currentTime) * 1000) : 0;
    this.cur = null;
    clearTimeout(this._endTimer);
    this._endTimer = setTimeout(() => {
      if (this.cur) return;                    // 期间已经开了新段
      const played = this.playedFrom(startCtx);
      this.playing = false;
      if (this.gain) this.gain.gain.value = 1;
      this.completedSegments++;
      this.hooks.onSegmentEnd?.({ ...seg, played_ms: Math.round(played), reason });
    }, Math.round(remain) + 60);
  }

  /**
   * 立即静音 + 清空。这就是新模型下全部的"打断"实现：
   * 用户开口那一刻调用，不需要等后端、不需要任何 ack。
   * @param {string} reason
   * @param {boolean} [emitEnd] 是否同时广播段结束（默认否，因为不是正常播完）
   */
  hardStop(reason, emitEnd = false) {
    clearTimeout(this._endTimer);
    for (const s of this.sources) { try { s.stop(0); } catch { /* 可能已经 ended */ } }
    this.sources = [];
    if (this.gain) this.gain.gain.value = 0;
    this.nextAt = 0;
    this.playing = false;
    const seg = this.cur;
    const played = this.playedMs();
    this.cur = null;
    if (emitEnd && seg) {
      this.hooks.onSegmentEnd?.({ ...seg, played_ms: Math.round(played), reason });
    }
    return seg;
  }

  /** 用户开口：硬停并记住打断了哪一段（给后端的 heard 游标） */
  interruptCursor() {
    const seg = this.cur;
    const cursor = this.cursor();
    this.hardStop('barge_in', false);
    return cursor ? { ...cursor, reply_id: cursor.reply_id || seg?.reply_id || null } : null;
  }

  /* ============================ 度量 ============================ */

  fireFirstFrame(ctxStartAt) {
    if (this.firstFrameSent) return;
    this.firstFrameSent = true;
    const ctx = this.engine?.ctx;
    const delayMs = ctxStartAt && ctx ? Math.max(0, (ctxStartAt - ctx.currentTime) * 1000) : 0;
    const seg = this.cur;
    setTimeout(() => {
      this.hooks.onFirstFrame?.({
        utt: seg?.utt,
        epoch: seg?.epoch,
        reply_id: seg?.reply_id,
        kind: seg?.kind,
        // 套话与正式回答分开计时（沿用旧契约里 formal 的口径）
        formal: seg?.kind !== 'filler'
      });
    }, delayMs);
  }

  /** @param {number} startCtx */
  playedFrom(startCtx) {
    const ctx = this.engine?.ctx;
    if (!ctx || !startCtx) return 0;
    return Math.max(0, (ctx.currentTime - startCtx) * 1000);
  }

  /** 当前段已播时长 */
  playedMs() { return this.playedFrom(this.segStartCtx); }

  /**
   * 已播游标：给后端"用户听到了哪一句"。
   * sentences 里没有时间轴时退化成"只报 played_ms"，不编造 char_idx。
   */
  cursor() {
    const seg = this.cur;
    const ms = this.playedMs();
    if (!seg) return null;
    const list = seg.sentences || [];
    let hit = null;
    for (const s of list) {
      const start = s.start_ms ?? 0;
      const end = s.end_ms ?? 0;
      if (ms >= start && ms < end) { hit = s; break; }
    }
    if (!hit) return { utt: seg.utt, reply_id: seg.reply_id, kind: seg.kind, played_ms: ms };
    const span = Math.max(1, (hit.end_ms ?? 0) - (hit.start_ms ?? 0));
    const ratio = Math.min(1, Math.max(0, (ms - (hit.start_ms ?? 0)) / span));
    const charSpan = Math.max(1, (hit.char_end ?? 0) - (hit.char_start ?? 0));
    return {
      utt: seg.utt,
      reply_id: seg.reply_id,
      kind: seg.kind,
      played_ms: ms,
      sentence_id: hit.sentence_id ?? null,
      char_idx: Math.round((hit.char_start ?? 0) + ratio * charSpan)
    };
  }

  isPlaying() { return this.playing; }
  currentKind() { return this.cur?.kind || null; }

  stats() {
    return {
      epoch: this.epoch,
      utt: this.cur?.utt || '',
      kind: this.cur?.kind || '',
      playing: this.playing,
      queued: this.sources.length,
      completedSegments: this.completedSegments,
      droppedOldEpoch: this.droppedOldEpoch,
      underruns: this.underruns,
      noCtx: this.droppedNoCtx
    };
  }

  ensureGain() {
    const ctx = this.engine?.ctx;
    if (!ctx) return null;
    if (!this.gain) {
      this.gain = ctx.createGain();
      this.gain.gain.value = 1;
      this.gain.connect(ctx.destination);
    }
    return this.gain;
  }

  /** 挂断时彻底清干净（不断 AudioContext，便于连续拨打） */
  dispose() {
    this.hardStop('dispose', false);
    this.epoch = 0;
    this.completedSegments = 0;
    this.droppedOldEpoch = 0;
    this.underruns = 0;
  }

  /** 便于排障：当前单调时间 */
  mono() { return now(); }
}
