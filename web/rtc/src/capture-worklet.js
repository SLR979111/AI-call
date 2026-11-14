/**
 * capture-worklet.js —— 采集到 16k / 20ms / 320 样本 的固定格式
 *
 * 输出：
 *   {type:'frame', pcm:ArrayBuffer(640), frameSeq, captureTime, energyDb}   // pcm 用 transferable 转移
 *   {type:'meter', db}                                                      // 每 100ms，仅驱动 UI
 *
 * 说明：
 *  - AudioWorkletGlobalScope 的 `sampleRate` 是 AudioContext 实际采样率。
 *    Chrome/Edge 尊重 `new AudioContext({sampleRate:16000})`；Safari 会忽略，
 *    所以这里保留一条**精确**的降采样路径（低通 + 分数延迟线性插值 + 跨块续算）。
 *  - 时长换算交给主线程（只有它知道 performance.now()），这里只报音频时钟 captureTime。
 *  - 不做 VAD：阈值的调参与回放都在主线程，便于现场演示时改参数。
 */

class Biquad {
  constructor(sampleRate, cutoff) {
    const w0 = (2 * Math.PI * cutoff) / sampleRate;
    const cos0 = Math.cos(w0);
    const alpha = Math.sin(w0) / (2 * Math.SQRT1_2);   // Q = 1/sqrt(2)，二阶 Butterworth
    const a0 = 1 + alpha;
    this.b0 = ((1 - cos0) / 2) / a0;
    this.b1 = (1 - cos0) / a0;
    this.b2 = ((1 - cos0) / 2) / a0;
    this.a1 = (-2 * cos0) / a0;
    this.a2 = (1 - alpha) / a0;
    this.x1 = 0; this.x2 = 0; this.y1 = 0; this.y2 = 0;
  }
  step(x) {
    const y = this.b0 * x + this.b1 * this.x1 + this.b2 * this.x2
            - this.a1 * this.y1 - this.a2 * this.y2;
    this.x2 = this.x1; this.x1 = x;
    this.y2 = this.y1; this.y1 = y;
    return y;
  }
}

class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.targetRate = o.targetRate || 16000;
    this.frameMs = o.frameMs || 20;
    this.inputRate = o.inputRate || sampleRate;
    this.frameSamples = Math.round((this.targetRate * this.frameMs) / 1000);   // 320

    this.acc = new Float32Array(this.frameSamples);
    this.off = 0;
    this.frameSeq = 0;

    this.needResample = Math.abs(this.inputRate - this.targetRate) > 1;
    this.ratio = this.inputRate / this.targetRate;
    this.lp = this.needResample
      ? new Biquad(this.inputRate, Math.min(0.45 * this.targetRate, this.inputRate * 0.49))
      : null;

    // 跨块续算的待插值缓冲（保证 0.4% 级别的速率误差，别用"每块丢弃余数"的省事写法）
    this.pending = new Float32Array(4096);
    this.pendLen = 0;
    this.pos = 0;            // 分数读位置（相对 pending 起点）

    this.meterAcc = 0;
    this.meterCount = 0;
    this.meterSamples = Math.max(1, Math.round(this.inputRate * 0.1));
  }

  /** @param {Float32Array} ch @returns {Float32Array} 16k 样本 */
  _resample(ch) {
    if (!this.needResample) return ch;

    // 1) 低通 + 追加到 pending
    if (this.pendLen + ch.length > this.pending.length) {
      // 压缩：把已消费部分挪走
      const drop = Math.floor(this.pos);
      if (drop > 0) {
        this.pending.copyWithin(0, drop, this.pendLen);
        this.pendLen -= drop;
        this.pos -= drop;
      }
      if (this.pendLen + ch.length > this.pending.length) {
        const bigger = new Float32Array(Math.max(this.pending.length * 2, this.pendLen + ch.length + 256));
        bigger.set(this.pending.subarray(0, this.pendLen));
        this.pending = bigger;
      }
    }
    for (let i = 0; i < ch.length; i++) {
      this.pending[this.pendLen++] = this.lp.step(ch[i]);
    }

    // 2) 分数位置线性插值
    const out = new Float32Array(Math.ceil(ch.length / this.ratio) + 4);
    let k = 0;
    let pos = this.pos;
    const limit = this.pendLen - 1;
    while (pos < limit) {
      const i0 = pos | 0;
      const frac = pos - i0;
      const a = this.pending[i0], b = this.pending[i0 + 1];
      out[k++] = a + (b - a) * frac;
      pos += this.ratio;
    }

    // 3) 丢弃已消费的整数部分，保留余量
    const drop = pos | 0;
    if (drop > 0) {
      this.pending.copyWithin(0, drop, this.pendLen);
      this.pendLen -= drop;
      this.pos = pos - drop;
    } else {
      this.pos = pos;
    }
    return out.subarray(0, k);
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;

    for (let i = 0; i < ch.length; i++) {
      this.meterAcc += ch[i] * ch[i];
      if (++this.meterCount >= this.meterSamples) {
        const rms = Math.sqrt(this.meterAcc / this.meterCount);
        this.port.postMessage({ type: 'meter', db: 20 * Math.log10(rms + 1e-9) });
        this.meterAcc = 0;
        this.meterCount = 0;
      }
    }

    const signal = this._resample(ch);
    let read = 0;
    while (read < signal.length) {
      const n = Math.min(this.frameSamples - this.off, signal.length - read);
      this.acc.set(signal.subarray(read, read + n), this.off);
      this.off += n;
      read += n;
      if (this.off === this.frameSamples) {
        this._flush();
        this.off = 0;
      }
    }
    return true;
  }

  _flush() {
    const out = new Int16Array(this.frameSamples);
    let sum = 0;
    for (let i = 0; i < this.frameSamples; i++) {
      let s = this.acc[i];
      if (s > 1) s = 1; else if (s < -1) s = -1;
      sum += s * s;
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;      // 标准 16-bit 映射
    }
    const energyDb = 20 * Math.log10(Math.sqrt(sum / this.frameSamples) + 1e-9);
    this.port.postMessage(
      { type: 'frame', pcm: out.buffer, frameSeq: this.frameSeq++, captureTime: currentTime, energyDb },
      [out.buffer]
    );
  }
}

registerProcessor('aicall-capture', CaptureProcessor);
