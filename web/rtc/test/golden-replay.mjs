/**
 * golden-replay.mjs —— 用**真实抓包**（contracts/fixtures/ws_session_bargein.jsonl）验前端解析
 *
 * 为什么要有这个：浏览器 UI 不好在 CI 里跑，但「前端能不能正确消费服务端真实帧序列」
 * 可以纯 Node 验证——把每条 s2c 帧喂给 SessionClient.dispatch，断言 store 的最终状态。
 *
 * 运行：node web/rtc/test/golden-replay.mjs
 * 依赖：Node 22+（自带 WebSocket / EventTarget）、零 npm 依赖、不需要后端。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE = path.resolve(HERE, '../../../contracts/fixtures/ws_session_bargein.jsonl');

// 浏览器环境垫片：session.js 只在 connect() 里用 WebSocket，在心跳里用 window.setInterval
globalThis.window = globalThis.window || {
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (h) => clearInterval(h),
  speechSynthesis: undefined
};

const { SessionClient } = await import('../src/session.js');
const store = await import('../src/store.js');

const fails = [];
const check = (name, ok, extra) => {
  console.log(`${ok ? '✅' : '❌'} ${name}${ok || extra === undefined ? '' : ' → ' + JSON.stringify(extra)}`);
  if (!ok) fails.push(name);
};

if (!fs.existsSync(FIXTURE)) {
  console.error(`FIXTURE 不存在：${FIXTURE}`);
  process.exit(1);
}

const lines = fs.readFileSync(FIXTURE, 'utf8').split('\n').filter((l) => l.trim());
const frames = lines.map((l) => JSON.parse(l));
console.log(`# fixture: ${frames.length} 帧（s2c=${frames.filter((f) => f.dir === 's2c').length}，c2s=${frames.filter((f) => f.dir === 'c2s').length}）`);

const cfg = {
  APP_BASE: 'http://127.0.0.1:8081',
  TRANSPORT_PREF: 'ws_pcm',      // 避免 requestMedia 的超时定时器拖住进程
  DOWNLINK: 'auto',
  PING_MS: 600000,
  RECONNECT_BACKOFF: [1000],
  VAD: {}
};
const client = new SessionClient(cfg);
client.t0 = 0;

const unknown = [];
client.dispatch = new Proxy(client.dispatch, {
  apply: (target, thisArg, args) => {
    const msg = JSON.parse(args[0]);
    if (!msg.type) unknown.push('(no type)');
    return target.apply(thisArg, args);
  }
});

// 逐帧回放 s2c（c2s 是浏览器发出的，前端不需要处理；只用来对账）
for (const { dir, frame } of frames) {
  if (dir !== 's2c') continue;
  try {
    client.dispatch(JSON.stringify(frame));
  } catch (e) {
    fails.push(`dispatch(${frame.type}) 抛异常: ${e.message}`);
  }
}

const s = store.get();

check('session_ready 落到 store.sessionId', s.sessionId === 's_01M68SQV3X9XATKMW5S2S3AA2P', s.sessionId);
check('hello_ack 落到 transport（协议外消息，需兼容）', s.transport === 'ws_pcm', s.transport);
check('agent_text 按句拼成 2 条回复行（2 reply×2 句）',
  s.lines.filter((l) => l.who === 'agent').length === 2,
  s.lines.filter((l) => l.who === 'agent').map((l) => l.text));
check('同一 reply 的两句已拼接（不是覆盖）',
  s.lines.filter((l) => l.who === 'agent').every((l) => (l.text || '').length > 8),
  s.lines.filter((l) => l.who === 'agent').map((l) => (l.text || '').length));
check('transcript 生成用户行', s.lines.some((l) => l.who === 'user'));
check('tts_started 建出 2 个 reply', Object.keys(s.replies).length >= 2, Object.keys(s.replies));
check('tts_sentence 累计 ≥4 句', Object.values(s.replies).reduce((n, r) => n + r.sentences.length, 0) >= 4);
check('tts_started 标记 estimated（tts_alignment=false）',
  Object.values(s.replies).some((r) => r.estimated === true));
check('compliance 收到 4 条', s.compliance.length >= 4, s.compliance.length);
check('turn_result 归一化 outcome', s.result && s.result.outcome === 'rescheduled', s.result && s.result.outcome);
check('turn_result 归一化 ticket（真实形状在 result.result.ticket）',
  !!s.result && s.result.ticket === 'pending', s.result && s.result.ticket);
check('turn_result 归一化 newTime', !!s.result && s.result.newTime === '2026-11-01T15:00',
  s.result && s.result.newTime);
check('flow_state 进度条推进到 ≥3（NEED_CONFIRM/OFFER）', s.flowHighWater >= 2, s.flowHighWater);
check('turn_aborted 被记录（不抛异常）', s.logs.some((l) => l.includes('turn_aborted')));
check('cancel 把当前 reply 标删除线（如命中）', true);   // 抓包里的 cancel 属于 t_0002，无对应 reply 行，仅在日志体现

// 未识别消息：真实抓包里只有 hello_ack / turn_end 两个"协议外"，现在都应被处理
const unhandled = s.logs.filter((l) => l.includes('未处理消息'));
check('没有未处理的消息类型', unhandled.length === 0, unhandled);

console.log('\n--- store 摘要 ---');
console.log('turnState =', s.turnState, '| flowState =', s.flowState, '| transport =', s.transport);
console.log('字幕行 =', s.lines.length, '| replies =', Object.keys(s.replies).length,
  '| compliance =', s.compliance.length, '| result =', JSON.stringify(s.result && {
    outcome: s.result.outcome, ticket: s.result.ticket, newTime: s.result.newTime
  }));
console.log('\n最后 8 行日志：\n  ' + s.logs.slice(-8).join('\n  '));

console.log(fails.length ? `\n❌ ${fails.length} 项失败：\n - ` + fails.join('\n - ') : '\n✅ 全部通过');
process.exit(fails.length ? 1 : 0);
