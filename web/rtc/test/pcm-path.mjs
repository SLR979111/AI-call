/**
 * pcm-path.mjs —— `audio_chunk`（PCM 下行）路径的无头单测
 *
 * 为什么要有：W2 接真 TTS 时，下行会从「浏览器 speechSynthesis」切到「服务端 24k PCM 块」，
 * 这是 W2 第一天的最大风险点。现在就用 stub AudioContext 把这条路径钉住：
 *   - pcm_b64 → Int16 解码（小端）
 *   - 逐块调度 + AudioBuffer 采样率自适应（浏览器自动重采样）
 *   - playback_first_frame **每轮只发一次**
 *   - 已播时钟 playedMs / 游标 cursor（含 tts_alignment=false 的估算分支）
 *   - duck 只静音、不丢缓冲（G11）
 *
 * 运行：node web/rtc/test/pcm-path.mjs
 */
import { b64ToInt16 } from '../src/protocol.js';
import { Playback } from '../src/playback.js';

const fails = [];
const check = (name, ok, extra) => {
  console.log(`${ok ? '✅' : '❌'} ${name}${ok || extra === undefined ? '' : ' → ' + JSON.stringify(extra)}`);
  if (!ok) fails.push(name);
};

/* ---------------- stub：AudioContext / gain / buffer / source ---------------- */

function makeStubEngine() {
  const ctx = {
    currentTime: 0,
    sampleRate: 48000,
    destination: { connect() {}, disconnect() {} },
    createGain() {
      const g = { gain: { value: 1 }, connect() {}, disconnect() {} };
      created.gains.push(g);
      return g;
    },
    createBuffer(channels, length, rate) {
      const ch = new Float32Array(length);
      const buf = {
        numberOfChannels: channels, length, sampleRate: rate,
        duration: length / rate,
        getChannelData: () => ch,
        copyToChannel(src) { ch.set(src.subarray(0, length)); return true; }
      };
      created.buffers.push(buf);
      return buf;
    },
    createBufferSource() {
      const src = {
        buffer: null, onended: null, started: -1, stopped: false,
        connect() {}, disconnect() {},
        start(when) { src.started = when; created.starts.push(when); },
        stop() { src.stopped = true; }
      };
      created.sources.push(src);
      return src;
    }
  };
  const created = { gains: [], buffers: [], sources: [], starts: [] };
  return { engine: { ctx, ctxTime: () => ctx.currentTime }, created, ctx };
}

/* ---------------- stub：SessionClient 的回调面 ---------------- */

function makeStubClient() {
  const calls = { firstFrame: [], ended: [], clientEvent: [] };
  return {
    calls,
    playbackFirstFrame: (...a) => calls.firstFrame.push(a),
    playbackEnded: (...a) => calls.ended.push(a),
    clientEvent: (name, payload) => calls.clientEvent.push({ name, payload })
  };
}

const int16 = (n, amp = 0.3) => {
  const a = new Int16Array(n);
  for (let i = 0; i < n; i++) a[i] = Math.round(Math.sin((2 * Math.PI * 440 * i) / 24000) * amp * 32767);
  return a;
};
const toB64 = (i16) => Buffer.from(new Uint8Array(i16.buffer, i16.byteOffset, i16.byteLength)).toString('base64');

/* ---------------- 0) b64 解码 ---------------- */

{
  const src = new Int16Array([0, 1, -1, 32767, -32768, 1234]);
  const back = b64ToInt16(toB64(src));
  check('b64ToInt16 往返一致（小端）', back.length === src.length && back.every((v, i) => v === src[i]),
    Array.from(back));
}

/* ---------------- 1) pcm 调度 ---------------- */

const { engine, created, ctx } = makeStubEngine();
const client = makeStubClient();
const cfg = { DOWNLINK: 'pcm', FIRST_FRAME_WAIT_MS: 400, PCM_LOOKAHEAD_MS: 120 };
const pb = new Playback(engine, cfg, client);

pb.begin({
  turn_id: 't_0001', reply_id: 'r_0001_1', sample_rate: 24000,
  tts_alignment: false, estimated: true, cursor: null, sentences: []
});
pb.sentence({ reply_id: 'r_0001_1', sentence_id: 1, text: '可以的，我先帮您看一下订单。', start_ms: 0, end_ms: 2000, char_start: 0, char_end: 15, estimated: true });
pb.sentence({ reply_id: 'r_0001_1', sentence_id: 2, text: '订单目前显示明天上午送达。', start_ms: 2000, end_ms: 4000, char_start: 15, char_end: 29, estimated: true, is_last: true });

// 3 块 × 100ms（2400 样本 @24k）
for (let i = 0; i < 3; i++) {
  pb.pcm({ reply_id: 'r_0001_1', sentence_id: 1, seq: i + 1, sample_rate: 24000, pcm_b64: toB64(int16(2400)) });
}

check('生成了 3 个 AudioBufferSource', created.sources.length === 3, created.sources.length);
check('AudioBuffer 用块自带采样率（24000）创建 → 交给浏览器重采样',
  created.buffers.every((b) => b.sampleRate === 24000), created.buffers.map((b) => b.sampleRate));
check('调度时间严格递增（nextAt 推进）',
  created.starts.length === 3 && created.starts.every((t, i) => i === 0 || t > created.starts[i - 1]),
  created.starts);
check('source 连到共享 gain（便于 duck 全局静音）', !!pb.gain);

// 首帧：异步（定时器），等一拍
await new Promise((r) => setTimeout(r, 120));
check('playback_first_frame 只发一次', client.calls.firstFrame.length === 1, client.calls.firstFrame.length);
check('playback_first_frame 带 reply_id / turn_id',
  client.calls.firstFrame[0][0] === 'r_0001_1' && client.calls.firstFrame[0][1] === 't_0001',
  client.calls.firstFrame[0]);

/* ---------------- 2) 播放时钟与游标 ---------------- */

ctx.currentTime = 1.0;                       // 第一个块被调度在 ctx 0.02s 起播
const ms = pb.playedMs();
check('playedMs 从实际起播时刻算（≈980ms，不是 1000ms）', ms > 900 && ms <= 1000, ms);
const cur = pb.cursor();
check('游标落在第 1 句（start_ms 0–2000）', cur && cur.sentence_id === 1, cur);
check('游标 char_idx 落在 0–15 区间且随进度前进',
  !!cur && cur.char_idx >= 0 && cur.char_idx <= 15, cur && cur.char_idx);
check('游标带 estimated 标记（tts_alignment=false 时不冒充精确）', cur && cur.estimated === true);

ctx.currentTime = 2.5;
const cur2 = pb.cursor();
check('游标切到第 2 句（start_ms 2000–4000）', cur2 && cur2.sentence_id === 2, cur2);

/* ---------------- 3) duck 不丢缓冲（G11） ---------------- */

const b3 = pb.duck();
check('duck 把 gain 压到 0', pb.gain && pb.gain.gain.value === 0, pb.gain && pb.gain.gain.value);
check('duck 不 stop 已调度的 source（保留缓冲可续播）', created.sources.every((s) => !s.stopped));
check('duck 上报 PlaybackCancelled 且带 played_ms',
  client.calls.clientEvent.some((c) => c.name === 'PlaybackCancelled' && typeof c.payload.played_ms === 'number'),
  client.calls.clientEvent.map((c) => c.name));
check('duck 返回 b3（单调时刻）', typeof b3 === 'number' && b3 > 0);

pb.unduck();
check('unduck 恢复 gain=1 并上报 BargeInCancelledLocal',
  pb.gain.gain.value === 1 && client.calls.clientEvent.some((c) => c.name === 'BargeInCancelledLocal'));

/* ---------------- 4) 硬停 ---------------- */

pb.stopNow();
check('stopNow 停掉全部 source', created.sources.every((s) => s.stopped));
check('stopNow 复位播放时钟', pb.playedMs() === 0);

console.log(fails.length ? `\n❌ ${fails.length} 项失败：\n - ` + fails.join('\n - ') : '\n✅ 全部通过');
process.exit(fails.length ? 1 : 0);
