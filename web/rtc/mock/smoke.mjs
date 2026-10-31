/**
 * smoke.mjs —— 无浏览器的契约冒烟（Node 22+ 自带 WebSocket）
 *
 * 验证：POST /sessions → WS 握手 → hello → 脚本序列 → media 503(C1) → 两轮 turn → 打断 → ping/pong → 挂断
 * 配合 FAST=1 启动 mock（脚本延时压到 15%），约 6 秒跑完。
 *
 * 用法：
 *   FAST=1 node mock/mock-server.mjs      # 另一个终端
 *   node mock/smoke.mjs
 */
const BASE = process.env.BASE || 'http://127.0.0.1:8099';
const seq = [];
const fails = [];

const res = await fetch(`${BASE}/sessions`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ mode: 'voice', scenario_id: 'service_outbound' })
});
const sess = await res.json();
check('POST /sessions 200', res.ok);
check('返回 session_id', !!sess.session_id);
check('返回完整 ws_url（含 token）', /\/ws\/session\/.+\?token=/.test(sess.ws_url || ''));
check('返回 version_set.release_id', !!(sess.version_set && sess.version_set.release_id));

const wsUrl = new URL(sess.ws_url, BASE);
wsUrl.protocol = wsUrl.protocol === 'https:' ? 'wss:' : 'ws:';
const ws = new WebSocket(wsUrl);
/** @type {any[]} */
const msgs = [];
ws.onmessage = (e) => {
  if (typeof e.data !== 'string') return;
  try { const m = JSON.parse(e.data); msgs.push(m); seq.push(m.type); } catch { /* noop */ }
};
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });

const send = (o) => ws.send(JSON.stringify(o));
const types = () => msgs.map((m) => m.type);
const count = (t) => types().filter((x) => x === t).length;

send({ type: 'hello', session_id: sess.session_id, client_version: 'web@0.2.1', capabilities: { local_vad: true } });
await sleep(1900);
check('收到 session_ready', count('session_ready') === 1);
check('收到 state（GREETING/OPEN）', count('state') >= 1);
check('收到 agent_text（开场白）', count('agent_text') >= 1);
check('收到 tts_started', count('tts_started') >= 1);
check('收到 tts_sentence ×2', count('tts_sentence') >= 2);
check('收到 tts_completed', count('tts_completed') >= 1);
const started = msgs.find((m) => m.type === 'tts_started');
check('tts_started 带 tts_alignment=false / estimated=true / cursor=null',
  !!started && started.tts_alignment === false && started.estimated === true && started.cursor === null);
const sent = msgs.find((m) => m.type === 'tts_sentence');
check('tts_sentence 带 start_ms/end_ms/char_start/char_end/estimated',
  !!sent && ['start_ms', 'end_ms', 'char_start', 'char_end', 'estimated'].every((k) => k in sent));

send({ type: 'media_request' });
await sleep(250);
check('media_request → error(MEDIA_NOT_CONFIGURED)（C1 演练）',
  msgs.some((m) => m.type === 'error' && m.error && m.error.code === 'MEDIA_NOT_CONFIGURED'));

send({ type: 'speech_ended', client_mono_ms: 100, last_voiced_mono_ms: 60 });
await sleep(1700);
check('第 1 轮 → compliance（REWRITE）', msgs.some((m) => m.type === 'compliance' && m.decision === 'REWRITE'));
check('第 1 轮 flow_state=NEED_CONFIRM', msgs.some((m) => m.type === 'state' && m.flow_state === 'NEED_CONFIRM'));

send({ type: 'speech_ended', client_mono_ms: 200, last_voiced_mono_ms: 160 });
await sleep(1900);
check('第 2 轮 → tool_progress（calling/ok）', count('tool_progress') >= 2);
check('第 2 轮 flow_state=OFFER', msgs.some((m) => m.type === 'state' && m.flow_state === 'OFFER'));

send({
  type: 'local_muted', turn_id: 't_1002', reply_id: 'r_1002_1',
  client_mono_ms: 900, b0_mono_ms: 860, last_frame_played_mono_ms: 900,
  stop_latency_ms: 40, played_ms: 300, sentence_id: 1, char_idx: 7, reason: 'barge_in'
});
await sleep(350);
check('打断 → cancel + turn_aborted(BARGE_IN)',
  count('cancel') >= 1 && msgs.some((m) => m.type === 'turn_aborted' && m.reason === 'BARGE_IN'));

send({ type: 'playback_first_frame', reply_id: 'r_1002_1', title: null, client_mono_ms: 950, audio_ctx_time: 12.3 });
send({ type: 'client_event', name: 'NetStats', payload: { rtt_ms: 12, loss_pct: 0.2 } });
send({ type: 'client_event', name: 'NotInWhitelist', payload: {} });
send({ type: 'ping', t: Date.now(), client_mono_ms: 1 });
await sleep(350);
check('client_event 被受理', msgs.some((m) => m.type === 'debug_client_event' && m.name === 'NetStats'));
check('ping → pong（回显 client_mono_ms）',
  msgs.some((m) => m.type === 'pong' && typeof m.client_mono_ms === 'number'));

send({ type: 'hangup', reason: 'user_click' });
await sleep(500);
check('hangup → session_ended', count('session_ended') === 1);

try { ws.close(); } catch { /* noop */ }

console.log('\n消息序列（S→C）：\n  ' + seq.join(' → '));
console.log('\n统计：agent_text=' + count('agent_text') + ' tts_started=' + count('tts_started')
  + ' tts_sentence=' + count('tts_sentence') + ' tts_completed=' + count('tts_completed')
  + ' state=' + count('state') + ' tool_progress=' + count('tool_progress'));
console.log(fails.length ? `\n❌ ${fails.length} 项失败：\n - ` + fails.join('\n - ') : '\n✅ 全部通过');
process.exit(fails.length ? 1 : 0);

function check(name, ok) { console.log(`${ok ? '✅' : '❌'} ${name}`); if (!ok) fails.push(name); }
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
