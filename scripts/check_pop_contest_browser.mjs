// Real browser, real audio playback scheduling, synthetic WAV input.
import { existsSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { createRequire } from 'node:module';

const [base, output, transport = 'ws_pcm', followup = 'reason'] = process.argv.slice(2);
if (!base || !output) throw new Error('Usage: node scripts/check_pop_contest_browser.mjs BASE OUTPUT');
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({
  executablePath: process.env.CONTEST_CHROME || undefined,
  headless: true,
  args: ['--autoplay-policy=no-user-gesture-required', '--disable-features=WebRtcHideLocalIpsWithMdns'],
});
const page = await browser.newPage();
const server = [];
const errors = [];
const measurements = [];
const client = [];
const uploads = [];
const openings = [];
const identityChecks = [];
let noMatch = null;
page.on('pageerror', error => errors.push(String(error)));
function recordServer(raw) {
  if (typeof raw !== 'string') return;
  const message = JSON.parse(raw);
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
      if (['call_ready', 'speech_ended', 'playback_first_frame', 'playback_ended', 'local_muted', 'speech_started'].includes(msg.type)) {
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

async function connect() {
  const start = server.length;
  await page.goto(base + '/rtc/?downlink=pcm&transport=' + transport);
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

let failure = null;
try {
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
      const expected = followup === 'guidance' ? ['打开京麦', '原申请', '上传有效材料'] : ['这次退回是因为'];
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
} catch (error) {
  failure = String(error);
  await page.screenshot({ path: join(output, 'browser-failure.png') });
} finally {
  client.push(await page.evaluate(() => window.contestWire));
  uploads.push(await page.evaluate(() => window.contestUploads));
  writeFileSync(join(output, 'browser-evidence.json'), JSON.stringify({
    scope: 'headless Chromium / local ' + transport + ' / paced synthetic WAV / Web Audio timing proxy',
    transport, failure, errors, openings, measurements, identity_checks: identityChecks,
    no_match_retry: noMatch, client, uploads, server,
  }, null, 2));
  await browser.close();
}
if (failure) throw new Error(failure);
