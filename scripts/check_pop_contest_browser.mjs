// Real browser, real audio playback scheduling, synthetic WAV input.
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { createRequire } from 'node:module';

const [base, output, transport = 'ws_pcm', followup = 'reason', endpointArg] = process.argv.slice(2);
if (!base || !output) throw new Error('Usage: node scripts/check_pop_contest_browser.mjs BASE OUTPUT');
const endpointMs = endpointArg === undefined ? null : Number(endpointArg);
if (endpointMs !== null && (followup !== 'pauses' || !Number.isInteger(endpointMs) || endpointMs < 200 || endpointMs > 1200)) {
  throw new Error('Endpoint override is available for pause diagnostics (200–1200 ms).');
}
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({
  executablePath: process.env.CONTEST_CHROME || undefined,
  headless: true,
  args: ['--autoplay-policy=no-user-gesture-required', '--disable-features=WebRtcHideLocalIpsWithMdns'],
});
const page = await browser.newPage();
if (endpointMs !== null) {
  await page.addInitScript(ms => {
    Object.defineProperty(window, '__AICALL_CONFIG__', {
      configurable: true,
      set(config) {
        config.VAD.END_FRAMES = Math.ceil(ms / config.FRAME_MS);
        Object.defineProperty(window, '__AICALL_CONFIG__', { value: config, configurable: true, writable: true });
      },
    });
  }, endpointMs);
}
const server = [];
const errors = [];
const measurements = [];
const client = [];
const uploads = [];
const openings = [];
const identityChecks = [];
const pauseChecks = [];
const textChecks = [];
const scenarioChecks = [];
let capturedAudio = null;
let noMatch = null;
page.on('pageerror', error => errors.push(String(error)));
function recordServer(raw) {
  if (typeof raw !== 'string') return;
  const message = JSON.parse(raw);
  if (capturedAudio && message.type === 'audio_chunk' && message.pcm_b64) capturedAudio.push(message);
  if (message.type === 'rtc_pcm_answer') delete message.sdp;
  if (message.type !== 'audio_chunk' && message.type !== 'transcript') server.push(message);
  else if (message.type === 'transcript' && message.final) server.push(message);
}
await page.exposeFunction('recordContestServer', recordServer);
page.on('websocket', socket => {
  socket.on('framereceived', frame => recordServer(frame.payload));
});
await page.addInitScript(() => {
  window.contestWire = [];
  window.contestUploads = { rtc_pcm: 0, ws_pcm: 0 };
  function watch(data, channel) {
    if (typeof data === 'string') {
      const msg = JSON.parse(data);
      if (['call_ready', 'speech_ended', 'playback_first_frame', 'playback_ended', 'local_muted', 'speech_started', 'text'].includes(msg.type)) {
        window.contestWire.push({ at: performance.now(), ...msg });
      }
    } else window.contestUploads[channel]++;
  }
  const send = WebSocket.prototype.send;
  WebSocket.prototype.send = function (data) {
    watch(data, 'ws_pcm');
    return send.call(this, data);
  };
  const rtcSend = RTCDataChannel.prototype.send;
  RTCDataChannel.prototype.send = function (data) {
    watch(data, 'rtc_pcm');
    return rtcSend.call(this, data);
  };
  const create = RTCPeerConnection.prototype.createDataChannel;
  RTCPeerConnection.prototype.createDataChannel = function (...options) {
    const channel = create.apply(this, options);
    let fragments = '';
    channel.addEventListener('message', ({ data }) => {
      if (data[0] === '=') {
        fragments = '';
        window.recordContestServer(data.slice(1));
      } else if (data[0] === '[') fragments = data.slice(1);
      else if (fragments) {
        fragments += data.slice(1);
        if (data[0] === '!') {
          window.recordContestServer(fragments);
          fragments = '';
        }
      }
    });
    return channel;
  };
});

async function waitForServer(type, start) {
  const deadline = performance.now() + 22000;
  while (performance.now() < deadline) {
    const recent = server.slice(start);
    const error = recent.find(row => row.type === 'error' && row.error?.code !== 'ASR_NO_MATCH');
    if (error) throw new Error(JSON.stringify(error));
    const match = recent.find(row => row.type === type);
    if (match) return match;
    await new Promise(ready => setTimeout(ready, 30));
  }
  throw new Error('Timed out waiting for ' + type);
}

async function connect(caseId) {
  const start = server.length;
  await page.goto(base + '/rtc/?downlink=pcm&transport=' + transport);
  if (caseId) await page.locator('#contestCase').selectOption(caseId);
  await page.locator('#inputSource').selectOption('file');
  await page.locator('#btnStart').click();
  await page.locator('#audioFile').waitFor({ state: 'visible' });
  await page.waitForFunction(() => !document.getElementById('audioFile').disabled);
  await page.waitForFunction(expected => document.getElementById('transport').textContent.includes(expected), transport);
  return await waitForServer('session_ready', start);
}

async function finishOpening(session, start) {
  const result = await waitForServer('turn_result', start);
  const turn = result.turn_id;
  if (result.result.outcome !== 'pending_identity' || server.slice(start).some(
    row => row.type === 'transcript' && row.turn_id === turn)) {
    throw new Error('The outbound opening must ask for identity without customer input');
  }
  await page.waitForFunction(id => window.contestWire.some(
    row => row.type === 'playback_ended' && row.turn_id === id), turn, { timeout: 20000 });
  const observed = await page.evaluate(id => ({
    ready: window.contestWire.find(row => row.type === 'call_ready'),
    first: window.contestWire.find(row => row.type === 'playback_first_frame' && row.turn_id === id),
    uploads: window.contestUploads,
  }), turn);
  if (!observed.ready || !observed.first || Object.values(observed.uploads).some(count => count !== 0)) {
    throw new Error('Opening playback must start before any customer audio upload');
  }
  const opening = {
    session_id: session.session_id, turn,
    ready_to_browser_playback_ms: Math.round((observed.first.at - observed.ready.at) * 100) / 100,
    uploads_before_opening: observed.uploads,
  };
  openings.push(opening);
  console.log('Outbound opening', opening.ready_to_browser_playback_ms, 'ms');
}

async function speak(session, filename) {
  const start = server.length;
  const localStart = await page.evaluate(() => window.contestWire.length);
  await page.locator('#audioFile').setInputFiles(resolve(output, 'input_audio', filename));
  const result = await waitForServer('turn_result', start);
  const input = server.slice(start).find(row => row.type === 'transcript' && row.turn_id === result.turn_id);
  if (!input) throw new Error('No correlated final transcript for ' + filename);
  await page.waitForFunction(([offset, turn]) => window.contestWire.slice(offset).some(
    row => row.type === 'playback_ended' && row.turn_id === turn), [localStart, result.turn_id], { timeout: 26000 });
  const local = await page.evaluate(offset => window.contestWire.slice(offset), localStart);
  const first = local.find(row => row.type === 'playback_first_frame' && row.turn_id === result.turn_id);
  const end = local.find(row => row.type === 'speech_ended' && row.at <= first?.at);
  const proxy = first && end && end.last_voiced_mono_ms > 0
    ? Math.round((first.at - end.last_voiced_mono_ms) * 100) / 100 : null;
  const row = { session_id: session.session_id, turn: result.turn_id, input_file: filename, asr: input.text,
                outcome: result.result.outcome, last_voiced_to_browser_playback_proxy_ms: proxy };
  console.log(filename, row.asr, row.outcome, proxy);
  return row;
}

async function checkCall() {
  const start = server.length;
  const session = await connect();
  await finishOpening(session, start);
  const conversation = [['turn-2.wav', 'pending_need'], ['turn-3.wav', 'pending_result']];
  if (existsSync(resolve(output, 'input_audio', 'turn-followup.wav'))) {
    conversation.push(['turn-followup.wav', 'pending_result']);
  }
  conversation.push(['turn-4.wav', 'pop_contact_recorded']);
  for (const [file, expected] of conversation) {
    const result = await speak(session, file);
    measurements.push(result);
    if (result.outcome !== expected) {
      throw new Error('Unexpected phase outcome: ' + JSON.stringify({ ...result, expected }));
    }
    if (file === 'turn-followup.wav') {
      const spoken = server.filter(row => row.type === 'agent_text' && row.turn_id === result.turn)
        .map(row => row.text).join('');
      const expected = ['guidance', 'guidance-only'].includes(followup)
        ? ['打开京麦', '原申请', '上传有效材料'] : ['这次退回是因为'];
      if (!expected.every(term => spoken.includes(term))) {
        throw new Error('Follow-up missed required grounded guidance: ' + spoken);
      }
    }
  }
  await page.waitForFunction(() => document.getElementById('result').textContent.includes('contact-'));
  await page.screenshot({ path: join(output, 'browser-writeback.png') });
  await page.locator('#btnHangup').click();
  if (await page.locator('#inputSource').isDisabled()) throw new Error('Input selector must reopen after hangup');
  client.push(await page.evaluate(() => window.contestWire));
  uploads.push(await page.evaluate(() => window.contestUploads));
  if (followup === 'guidance-only') return;

  for (const [file, expected] of [['turn-2-repeat.wav', 'pending_need'], ['turn-2-denied.wav', 'safe_exit']]) {
    const start = server.length;
    const contact = await connect();
    await finishOpening(contact, start);
    const row = await speak(contact, file);
    identityChecks.push(row);
    if (row.outcome !== expected) throw new Error('Unexpected identity result: ' + JSON.stringify(row));
    await page.locator('#btnHangup').click();
    client.push(await page.evaluate(() => window.contestWire));
    uploads.push(await page.evaluate(() => window.contestUploads));
  }

  const veryShort = resolve(output, 'input_audio', 'turn-2-too-short.wav');
  if (existsSync(veryShort)) {
    const start = server.length;
    const contact = await connect();
    await finishOpening(contact, start);
    await page.locator('#audioFile').setInputFiles(veryShort);
    await page.waitForFunction(() => document.getElementById('status').textContent.includes('没有听清'));
    const recognized = await speak(contact, 'turn-2-repeat.wav');
    if (recognized.outcome !== 'pending_need') throw new Error('Could not retry after an empty transcript');
    noMatch = { session_id: contact.session_id, recovered_asr: recognized.asr, retry_turn: recognized.turn };
    await page.locator('#btnHangup').click();
    client.push(await page.evaluate(() => window.contestWire));
    uploads.push(await page.evaluate(() => window.contestUploads));
  }

  const second = server.length;
  await connect();
  await page.waitForFunction(() => window.contestWire.some(
    row => row.type === 'playback_first_frame'), undefined, { timeout: 14000 });
  await page.locator('#btnBarge').click();
  const cancelled = await waitForServer('cancel', second);
  const local = await page.evaluate(() => window.contestWire);
  if (!local.some(row => row.type === 'local_muted' && row.turn_id === cancelled.turn_id)) {
    throw new Error('Browser mute event missing');
  }
  console.log('Browser manual interruption', cancelled.turn_id, cancelled.reason);
  await page.locator('#btnHangup').click();
}

async function checkResilient() {
  const start = server.length;
  const session = await connect();
  await finishOpening(session, start);
  if (!(await page.locator('#version').textContent()).includes('pop-contest-resilient-')) {
    throw new Error('Browser did not load the resilient release');
  }
  const steps = [
    {text: '哪一家店？', outcome: 'pending_identity', anchors: ['简称', '负责人'], excludes: ['未通过', '到期']},
    {file: 'turn-2.wav', outcome: 'pending_need'},
    {text: '什么资质审核？', outcome: 'pending_need', anchors: ['证明材料', '方便'], excludes: ['未通过', '到期']},
    {file: 'turn-3.wav', outcome: 'pending_result'},
    {text: '什么东西给我退回了？', outcome: 'pending_result', anchors: ['店铺资质申请', '主体资质到期']},
    {text: '主体资质是什么？', outcome: 'pending_result', anchors: ['证明谁在开店', '比如营业执照']},
    {text: '那是不是必须交营业执照？', outcome: 'pending_result', anchors: ['无法确认', '原申请']},
    {text: '拜拜再见。', outcome: 'safe_exit', anchors: ['再见'], excludes: ['已记录']},
  ];
  for (const step of steps) {
    if (step.file) {
      const row = await speak(session, step.file);
      measurements.push(row);
      if (row.outcome !== step.outcome) throw new Error(JSON.stringify(row));
      continue;
    }
    const offset = server.length;
    const localStart = await page.evaluate(() => window.contestWire.length);
    await page.locator('#txtInput').fill(step.text);
    await page.locator('#btnSend').click();
    const result = await waitForServer('turn_result', offset);
    await page.waitForFunction(([index, turn]) => window.contestWire.slice(index).some(
      row => row.type === 'playback_ended' && row.turn_id === turn), [localStart, result.turn_id]);
    const local = await page.evaluate(index => window.contestWire.slice(index), localStart);
    const sent = local.find(row => row.type === 'text');
    const first = local.find(row => row.type === 'playback_first_frame' && row.turn_id === result.turn_id);
    const reply = server.slice(offset).filter(row => row.type === 'agent_text' && row.turn_id === result.turn_id)
      .map(row => row.text).join('');
    const row = { session_id: session.session_id, turn: result.turn_id, text: step.text, reply,
      outcome: result.result.outcome, text_to_browser_playback_ms: sent && first ? Math.round((first.at - sent.at) * 10) / 10 : null };
    textChecks.push(row);
    console.log(JSON.stringify(row));
    if (row.outcome !== step.outcome || row.text_to_browser_playback_ms == null || row.text_to_browser_playback_ms >= 2000 ||
        !step.anchors.every(word => reply.includes(word)) || (step.excludes || []).some(word => reply.includes(word))) {
      throw new Error('Resilient browser check failed: ' + JSON.stringify(row));
    }
  }
  client.push(await page.evaluate(() => window.contestWire));
  uploads.push(await page.evaluate(() => window.contestUploads));
  await page.locator('#btnHangup').click();
}

async function checkPauses() {
  const manifest = JSON.parse(readFileSync(join(output, 'input_audio', 'manifest.json'), 'utf8'));
  for (const { file, gap_ms } of manifest.samples) {
    const openingStart = server.length;
    const contact = await connect();
    await finishOpening(contact, openingStart);
    if ((await speak(contact, 'turn-2.wav')).outcome !== 'pending_need' ||
        (await speak(contact, 'turn-3.wav')).outcome !== 'pending_result') {
      throw new Error('Could not reach the verified follow-up phase');
    }
    const start = server.length;
    const logCount = await page.locator('#log').evaluate(el => el.textContent.split('回放结束').length);
    const localStart = await page.evaluate(() => window.contestWire.length);
    capturedAudio = [];
    await page.locator('#audioFile').setInputFiles(join(output, 'input_audio', file));
    await page.waitForFunction(count => document.getElementById('log').textContent.split('回放结束').length > count, logCount);
    // File EOF also sends speech_ended. Let that last ASR flush settle before
    // collecting replies. Failures in a single probe are evidence for comparison.
    const deadline = performance.now() + 12000;
    while (performance.now() < deadline) {
      const recent = server.slice(start);
      const latest = recent.findLast(row => row.type === 'transcript' && row.final);
      const terminal = recent.findLast(row => row.turn_id === latest?.turn_id &&
        ['turn_result', 'cancel', 'error'].includes(row.type));
      if (terminal && performance.now() > deadline - 11000) break;
      await new Promise(ready => setTimeout(ready, 50));
    }
    const turns = server.slice(start).filter(row => row.type === 'transcript' && row.final);
    const results = server.slice(start).filter(row => row.type === 'turn_result');
    const interruptions = server.slice(start).filter(row => ['error', 'cancel'].includes(row.type));
    const local = await page.evaluate(offset => window.contestWire.slice(offset), localStart);
    const playback = [];
    for (const turn_id of new Set(capturedAudio.map(row => row.turn_id))) {
      const chunks = capturedAudio.filter(row => row.turn_id === turn_id)
        .sort((a, b) => a.seq - b.seq);
      const pcm = Buffer.concat(chunks.map(row => Buffer.from(row.pcm_b64, 'base64')));
      const rate = chunks[0].sample_rate;
      const header = Buffer.alloc(44);
      header.write('RIFF', 0); header.writeUInt32LE(pcm.length + 36, 4); header.write('WAVEfmt ', 8);
      header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20); header.writeUInt16LE(1, 22);
      header.writeUInt32LE(rate, 24); header.writeUInt32LE(rate * 2, 28);
      header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34); header.write('data', 36);
      header.writeUInt32LE(pcm.length, 40);
      const name = `agent-pause-${gap_ms}-${turn_id}.wav`;
      writeFileSync(join(output, name), Buffer.concat([header, pcm]));
      playback.push(name);
    }
    capturedAudio = null;
    pauseChecks.push({ gap_ms, file, session_id: contact.session_id, turns, results, interruptions, local, playback });
    console.log(file, JSON.stringify(turns.map(turn => turn.text)));
    await page.locator('#btnHangup').click();
  }
}

async function checkScenarios() {
  const catalog = await (await page.request.get(base + '/contest/cases')).json();
  const invalid = await page.request.post(base + '/sessions', {
    data: { mode: 'voice', case_id: 'pop-scene-invalid' },
  });
  if (invalid.status() !== 422) throw new Error('Unknown case must be rejected');
  const expected = [
    ['pop-scene-034', '主体资质到期', '更新已失效的主体资质'],
    ['pop-scene-039', '授权链路不完整', '补齐从品牌方'],
    ['pop-scene-023', '图形商标', '文字商标'],
  ];
  for (const [id, reason, action] of expected) {
    const start = server.length;
    const session = await connect(id);
    await finishOpening(session, start);
    const selector = page.locator('#contestCase');
    if (!(await selector.isDisabled()) || await selector.locator('option').count() !== catalog.cases.length) {
      throw new Error('Scenario menu must show the catalog and lock while connected');
    }
    if (server.slice(start).some(row => row.type === 'agent_text' && row.text.includes(reason))) {
      throw new Error('Scenario reason disclosed before identity verification');
    }
    const results = [];
    for (const text of ['对啊', '可以说', '清楚了']) {
      const offset = server.length;
      await page.locator('#txtInput').fill(text);
      await page.locator('#btnSend').click();
      const result = await waitForServer('turn_result', offset);
      await page.waitForFunction(turn => window.contestWire.some(
        row => row.type === 'playback_ended' && row.turn_id === turn), result.turn_id);
      const answer = server.slice(offset).filter(row => row.type === 'agent_text' && row.turn_id === result.turn_id)
        .map(row => row.text).join('');
      results.push({ text, answer, result: result.result });
    }
    const [identity, explanation, confirmed] = results;
    const writeback = confirmed.result.result?.writeback;
    if (identity.result.outcome !== 'pending_need' || explanation.result.outcome !== 'pending_result' ||
        !explanation.answer.includes(reason) || !explanation.answer.includes(action) ||
        confirmed.result.outcome !== 'pop_contact_recorded' || writeback?.knowledge_id !== id) {
      throw new Error(JSON.stringify({ id, results }));
    }
    scenarioChecks.push({ id, session_id: session.session_id, menu_count: catalog.cases.length, reason, action, results });
    console.log(id, explanation.answer, writeback.request_id);
    await page.locator('#btnHangup').click();
  }
}

async function checkSceneRsi() {
  const start = server.length;
  const session = await connect('pop-scene-035');
  await finishOpening(session, start);
  const steps = [
    ['对啊', 'pending_need', '方便沟通'],
    ['可以说', 'pending_result', '主体名称'],
    ['为什么主体名称会有问题？', 'pending_result', '营业执照号码或公司名称', 'scene_explain'],
    ['继续', 'pending_result', '核对营业执照号码和公司名称', 'scene_action'],
    ['我填的公司名尾巴上多了个点，就是这个错吗？', 'pending_result', '无法确认', 'scene_detail'],
  ];
  for (const [text, outcome, anchor, route] of steps) {
    const offset = server.length;
    await page.locator('#txtInput').fill(text);
    await page.locator('#btnSend').click();
    const result = await waitForServer('turn_result', offset);
    await page.waitForFunction(turn => window.contestWire.some(
      row => row.type === 'playback_ended' && row.turn_id === turn), result.turn_id);
    const reply = server.slice(offset).filter(row => row.type === 'agent_text' && row.turn_id === result.turn_id)
      .map(row => row.text).join('');
    const followup = result.result.result?.followup;
    const row = { session_id: session.session_id, text, reply, outcome: result.result.outcome, followup };
    textChecks.push(row);
    if (row.outcome !== outcome || !reply.includes(anchor) || (route && followup?.source !== route)) {
      throw new Error('Scene RSI browser check failed: ' + JSON.stringify(row));
    }
    console.log(JSON.stringify(row));
  }
  await page.locator('#btnHangup').click();
}

let failure = null;
try {
  if (followup === 'pauses') await checkPauses();
  else if (followup === 'resilient') await checkResilient();
  else if (followup === 'scenarios') await checkScenarios();
  else if (followup === 'scene-rsi') await checkSceneRsi();
  else await checkCall();
} catch (error) {
  failure = String(error);
  await page.screenshot({ path: join(output, 'browser-failure.png') });
} finally {
  client.push(await page.evaluate(() => window.contestWire));
  uploads.push(await page.evaluate(() => window.contestUploads));
  writeFileSync(join(output, 'browser-evidence.json'), JSON.stringify({
    scope: 'headless Chromium / local ' + transport + ' / synthetic WAV and typed probes / Web Audio timing proxy',
    transport, failure, errors, openings, measurements, identity_checks: identityChecks, pause_checks: pauseChecks,
    endpoint_override_ms: endpointMs,
    text_checks: textChecks, scenario_checks: scenarioChecks,
    no_match_retry: noMatch, client, uploads, server,
  }, null, 2));
  await browser.close();
}
if (failure) throw new Error(failure);
