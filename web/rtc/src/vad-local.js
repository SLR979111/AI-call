// @ts-check
/**
 * vad-local.js —— 前端 VAD（快判，不裁决端点）
 *
 * 用途（严格限定）：
 *   1. 界面电平与状态灯
 *   2. 打断预触发（barge-in），据此产生 b0（C2）
 *   3. 埋点
 * 服务端 vad 才是端点权威（03 §4.2）。前端**不得**据此决定"用户说完了"。
 *
 * 参数（可在 UI 调试面板调）：
 *   START_FRAMES=3   → 连续 60ms 越限＝开口，**第一帧**时刻即 b0
 *   END_FRAMES=20    → 连续 400ms 静音＝收起（仅用于 UI 与误打断判定）
 *   BARGE_FRAMES=8   → 播放中连续 160ms 越限＝打断
 */
import { now } from './protocol.js';

export class LocalVad {
  /** @param {any} cfg */
  constructor(cfg) {
    const v = (cfg && cfg.VAD) || {};
    this.startFrames = v.START_FRAMES ?? 3;
    this.endFrames = v.END_FRAMES ?? 20;
    this.bargeFrames = v.BARGE_FRAMES ?? 8;
    this.floorMargin = v.FLOOR_MARGIN_DB ?? 9;
    this.refMargin = v.REF_MARGIN_DB ?? 6;

    this.noiseDb = -60;
    this.speech = false;
    this.spRun = 0;
    this.silRun = 0;
    this.bargeRun = 0;
    this.b0Mono = 0;             // 本次语音段的 b0
    this.firstFrameMono = 0;     // 当前 spRun 里第一帧的时间
    this.lastVoicedMono = 0;
    this.lastDb = -99;
  }

  /**
   * @param {{energyDb:number, monoMs:number, playing:boolean, refDb:number}} p
   * @returns {{start?:number, end?:number, barge?:number, db:number, gate:number, floor:number}}
   */
  push(p) {
    const db = p.energyDb;
    this.lastDb = db;

    // 噪声底跟踪：只在下行静音或明显低声时更新，避免把人声吸进 floor
    if (!p.playing || p.refDb < this.noiseDb + 6) {
      this.noiseDb = this.noiseDb * 0.95 + db * 0.05;
      this.noiseDb = Math.min(-35, Math.max(-80, this.noiseDb));
    }

    const thr = Math.max(this.noiseDb + this.floorMargin, -50);
    // 播放期抬高门限：AEC 残留不触发打断（双讲门控）
    const gate = (p.playing && p.refDb > -45) ? Math.max(this.noiseDb + this.floorMargin, p.refDb + this.refMargin) : thr;

    const isSpeech = db > gate;
    this.spRun = isSpeech ? this.spRun + 1 : 0;
    this.silRun = isSpeech ? 0 : this.silRun + 1;
    if (isSpeech && this.spRun === 1) this.firstFrameMono = p.monoMs;
    if (isSpeech) this.lastVoicedMono = p.monoMs;

    const out = { db, gate, floor: this.noiseDb };

    if (!this.speech && this.spRun >= this.startFrames) {
      this.speech = true;
      this.b0Mono = this.firstFrameMono;     // ★ b0 = 第一帧，不是第 3 帧
      out.start = this.b0Mono;
    }
    if (this.speech && this.silRun >= this.endFrames) {
      this.speech = false;
      out.end = now();
    }
    if (p.playing && isSpeech) {
      this.bargeRun++;
      if (this.bargeRun === this.bargeFrames) out.barge = this.firstFrameMono;
    } else if (!isSpeech) {
      this.bargeRun = 0;
    }
    return out;
  }

  /** 误打断恢复用：当前是否仍在说话（不依赖服务端） */
  isStillSpeaking() { return this.speech; }
  reset() { this.spRun = 0; this.silRun = 0; this.bargeRun = 0; this.speech = false; }
}
