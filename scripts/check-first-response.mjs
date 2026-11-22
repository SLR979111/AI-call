// Feed the same saved 24 kHz mono PCM through the real browser capture path.
// Example: node scripts/check-first-response.mjs runtime/validation/user-question.pcm before
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';

const [fixture, label = 'current', mode = 'on'] = process.argv.slice(2);
const suite = (await fs.stat(fixture)).isDirectory();
async function recordings(dir) {
  return (await fs.readdir(dir)).filter(name => name.endsWith('.pcm')).sort().map(name => path.join(dir, name));
}
const calls = [];
if (suite) {
  for (const item of (await fs.readdir(fixture, {withFileTypes: true})).sort((a,b) => a.name.localeCompare(b.name))) {
    const location = path.join(fixture, item.name);
    if (item.isDirectory()) calls.push(await recordings(location));
    else if (item.name.endsWith('.pcm')) calls.push([location]);
  }
} else {
  calls.push([fixture], [fixture], [fixture]);
}
assert.ok(calls.length && calls.every(call => call.length) && ['on', 'off'].includes(mode));
const browser = await chromium.launch({channel: 'msedge', args: ['--autoplay-policy=no-user-gesture-required']});
const results = [];
try {
  for (const call of calls) {
    const page = await browser.newPage();
    await page.addInitScript(() => {
      // Observe the actual playback schedule on the same clock as capture.
      const OriginalSocket = window.WebSocket;
      window.trace = {units: new Map(), starts: new Map(), replies: [], transcripts: [], partials: []};
      // Locate voiced 5 ms windows on the two actual PCM streams.
      function voicedWindow(data, last = false) {
        const size = 120;
        for (let k = 0; k < Math.ceil(data.length / size); k++) {
          const offset = (last ? Math.ceil(data.length / size) - 1 - k : k) * size;
          const end = Math.min(offset + size, data.length);
          let energy = 0;
          for (let i = offset; i < end; i++) energy += data[i] ** 2;
          if (energy > (end - offset) * 0.003 ** 2) return last ? end : offset;
        }
        return null;
      }
      window.WebSocket = class extends OriginalSocket {
        constructor(...args) {
          super(...args);
          this.addEventListener('message', ({data}) => {
            trace.packet = null;
            if (data instanceof ArrayBuffer) {
              const v = new DataView(data);
              trace.packet = v.getUint32(0, true) + ':' + v.getUint32(4, true);
              return;
            }
            const e = JSON.parse(data);
            if (e.type === 'utterance') trace.units.set(e.epoch + ':' + e.unit, {...e, at: performance.now()});
            if (e.type === 'transcript' && e.final) trace.transcripts.push({...e, at: performance.now()});
            if (e.type === 'transcript' && !e.final) trace.partials.push({text:e.text, at:performance.now()});
            if (e.type === 'response_source') trace.source = e.source;
            if (e.type === 'ready') trace.session = e.session_id;
            if (e.type === 'reply_done') trace.replies.push(e.epoch);
            if (e.type === 'latency' && e.voice_to_playback_ms != null) trace.latency = e;
            if (e.type === 'error') trace.error = e.message;
          });
        }
      };
      const start = AudioBufferSourceNode.prototype.start;
      AudioBufferSourceNode.prototype.start = function(when, ...rest) {
        if (this.buffer?.sampleRate === 24000 && trace.packet && !trace.starts.has(trace.packet)) {
          const first = voicedWindow(this.buffer.getChannelData(0));
          if (first !== null) trace.starts.set(trace.packet, performance.now() + (when - this.context.currentTime) * 1000 + first / 24);
        }
        return start.call(this, when, ...rest);
      };
      navigator.mediaDevices.getUserMedia = async () => {
        const ctx = new AudioContext(); await ctx.resume();
        const dest = ctx.createMediaStreamDestination();
        window.speak = base64 => {
          const data = new Int16Array(Uint8Array.from(atob(base64), c => c.charCodeAt(0)).buffer);
          const b = ctx.createBuffer(1, data.length, 24000);
          b.copyToChannel(Float32Array.from(data, sample => sample / 32768), 0);
          const end = voicedWindow(b.getChannelData(0), true);
          if (end === null) throw new Error('Silent microphone fixture');
          const when = ctx.currentTime + .04;
          trace.voiceEnd = performance.now() + (when - ctx.currentTime) * 1000 + end / 24;
          const src = ctx.createBufferSource(); src.buffer = b; src.connect(dest); src.start(when);
        };
        return dest.stream;
      };
    });
    await page.goto(process.env.CALL_URL || 'http://127.0.0.1:8088');
    await page.locator('#startVoice:enabled').waitFor();
    await page.getByRole('button', {name:'选择工单', exact:true}).click();
    await page.locator('#toggleCallOptions').click();
    await page.locator('#callMode').selectOption(process.env.CALL_MODE || 'cloud');
    await page.locator('#prediction').setChecked(mode === 'on');
    await page.locator('#closeDialog').click();
    await page.locator('#startVoice').click();
    await page.waitForFunction(() => trace.replies.length || trace.error, null, {timeout:55000});
    for (const file of call) {
      const pcm = (await fs.readFile(file)).toString('base64');
      const expected = await page.evaluate(data => {
        trace.source = trace.latency = undefined;
        trace.partials = [];
        speak(data);
        return trace.replies.length + 1;
      }, pcm);
      await page.waitForFunction(expected => trace.error || (trace.replies.length >= expected && trace.latency), expected, {timeout:55000});
      const row = await page.evaluate(() => {
        if (trace.error) throw new Error(trace.error);
        const epoch = trace.replies.at(-1);
        const units = [...trace.units].filter(([, u]) => u.epoch === epoch);
        const [id, body] = units.find(([, u]) => !u.filler);
        const voiceEnd = trace.voiceEnd;
        return {session: trace.session, source: trace.source, transcript: trace.transcripts.at(-1).text, firstText: body.text,
          partials: trace.partials.map(p => ({text:p.text, ms:Math.round(p.at - voiceEnd)})),
          endToTranscriptMs: Math.round(trace.transcripts.at(-1).at - voiceEnd),
          endToBodyTextMs: Math.round(body.at - voiceEnd),
          endToBodyAudioMs: Math.round(trace.starts.get(id) - voiceEnd)};
      });
      assert.ok(row.endToBodyAudioMs > 0 && row.transcript);
      results.push({fixture: suite ? path.relative(fixture, file) : path.basename(file), ...row}); console.log(JSON.stringify(results.at(-1)));
    }
    await page.close();
  }
  await fs.mkdir('runtime/validation', {recursive:true});
  await fs.writeFile('runtime/validation/first-response-' + label + '.json', JSON.stringify(results, null, 2));
  const times = results.map(r => r.endToBodyAudioMs).sort((a, b) => a - b);
  const median = (times[Math.floor((times.length - 1) / 2)] + times[Math.floor(times.length / 2)]) / 2;
  console.log(JSON.stringify({samples:times.length, p50Ms:median, maxMs:times.at(-1)}));
  assert.ok(median < 2000 && times.at(-1) <= 3000, 'Voice latency budget exceeded');
} finally { await browser.close(); }
