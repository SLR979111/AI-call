// @ts-check
/**
 * uplink-file.js —— 无麦克风回放源（FileUplink）
 *
 * 用途（三件，按优先级）：
 *   1. 现场保险：评委机器麦克风被拒/没有设备时，仍能演示完整链路
 *   2. E2E：wav 灌进同一条采集管线 → VAD/打断/埋点全部可自动化、可复现
 *   3. 调参：固定音源反复跑，便于对齐 VAD 阈值与 b0/b3
 *
 * 与 MicUplink 同形：输出 {pcm:Int16Array(320), frameSeq, monoMs, energyDb}
 * 采样率统一 16k/20ms（先用 OfflineAudioContext 重采样，再走主线程 20ms 泵）。
 */
import { now } from './protocol.js';

export class FileUplink {
  /** @param {import('./audio-engine.js').AudioEngine} engine @param {any} cfg */
  constructor(engine, cfg) {
    this.engine = engine;
    this.cfg = cfg;
    /** @type {Int16Array|null} */
    this.pcm = null;
    this.durationMs = 0;
    this.frameSeq = 0;
    this.cursor = 0;              // 已发送样本数
    this.timer = 0;
    this.t0 = 0;
    this.running = false;
    this.muted = false;
    /** @type {((f:{pcm:Int16Array, frameSeq:number, monoMs:number, energyDb:number})=>void)|null} */
    this.onFrame = null;
    this.onEnd = /** @type {(()=>void)|null} */ (null);
    this.sourceLabel = '';
  }

  /**
   * 加载并重采样到 16k 单声道。
   * @param {string|File|Blob} source URL 或用户选择的文件
   */
  async load(source) {
    const ctx = this.engine.ctx;
    if (!ctx) throw new Error('AudioContext 未就绪：请先点「开始通话」（需要用户手势）');

    let ab;
    if (typeof source === 'string') {
      const res = await fetch(source);
      if (!res.ok) throw new Error(`音源 ${source} 读取失败 ${res.status}`);
      ab = await res.arrayBuffer();
      this.sourceLabel = source;
    } else {
      ab = await source.arrayBuffer();
      this.sourceLabel = source.name || 'file';
    }

    const decoded = await ctx.decodeAudioData(ab.slice(0));

    // 重采样到 16k（OfflineAudioContext 走浏览器自己的重采样器，比手写更稳）
    const targetRate = this.cfg.UPLINK_RATE || 16000;
    let mono;
    if (Math.abs(decoded.sampleRate - targetRate) < 1) {
      mono = downmix(decoded);
    } else {
      const frames = Math.ceil(decoded.duration * targetRate);
      const off = new OfflineAudioContext(1, frames, targetRate);
      const src = off.createBufferSource();
      src.buffer = decoded;
      src.connect(off.destination);
      src.start();
      const rendered = await off.startRendering();
      mono = rendered.getChannelData(0);
    }

    const n = mono.length;
    const i16 = new Int16Array(n);
    for (let i = 0; i < n; i++) {
      let s = mono[i];
      if (s > 1) s = 1; else if (s < -1) s = -1;
      i16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    this.pcm = i16;
    this.durationMs = Math.round((n / targetRate) * 1000);
    this.cursor = 0;
    this.frameSeq = 0;
    return { durationMs: this.durationMs, sampleRate: targetRate, frames: Math.ceil(n / 320) };
  }

  /** @param {{onFrame:(f:any)=>void, onEnd?:()=>void}} handlers */
  async start(handlers) {
    if (!this.pcm) throw new Error('没有音源：先 load()');
    this.onFrame = handlers.onFrame;
    this.onEnd = handlers.onEnd || null;
    this.running = true;
    this.t0 = now();
    this.cursor = 0;

    const FRAME = 320;
    const TICK = this.cfg.FRAME_MS || 20;
    let nextAt = now();
    const pump = () => {
      if (!this.running) return;
      // 按单调时钟校正，避免 setInterval 漂移（漂移会让 monoMs 与音频脱节）
      while (nextAt <= now() && this.running) {
        if (this.cursor >= (this.pcm ? this.pcm.length : 0)) { this._finish(); return; }
        const end = Math.min(this.cursor + FRAME, this.pcm.length);
        const chunk = this.pcm.subarray(this.cursor, end);
        let pcm = new Int16Array(FRAME);                 // 末尾不足一帧补零
        pcm.set(chunk);
        if (this.muted) pcm.fill(0);
        this.cursor = end;
        this.onFrame && this.onFrame({
          pcm,
          frameSeq: this.frameSeq++,
          monoMs: now(),
          energyDb: energyDbOf(pcm)
        });
        nextAt += TICK;
      }
      this.timer = window.setTimeout(pump, Math.max(0, nextAt - now()));
    };
    pump();
  }

  _finish() {
    this.running = false;
    clearTimeout(this.timer);
    this.timer = 0;
    this.onEnd && this.onEnd();
  }

  stop() { this.running = false; clearTimeout(this.timer); this.timer = 0; }
  /** @param {boolean} m */
  mute(m) { this.muted = m; }
  isRunning() { return this.running; }
}

/** @param {AudioBuffer} buf @returns {Float32Array} */
function downmix(buf) {
  const n = buf.length;
  const out = new Float32Array(n);
  for (let c = 0; c < buf.numberOfChannels; c++) {
    const d = buf.getChannelData(c);
    for (let i = 0; i < n; i++) out[i] += d[i] / buf.numberOfChannels;
  }
  return out;
}

/** @param {Int16Array} pcm */
function energyDbOf(pcm) {
  let sum = 0;
  for (let i = 0; i < pcm.length; i++) { const s = pcm[i] / 32768; sum += s * s; }
  return 20 * Math.log10(Math.sqrt(sum / pcm.length) + 1e-9);
}
