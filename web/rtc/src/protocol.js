// @ts-check
/**
 * protocol.js —— 契约 v0.2.1 的唯一编码层
 * 字段与语义以 docs/03-interface-contract-v0.1.md + docs/11-contract-v0.2-patch.md 为准。
 * 命名规则（不许自行发明）：
 *   session_id / turn_id / trace_id / reply_id / sentence_id / call_id(工具调用!) / release_id
 */

export const PROTOCOL_VERSION = '1.0';
export const CLIENT_VERSION = 'web@0.2.1';

/** 客户端 → 服务端（JSON 控制帧） */
export const C2S = Object.freeze({
  HELLO: 'hello',
  SPEECH_STARTED: 'speech_started',
  SPEECH_ENDED: 'speech_ended',
  LOCAL_MUTED: 'local_muted',
  PLAYBACK_FIRST_FRAME: 'playback_first_frame',
  PLAYBACK_ENDED: 'playback_ended',
  CLIENT_EVENT: 'client_event',
  MEDIA_REQUEST: 'media_request',
  TRANSPORT_DEGRADED: 'transport_degraded',
  HANGUP: 'hangup',
  PING: 'ping',
  TEXT: 'text'                    // 文本输入（无麦演示 / text 模式）；03 §4.1 有此能力，04 未列
});

/** 服务端 → 客户端 */
export const S2C = Object.freeze({
  SESSION_READY: 'session_ready',
  HELLO_ACK: 'hello_ack',         // ★ 真实抓包有，03/11 未定义：回显 protocol/server_version/transport
  STATE: 'state',
  TRANSCRIPT: 'transcript',
  AGENT_TEXT: 'agent_text',
  TTS_STARTED: 'tts_started',
  TTS_SENTENCE: 'tts_sentence',
  TTS_COMPLETED: 'tts_completed',
  AUDIO_CHUNK: 'audio_chunk',
  MEDIA_READY: 'media_ready',
  CANCEL: 'cancel',
  TURN_ABORTED: 'turn_aborted',
  COMPLIANCE: 'compliance',
  TOOL_PROGRESS: 'tool_progress',
  TURN_RESULT: 'turn_result',
  TURN_END: 'turn_end',           // ★ 真实抓包有：与 turn_result 同 payload（{turn_id,result:{kind,end,...}}）
  TRANSPORT_SWITCHED: 'transport_switched',
  TRANSPORT_FAILED: 'transport_failed',
  PONG: 'pong',
  SESSION_ENDED: 'session_ended',
  ERROR: 'error'
});

/** C4：client_event 白名单（非白名单服务端回 CLIENT_EVENT_NOT_ALLOWED） */
export const CLIENT_EVENTS = Object.freeze([
  'PlaybackCancelled', 'PlaybackUnderrun', 'NetStats', 'AecState',
  'ClockSync', 'BargeInCancelledLocal', 'VadMetrics'
]);

/** G7：turn_state 枚举（唯一状态来源，前端不得自造） */
export const TURN_STATES = Object.freeze([
  'IDLE', 'CONNECTING', 'GREETING', 'LISTENING', 'USER_SPEAKING', 'ENDPOINTING',
  'THINKING', 'SPEAKING', 'WRAP_UP', 'CLOSING', 'ENDED', 'DEGRADED', 'FAILED'
]);

/** G7：flow_state → 赛题六阶段进度条 */
export const FLOW_TO_STAGE = Object.freeze({
  OPEN: 0, IDENTITY_CONFIRM: 1, NEED_CONFIRM: 2, OFFER: 3,
  RESULT_CONFIRM: 4, WRITEBACK: 5, CLOSE: 5,
  TRANSFER: -1, END: -1, FALLBACK: -1
});
export const STAGE_LABELS = ['① 身份披露', '② 身份确认', '③ 需求确认', '④ 方案沟通', '⑤ 结果确认', '⑥ 回写完成'];

/** 音频帧：magic 0xA1，16B 头 + 640B PCM（20ms@16k） */
export const AUDIO_MAGIC_C2S = 0xa1;
export const AUDIO_MAGIC_S2C = 0xa2;
export const UPLINK_FRAME_SAMPLES = 320;
export const UPLINK_HEADER_BYTES = 16;

/** 单连接内单调递增 seq（G4：服务端按 seq 检测丢帧；重连后新 WS 连接从 0 重计） */
export function makeSeq(initial = 0) {
  let n = initial;
  return {
    next() { return n++; },
    peek() { return n; },
    reset(v = 0) { n = v; }
  };
}

/**
 * 组装一帧上行音频。
 * @param {{frameSeq:number, clientMonoMs:number, flags:number, pcm:Int16Array}} p
 * @returns {ArrayBuffer}
 */
export function buildAudioFrame(p) {
  const { frameSeq, clientMonoMs, flags, pcm } = p;
  const buf = new ArrayBuffer(UPLINK_HEADER_BYTES + UPLINK_FRAME_SAMPLES * 2);
  const dv = new DataView(buf);
  let o = 0;
  dv.setUint8(o, AUDIO_MAGIC_C2S); o += 1;
  dv.setUint8(o, 1); o += 1;                          // version
  dv.setUint16(o, 16000, false); o += 2;              // sample_rate
  dv.setUint16(o, UPLINK_FRAME_SAMPLES, false); o += 2;
  dv.setUint32(o, frameSeq >>> 0, false); o += 4;
  dv.setUint32(o, Math.min(0xffffffff, Math.max(0, Math.round(clientMonoMs))) >>> 0, false); o += 4;
  dv.setUint16(o, flags & 0xffff, false); o += 2;
  const out = new Int16Array(buf, UPLINK_HEADER_BYTES);
  out.set(pcm.subarray(0, UPLINK_FRAME_SAMPLES));
  return buf;
}

/**
 * 服务端下行 PCM 块（audio_chunk.pcm_b64）→ Int16Array。
 * 手写 base64 解码，避免对 atob 的逐字节拷贝开销。
 * @param {string} b64
 * @returns {Int16Array}
 */
export function b64ToInt16(b64) {
  const bin = atob(b64);
  const n = bin.length >> 1;
  const out = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    const lo = bin.charCodeAt(i * 2), hi = bin.charCodeAt(i * 2 + 1);
    let v = (hi << 8) | lo;                 // 服务端为 little-endian
    if (v & 0x8000) v -= 0x10000;
    out[i] = v;
  }
  return out;
}

/** 时间工具：单调毫秒（绝对禁止 Date.now 参与时延计算） */
export const now = () => performance.now();

/** AudioContext.currentTime 安全读取（无 ctx 时返回 0） */
export function ctxTime(ctx) {
  return ctx && typeof ctx.currentTime === 'number' ? ctx.currentTime : 0;
}
