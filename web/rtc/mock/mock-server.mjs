/**
 * mock-server.mjs —— 零依赖的契约 Mock（v0.2.1）
 *
 * 作用：在真后端跑起来之前，让 frontend 走完整链路。
 *   POST /sessions                      → {session_id, token, ws_url, version_set}
 *   WS   /ws/session/{id}?token=...     → hello/session_ready/state/transcript/agent_text/
 *                                          tts_started/tts_sentence/tts_completed/audio_chunk?/
 *                                          compliance/tool_progress/turn_result/turn_aborted/
 *                                          transport_switched/session_ended/pong
 *   GET  /                              → 静态托管 web/（方便同机 localhost 打开，麦克风可用）
 *   JSON {code:"MEDIA_NOT_CONFIGURED"}  → 用于验证 C1「静默回落 ws_pcm」
 *
 * 运行：node mock/mock-server.mjs            （默认 8099）
 * 打开：http://127.0.0.1:8099/?app=http://127.0.0.1:8099
 */
import http from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const PORT = Number(process.env.PORT || 8099);
/** FAST=1：把所有脚本延时压到 15%，供 smoke/E2E 快速跑（不影响协议时序形状） */
const SCALE = process.env.FAST === '1' ? 0.15 : 1;
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.md': 'text/markdown; charset=utf-8', '.wav': 'audio/wav'
};

/* ============================ 静态文件 ============================ */

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);

  if (req.method === 'POST' && url.pathname === '/sessions') {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      const sid = 's_' + crypto.randomBytes(6).toString('hex');
      const token = crypto.randomBytes(8).toString('hex');
      json(res, 200, {
        session_id: sid,
        token,
        mode: (safeParse(body) || {}).mode || 'voice',
        ws_url: `/ws/session/${sid}?token=${token}`,      // 相对也支持：前端会按 APP_BASE 解析
        version_set: {
          release_id: 'rel_mock_01', flow: 'service_outbound@1.0.0',
          prompt: 'agent_system@12', knowledge: 'faq@mock', model: 'fake', config: 'runtime@1'
        }
      });
    });
    return;
  }

  if (req.method === 'POST' && /^\/sessions\/[^/]+\/(end|hangup)$/.test(url.pathname)) { return json(res, 200, { ok: true }); }
  if (req.method === 'POST' && /^\/sessions\/[^/]+\/rtc$/.test(url.pathname)) {
    return json(res, 503, { error: { code: 'MEDIA_NOT_CONFIGURED', message: 'LiveKit 未配置', retryable: false, severity: 'warn', user_message: '当前为兼容模式' } });
  }
  if (url.pathname === '/healthz') return json(res, 200, { status: 'ok', mode: 'mock' });

  // 静态
  let p = url.pathname === '/' ? '/index.html' : url.pathname;
  const file = path.join(ROOT, decodeURIComponent(p));
  if (!file.startsWith(ROOT)) { res.writeHead(403); return res.end('forbidden'); }
  fs.readFile(file, (err, buf) => {
    if (err) { res.writeHead(404); return res.end('not found: ' + p); }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
    res.end(buf);
  });
});

/* ============================ WebSocket（手写，无依赖） ============================ */

server.on('upgrade', (req, socket) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  if (!url.pathname.startsWith('/ws/session/')) { socket.destroy(); return; }
  if (!url.searchParams.get('token')) {
    socket.end('HTTP/1.1 441 Unauthorized\r\n\r\n');
    return;
  }
  const key = req.headers['sec-websocket-key'];
  const accept = crypto.createHash('sha1').update(key + GUID).digest('base64');
  socket.write(
    'HTTP/1.1 101 Switching Protocols\r\n' +
    'Upgrade: websocket\r\nConnection: Upgrade\r\n' +
    `Sec-WebSocket-Accept: ${accept}\r\n\r\n`
  );

  const sessionId = url.pathname.split('/').pop();
  new Conn(socket, sessionId);          // 构造函数里挂 data/close 处理器
});

class Conn {
  constructor(socket, sessionId) {
    this.socket = socket;
    this.sessionId = sessionId;
    this.buf = Buffer.alloc(0);
    this.s2cSeq = 1;
    this.closed = false;
    this.replyId = null;
    this.turnN = 0;
    this.scriptTimers = [];          // 脚本推进用的定时器（打断时必须全部取消）
    this.scriptAborted = false;      // 当前 reply 已被打断：后续脚本步骤一律空转

    socket.on('data', (chunk) => { this.buf = Buffer.concat([this.buf, chunk]); this.drain(); });
    socket.on('close', () => { this.closed = true; });
    socket.on('error', () => { this.closed = true; });
  }

  drain() {
    while (true) {
      const f = decodeFrame(this.buf);
      if (!f) return;
      this.buf = this.buf.subarray(f.consumed);
      if (f.opcode === 0x8) { this.close(); return; }
      if (f.opcode === 0x9) { this.raw(0xa, f.payload); continue; }
      if (f.opcode === 0x2) { this.onBinary(f.payload); continue; }
      if (f.opcode === 0x1) this.onText(f.payload.toString('utf8'));
    }
  }

  onBinary(b) {
    // 上行音频帧：0xA1 + 16B 头 + 640B PCM（Mock 只计数，不做 ASR）
    if (b[0] === 0xa1) {
      this.audioFrames = (this.audioFrames || 0) + 1;
      if (this.audioFrames % 50 === 0) this.send('debug_audio_frames', { count: this.audioFrames });
    }
  }

  onText(txt) {
    let m; try { m = JSON.parse(txt); } catch { return; }
    switch (m.type) {
      case 'hello':
        this.send('session_ready', {
          session_id: this.sessionId,
          version_set: { release_id: 'rel_mock_01', flow: 'service_outbound@1.0.0' },
          timeouts: { listening_silence_ms: 10000, session_max_ms: 180000, wrapup_ms: 3000 }
        });
        this.scriptGreeting();
        break;

      case 'media_request':
        // C1：验证前端「静默回落 ws_pcm」
        this.send('error', { error: { code: 'MEDIA_NOT_CONFIGURED', message: 'LiveKit 未配置（Mock）', retryable: false, severity: 'warn', user_message: '当前为兼容模式' } });
        break;

      case 'speech_started': this.send('state', { turn_state: 'USER_SPEAKING', flow_state: this.flow || 'IDENTITY_CONFIRM', slots: this.slots || {} }); break;
      case 'speech_ended': this.scriptTurn(++this.turnN); break;

      case 'local_muted':
        // 打断：**先停脚本**（否则被"打断"的这一轮还会继续下发 tts_*/agent_text，
        // 前端会以为服务端没取消，看起来像"打断之后继续播报原内容"）
        this.clearScript();
        this.scriptAborted = true;
        this.send('cancel', { turn_id: m.turn_id, reason: 'BARGE_IN' });
        this.send('turn_aborted', { turn_id: m.turn_id, reason: 'BARGE_IN' });
        this.send('state', { turn_state: 'LISTENING', flow_state: this.flow || 'IDENTITY_CONFIRM', slots: this.slots || {} });
        break;

      case 'playback_first_frame': this.send('debug_latency', { note: '收到 playback_first_frame', reply_id: m.reply_id }); break;
      case 'playback_ended': break;
      case 'client_event': this.send('debug_client_event', { name: m.name, allowed: true }); break;
      case 'ping': this.send('pong', { t: m.t, client_mono_ms: m.client_mono_ms, server_rx: Date.now(), server_tx: Date.now() }); break;
      case 'hangup': this.send('session_ended', { reason: m.reason || 'user_click', duration_ms: 0, turns: 3 }); this.close(); break;
      case 'transport_degraded': this.send('transport_switched', { to: 'ws_pcm', audio: { uplink: 'pcm16/16000', downlink: 'pcm16/24000' } }); break;
      default: break;
    }
  }

  /* ---------- 脚本：把 03/11 的消息顺序原样演一遍 ---------- */

  scriptGreeting() {
    this.scriptAborted = false;                 // 新一轮脚本开始，解除打断锁
    this.at(250, () => this.send('state', { turn_state: 'GREETING', flow_state: 'OPEN', slots: {} }));
    this.at(500, () => {
      const reply = 'r_greet_1';
      this.replyId = reply;
      this.send('agent_text', { turn_id: 't_0001', reply_id: reply, sentence_id: 1, text: '您好，我是配送中心的智能助理，本次通话会录音。请问是王先生本人吗？', final: true });
      this.tts(reply, 't_0001', ['您好，我是配送中心的智能助理，本次通话会录音。', '请问是王先生本人吗？'], 0);
    });
  }

  scriptTurn(n) {
    this.clearScript();                         // 上一轮残留（若被打断）一并清掉
    this.scriptAborted = false;
    const turnId = 't_' + String(1000 + n);
    this.send('state', { turn_state: 'THINKING', flow_state: this.flow || 'NEED_CONFIRM', slots: this.slots || {} });
    const reply = 'r_' + turnId.slice(2) + '_1';
    this.replyId = reply;
    const plan = planFor(n);
    this.flow = plan.flow;
    Object.assign(this.slots = this.slots || {}, plan.slots);

    this.at(200, () => this.send('state', { turn_state: 'SPEAKING', flow_state: plan.flow, slots: this.slots }));
    this.at(300, () => {
      plan.sentences.forEach((text, i) => {
        this.at(i * 60, () => this.send('agent_text', { turn_id: turnId, reply_id: reply, sentence_id: i + 1, text, final: i === plan.sentences.length - 1 }));
      });
      this.tts(reply, turnId, plan.sentences, 0);
    });
    if (plan.tool) {
      const callId = 'c_' + turnId.slice(2);
      this.at(600, () => this.send('tool_progress', { call_id: callId, tool: plan.tool, state: 'calling' }));
      this.at(1100, () => this.send('tool_progress', { call_id: callId, tool: plan.tool, state: 'ok', latency_ms: 138 }));
    }
    if (plan.compliance) this.at(700, () => this.send('compliance', plan.compliance));
    if (plan.result) {
      this.at(1500, () => this.send('turn_result', { turn_id: turnId, outcome: plan.result.outcome, slots: this.slots, result: plan.result.result }));
      this.at(1700, () => this.send('state', { turn_state: 'WRAP_UP', flow_state: 'CLOSE', slots: this.slots }));
    } else {
      this.at(1400, () => this.send('state', { turn_state: 'LISTENING', flow_state: plan.flow, slots: this.slots }));
    }
  }

  /** 按 tts_started → 逐句 tts_sentence → tts_completed 发（tts_alignment=false：start/end 为估算） */
  tts(replyId, turnId, sentences, delay) {
    this.at(delay, () => this.send('tts_started', {
      turn_id: turnId, reply_id: replyId, sample_rate: 24000, channels: 1,
      tts_alignment: false, estimated: true, cursor: null, sentences: []
    }));
    let t = 0;
    let charOffset = 0;
    sentences.forEach((text, i) => {
      const dur = Math.max(700, text.length * 180);        // 估算：每秒约 5.5 字
      const s = {
        turn_id: turnId, reply_id: replyId, sentence_id: i + 1, text,
        start_ms: t, end_ms: t + dur,
        char_start: charOffset, char_end: charOffset + text.length,
        estimated: true, is_last: i === sentences.length - 1
      };
      this.at(delay + 40 + i * 30, () => this.send('tts_sentence', s));
      t += dur;
      charOffset += text.length;
    });
    this.at(delay + 120 + t, () => this.send('tts_completed', { turn_id: turnId, reply_id: replyId, total_ms: t, interrupted: false, estimated: true }));
  }

  at(ms, fn) {
    const id = setTimeout(() => {
      if (this.closed) return;
      if (this.scriptAborted) return;          // 当前 reply 已被打断：旧脚本不得继续推进
      try { fn(); } catch { /* noop */ }
    }, Math.max(0, Math.round(ms * SCALE)));
    this.scriptTimers.push(id);
  }

  /** 打断时清空所有待执行的脚本步骤（真实后端会在收到 interrupt 时停 TTS 推流） */
  clearScript() {
    for (const id of this.scriptTimers) clearTimeout(id);
    this.scriptTimers = [];
  }

  send(type, data) {
    this.raw(0x1, Buffer.from(JSON.stringify({ type, seq: this.s2cSeq++, ...data }), 'utf8'));
  }

  raw(opcode, payload) {
    if (this.closed || this.socket.destroyed) return;
    try { this.socket.write(encodeFrame(opcode, payload)); } catch { this.closed = true; }
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    try { this.socket.write(encodeFrame(0x8, Buffer.alloc(0))); this.socket.end(); } catch { /* noop */ }
  }
}

/* ============================ 帧编解码 ============================ */

function decodeFrame(buf) {
  if (buf.length < 2) return null;
  const opcode = buf[0] & 0x0f;
  const masked = (buf[1] & 0x80) !== 0;
  let len = buf[1] & 0x7f;
  let off = 2;
  if (len === 126) { if (buf.length < 4) return null; len = buf.readUInt16BE(2); off = 4; }
  else if (len === 127) { if (buf.length < 10) return null; len = Number(buf.readBigUInt64BE(2)); off = 10; }
  const maskLen = masked ? 4 : 0;
  if (buf.length < off + maskLen + len) return null;
  let payload = buf.subarray(off + maskLen, off + maskLen + len);
  if (masked) {
    const mask = buf.subarray(off, off + 4);
    const out = Buffer.allocUnsafe(len);
    for (let i = 0; i < len; i++) out[i] = payload[i] ^ mask[i & 3];
    payload = out;
  }
  return { opcode, payload, consumed: off + maskLen + len };
}

function encodeFrame(opcode, payload) {
  const len = payload.length;
  let head;
  if (len < 126) { head = Buffer.allocUnsafe(2); head[1] = len; }
  else if (len < 65536) { head = Buffer.allocUnsafe(4); head[1] = 126; head.writeUInt16BE(len, 2); }
  else { head = Buffer.allocUnsafe(10); head[1] = 127; head.writeBigUInt64BE(BigInt(len), 2); }
  head[0] = 0x80 | opcode;
  return Buffer.concat([head, payload]);
}

/* ============================ 脚本内容 ============================ */

function planFor(n) {
  if (n === 1) return {
    flow: 'NEED_CONFIRM',
    slots: { order_id: 'SO20261031001' },
    sentences: ['我这边看到您有一单今天下午的配送，', '想跟您确认一下今天还方便收货吗？'],
    compliance: { scope: 'sentence', decision: 'REWRITE', rule_ids: ['false_promise'], before: '保证今天一定送到', after: '以实际配送结果为准', severity: 'warn' }
  };
  if (n === 2) return {
    flow: 'OFFER',
    slots: { new_time: '2026-11-03T14:00+08:00' },
    sentences: ['可以的，我帮您改到明天下午三点，', '快递员到前会给您电话。'],
    tool: 'order.query'
  };
  return {
    flow: 'RESULT_CONFIRM',
    slots: { consent: true },
    sentences: ['已经帮您改约成功，稍后会收到短信。', '祝您生活愉快，再见。'],
    tool: 'order.reschedule',
    result: { outcome: 'rescheduled', result: { ticket_id: 'WO20261103007' } }
  };
}

/* ============================ 启动 ============================ */

function json(res, code, obj) {
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Access-Control-Allow-Origin': '*' });
  res.end(JSON.stringify(obj));
}
function safeParse(s) { try { return JSON.parse(s); } catch { return null; } }

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[mock] http://127.0.0.1:${PORT}/?app=http://127.0.0.1:${PORT}`);
  console.log(`[mock] POST /sessions · WS /ws/session/{id}?token= · MEDIA_NOT_CONFIGURED 演练 C1`);
});
