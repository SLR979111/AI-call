// @ts-check
/**
 * audio-engine.js —— 单例 AudioContext + 采集图（全项目只有这里 new AudioContext()）
 *
 * 不变量：
 *  1. 必须在用户手势调用链内 start()（自动播放策略，G19）
 *  2. AudioContext 全局唯一，连续拨打复用；只停 track，不 close()
 *  3. 采集图工作节点 → gain(0) → destination 保证持续拉取（不出声）
 */
import { now } from './protocol.js';

export class AudioEngine {
  /** @param {any} cfg */
  constructor(cfg) {
    this.cfg = cfg;
    /** @type {AudioContext|null} */
    this.ctx = null;
    /** @type {MediaStream|null} */
    this.stream = null;
    /** @type {AudioWorkletNode|null} */
    this.node = null;
    this.actualRate = 0;         // AudioContext 实际采样率（Safari 会忽略 16000 请求）
    this.inputRate = 0;          // 麦克风轨道实际采样率（通常 48000）
    this.device = null;
    this.micAvailable = false;
    this.muted = false;
    this.workletLoaded = false;
    this._monoBase = 0;          // mono = (monoBase - ctxBaseSec*1000) + captureTimeSec*1000
    this._ctxBaseSec = 0;
    this._monoCalibrated = false;
    /** @type {((frame:{pcm:Int16Array, frameSeq:number, monoMs:number, energyDb:number})=>void)|null} */
    this.onFrame = null;
    this.onMeter = /** @type {((db:number)=>void)|null} */ (null);
    this.frameSeq = 0;
  }

  /** 必须在用户点击的回调里调用 @returns {Promise<void>} */
  async start() {
    if (!this.ctx) {
      try {
        this.ctx = new AudioContext({ sampleRate: 16000 });
      } catch {
        this.ctx = new AudioContext();
      }
      this.actualRate = this.ctx.sampleRate;
      if (this.actualRate !== 16000) {
        console.warn(`[engine] AudioContext 采样率 ${this.actualRate}，worklet 内启用降采样`);
      }
    }
    if (this.ctx.state === 'suspended') await this.ctx.resume();

    if (!this.workletLoaded) {
      await this.ctx.audioWorklet.addModule(new URL('./capture-worklet.js', import.meta.url));
      this.workletLoaded = true;
    }
    if (this.micAvailable) return;                  // 幂等
    if (!this.cfg.ENABLE_MIC) { console.warn('[engine] ENABLE_MIC=0，跳过麦克风'); return; }

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
          sampleRate: 16000
        },
        video: false
      });
      const track = this.stream.getAudioTracks()[0];
      this.device = track.getSettings();
      this.inputRate = this.device.sampleRate || this.actualRate;
      console.info('[engine] 设备实际参数', this.device);

      const src = this.ctx.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.ctx, 'aicall-capture', {
        processorOptions: {
          targetRate: 16000,
          frameMs: this.cfg.FRAME_MS,
          inputRate: this.actualRate
        }
      });
      this.node.port.onmessage = (ev) => this._onWorklet(ev.data);

      const mute = this.ctx.createGain();
      mute.gain.value = 0;
      src.connect(this.node);
      this.node.connect(mute);
      mute.connect(this.ctx.destination);            // 保证图持续被拉取，不发声
      this.micAvailable = true;
    } catch (e) {
      // 麦克风失败不是致命错误：pcm 播放、字幕、按钮联调都还能用
      console.warn('[engine] 麦克风不可用，进入无麦模式', e);
      this.micAvailable = false;
    }
  }

  /** @param {any} d */
  _onWorklet(d) {
    if (d.type === 'meter') {
      if (this.onMeter) this.onMeter(d.db);
      return;
    }
    if (d.type !== 'frame') return;

    // 用音频时钟换算 client_mono_ms，避免 postMessage 投递抖动影响 b0 精度（C2）
    if (!this._monoCalibrated) {
      this._ctxBaseSec = d.captureTime;
      this._monoBase = now();
      this._monoCalibrated = true;
    }
    const monoMs = (this._monoBase - this._ctxBaseSec * 1000) + d.captureTime * 1000;

    const pcm = new Int16Array(d.pcm);
    if (this.muted) pcm.fill(0);
    if (this.onFrame) this.onFrame({ pcm, frameSeq: this.frameSeq++, monoMs, energyDb: d.energyDb });
  }

  setMuted(m) {
    this.muted = m;
    if (this.stream) this.stream.getAudioTracks().forEach((t) => { t.enabled = !m; });
  }

  /** 停采集但不销毁上下文（支持连续拨打） */
  stopCapture() {
    if (this.stream) {
      this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
    }
    if (this.node) { try { this.node.disconnect(); } catch { /* noop */ } this.node = null; }
    this.micAvailable = false;
    this._monoCalibrated = false;
  }

  /** AudioContext 时间（用于 playback_first_frame.audio_ctx_time） */
  ctxTime() { return this.ctx ? this.ctx.currentTime : 0; }
}
