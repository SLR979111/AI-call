/**
 * decision.mjs —— 新前端逻辑的单测：两个接口 + epoch 即取消
 *
 *   A. 接口① push()：音频走 0xA1 二进制帧，文本/信号走 JSON input
 *   B. 接口② onAudio()：段元数据 / 块自带元数据两种形态都能解析；per-chunk 采样率
 *   C. epoch 就是取消：旧 epoch 丢弃、新 epoch 广播 epoch_switch，不需要 cancel 消息
 *   D. 播放器：epoch 切换硬停、is_last 收尾、cursor() 给出"听到哪儿"
 *
 * 运行：node web/rtc/test/decision.mjs
 */

/* ---------------- 最小 stub ---------------- */

globalThis.WebSocket = globalThis.WebSocket || class {
  constructor(url) { this.url = url; this.readyState = 0; this.sent = []; }
  send(x) { this.sent.push(x); }
  close() { this.readyState = 3; }
};

const fails = [];
const check = (name, ok, extra) => {
  console.log(`${ok ? '✅' : '❌'} ${name}${ok || extra === undefined ? '' : ' → ' + JSON.stringify(extra)}`);
  if (!ok) fails.push(name);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const { DecisionClient, normalizeMeta } = await import('../src/decision.js');
const { StreamPlayer } = await import('../src/player.js');

/** 假 AudioContext：只记录，不真的出声 */
function fakeEngine() {
  const rates = [];                     // 记录每次 createBuffer 用的采样率
  const ctx = {
    currentTime: 0,
    destination: {},
    createGain: () => ({ gain: { value: 1 }, connect() { } }),
    createBuffer: (ch, len, rate) => {
      rates.push(rate);
      return { duration: len / rate, __rate: rate, __len: len, copyToChannel() { } };
    },
    createBufferSource() {
      return {
        buffer: null, connect() { }, start() { }, stop() { }, onended: null,
      };
    },
  };
  return { ctx, rates };
}

const cfg = {
  STREAM: { WS_PATH: '/ws/decision', HEARTBEAT_MS: 60000, RECONNECT_BACKOFF: [10] },
  APP_BASE: 'http://127.0.0.1:8099'
};

const SEG = (over) => ({
  type: 'audio_segment', utt: 'u_1', epoch: 1, kind: 'filler', reply_id: 'r_1',
  sample_rate: 24000, format: 'pcm16le', channels: 1, is_last: false,
  sentences: [{ sentence_id: 1, text: '好的', start_ms: 0, end_ms: 400, char_start: 0, char_end: 2 }],
  ...over
});
const pcm = (n = 8) => new Int16Array(n).fill(1000);

/* ================= A. 接口① push ================= */

{
  const c = new DecisionClient(cfg);
  check('A0 未连接时 push 返回 false', c.push({ text: 'hi' }) === false, c.push({ text: 'hi' }));

  c.connected = true;
  c.ws = { sent: [], send(x) { this.sent.push(x); } };
  c.sessionId = 's_test';

  const okText = c.push({ text: '你好', final: true });
  const msg = JSON.parse(c.ws.sent.at(-1));
  check('A1 文本 → input 消息', okText && msg.type === 'input' && msg.text === '你好' && msg.final === true, msg);

  c.push({ signal: { name: 'barge_in', b0_ms: 1200 }, heard: { played_ms: 860, sentence_id: 2, char_idx: 7 } });
  const barge = JSON.parse(c.ws.sent.at(-1));
  check('A2 打断只是数据：signal + heard 随 input 上行，没有 cancel/turn_aborted',
    barge.type === 'input' && barge.signal.name === 'barge_in' && barge.heard.played_ms === 860
    && !('cancel' in barge), barge);

  const okAudio = c.push({ audio: { pcm: pcm(320), monoMs: 123.4, muted: false } });
  const frame = c.ws.sent.at(-1);
  check('A3 音频 → 0xA1 二进制帧（沿用旧编码，服务端那侧有解析器）',
    okAudio && frame instanceof ArrayBuffer && new Uint8Array(frame)[0] === 0xa1,
    frame instanceof ArrayBuffer ? new Uint8Array(frame)[0] : typeof frame);
}

/* ================= B. 接口② 元数据解析 ================= */

{
  const bare = normalizeMeta({ type: 'audio_chunk', utt: 'u_9', epoch: 3, kind: 'answer', sample_rate: 22000 });
  check('B1 块自带元数据可解析', bare && bare.epoch === 3 && bare.kind === 'answer' && bare.sample_rate === 22000, bare);

  const fromSeg = normalizeMeta({ type: 'audio_segment', utt: 'u_9', epoch: 3, sample_rate: 16000 });
  check('B2 段消息缺 kind 时默认 answer', fromSeg && fromSeg.kind === 'answer', fromSeg);

  check('B3 没有 utt 也没有元数据 → null（不会被误当音频）', normalizeMeta({ pcm_b64: 'AA==' }) === null, null);

  const c = new DecisionClient(cfg);
  c.connected = true;
  c.ws = { sent: [], send() { } };
  const got = [];
  c.onAudio((ev) => got.push(ev));

  c.dispatch(JSON.stringify(SEG({ utt: 'u_1', epoch: 1, sample_rate: 22000 })));
  c.dispatch(JSON.stringify({
    type: 'audio_chunk', utt: 'u_1', epoch: 1, seq: 0,
    pcm_b64: Buffer.from(new Int16Array([1, 2, 3, 4]).buffer).toString('base64')
  }));
  const chunk = got.find((e) => e.pcm && e.pcm.length);
  check('B4 块只带 {utt,epoch,seq,pcm} 时继承段的 kind/采样率（不能被默认值覆盖）',
    chunk && chunk.meta.sample_rate === 22000 && chunk.meta.kind === 'filler',
    chunk && { rate: chunk.meta.sample_rate, kind: chunk.meta.kind });
  check('B5 base64 PCM 正确解码', chunk && chunk.pcm.length === 4 && chunk.pcm[0] === 1 && chunk.pcm[3] === 4,
    chunk && Array.from(chunk.pcm || []));
}

/* ================= C. epoch 就是取消 ================= */

{
  const c = new DecisionClient(cfg);
  c.connected = true;
  c.ws = { sent: [], send() { } };
  const events = [];
  c.onAudio((ev) => events.push(ev));
  c.addEventListener('epoch', (ev) => events.push({ epochEvent: ev.detail.epoch }));

  c.dispatch(JSON.stringify(SEG({ utt: 'u_a', epoch: 4, kind: 'filler' })));
  c.dispatch(JSON.stringify(SEG({ utt: 'u_b', epoch: 5, kind: 'answer' })));

  const epochEvents = events.filter((e) => e.epochEvent != null).map((e) => e.epochEvent);
  const switchEv = events.find((e) => e.reason === 'epoch_switch');
  check('C1 新 epoch 到来 → 广播 epoch_switch（前端据此硬停，无需 cancel）',
    switchEv != null && epochEvents.join(',') === '4,5', { epochEvents, hasSwitch: !!switchEv });
  check('C2 已接受的最大 epoch', c.stats().epoch === 5, c.stats());

  const before = c.droppedOldEpoch;
  c.dispatch(JSON.stringify({
    type: 'audio_chunk', utt: 'u_a', epoch: 4, seq: 9, pcm_b64: 'AAAA'
  }));
  check('C3 旧 epoch 的块被丢弃并计数（迟到的尾巴不会混进新一段）',
    c.droppedOldEpoch === before + 1, { before, after: c.droppedOldEpoch });
}

/* ================= D. 播放器 ================= */

{
  const engine = fakeEngine();
  const ended = [];
  const starts = [];
  const p = new StreamPlayer(engine, cfg, {
    onSegmentStart: (s) => starts.push(s.utt),
    onSegmentEnd: (s) => ended.push(s),
    onEpochSwitch: () => { }
  });

  p.push({ meta: normalizeMeta(SEG({ utt: 'u_1', epoch: 1, kind: 'filler' })), pcm: pcm(960) });
  check('D1 起播：playing=true 且记下 kind', p.isPlaying() === true && p.currentKind() === 'filler', p.stats());

  const droppedBefore = p.droppedOldEpoch;
  p.push({ meta: normalizeMeta(SEG({ utt: 'u_0', epoch: 0 })), pcm: pcm(960) });
  check('D2 播放器再挡一次旧 epoch（双保险）', p.droppedOldEpoch === droppedBefore + 1, p.stats());

  // 同一 epoch 内 filler → answer 换段
  p.push({ meta: normalizeMeta(SEG({ utt: 'u_2', epoch: 1, kind: 'answer' })), pcm: pcm(960) });
  check('D3 同 epoch 换段：kind 跟着变，且旧段被记为 segment_switch',
    p.currentKind() === 'answer' && starts.join(',') === 'u_1,u_2',
    { kind: p.currentKind(), starts });

  // epoch 变大 = 硬停
  p.push({ meta: normalizeMeta(SEG({ utt: 'u_3', epoch: 2, kind: 'filler' })), pcm: pcm(960) });
  check('D4 epoch 变大后队列被清空、进入新段', p.stats().epoch === 2 && p.stats().queued <= 1, p.stats());

  // is_last 收尾（块自带不同的采样率，验证 per-chunk 重采样）
  engine.ctx.currentTime = 0.03;                 // 假装已经播了 10ms
  const ratesBefore = engine.rates.length;
  p.push({
    meta: normalizeMeta(SEG({ utt: 'u_3', epoch: 2, kind: 'filler', is_last: true, sample_rate: 16000 })),
    pcm: pcm(480), ended: true
  });
  await sleep(260);                     // 等队列播完（finishSegment 按 nextAt 延后触发）
  const last = ended.at(-1);
  check('D5 is_last → 段结束回调带 played_ms，段的采样率以段声明为准',
    !!last && last.utt === 'u_3' && last.played_ms > 0 && last.sample_rate === 24000,
    ended);
  check('D5b 块自带的不同采样率用于调度（不是整段一个 rate，否则会变调）',
    engine.rates.slice(ratesBefore).includes(16000),
    engine.rates.slice(ratesBefore));

  // 打断游标
  p.push({ meta: normalizeMeta(SEG({ utt: 'u_4', epoch: 3, kind: 'answer' })), pcm: pcm(960) });
  engine.ctx.currentTime = 0.2;                  // 假装播了 200ms
  const cur = p.interruptCursor();
  check('D6 打断给出 heard 游标（reply_id + played_ms + 句内字符位）',
    !!cur && cur.reply_id === 'r_1' && cur.played_ms > 0 && cur.sentence_id === 1,
    cur);
  check('D7 打断后立即不播（本地硬停，不等后端）', p.isPlaying() === false, p.stats());
}

console.log('');
if (fails.length) {
  console.error(`❌ decision 测试失败 ${fails.length} 项：\n  - ` + fails.join('\n  - '));
  process.exit(1);
}
console.log('✅ decision 全部通过');
