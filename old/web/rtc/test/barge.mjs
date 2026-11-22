/**
 * barge.mjs —— 打断语义的单测（三件事，都是"打断后还在播原内容"这个 bug 的根因）
 *
 *   A. 误打断恢复必须**从下一句继续**，不许把已播的句子重播一遍（02 §5.2 规则 4）
 *   B. 服务端已取消（abort）后，`unduck()` 必须**拒绝恢复**（否则打不死）
 *   C. 被取消的 reply 在 session 层被**封禁**：同 reply 的 tts_started/tts_sentence 一律丢弃
 *      （否则服务端/上游继续下发旧 reply，前端就又唱起来了）
 *
 * 运行：node web/rtc/test/barge.mjs
 */
const spoken = [];
let cancelCount = 0;

globalThis.window = globalThis.window || {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h),
  speechSynthesis: {
    speak(u) { spoken.push(u.text); queueMicrotask(() => u.onstart && u.onstart()); },
    cancel() { cancelCount++; }
  }
};
globalThis.SpeechSynthesisUtterance = globalThis.SpeechSynthesisUtterance || class {
  constructor(text) { this.text = text; this.onstart = null; this.onend = null; this.onerror = null; }
};

const { Playback } = await import('../src/playback.js');
const { SessionClient } = await import('../src/session.js');
const { LocalVad } = await import('../src/vad-local.js');
const store = await import('../src/store.js');

const fails = [];
const check = (name, ok, extra) => {
  console.log(`${ok ? '✅' : '❌'} ${name}${ok || extra === undefined ? '' : ' → ' + JSON.stringify(extra)}`);
  if (!ok) fails.push(name);
};

function makeClient() {
  const calls = { clientEvent: [], firstFrame: [], ended: [] };
  return {
    calls,
    clientEvent: (name, payload) => calls.clientEvent.push({ name, payload }),
    playbackFirstFrame: (...a) => calls.firstFrame.push(a),
    playbackEnded: (...a) => calls.ended.push(a)
  };
}
const S = (id, text) => ({ sentence_id: id, text, start_ms: 0, end_ms: 1000, char_start: 0, char_end: 5 });
const flush = () => new Promise((r) => setTimeout(r, 20));

/* ---------------- A. 误打断：从下一句继续 ---------------- */

{
  spoken.length = 0;
  const client = makeClient();
  const pb = new Playback({ ctx: null, ctxTime: () => 0 }, { DOWNLINK: 'speech' }, client);
  pb.begin({ reply_id: 'r_1', turn_id: 't_1', sentences: [], tts_alignment: false, estimated: true });
  pb.sentence(S(1, '第一句'));
  await flush();
  check('A1 第 1 句已播', spoken.join('|') === '第一句', spoken.slice());

  const b3 = pb.duck();
  check('A2 duck 后取消当前 utterance（speechSynthesis.cancel）', cancelCount === 1, cancelCount);
  check('A3 duck 记录被打断的句子号', pb.duckSentenceId === 1, pb.duckSentenceId);
  check('A4 duck 返回 b3（单调时刻）', typeof b3 === 'number' && b3 > 0);

  pb.sentence(S(2, '第二句'));
  pb.sentence(S(3, '第三句'));
  check('A5 duck 期间新到的句子不播（保留待播）', spoken.join('|') === '第一句', spoken.slice());
  check('A6 记录被压下的句子数', pb.droppedWhileDucked === 2, pb.droppedWhileDucked);

  const resumes = pb.unduck();
  await flush();
  check('A7 unduck 成功恢复', resumes === true);
  check('A8 **从第 2 句继续，不重播第 1 句**', spoken.join('|') === '第一句|第二句|第三句', spoken.slice());
  check('A9 上报 BargeInCancelledLocal 且带 resume_from_sentence',
    client.calls.clientEvent.some((c) => c.name === 'BargeInCancelledLocal' && c.payload.resume_from_sentence === 1),
    client.calls.clientEvent.map((c) => c.name));
}

/* ---------------- B. 服务端已取消：拒绝恢复 ---------------- */

{
  spoken.length = 0;
  cancelCount = 0;
  const client = makeClient();
  const pb = new Playback({ ctx: null, ctxTime: () => 0 }, { DOWNLINK: 'speech' }, client);
  pb.begin({ reply_id: 'r_2', turn_id: 't_2', sentences: [], tts_alignment: false, estimated: true });
  pb.sentence(S(1, '会被打断的第一句'));
  await flush();
  pb.duck();
  pb.abort('turn_aborted:BARGE_IN');
  const before = spoken.length;

  const resumes = pb.unduck();
  await flush();
  check('B1 abort 后 unduck 返回 false（拒绝恢复）', resumes === false);
  check('B2 abort 后没有任何新语音', spoken.length === before, spoken.slice());
  check('B3 abort 后 playing=false', pb.playing === false);
  check('B4 上报 PlaybackCancelled 且 reason 标 server_abort（C4 白名单内）',
    client.calls.clientEvent.some((c) => c.name === 'PlaybackCancelled' && String(c.payload.reason).startsWith('server_abort')),
    client.calls.clientEvent.map((c) => c.name + ':' + (c.payload && c.payload.reason)));
  check('B5 abort 后 aborted 标记为 true', pb.aborted === true);
  check('B6 记录"拒绝恢复"次数（排障指标，不污染 C4 白名单）', pb.resumeRefused === 1, pb.resumeRefused);
}

/* ---------------- C. session 层封禁旧 reply ---------------- */

{
  store.resetForNewCall();
  const sclient = new SessionClient({ APP_BASE: 'http://127.0.0.1:1', PING_MS: 1e9, TRANSPORT_PREF: 'ws_pcm' });
  const events = [];
  sclient.addEventListener('tts-started', (e) => events.push('started:' + e.detail.reply_id));

  // 正常一轮
  sclient.dispatch(JSON.stringify({ type: 'tts_started', reply_id: 'r_A', turn_id: 't_1', tts_alignment: false, estimated: true, sentences: [] }));
  sclient.dispatch(JSON.stringify({ type: 'tts_sentence', reply_id: 'r_A', sentence_id: 1, text: '原话第一句' }));
  check('C1 正常 reply 被接受', events.includes('started:r_A') && Object.keys(store.get().replies).includes('r_A'));

  // 打断：服务端 cancel → 封禁
  sclient.dispatch(JSON.stringify({ type: 'cancel', turn_id: 't_1', reason: 'BARGE_IN' }));
  check('C2 cancel 后该 reply 进入封禁集合', sclient.isAborted('r_A'));

  // 上游继续下发旧 reply（真实场景：服务端清缓冲慢 / mock 脚本没停）
  sclient.dispatch(JSON.stringify({ type: 'tts_started', reply_id: 'r_A', turn_id: 't_1', sentences: [] }));
  sclient.dispatch(JSON.stringify({ type: 'tts_sentence', reply_id: 'r_A', sentence_id: 2, text: '原话第二句' }));
  sclient.dispatch(JSON.stringify({ type: 'audio_chunk', reply_id: 'r_A', seq: 1, pcm_b64: '' }));
  const rA = store.get().replies.r_A;
  check('C3 旧 reply 的 tts_started 被丢弃（没有再次 started 事件）',
    events.filter((e) => e === 'started:r_A').length === 1, events);
  check('C4 旧 reply 的 tts_sentence 被丢弃（句子数没变）',
    rA.sentences.length === 1, rA.sentences.length);
  check('C5 丢弃计数有增加（可埋点/排障）', sclient.droppedStaleReply >= 2, sclient.droppedStaleReply);

  // 新一轮
  sclient.dispatch(JSON.stringify({ type: 'tts_started', reply_id: 'r_B', turn_id: 't_2', sentences: [] }));
  check('C6 新 reply 正常接受', events.includes('started:r_B') && Object.keys(store.get().replies).includes('r_B'));
}

/* ---------------- D. 播放时打断阈值，不走普通开口的快路径 ---------------- */

{
  const fixtures = [
    { name: '安静 300 ms', values: Array(15).fill(-68), expected: [false, false, false, false] },
    { name: '轻微背景 300 ms', values: Array(15).fill(-54), expected: [false, false, false, false] },
    { name: '碰麦 60 ms', values: Array(3).fill(-28), expected: [true, false, false, false] },
    { name: '短响动 120 ms', values: Array(6).fill(-35), expected: [true, true, false, false] },
    { name: '短插话 180 ms', values: [-36, -31, -29, -28, -25, -27, -30, -32, -34], expected: [true, true, true, false] },
    { name: '插话 260 ms', values: [-39, -34, -27, -30, -26, -29, -33, -28, -27, -32, -31, -29, -37], expected: [true, true, true, true] },
    { name: '持续突发声 260 ms', values: Array(13).fill(-38), expected: [true, true, true, true] }
  ];
  const durations = [60, 120, 160, 200];
  for (const { name, values, expected } of fixtures) {
    const results = durations.map((ms) => {
      const vad = new LocalVad({ VAD: { BARGE_FRAMES: ms / 20 } });
      const events = values.map((energyDb, i) => vad.push({ energyDb, monoMs: 100 + i * 20, playing: true, refDb: -99 }));
      check(`D ${name} ${ms}ms 无提前 speech_started`, events.every((e) => e.start == null));
      return events.some((e) => e.barge != null);
    });
    check(`D ${name} 60/120/160/200 ms`, results.every((r, i) => r === expected[i]), results);
  }

  const vad = new LocalVad({});
  let ts = 100;
  const feed = (playing, energyDb = -29) => {
    const event = vad.push({ energyDb, monoMs: ts, playing, refDb: -99 });
    ts += 20;
    return event;
  };
  // Use higher energyDb (-20) to stay above adaptive noise floor
  for (let i = 0; i < 7; i++) feed(true, -20);
  feed(false, -75);
  const nextB0 = ts;
  const second = Array.from({ length: 29 }, () => feed(true, -20));
  check('D 分开的播报片段不累计打断帧', second.every((e) => e.barge == null));
  check('D 打断的 b0 仍是这段插话的第一帧', feed(true, -20).barge === nextB0);
  vad.reset();
  feed(true, -20);
  feed(true, -20);
  const idleB0 = ts;
  check('D 播报结束后重新计数', feed(false, -20).start == null && feed(false, -20).start == null);
  check('D 空闲语音仍走 60 ms 开口判断', feed(false, -20).start === idleB0);
}

console.log(fails.length ? `\n❌ ${fails.length} 项失败：\n - ` + fails.join('\n - ') : '\n✅ 全部通过');
process.exit(fails.length ? 1 : 0);
